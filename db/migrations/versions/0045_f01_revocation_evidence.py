"""Prove committed staff revocation across subtransactions using private activation evidence."""

from alembic import op

revision = "0045_f01_revocation_evidence"
down_revision = "0044_f01_delivery_acl"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        "GRANT TRIGGER ON public.staff_accounts, public.sensitive_audit TO carepulse_identity_owner"
    )
    op.execute("SET LOCAL ROLE carepulse_identity_owner")
    op.execute(r"""
        ALTER TABLE f01_private.activations
            ADD COLUMN revoked_staff_id uuid,
            ADD COLUMN revocation_audit_id uuid,
            ADD CHECK (revocation_audit_id IS NULL OR revoked_staff_id IS NOT NULL);

        CREATE FUNCTION f01_private.capture_revocation() RETURNS trigger
        LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, pg_temp AS $$
        DECLARE ctx record;
        BEGIN
            IF session_user <> 'carepulse_api' THEN RETURN NEW; END IF;
            SELECT * INTO ctx FROM public.f01_context();
            IF NOT FOUND THEN RETURN NEW; END IF;
            IF ctx.stage = 'PRIMARY' AND ctx.purpose = 'STAFF_MANAGEMENT'
               AND ctx.role = 'DOCTOR_OWNER' AND OLD.role = 'RECEPTIONIST'
               AND OLD.organization_id::text = ctx.organization_id AND OLD.id = ctx.target_id
               AND OLD.revision = ctx.expected_staff_revision
               AND NEW.revision::numeric = OLD.revision::numeric + 1
               AND OLD.security_revision = ctx.expected_security_revision
               AND NEW.security_revision::numeric = OLD.security_revision::numeric + 1
               AND ((ctx.method = 'POST' AND ctx.path LIKE '%/disable'
                     AND OLD.enabled AND NOT NEW.enabled)
                    OR (ctx.method = 'PATCH' AND ctx.path LIKE '%/permissions'
                        AND ctx.manager_enabled IS FALSE
                        AND OLD.operational_manager AND NOT NEW.operational_manager)) THEN
                UPDATE f01_private.activations
                    SET revoked_staff_id = NEW.id
                    WHERE request_id = ctx.request_id AND stage = 'PRIMARY'
                        AND revoked_staff_id IS NULL;
            END IF;
            RETURN NEW;
        END $$;

        CREATE FUNCTION f01_private.capture_revocation_audit() RETURNS trigger
        LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, pg_temp AS $$
        DECLARE ctx record;
        BEGIN
            IF session_user <> 'carepulse_api' OR NEW.action NOT IN
                ('RECEPTIONIST_DISABLE', 'RECEPTIONIST_PERMISSIONS') THEN RETURN NEW; END IF;
            SELECT * INTO ctx FROM public.f01_context();
            IF NOT FOUND THEN RETURN NEW; END IF;
            IF ctx.stage = 'PRIMARY' AND ctx.purpose = 'STAFF_MANAGEMENT'
                AND ctx.role = 'DOCTOR_OWNER' AND NEW.actor_role = ctx.role
                AND NEW.actor_staff_id::text = ctx.staff_id
                AND NEW.organization_id::text = ctx.organization_id
                AND NEW.before_state->>'id' = ctx.target_id::text
                AND NEW.after_state->>'id' = ctx.target_id::text
                AND ((ctx.method = 'POST' AND ctx.path LIKE '%/disable'
                      AND NEW.action = 'RECEPTIONIST_DISABLE'
                      AND NEW.before_state->'enabled' = 'true'::jsonb
                      AND NEW.after_state->'enabled' = 'false'::jsonb)
                     OR (ctx.method = 'PATCH' AND ctx.path LIKE '%/permissions'
                         AND ctx.manager_enabled IS FALSE AND NEW.action = 'RECEPTIONIST_PERMISSIONS'
                         AND NEW.before_state->'operational_manager' = 'true'::jsonb
                         AND NEW.after_state->'operational_manager' = 'false'::jsonb
                         AND NEW.before_state->>'security_revision' = ctx.expected_security_revision::text
                         AND NEW.after_state->>'security_revision' = (ctx.expected_security_revision::numeric + 1)::text)) THEN
                UPDATE f01_private.activations
                    SET revocation_audit_id = NEW.id
                    WHERE request_id = ctx.request_id AND stage = 'PRIMARY'
                        AND revoked_staff_id = ctx.target_id AND revocation_audit_id IS NULL;
            END IF;
            RETURN NEW;
        END $$;

        REVOKE ALL ON FUNCTION f01_private.capture_revocation(),
            f01_private.capture_revocation_audit() FROM PUBLIC;
        CREATE TRIGGER f01_capture_revocation
            AFTER UPDATE OF enabled, operational_manager, security_revision ON public.staff_accounts
            FOR EACH ROW EXECUTE FUNCTION f01_private.capture_revocation();
        CREATE TRIGGER f01_capture_revocation_audit
            AFTER INSERT ON public.sensitive_audit
            FOR EACH ROW EXECUTE FUNCTION f01_private.capture_revocation_audit();

        CREATE OR REPLACE FUNCTION f01_private.cleanup_proven(req f01_private.requests, primary_xid xid8)
        RETURNS boolean LANGUAGE sql SECURITY DEFINER SET search_path = pg_catalog, pg_temp AS $$
            SELECT req.purpose = 'STAFF_MANAGEMENT' AND req.role = 'DOCTOR_OWNER'
                AND req.expected_security_revision IS NOT NULL AND EXISTS (
                    SELECT 1 FROM public.staff_accounts s
                    JOIN f01_private.bindings b ON b.request_id = req.id
                        AND b.stage = 'PRIMARY' AND b.xid = primary_xid
                    JOIN f01_private.activations p ON p.request_id = b.request_id
                        AND p.stage = b.stage AND p.revoked_staff_id = req.target_id
                    JOIN public.sensitive_audit a ON a.id = p.revocation_audit_id
                        AND a.organization_id = s.organization_id
                    WHERE s.organization_id = req.organization_id AND s.id = req.target_id
                      AND s.role = 'RECEPTIONIST'
                      AND s.security_revision::numeric = req.expected_security_revision::numeric + 1
                      AND a.actor_staff_id = req.staff_id AND a.actor_role = req.role
                      AND a.before_state->>'id' = req.target_id::text
                      AND a.after_state->>'id' = req.target_id::text
                      AND ((req.path LIKE '%/disable' AND NOT s.enabled
                            AND a.action = 'RECEPTIONIST_DISABLE'
                            AND a.before_state->'enabled' = 'true'::jsonb
                            AND a.after_state->'enabled' = 'false'::jsonb)
                           OR (req.path LIKE '%/permissions' AND req.manager_enabled IS FALSE AND NOT s.operational_manager
                            AND a.action = 'RECEPTIONIST_PERMISSIONS'
                            AND a.before_state->'operational_manager' = 'true'::jsonb
                            AND a.after_state->'operational_manager' = 'false'::jsonb
                            AND a.before_state->>'security_revision' = req.expected_security_revision::text
                            AND a.after_state->>'security_revision' = s.security_revision::text))
                );
        $$;
    """)
    op.execute("RESET ROLE")
    op.execute(
        "REVOKE TRIGGER ON public.staff_accounts, public.sensitive_audit FROM carepulse_identity_owner"
    )


def downgrade() -> None:
    raise RuntimeError("F-01 revocation evidence migration is forward-only")

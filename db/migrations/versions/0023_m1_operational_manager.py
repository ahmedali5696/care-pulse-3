"""M1: revocable Receptionist operational authority and bound policy previews."""

from alembic import op

revision = "0023_m1_operational_manager"
down_revision = "0022_d08_followup_claims"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
        ALTER TABLE public.staff_accounts
        ADD COLUMN operational_manager boolean NOT NULL DEFAULT false,
        ADD CONSTRAINT staff_operational_manager_receptionist
            CHECK (NOT operational_manager OR role = 'RECEPTIONIST')
    """)
    op.execute("""
        ALTER TABLE public.clinic_policy_previews
        ADD COLUMN actor_security_revision bigint
            CHECK (actor_security_revision >= 0)
    """)

    # Preserve the existing guards for invitations and recovery. The staff
    # branch additionally fences the new authority on INSERT and UPDATE.
    op.execute("""
        CREATE OR REPLACE FUNCTION public.guard_identity_row() RETURNS trigger
        LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog AS $$
        BEGIN
            IF TG_TABLE_NAME = 'staff_accounts' THEN
                IF TG_OP = 'DELETE' THEN
                    IF OLD.role = 'DOCTOR_OWNER' THEN
                        RAISE EXCEPTION 'Owner replacement is forbidden'
                            USING ERRCODE = '23514';
                    END IF;
                    PERFORM 1 FROM public.organizations
                    WHERE id = OLD.organization_id FOR UPDATE;
                    RETURN OLD;
                END IF;
                PERFORM 1 FROM public.organizations
                WHERE id = NEW.organization_id FOR UPDATE;
                IF TG_OP = 'INSERT' THEN
                    IF NEW.revision <> 0 OR NEW.security_revision <> 0 OR
                       NEW.reset_block_operation_id IS NOT NULL OR
                       NEW.operational_manager THEN
                        RAISE EXCEPTION 'Invalid initial staff authority'
                            USING ERRCODE = '23514';
                    END IF;
                ELSE
                    IF (NEW.id, NEW.organization_id, NEW.auth_user_id,
                        NEW.verified_email, NEW.role, NEW.created_at)
                       IS DISTINCT FROM
                       (OLD.id, OLD.organization_id, OLD.auth_user_id,
                        OLD.verified_email, OLD.role, OLD.created_at) THEN
                        RAISE EXCEPTION 'Immutable staff association'
                            USING ERRCODE = '23514';
                    END IF;
                    IF NEW.security_revision < OLD.security_revision THEN
                        RAISE EXCEPTION 'Security revision cannot decrease'
                            USING ERRCODE = '23514';
                    END IF;
                    IF ((NEW.enabled, NEW.verified, NEW.bulk_reschedule,
                         NEW.operational_manager) IS DISTINCT FROM
                        (OLD.enabled, OLD.verified, OLD.bulk_reschedule,
                         OLD.operational_manager) OR
                        (NEW.reset_block_operation_id IS NOT NULL AND
                         NEW.reset_block_operation_id IS DISTINCT FROM
                         OLD.reset_block_operation_id)) AND
                       NEW.security_revision <= OLD.security_revision THEN
                        RAISE EXCEPTION 'Security change requires a fresh revision'
                            USING ERRCODE = '23514';
                    END IF;
                    NEW.revision := OLD.revision + 1;
                    NEW.updated_at := clock_timestamp();
                END IF;
                RETURN NEW;
            END IF;
            IF TG_TABLE_NAME = 'invitations' THEN
                IF TG_OP = 'DELETE' THEN
                    RAISE EXCEPTION 'Invitation deletion is forbidden'
                        USING ERRCODE = '23514';
                END IF;
                PERFORM 1 FROM public.organizations
                WHERE id = NEW.organization_id FOR UPDATE;
                IF TG_OP = 'INSERT' THEN
                    IF NEW.state <> 'ISSUED' OR NEW.revision <> 0 THEN
                        RAISE EXCEPTION 'Invitation must start issued'
                            USING ERRCODE = '23514';
                    END IF;
                ELSE
                    IF (NEW.id, NEW.organization_id, NEW.staff_id, NEW.purpose,
                        NEW.target_email, NEW.proof_digest, NEW.issued_at,
                        NEW.expires_at, NEW.issued_by_staff_id, NEW.issued_by_operator_id)
                       IS DISTINCT FROM
                       (OLD.id, OLD.organization_id, OLD.staff_id, OLD.purpose,
                        OLD.target_email, OLD.proof_digest, OLD.issued_at,
                        OLD.expires_at, OLD.issued_by_staff_id, OLD.issued_by_operator_id)
                       OR OLD.state <> 'ISSUED'
                       OR NEW.state NOT IN ('ACCEPTED', 'REVOKED') THEN
                        RAISE EXCEPTION 'Invalid invitation transition'
                            USING ERRCODE = '23514';
                    END IF;
                    IF NEW.state = 'ACCEPTED' THEN
                        IF clock_timestamp() >= OLD.expires_at THEN
                            RAISE EXCEPTION 'Invitation expired'
                                USING ERRCODE = '23514';
                        END IF;
                        NEW.accepted_at := clock_timestamp();
                    ELSE
                        NEW.revoked_at := clock_timestamp();
                    END IF;
                    NEW.revision := OLD.revision + 1;
                END IF;
                RETURN NEW;
            END IF;
            IF TG_TABLE_NAME = 'recovery_operations' THEN
                IF TG_OP = 'DELETE' THEN
                    RAISE EXCEPTION 'Recovery operation deletion is forbidden'
                        USING ERRCODE = '23514';
                END IF;
                IF TG_OP = 'UPDATE' THEN
                    IF (NEW.id, NEW.organization_id, NEW.staff_id, NEW.auth_user_id,
                        NEW.purpose, NEW.proof_digest, NEW.proof_reference,
                        NEW.proof_expires_at, NEW.request_fingerprint,
                        NEW.fingerprint_key_version, NEW.captured_staff_revision,
                        NEW.captured_lifecycle_revision, NEW.reserved_security_revision,
                        NEW.created_at) IS DISTINCT FROM
                       (OLD.id, OLD.organization_id, OLD.staff_id, OLD.auth_user_id,
                        OLD.purpose, OLD.proof_digest, OLD.proof_reference,
                        OLD.proof_expires_at, OLD.request_fingerprint,
                        OLD.fingerprint_key_version, OLD.captured_staff_revision,
                        OLD.captured_lifecycle_revision, OLD.reserved_security_revision,
                        OLD.created_at) OR NEW.auth_fence < OLD.auth_fence OR
                        (OLD.auth_done_at IS NOT NULL AND
                         NEW.auth_done_at IS DISTINCT FROM OLD.auth_done_at) OR
                        (OLD.invalidated AND NOT NEW.invalidated) OR
                        (OLD.invalidation_reference IS NOT NULL AND
                         NEW.invalidation_reference IS DISTINCT FROM
                         OLD.invalidation_reference) OR
                        NEW.state NOT IN ('RESERVED', 'AUTH_DONE', 'FINALIZED', 'ABORTED') OR
                        (OLD.state = 'RESERVED' AND NEW.state NOT IN
                         ('RESERVED', 'AUTH_DONE', 'ABORTED')) OR
                        (OLD.state = 'AUTH_DONE' AND NEW.state NOT IN
                         ('AUTH_DONE', 'FINALIZED', 'ABORTED')) OR
                        OLD.state IN ('FINALIZED', 'ABORTED') THEN
                        RAISE EXCEPTION 'Invalid recovery operation mutation'
                            USING ERRCODE = '23514';
                    END IF;
                    NEW.updated_at := clock_timestamp();
                ELSIF NEW.state <> 'RESERVED' OR NEW.auth_fence <> 0 OR
                      NEW.invalidated OR NEW.closed_at IS NOT NULL THEN
                    RAISE EXCEPTION 'Invalid initial recovery reservation'
                        USING ERRCODE = '23514';
                END IF;
                RETURN NEW;
            END IF;
            IF TG_OP <> 'INSERT' THEN
                RAISE EXCEPTION 'Completion evidence is immutable'
                    USING ERRCODE = '23514';
            END IF;
            RETURN NEW;
        END
        $$
    """)

    # Existing table-wide UPDATE also grants writes to new columns. Remove it
    # before granting only columns used by staff and recovery commands.
    op.execute("REVOKE UPDATE ON public.staff_accounts FROM carepulse_runtime")
    op.execute("""
        GRANT UPDATE (
            enabled, verified, security_revision, reset_block_operation_id,
            updated_at
        ) ON public.staff_accounts TO carepulse_runtime
    """)

    # Owner authorization comes from signed protected_transaction. GUC identity
    # checks here are defense in depth: the shared SQL runtime can set both GUCs.
    op.execute("""
        CREATE FUNCTION public.set_receptionist_operational_manager(
            p_tenant uuid, p_actor uuid, p_staff uuid, p_enabled boolean,
            p_expected_revision bigint, p_expected_security_revision bigint,
            p_reason text, p_correlation uuid
        ) RETURNS void
        LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog AS $$
        DECLARE
            actor_row public.staff_accounts%ROWTYPE;
            target_row public.staff_accounts%ROWTYPE;
            changed_row public.staff_accounts%ROWTYPE;
        BEGIN
            IF session_user <> 'carepulse_runtime' OR p_tenant IS NULL OR
               p_actor IS NULL OR p_staff IS NULL OR p_actor = p_staff OR
               p_enabled IS NULL OR p_expected_revision IS NULL OR
               p_expected_security_revision IS NULL OR
               p_expected_revision < 0 OR p_expected_security_revision < 0 OR
               p_reason IS NULL OR btrim(p_reason) = '' OR p_correlation IS NULL OR
               p_tenant IS DISTINCT FROM
                   NULLIF(current_setting('app.organization_id', true), '')::uuid OR
               p_actor IS DISTINCT FROM
                   NULLIF(current_setting('app.staff_id', true), '')::uuid THEN
                RAISE EXCEPTION 'Operational authority denied' USING ERRCODE = '42501';
            END IF;
            PERFORM 1 FROM public.organizations
            WHERE id = p_tenant AND state = 'ACTIVE' FOR UPDATE;
            IF NOT FOUND THEN
                RAISE EXCEPTION 'Inactive clinic' USING ERRCODE = '42501';
            END IF;
            SELECT s.* INTO actor_row FROM public.staff_accounts s
            JOIN public."user" u ON u.id = s.auth_user_id
            WHERE s.organization_id = p_tenant AND s.id = p_actor
              AND s.role = 'DOCTOR_OWNER' AND s.enabled AND s.verified
              AND s.reset_block_operation_id IS NULL
              AND u."emailVerified" AND u.email = s.verified_email
            FOR UPDATE OF s;
            IF NOT FOUND THEN
                RAISE EXCEPTION 'Owner authorization denied' USING ERRCODE = '42501';
            END IF;
            SELECT s.* INTO target_row FROM public.staff_accounts s
            JOIN public."user" u ON u.id = s.auth_user_id
            WHERE s.organization_id = p_tenant AND s.id = p_staff
              AND u."emailVerified" AND u.email = s.verified_email
            FOR UPDATE OF s;
            IF NOT FOUND OR target_row.role <> 'RECEPTIONIST' OR
               NOT target_row.enabled OR NOT target_row.verified OR
               target_row.reset_block_operation_id IS NOT NULL OR
               EXISTS (
                   SELECT 1 FROM public.invitations i
                   WHERE i.organization_id = p_tenant AND i.staff_id = p_staff
                     AND i.purpose = 'RECEPTIONIST' AND i.state = 'ISSUED'
               ) THEN
                RAISE EXCEPTION 'Receptionist authority denied' USING ERRCODE = '42501';
            END IF;
            IF target_row.revision <> p_expected_revision OR
               target_row.security_revision <> p_expected_security_revision OR
               target_row.operational_manager = p_enabled THEN
                RAISE EXCEPTION 'Staff revision conflict' USING ERRCODE = '40001';
            END IF;
            UPDATE public.staff_accounts s
            SET operational_manager = p_enabled,
                security_revision = s.security_revision + 1
            WHERE s.organization_id = p_tenant AND s.id = p_staff
              AND s.revision = p_expected_revision
              AND s.security_revision = p_expected_security_revision
            RETURNING s.* INTO changed_row;
            IF NOT FOUND THEN
                RAISE EXCEPTION 'Staff revision conflict' USING ERRCODE = '40001';
            END IF;
            INSERT INTO public.sensitive_audit (
                id, organization_id, actor_staff_id, actor_role, action, reason,
                correlation_id, before_state, after_state
            ) VALUES (
                gen_random_uuid(), p_tenant, p_actor, 'DOCTOR_OWNER',
                'RECEPTIONIST_PERMISSIONS', p_reason, p_correlation::text,
                jsonb_build_object(
                    'id', p_staff, 'operational_manager', target_row.operational_manager,
                    'revision', target_row.revision,
                    'security_revision', target_row.security_revision),
                jsonb_build_object(
                    'id', p_staff, 'operational_manager', changed_row.operational_manager,
                    'revision', changed_row.revision,
                    'security_revision', changed_row.security_revision)
            );
        END
        $$
    """)
    op.execute("""
        REVOKE ALL ON FUNCTION public.set_receptionist_operational_manager(
            uuid,uuid,uuid,boolean,bigint,bigint,text,uuid
        ) FROM PUBLIC
    """)
    op.execute("""
        GRANT EXECUTE ON FUNCTION public.set_receptionist_operational_manager(
            uuid,uuid,uuid,boolean,bigint,bigint,text,uuid
        ) TO carepulse_runtime
    """)

    op.execute("""
        CREATE FUNCTION public.require_preview_actor_revision() RETURNS trigger
        LANGUAGE plpgsql SET search_path = pg_catalog AS $$
        BEGIN
            IF NEW.actor_security_revision IS NULL THEN
                RAISE EXCEPTION 'New preview requires captured security revision'
                    USING ERRCODE = '23514';
            END IF;
            RETURN NEW;
        END
        $$
    """)
    op.execute(
        "REVOKE ALL ON FUNCTION public.require_preview_actor_revision() FROM PUBLIC"
    )
    op.execute("""
        CREATE TRIGGER clinic_policy_previews_actor_revision
        BEFORE INSERT ON public.clinic_policy_previews
        FOR EACH ROW EXECUTE FUNCTION public.require_preview_actor_revision()
    """)
    op.execute("""
        GRANT INSERT (actor_security_revision)
        ON public.clinic_policy_previews TO carepulse_runtime
    """)


def downgrade() -> None:
    raise RuntimeError("M1 operational authority migration is forward-only")

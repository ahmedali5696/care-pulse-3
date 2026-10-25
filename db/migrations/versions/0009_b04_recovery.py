"""B-04: strictly purpose-limited Owner recovery and suspended reset reconciliation."""

from alembic import op

revision = "0009_b04_recovery"
down_revision = "0008_provisioning_admin"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Only identifies an existing eligible Owner; never authorizes a clinic session.
    # A reset-owned block deliberately survives issuance of a replacement proof.
    op.execute("""
        CREATE FUNCTION public.resolve_recovery_tenant(native_user_id text)
        RETURNS uuid LANGUAGE sql STABLE SECURITY DEFINER
        SET search_path = pg_catalog AS $$
            SELECT s.organization_id FROM public.staff_accounts s
            JOIN public.organizations o ON o.id = s.organization_id
            JOIN public."user" u ON u.id = s.auth_user_id
            WHERE s.auth_user_id = native_user_id AND o.state = 'ACTIVE'
              AND s.role = 'DOCTOR_OWNER' AND s.enabled AND s.verified
              AND u."emailVerified" AND u.email = s.verified_email
              AND EXISTS (SELECT 1 FROM public."account" a
                          WHERE a."userId" = u.id AND a."providerId" = 'credential'
                            AND a.password IS NOT NULL)
        $$
    """)
    op.execute(
        "REVOKE ALL ON FUNCTION public.resolve_recovery_tenant(text) FROM PUBLIC"
    )
    op.execute(
        "GRANT EXECUTE ON FUNCTION public.resolve_recovery_tenant(text) TO carepulse_runtime"
    )

    # Native verification is deleted on successful reset. This digest-only
    # locator enables the same proof's safe completion retry, after which the
    # signed Python coordinator still validates the full immutable binding.
    op.execute("""
        CREATE FUNCTION public.resolve_recovery_operation(target_digest text)
        RETURNS TABLE(organization_id uuid, staff_id uuid, native_user_id text,
                      proof_reference text, fingerprint_key_version text)
        LANGUAGE sql STABLE SECURITY DEFINER SET search_path = pg_catalog AS $$
            SELECT r.organization_id, r.staff_id, r.auth_user_id,
                   r.proof_reference, r.fingerprint_key_version
              FROM public.recovery_operations r
              JOIN public.staff_accounts s ON s.organization_id = r.organization_id
                  AND s.id = r.staff_id AND s.auth_user_id = r.auth_user_id
              JOIN public.organizations o ON o.id = r.organization_id
              JOIN public."user" u ON u.id = s.auth_user_id
             WHERE target_digest ~ '^[0-9a-f]{64}$'
               AND r.purpose = 'PASSWORD_RESET' AND r.proof_digest = target_digest
               AND s.role = 'DOCTOR_OWNER' AND s.enabled AND s.verified
               AND o.state = 'ACTIVE' AND u."emailVerified"
               AND s.verified_email = u.email
        $$
    """)
    op.execute(
        "REVOKE ALL ON FUNCTION public.resolve_recovery_operation(text) FROM PUBLIC"
    )
    op.execute(
        "GRANT EXECUTE ON FUNCTION public.resolve_recovery_operation(text) TO carepulse_runtime"
    )

    # Operator revocation evidence is written only by the authenticated
    # operator SECURITY DEFINER transition. Runtime recovery may read its
    # tenant's evidence, but cannot impersonate an OPERATOR audit event.
    tenant = "NULLIF(current_setting('app.organization_id', true), '')::uuid"
    op.execute(f"""
        ALTER POLICY sensitive_audit_insert ON public.sensitive_audit
        WITH CHECK (organization_id = {tenant} AND actor_role <> 'OPERATOR')
    """)

    # Never allow a revoked operation to become FINALIZED, irrespective of
    # which application path attempts the write.
    op.execute("""
        ALTER TABLE public.recovery_operations
        ADD CONSTRAINT recovery_invalidated_never_finalized
        CHECK (state <> 'FINALIZED' OR NOT invalidated)
    """)
    # The operator transaction is the sole author of OPERATOR_SUSPEND evidence.
    # Runtime may abandon only an expired RESERVED proof with no auth effect.
    op.execute("""
        CREATE FUNCTION public.guard_recovery_operator_provenance()
        RETURNS trigger LANGUAGE plpgsql SECURITY DEFINER
        SET search_path = pg_catalog AS $$
        BEGIN
            IF session_user = 'carepulse_runtime' AND
               (NEW.invalidation_reference IS DISTINCT FROM OLD.invalidation_reference OR
                (NEW.invalidated AND NOT OLD.invalidated)) AND NOT (
                    OLD.state = 'RESERVED' AND NEW.state = 'ABORTED'
                    AND OLD.auth_done_at IS NULL AND OLD.auth_fence = 0
                    AND clock_timestamp() >= OLD.proof_expires_at
                    AND NEW.invalidated AND NEW.invalidation_reference = 'EXPIRED_INCOMPLETE'
               ) THEN
                RAISE EXCEPTION 'Invalid recovery provenance' USING ERRCODE = '23514';
            END IF;
            RETURN NEW;
        END $$
    """)
    op.execute("""
        CREATE TRIGGER recovery_operator_provenance_guard
        BEFORE UPDATE ON public.recovery_operations
        FOR EACH ROW EXECUTE FUNCTION public.guard_recovery_operator_provenance()
    """)

    # Finalization must carry its native completion and exactly-once audit at
    # commit, after both updates and the audit insert have been applied.
    op.execute("""
        CREATE FUNCTION public.check_recovery_final_audit()
        RETURNS trigger LANGUAGE plpgsql SECURITY DEFINER
        SET search_path = pg_catalog AS $$
        DECLARE finalized public.recovery_operations%ROWTYPE;
        BEGIN
            SELECT * INTO finalized FROM public.recovery_operations WHERE id = NEW.id;
            IF finalized.state = 'FINALIZED' THEN
                IF finalized.invalidated OR finalized.auth_done_at IS NULL OR
                   finalized.auth_fence < 1 OR NOT EXISTS (
                       SELECT 1 FROM public.auth_recovery_completions c
                        WHERE c.id = finalized.id AND c.auth_user_id = finalized.auth_user_id
                          AND c.proof_digest = finalized.proof_digest
                          AND c.request_fingerprint = finalized.request_fingerprint
                          AND c.reserved_security_revision = finalized.reserved_security_revision
                          AND c.auth_fence = finalized.auth_fence
                          AND c.completed_at = finalized.auth_done_at
                   ) OR (SELECT count(*) FROM public.sensitive_audit a
                        WHERE a.recovery_operation_id = finalized.id
                          AND a.organization_id = finalized.organization_id
                          AND a.actor_staff_id = finalized.staff_id
                          AND a.action = 'PASSWORD_RESET_SUCCEEDED') <> 1 OR
                   NOT EXISTS (
                       SELECT 1 FROM public.staff_accounts s
                       JOIN public.organizations o ON o.id = s.organization_id
                        WHERE s.organization_id = finalized.organization_id
                          AND s.id = finalized.staff_id AND s.auth_user_id = finalized.auth_user_id
                          AND s.role = 'DOCTOR_OWNER' AND s.enabled AND s.verified
                          AND s.security_revision = finalized.reserved_security_revision
                          AND s.reset_block_operation_id IS NULL
                          AND o.state = 'ACTIVE'
                          AND o.lifecycle_revision = finalized.captured_lifecycle_revision
                   ) THEN
                    RAISE EXCEPTION 'Unverified recovery finalization' USING ERRCODE = '23514';
                END IF;
            ELSIF finalized.state = 'ABORTED' THEN
                -- The old operation may close only together with an active,
                -- differently bound successor that owns the same Owner block.
                IF NOT finalized.invalidated OR NOT EXISTS (
                    SELECT 1 FROM public.recovery_operations successor
                    JOIN public.staff_accounts s ON s.organization_id = successor.organization_id
                        AND s.id = successor.staff_id
                    WHERE successor.auth_user_id = finalized.auth_user_id
                      AND successor.organization_id = finalized.organization_id
                      AND successor.staff_id = finalized.staff_id
                      AND successor.id <> finalized.id
                      AND successor.proof_digest <> finalized.proof_digest
                      AND successor.reserved_security_revision = finalized.reserved_security_revision + 1
                      AND successor.state = 'RESERVED' AND NOT successor.invalidated
                      AND successor.closed_at IS NULL
                      AND s.reset_block_operation_id = successor.id
                      AND s.security_revision = successor.reserved_security_revision
                ) THEN
                    RAISE EXCEPTION 'Abandoned recovery has no owned successor' USING ERRCODE = '23514';
                END IF;
            END IF;
            RETURN NULL;
        END $$
    """)
    op.execute("""
        CREATE CONSTRAINT TRIGGER recovery_final_audit_constraint
        AFTER UPDATE ON public.recovery_operations
        DEFERRABLE INITIALLY DEFERRED
        FOR EACH ROW EXECUTE FUNCTION public.check_recovery_final_audit()
    """)

    # An Owner reset block protects app access independently from being an
    # enabled/verified Owner. Reactivation may not silently clear the block.
    # Suspension evidence is linked to the exact committed lifecycle revision.
    op.execute("""
        CREATE OR REPLACE FUNCTION public.operator_change_clinic_state(
            actor uuid, supplied text, clinic uuid, expected_revision bigint,
            next_state text)
        RETURNS bigint LANGUAGE plpgsql SECURITY DEFINER
        SET search_path = pg_catalog AS $$
        DECLARE org public.organizations%ROWTYPE; staff_id uuid; operation_id uuid;
        BEGIN
            IF NOT public.operator_secret_matches(actor, supplied) OR
               next_state NOT IN ('SUSPENDED', 'ACTIVE') THEN
                RAISE EXCEPTION 'Operator denied' USING ERRCODE = '42501';
            END IF;
            SELECT * INTO org FROM public.organizations WHERE id = clinic FOR UPDATE;
            IF NOT FOUND OR org.lifecycle_revision <> expected_revision OR
               (next_state = 'ACTIVE' AND
                (org.state <> 'SUSPENDED' OR org.first_activated_at IS NULL)) OR
               (next_state = 'SUSPENDED' AND
                org.state NOT IN ('PROVISIONING', 'ACTIVE')) THEN
                RAISE EXCEPTION 'Clinic lifecycle conflict' USING ERRCODE = '40001';
            END IF;
            FOR staff_id IN SELECT id FROM public.staff_accounts
                WHERE organization_id = clinic ORDER BY id FOR UPDATE LOOP
                NULL;
            END LOOP;
            IF next_state = 'ACTIVE' AND NOT EXISTS (
                SELECT 1 FROM public.staff_accounts s
                JOIN public."user" u ON u.id = s.auth_user_id
                WHERE s.organization_id = clinic AND s.role = 'DOCTOR_OWNER'
                  AND s.enabled AND s.verified
                  AND u."emailVerified" AND u.email = s.verified_email
            ) THEN
                RAISE EXCEPTION 'Verified Owner unavailable' USING ERRCODE = '40001';
            END IF;
            IF next_state = 'SUSPENDED' THEN
                FOR operation_id IN SELECT id FROM public.recovery_operations
                    WHERE organization_id = clinic AND closed_at IS NULL
                    ORDER BY id FOR UPDATE LOOP
                    UPDATE public.recovery_operations SET invalidated = true,
                        invalidation_reference = 'OPERATOR_SUSPEND:' ||
                            (expected_revision + 1)::text
                    WHERE id = operation_id AND NOT invalidated;
                    IF FOUND THEN
                        INSERT INTO public.sensitive_audit
                            (id, organization_id, operator_id, actor_role,
                             action, reason, recovery_operation_id, correlation_id)
                        VALUES (gen_random_uuid(), clinic, actor, 'OPERATOR',
                            'RECOVERY_AUTH_INVALIDATED', 'Operator suspension',
                            operation_id,
                            'OPERATOR_SUSPEND:' || (expected_revision + 1)::text);
                    END IF;
                END LOOP;
            END IF;
            UPDATE public.organizations SET state = next_state WHERE id = clinic;
            INSERT INTO public.operator_action_pending
                (id, organization_id, operator_id, action, correlation_id)
            VALUES (gen_random_uuid(), clinic, actor,
                CASE WHEN next_state = 'SUSPENDED' THEN 'CLINIC_SUSPENDED'
                     ELSE 'CLINIC_REACTIVATED' END,
                clinic::text || ':' || (expected_revision + 1)::text || ':' || next_state);
            RETURN expected_revision + 1;
        END
        $$
    """)


def downgrade() -> None:
    op.execute("""
        DO $$ BEGIN
            IF EXISTS (SELECT 1 FROM public.recovery_operations)
               OR EXISTS (SELECT 1 FROM public.organizations) THEN
                RAISE EXCEPTION 'Cannot downgrade populated recovery state'
                    USING ERRCODE = '23514';
            END IF;
        END $$
    """)
    op.execute(
        "DROP TRIGGER recovery_final_audit_constraint ON public.recovery_operations"
    )
    op.execute("DROP FUNCTION public.check_recovery_final_audit()")
    op.execute(
        "DROP TRIGGER recovery_operator_provenance_guard ON public.recovery_operations"
    )
    op.execute("DROP FUNCTION public.guard_recovery_operator_provenance()")
    op.execute(
        "ALTER TABLE public.recovery_operations DROP CONSTRAINT recovery_invalidated_never_finalized"
    )
    tenant = "NULLIF(current_setting('app.organization_id', true), '')::uuid"
    op.execute(f"""
        ALTER POLICY sensitive_audit_insert ON public.sensitive_audit
        WITH CHECK (organization_id = {tenant})
    """)
    # Restore 0008's exact active-Owner guard and invalidation behavior before
    # removing the B-04-only locators. Data-bearing downgrade is prohibited.
    op.execute("""
        CREATE OR REPLACE FUNCTION public.operator_change_clinic_state(
            actor uuid, supplied text, clinic uuid, expected_revision bigint,
            next_state text)
        RETURNS bigint LANGUAGE plpgsql SECURITY DEFINER
        SET search_path = pg_catalog AS $$
        DECLARE org public.organizations%ROWTYPE; staff_id uuid; operation_id uuid;
        BEGIN
            IF NOT public.operator_secret_matches(actor, supplied) OR
               next_state NOT IN ('SUSPENDED', 'ACTIVE') THEN
                RAISE EXCEPTION 'Operator denied' USING ERRCODE = '42501';
            END IF;
            SELECT * INTO org FROM public.organizations WHERE id = clinic FOR UPDATE;
            IF NOT FOUND OR org.lifecycle_revision <> expected_revision OR
               (next_state = 'ACTIVE' AND
                (org.state <> 'SUSPENDED' OR org.first_activated_at IS NULL)) OR
               (next_state = 'SUSPENDED' AND
                org.state NOT IN ('PROVISIONING', 'ACTIVE')) THEN
                RAISE EXCEPTION 'Clinic lifecycle conflict' USING ERRCODE = '40001';
            END IF;
            FOR staff_id IN SELECT id FROM public.staff_accounts
                WHERE organization_id = clinic ORDER BY id FOR UPDATE LOOP
                NULL;
            END LOOP;
            IF next_state = 'ACTIVE' AND NOT EXISTS (
                SELECT 1 FROM public.staff_accounts s
                JOIN public."user" u ON u.id = s.auth_user_id
                WHERE s.organization_id = clinic AND s.role = 'DOCTOR_OWNER'
                  AND s.enabled AND s.verified AND s.reset_block_operation_id IS NULL
                  AND u."emailVerified" AND u.email = s.verified_email
            ) THEN
                RAISE EXCEPTION 'Verified Owner unavailable' USING ERRCODE = '40001';
            END IF;
            IF next_state = 'SUSPENDED' THEN
                FOR operation_id IN SELECT id FROM public.recovery_operations
                    WHERE organization_id = clinic AND closed_at IS NULL
                    ORDER BY id FOR UPDATE LOOP
                    UPDATE public.recovery_operations SET invalidated = true,
                        invalidation_reference = 'OPERATOR_SUSPEND'
                    WHERE id = operation_id AND NOT invalidated;
                END LOOP;
            END IF;
            UPDATE public.organizations SET state = next_state WHERE id = clinic;
            INSERT INTO public.operator_action_pending
                (id, organization_id, operator_id, action, correlation_id)
            VALUES (gen_random_uuid(), clinic, actor,
                CASE WHEN next_state = 'SUSPENDED' THEN 'CLINIC_SUSPENDED'
                     ELSE 'CLINIC_REACTIVATED' END,
                clinic::text || ':' || (expected_revision + 1)::text || ':' || next_state);
            RETURN expected_revision + 1;
        END
        $$
    """)
    op.execute("DROP FUNCTION public.resolve_recovery_operation(text)")
    op.execute("DROP FUNCTION public.resolve_recovery_tenant(text)")

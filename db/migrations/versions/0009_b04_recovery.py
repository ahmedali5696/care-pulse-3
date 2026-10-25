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

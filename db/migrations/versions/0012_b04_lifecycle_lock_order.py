"""B-04: serialize operator lifecycle at Organization, not all Staff rows."""

from alembic import op

revision = "0012_b04_lifecycle_lock_order"
down_revision = "0011_b04_recovery_lock"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Every Staff write uses guard_identity_row, which locks its Organization
    # before changing authority. Explicitly locking all Staff rows here is
    # redundant and inverts the native recovery ordering: auth owns the
    # recovery row while PostgreSQL can take FK key-share locks on Staff.
    # Keep the Organization -> RecoveryOperation order for operator transitions.
    op.execute("""
        CREATE OR REPLACE FUNCTION public.operator_change_clinic_state(
            actor uuid, supplied text, clinic uuid, expected_revision bigint,
            next_state text)
        RETURNS bigint LANGUAGE plpgsql SECURITY DEFINER
        SET search_path = pg_catalog AS $$
        DECLARE org public.organizations%ROWTYPE; operation_id uuid;
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
    raise RuntimeError("B-04 lifecycle lock ordering is forward-only")

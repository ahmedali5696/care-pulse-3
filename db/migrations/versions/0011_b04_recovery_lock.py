"""B-04: narrow Owner-scoped recovery row locking without runtime UPDATE grants."""

from alembic import op

revision = "0011_b04_recovery_lock"
down_revision = "0010_b04_native_boundary"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Runtime has SELECT/INSERT but intentionally cannot UPDATE recovery
    # operations. PostgreSQL also requires UPDATE for SELECT ... FOR UPDATE.
    # A signed recovery handler establishes the tenant and verified Owner staff
    # GUC after locking Organization -> Staff; only this function locks the
    # matching operation row, never another Owner's operation.
    op.execute("""
        CREATE FUNCTION public.lock_owned_recovery(
            native_user text, target_digest text)
        RETURNS SETOF public.recovery_operations
        LANGUAGE sql VOLATILE SECURITY DEFINER
        SET search_path = pg_catalog AS $$
            SELECT r.*
            FROM public.recovery_operations r
            JOIN public.staff_accounts s
              ON s.organization_id = r.organization_id AND s.id = r.staff_id
             AND s.auth_user_id = r.auth_user_id
            JOIN public.organizations o ON o.id = s.organization_id
            JOIN public."user" u ON u.id = s.auth_user_id
            WHERE session_user = 'carepulse_runtime'
              AND r.organization_id =
                    NULLIF(current_setting('app.organization_id', true), '')::uuid
              AND r.staff_id =
                    NULLIF(current_setting('app.staff_id', true), '')::uuid
              AND r.auth_user_id = native_user
              AND r.purpose = 'PASSWORD_RESET'
              AND ((target_digest IS NULL AND r.closed_at IS NULL)
                OR (target_digest IS NOT NULL AND r.proof_digest = target_digest))
              AND s.role = 'DOCTOR_OWNER' AND s.enabled AND s.verified
              AND u."emailVerified" AND u.email = s.verified_email
              AND o.state = 'ACTIVE'
            ORDER BY r.id LIMIT 1 FOR UPDATE OF r
        $$
    """)
    op.execute(
        "REVOKE ALL ON FUNCTION public.lock_owned_recovery(text,text) FROM PUBLIC"
    )
    op.execute(
        "GRANT EXECUTE ON FUNCTION public.lock_owned_recovery(text,text) TO carepulse_runtime"
    )


def downgrade() -> None:
    # A 0011 caller must never silently run against the old unlocked boundary.
    raise RuntimeError("B-04 recovery lock migration is forward-only")

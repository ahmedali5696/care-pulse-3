"""B-02: minimal nonclinical sign-in discovery and durable login throttles."""

from alembic import op

revision = "0007_bff_sessions"
down_revision = "0006_authorization_barrier"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
        CREATE FUNCTION public.resolve_login_tenant(native_user_id text)
        RETURNS uuid LANGUAGE sql STABLE SECURITY DEFINER
        SET search_path = pg_catalog AS $$
          SELECT s.organization_id FROM public.staff_accounts s
          JOIN public.organizations o ON o.id = s.organization_id
          JOIN public."user" u ON u.id = s.auth_user_id
          WHERE s.auth_user_id = native_user_id AND o.state = 'ACTIVE'
            AND s.enabled AND s.verified AND s.reset_block_operation_id IS NULL
            AND u."emailVerified" AND u.email = s.verified_email
        $$
    """)
    op.execute("REVOKE ALL ON FUNCTION public.resolve_login_tenant(text) FROM PUBLIC")
    op.execute(
        "GRANT EXECUTE ON FUNCTION public.resolve_login_tenant(text) TO carepulse_runtime"
    )
    op.execute("""
        CREATE TABLE public.auth_login_attempts (
          dimension text NOT NULL CHECK (dimension IN ('identity', 'source')),
          scope_digest text NOT NULL CHECK (scope_digest ~ '^[0-9a-f]{64}$'),
          attempts integer NOT NULL CHECK (attempts > 0),
          window_start timestamptz NOT NULL,
          PRIMARY KEY (dimension, scope_digest)
        )
    """)
    op.execute("REVOKE ALL ON public.auth_login_attempts FROM PUBLIC")
    op.execute(
        "GRANT SELECT, INSERT, UPDATE ON public.auth_login_attempts TO carepulse_runtime"
    )


def downgrade() -> None:
    op.execute("DROP TABLE public.auth_login_attempts")
    op.execute("DROP FUNCTION public.resolve_login_tenant(text)")

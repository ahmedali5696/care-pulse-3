"""Restrict clinic runtime to purpose-bound native account existence checks."""

from alembic import op

revision = "0013_b04_native_secret_isolation"
down_revision = "0012_b04_lifecycle_lock_order"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute('REVOKE ALL ON public."account" FROM carepulse_runtime')

    op.execute("""
        CREATE FUNCTION public.native_account_unbound_for_receptionist(
            tenant_id uuid, native_user_id text)
        RETURNS boolean LANGUAGE sql STABLE SECURITY DEFINER
        SET search_path = pg_catalog AS $$
            SELECT session_user = 'carepulse_runtime'
               AND tenant_id = NULLIF(current_setting('app.organization_id', true), '')::uuid
               AND EXISTS (
                   SELECT 1 FROM public."user" u
                   WHERE u.id = native_user_id AND NOT u."emailVerified"
                     AND NOT EXISTS (
                         SELECT 1 FROM public."account" a WHERE a."userId" = u.id)
                     AND NOT EXISTS (
                         SELECT 1 FROM public.staff_accounts s WHERE s.auth_user_id = u.id)
               )
        $$
    """)
    op.execute("""
        CREATE FUNCTION public.native_receptionist_credential_ready(
            tenant_id uuid, invitation_id uuid)
        RETURNS boolean LANGUAGE sql STABLE SECURITY DEFINER
        SET search_path = pg_catalog AS $$
            SELECT session_user = 'carepulse_runtime'
               AND tenant_id = NULLIF(current_setting('app.organization_id', true), '')::uuid
               AND EXISTS (
                   SELECT 1 FROM public.invitations i
                   JOIN public.staff_accounts s ON s.organization_id = i.organization_id
                       AND s.id = i.staff_id
                   JOIN public."user" u ON u.id = s.auth_user_id
                   JOIN public."account" a ON a."userId" = u.id
                   WHERE i.id = invitation_id AND i.organization_id = tenant_id
                     AND i.purpose = 'RECEPTIONIST' AND i.state = 'ISSUED'
                     AND s.role = 'RECEPTIONIST' AND s.enabled AND NOT s.verified
                     AND s.verified_email = i.target_email
                     AND u.email = i.target_email AND u."emailVerified"
                     AND a."providerId" = 'credential' AND a.password IS NOT NULL
               )
        $$
    """)
    for signature in (
        "native_account_unbound_for_receptionist(uuid,text)",
        "native_receptionist_credential_ready(uuid,uuid)",
    ):
        op.execute(f"REVOKE ALL ON FUNCTION public.{signature} FROM PUBLIC")
        op.execute(f"GRANT EXECUTE ON FUNCTION public.{signature} TO carepulse_runtime")


def downgrade() -> None:
    raise RuntimeError("Native auth secret isolation is forward-only")

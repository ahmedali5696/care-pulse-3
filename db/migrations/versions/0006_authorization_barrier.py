"""B-01: serialize staff commands with organization lifecycle writes."""

from alembic import op

revision = "0006_authorization_barrier"
down_revision = "0005_delivery_failed_fence"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Runtime has SELECT-only Organization grants. A narrow definer function
    # takes the real row lock without granting runtime permission to mutate it.
    op.execute("""
        CREATE FUNCTION public.lock_active_organization(target uuid)
        RETURNS boolean
        LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog AS $$
        DECLARE active boolean;
        BEGIN
            IF target IS NULL OR target::text IS DISTINCT FROM
                NULLIF(current_setting('app.organization_id', true), '') THEN
                RETURN false;
            END IF;
            SELECT true INTO active FROM public.organizations
              WHERE id = target AND state = 'ACTIVE' FOR UPDATE;
            RETURN COALESCE(active, false);
        END
        $$
    """)
    op.execute(
        "REVOKE ALL ON FUNCTION public.lock_active_organization(uuid) FROM PUBLIC"
    )
    op.execute(
        "GRANT EXECUTE ON FUNCTION public.lock_active_organization(uuid) TO carepulse_runtime"
    )


def downgrade() -> None:
    op.execute("DROP FUNCTION public.lock_active_organization(uuid)")

"""Tenant metadata and operator identity; activation remains closed until D-02."""

from alembic import op

revision = "0002_organizations"
down_revision = "0001_foundation"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE public.operator_identities (
            id uuid PRIMARY KEY,
            credential_digest text NOT NULL UNIQUE CHECK (btrim(credential_digest) <> ''),
            enabled boolean NOT NULL DEFAULT true,
            credential_revision bigint NOT NULL DEFAULT 1 CHECK (credential_revision > 0),
            issued_at timestamptz NOT NULL DEFAULT now(),
            issued_by_reference text NOT NULL CHECK (btrim(issued_by_reference) <> ''),
            revoked_at timestamptz,
            revoked_by_reference text,
            CHECK ((revoked_at IS NULL) = (revoked_by_reference IS NULL)),
            CHECK (revoked_by_reference IS NULL OR btrim(revoked_by_reference) <> '')
        )
        """
    )
    op.execute(
        """
        CREATE TABLE public.organizations (
            id uuid PRIMARY KEY,
            branch text NOT NULL CHECK (btrim(branch) <> ''),
            region text NOT NULL DEFAULT 'EGYPT' CHECK (region = 'EGYPT'),
            timezone text NOT NULL DEFAULT 'Africa/Cairo' CHECK (timezone = 'Africa/Cairo'),
            currency text NOT NULL DEFAULT 'EGP' CHECK (currency = 'EGP'),
            state text NOT NULL DEFAULT 'PROVISIONING'
                CHECK (state IN ('PROVISIONING', 'ACTIVE', 'SUSPENDED')),
            lifecycle_revision bigint NOT NULL DEFAULT 0 CHECK (lifecycle_revision >= 0),
            created_by_operator_id uuid NOT NULL REFERENCES public.operator_identities(id),
            first_activated_at timestamptz,
            created_at timestamptz NOT NULL DEFAULT now(),
            updated_at timestamptz NOT NULL DEFAULT now()
        )
        """
    )
    op.execute(
        """
        CREATE FUNCTION public.guard_organization_foundation() RETURNS trigger
        LANGUAGE plpgsql SET search_path = pg_catalog AS $$
        BEGIN
            IF TG_OP = 'DELETE' THEN
                RAISE EXCEPTION 'Organization deletion is forbidden' USING ERRCODE = '23514';
            END IF;
            IF NEW.state <> 'PROVISIONING' THEN
                RAISE EXCEPTION 'Organization activation requires D-02 guard'
                    USING ERRCODE = '23514';
            END IF;
            IF NEW.first_activated_at IS NOT NULL THEN
                RAISE EXCEPTION 'Organization has not been activated'
                    USING ERRCODE = '23514';
            END IF;
            IF TG_OP = 'UPDATE' THEN
                IF (NEW.id, NEW.region, NEW.timezone, NEW.currency,
                    NEW.created_by_operator_id, NEW.created_at) IS DISTINCT FROM
                   (OLD.id, OLD.region, OLD.timezone, OLD.currency,
                    OLD.created_by_operator_id, OLD.created_at) THEN
                    RAISE EXCEPTION 'Immutable organization identity'
                        USING ERRCODE = '23514';
                END IF;
                NEW.lifecycle_revision := OLD.lifecycle_revision + 1;
                NEW.updated_at := now();
            END IF;
            RETURN NEW;
        END
        $$
        """
    )
    op.execute(
        "REVOKE ALL ON FUNCTION public.guard_organization_foundation() FROM PUBLIC"
    )
    op.execute(
        """
        CREATE TRIGGER organizations_foundation_guard
        BEFORE INSERT OR UPDATE OR DELETE ON public.organizations
        FOR EACH ROW EXECUTE FUNCTION public.guard_organization_foundation()
        """
    )
    op.execute("ALTER TABLE public.organizations ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE public.organizations FORCE ROW LEVEL SECURITY")
    op.execute(
        """
        CREATE POLICY organization_runtime_read ON public.organizations
        FOR SELECT TO carepulse_runtime
        USING (id = NULLIF(current_setting('app.organization_id', true), '')::uuid)
        """
    )
    op.execute(
        """
        CREATE POLICY organization_operator_read ON public.organizations
        FOR SELECT TO carepulse_operator USING (true)
        """
    )
    op.execute(
        """
        CREATE POLICY organization_migration_access ON public.organizations
        FOR ALL TO carepulse_migration USING (true) WITH CHECK (true)
        """
    )
    # The worker will enumerate only active tenant IDs before opening tenant-scoped work.
    op.execute(
        """
        CREATE VIEW public.runtime_active_tenant_ids WITH (security_barrier = true) AS
        SELECT id FROM public.organizations WHERE state = 'ACTIVE'
        """
    )
    op.execute("GRANT USAGE ON SCHEMA public TO carepulse_operator")
    op.execute("GRANT SELECT ON public.runtime_active_tenant_ids TO carepulse_runtime")
    op.execute(
        """
        GRANT SELECT (id, branch, region, timezone, currency, state,
                      lifecycle_revision, created_at, updated_at)
        ON public.organizations TO carepulse_runtime, carepulse_operator
        """
    )
    op.execute(
        """
        GRANT SELECT (id, enabled, credential_revision,
                      issued_at, issued_by_reference, revoked_at, revoked_by_reference)
        ON public.operator_identities TO carepulse_operator
        """
    )


def downgrade() -> None:
    op.execute("DROP VIEW public.runtime_active_tenant_ids")
    op.execute("DROP TABLE public.organizations")
    op.execute("DROP FUNCTION public.guard_organization_foundation()")
    op.execute("DROP TABLE public.operator_identities")
    op.execute("REVOKE USAGE ON SCHEMA public FROM carepulse_operator")

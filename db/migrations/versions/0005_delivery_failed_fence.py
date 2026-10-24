"""Close D-09's unowned live-lease CLAIMED -> FAILED transition."""

from alembic import op

revision = "0005_delivery_failed_fence"
down_revision = "0004_evidence"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Keep the deployed 0004 migration immutable. This additional guard runs
    # alongside its existing state-machine trigger.
    op.execute("""
        CREATE FUNCTION public.guard_active_delivery_failure() RETURNS trigger
        LANGUAGE plpgsql SET search_path = pg_catalog AS $$
        BEGIN
            IF OLD.state = 'CLAIMED' AND NEW.state = 'FAILED'
               AND OLD.lease_expires_at > clock_timestamp()
               AND NOT COALESCE(
                   current_setting('app.delivery_claim_owner', true) =
                       OLD.claim_owner::text
                   AND current_setting('app.delivery_claim_generation', true) =
                       OLD.claim_generation::text,
                   false
               ) THEN
                RAISE EXCEPTION 'Unowned active delivery lease cannot fail'
                    USING ERRCODE = '23514';
            END IF;
            RETURN NEW;
        END
        $$
    """)
    op.execute(
        "REVOKE ALL ON FUNCTION public.guard_active_delivery_failure() FROM PUBLIC"
    )
    op.execute("""
        CREATE TRIGGER delivery_jobs_active_failure_fence
        BEFORE UPDATE ON public.delivery_jobs
        FOR EACH ROW EXECUTE FUNCTION public.guard_active_delivery_failure()
    """)


def downgrade() -> None:
    # Downgrading disables this defense: never downgrade production to a
    # vulnerable revision. Disposable CI tests exercise the downgrade path.
    op.execute(
        "DROP TRIGGER delivery_jobs_active_failure_fence ON public.delivery_jobs"
    )
    op.execute("DROP FUNCTION public.guard_active_delivery_failure()")

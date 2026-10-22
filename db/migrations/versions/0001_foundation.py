"""Empty foundation baseline; clinical, identity and job schema remain deferred."""

from alembic import op

revision = "0001_foundation"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Alembic creates its supported version table before invoking this revision.
    op.execute("GRANT SELECT ON TABLE public.alembic_version TO carepulse_runtime")


def downgrade() -> None:
    op.execute("REVOKE SELECT ON TABLE public.alembic_version FROM carepulse_runtime")

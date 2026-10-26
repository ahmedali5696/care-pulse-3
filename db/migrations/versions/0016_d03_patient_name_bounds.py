"""D-03: require conventional array subscripts for patient name parts."""

from alembic import op

revision = "0016_d03_patient_name_bounds"
down_revision = "0015_d03_patient_initial_state"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
        ALTER TABLE public.patients
        ADD CONSTRAINT patients_name_array_starts_at_one
        CHECK (array_lower(name_parts, 1) = 1)
    """)


def downgrade() -> None:
    raise RuntimeError("D-03 patient name-array correction is forward-only")

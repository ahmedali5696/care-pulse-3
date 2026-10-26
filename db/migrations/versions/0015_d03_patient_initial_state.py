"""D-03: refuse forged Patient initial archive and revisions."""

from alembic import op

revision = "0015_d03_patient_initial_state"
down_revision = "0014_patients"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # PostgreSQL btrim() without an explicit character set leaves tabs and newlines.
    op.execute("ALTER TABLE public.patients DROP CONSTRAINT patients_name_parts_valid")
    op.execute("""
        ALTER TABLE public.patients ADD CONSTRAINT patients_name_parts_valid CHECK (
            array_ndims(name_parts) = 1
            AND cardinality(name_parts) IN (3, 4)
            AND array_position(name_parts, NULL) IS NULL
            AND name_parts[1] ~ '[^[:space:]]'
            AND name_parts[2] ~ '[^[:space:]]'
            AND name_parts[3] ~ '[^[:space:]]'
            AND (cardinality(name_parts) = 3
                 OR name_parts[4] ~ '[^[:space:]]')
        )
    """)
    op.execute("""
        CREATE FUNCTION public.guard_patient_initial_state() RETURNS trigger
        LANGUAGE plpgsql SET search_path = pg_catalog AS $$
        BEGIN
            IF NEW.archived OR NEW.archived_at IS NOT NULL
               OR NEW.demographic_revision <> 0
               OR NEW.lifecycle_revision <> 0 THEN
                RAISE EXCEPTION 'Invalid initial patient lifecycle state'
                    USING ERRCODE = '23514';
            END IF;
            RETURN NEW;
        END
        $$
    """)
    op.execute(
        "REVOKE ALL ON FUNCTION public.guard_patient_initial_state() FROM PUBLIC"
    )
    op.execute("""
        CREATE TRIGGER patients_initial_state_guard
        BEFORE INSERT ON public.patients
        FOR EACH ROW EXECUTE FUNCTION public.guard_patient_initial_state()
    """)


def downgrade() -> None:
    raise RuntimeError("D-03 patient initial-state correction is forward-only")

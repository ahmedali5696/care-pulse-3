"""T159: accept retained integral JSON duration numerics in the exact-time guard."""

from alembic import op

revision = "0038_t159_duration_numeric"
down_revision = "0037_t155_followup_reason"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # The existing SECURITY DEFINER function retains its owner and EXECUTE fence.
    op.execute("""
        DO $migration$
        DECLARE
            definition text;
            old_cast constant text := '(policy->>''duration_minutes'')::integer';
            new_cast constant text :=
                '(CASE WHEN (policy->>''duration_minutes'')::numeric BETWEEN 1 AND 1439'
                || ' THEN (policy->>''duration_minutes'')::numeric::integer END)';
        BEGIN
            SELECT pg_get_functiondef(
                to_regprocedure('public.b10_schedule_without_offset_guard(uuid,jsonb)')
            ) INTO definition;
            IF definition IS NULL
               OR (length(definition) - length(replace(definition, old_cast, '')))
                  / length(old_cast) <> 1 THEN
                RAISE EXCEPTION 'Unexpected exact-time duration guard source';
            END IF;
            EXECUTE replace(definition, old_cast, new_cast);
        END $migration$;
    """)


def downgrade() -> None:
    raise RuntimeError("T159 exact-time duration compatibility is forward-only")

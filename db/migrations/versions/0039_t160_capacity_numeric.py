"""Read retained integral JSON capacity numerics consistently across Wave 2."""

from alembic import op

revision = "0039_t160_capacity_numeric"
down_revision = "0038_t159_duration_numeric"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Rebuild installed, privileged functions in place, preserving their owners,
    # security-definer attributes, signatures and explicit EXECUTE revocations.
    op.execute("""
        DO $migration$
        DECLARE
            function_name text;
            definition text;
            old_cast text;
            new_cast text;
            occurrences integer;
        BEGIN
            FOR function_name, old_cast, new_cast, occurrences IN
                SELECT * FROM (VALUES
                    ('public.lock_doctor_admission_capacity(uuid,uuid,date,text)',
                     '(p.payload->>''capacity'')::bigint',
                     '((p.payload->>''capacity'')::numeric)::bigint', 1),
                    ('public.assert_confirmation_issued_scope(public.doctor_admission_confirmations)',
                     '(capacity_policy.payload->>''capacity'')::bigint',
                     '((capacity_policy.payload->>''capacity'')::numeric)::bigint', 1),
                    ('public.assert_admission_confirmation_final()',
                     '(capacity_policy.payload->>''capacity'')::bigint',
                     '((capacity_policy.payload->>''capacity'')::numeric)::bigint', 2),
                    ('public.b10_schedule_without_offset_guard(uuid,jsonb)',
                     '(v.payload->>''capacity'')::integer',
                     '((v.payload->>''capacity'')::numeric)::integer', 1)
                ) AS replacements(name, before_text, after_text, expected_count)
            LOOP
                SELECT pg_get_functiondef(to_regprocedure(function_name))
                  INTO definition;
                IF definition IS NULL THEN
                    RAISE EXCEPTION 'Required capacity function missing: %', function_name;
                END IF;
                IF (length(definition) - length(replace(definition, old_cast, '')))
                    / length(old_cast) <> occurrences THEN
                    RAISE EXCEPTION 'Unexpected capacity guard source: %', function_name;
                END IF;
                EXECUTE replace(definition, old_cast, new_cast);
            END LOOP;
        END $migration$;
    """)


def downgrade() -> None:
    raise RuntimeError("T160 capacity numeric compatibility migration is forward-only")

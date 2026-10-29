"""B-07: permit weekly manual slots while preserving dated schedule intervals."""

from alembic import op

revision = "0020_b07_manual_slot_weekly"
down_revision = "0019_b06_price_policy_strings"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
        DO $$
        DECLARE
            matching_count integer;
            old_name name;
            old_definition text;
        BEGIN
            SELECT count(*), (array_agg(c.conname))[1],
                   (array_agg(pg_get_constraintdef(c.oid)))[1]
              INTO matching_count, old_name, old_definition
            FROM pg_constraint c
            JOIN pg_attribute kind ON kind.attrelid = c.conrelid
                AND kind.attname = 'interval_kind'
            JOIN pg_attribute weekday ON weekday.attrelid = c.conrelid
                AND weekday.attname = 'weekday'
            JOIN pg_attribute service_date ON service_date.attrelid = c.conrelid
                AND service_date.attname = 'service_date'
            WHERE c.conrelid = 'public.clinic_schedule_intervals'::regclass
              AND c.contype = 'c'
              AND c.conkey @> ARRAY[kind.attnum, weekday.attnum,
                                   service_date.attnum]::smallint[];

            IF matching_count <> 1
               OR old_definition NOT LIKE '%''WORK''%'
               OR old_definition NOT LIKE '%''BREAK''%'
               OR old_definition NOT LIKE '%''OVERRIDE_WORK''%'
               OR old_definition NOT LIKE '%''MANUAL_SLOT''%'
               OR lower(old_definition) NOT LIKE '%weekday is not null%'
               OR lower(old_definition) NOT LIKE '%weekday is null%'
               OR lower(old_definition) NOT LIKE '%service_date is null%'
               OR lower(old_definition) NOT LIKE '%service_date is not null%'
               OR lower(old_definition) NOT LIKE '%isfinite(service_date)%' THEN
                RAISE EXCEPTION 'Unexpected schedule interval shape constraint'
                    USING ERRCODE = '23514';
            END IF;

            EXECUTE format(
                'ALTER TABLE public.clinic_schedule_intervals DROP CONSTRAINT %I', old_name
            );
            ALTER TABLE public.clinic_schedule_intervals
                ADD CONSTRAINT clinic_schedule_interval_shape CHECK (
                    (interval_kind IN ('WORK', 'BREAK', 'MANUAL_SLOT')
                     AND weekday IS NOT NULL AND weekday BETWEEN 1 AND 7
                     AND service_date IS NULL)
                    OR (interval_kind IN ('OVERRIDE_WORK', 'MANUAL_SLOT')
                        AND weekday IS NULL AND service_date IS NOT NULL
                        AND isfinite(service_date))
                );
        END $$
    """)


def downgrade() -> None:
    raise RuntimeError("B-07 weekly manual slots migration is forward-only")

"""B-06: accept canonical string prices without invalidating legacy policies."""

from alembic import op

revision = "0019_b06_price_policy_strings"
down_revision = "0018_d05_appointments"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(r"""
        CREATE OR REPLACE FUNCTION public.valid_clinic_policy(kind text, value jsonb)
        RETURNS boolean LANGUAGE plpgsql IMMUTABLE SET search_path = pg_catalog AS $$
        DECLARE amount text;
        BEGIN
            IF jsonb_typeof(value) <> 'object' THEN RETURN false; END IF;
            IF kind = 'PRICE' THEN
                IF NOT value ?& ARRAY['standard_new_exam_egp',
                    'preferential_followup_egp', 'urgent_base_egp', 'urgent_enabled']
                   OR jsonb_typeof(value->'urgent_enabled') <> 'boolean' THEN
                    RETURN false;
                END IF;
                FOREACH amount IN ARRAY ARRAY['standard_new_exam_egp',
                    'preferential_followup_egp','urgent_base_egp'] LOOP
                    IF jsonb_typeof(value->amount) NOT IN ('number', 'null', 'string') THEN
                        RETURN false;
                    END IF;
                    IF jsonb_typeof(value->amount) = 'number'
                       AND (value->>amount)::numeric < 0 THEN RETURN false; END IF;
                    IF jsonb_typeof(value->amount) = 'string'
                       AND (value->>amount) !~ '^[0-9]+\.[0-9]{2}$' THEN
                        RETURN false;
                    END IF;
                END LOOP;
                IF (value->>'urgent_enabled')::boolean AND
                    (value->>'urgent_base_egp' IS NULL OR
                     value->>'standard_new_exam_egp' IS NULL OR
                     (value->>'urgent_base_egp')::numeric <=
                     (value->>'standard_new_exam_egp')::numeric) THEN
                    RETURN false;
                END IF;
                RETURN true;
            ELSIF kind = 'SCHEDULE' THEN
                RETURN value ?& ARRAY['booking_mode','duration_minutes',
                    'holiday_booking_enabled','routine_walk_in_enabled',
                    'allow_overlap','no_show_cutoff']
                  AND value->>'booking_mode' IN ('OPEN','EXACT_TIME')
                  AND jsonb_typeof(value->'holiday_booking_enabled') = 'boolean'
                  AND jsonb_typeof(value->'routine_walk_in_enabled') = 'boolean'
                  AND jsonb_typeof(value->'allow_overlap') = 'boolean'
                  AND jsonb_typeof(value->'duration_minutes') IN ('null','number')
                  AND (value->>'duration_minutes' IS NULL OR
                       ((value->>'duration_minutes')::numeric > 0
                        AND (value->>'duration_minutes')::numeric =
                            trunc((value->>'duration_minutes')::numeric)))
                  AND jsonb_typeof(value->'no_show_cutoff') = 'object'
                  AND (value->'no_show_cutoff') ?& ARRAY['enabled','local_time']
                  AND jsonb_typeof(value->'no_show_cutoff'->'enabled') = 'boolean'
                  AND jsonb_typeof(value->'no_show_cutoff'->'local_time') IN
                      ('null','string')
                  AND (value->'no_show_cutoff'->>'local_time' IS NULL OR
                       (value->'no_show_cutoff'->>'local_time') ~
                       '^([01][0-9]|2[0-3]):[0-5][0-9]$');
            ELSIF kind = 'QUEUE' THEN
                RETURN value ?& ARRAY['routine_order','late_depth']
                  AND value->>'routine_order' IN ('BOOKED_NUMBER','ARRIVAL_FIFO')
                  AND jsonb_typeof(value->'late_depth') = 'number'
                  AND (value->>'late_depth')::numeric BETWEEN 0 AND 2147483647
                  AND (value->>'late_depth')::numeric =
                      trunc((value->>'late_depth')::numeric);
            ELSIF kind = 'ADMISSION' THEN
                RETURN value ? 'actors'
                  AND value->>'actors' IN ('BOTH','DOCTOR','RECEPTIONIST');
            ELSIF kind = 'CAPACITY' THEN
                RETURN value ?& ARRAY['capacity','paused']
                  AND jsonb_typeof(value->'paused') = 'boolean'
                  AND jsonb_typeof(value->'capacity') = 'number'
                  AND (value->>'capacity')::numeric BETWEEN 1 AND 2147483647
                  AND (value->>'capacity')::numeric =
                      trunc((value->>'capacity')::numeric);
            ELSIF kind = 'FOLLOWUP' THEN
                RETURN value ?& ARRAY['completion_day_zero','last_day_inclusive']
                  AND value->'completion_day_zero' = 'true'::jsonb
                  AND jsonb_typeof(value->'last_day_inclusive') = 'number'
                  AND (value->>'last_day_inclusive')::numeric BETWEEN 0 AND 365
                  AND (value->>'last_day_inclusive')::numeric =
                      trunc((value->>'last_day_inclusive')::numeric);
            END IF;
            RETURN false;
        END
        $$
    """)


def downgrade() -> None:
    raise RuntimeError("B-06 price policy strings migration is forward-only")

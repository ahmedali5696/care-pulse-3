"""T146: preserve exact timed instants and validate the full Cairo fold."""

from alembic import op

revision = "0035_t146_cairo_exact_schedule"
down_revision = "0034_t145_exact_percentage"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(r"""
        ALTER FUNCTION public.b10_assert_followup_schedule(uuid,jsonb)
            RENAME TO b10_schedule_without_exact_guard;
        REVOKE ALL ON FUNCTION public.b10_schedule_without_exact_guard(uuid,jsonb)
            FROM PUBLIC, carepulse_runtime;

        CREATE FUNCTION public.b10_assert_followup_schedule(org uuid, booking jsonb)
        RETURNS void LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog AS $$
        DECLARE
            endpoint_key text;
            day date := (booking->>'service_date')::date;
            weekday integer := extract(isodow FROM day);
            starts timestamptz;
            ends timestamptz;
            local_start time;
            local_end time;
            version bigint;
            has_override boolean;
            period_kind text;
        BEGIN
            IF booking->>'booking_mode' = 'EXACT_TIME' THEN
                FOREACH endpoint_key IN ARRAY ARRAY['starts_at','ends_at'] LOOP
                    -- PostgreSQL timestamptz stores microseconds; never round a requested instant.
                    IF substring(booking->>endpoint_key FROM '\.[0-9]{6}([0-9]+)')
                        ~ '[1-9]' THEN
                        RAISE EXCEPTION 'Timed endpoint precision is not representable'
                            USING ERRCODE = '23514';
                    END IF;
                END LOOP;
            END IF;

            PERFORM public.b10_schedule_without_exact_guard(org, booking);
            IF booking->>'booking_mode' <> 'EXACT_TIME' THEN
                RETURN;
            END IF;

            starts := (booking->>'starts_at')::timestamptz;
            ends := (booking->>'ends_at')::timestamptz;
            local_start := (starts AT TIME ZONE 'Africa/Cairo')::time;
            local_end := (ends AT TIME ZONE 'Africa/Cairo')::time;
            SELECT current_version INTO version FROM public.clinic_policy_heads
            WHERE organization_id=org AND kind='SCHEDULE';
            SELECT EXISTS (
                SELECT 1 FROM public.clinic_schedule_intervals
                WHERE organization_id=org AND policy_version=version
                  AND interval_kind='OVERRIDE_WORK' AND service_date=day
            ) INTO has_override;
            period_kind := CASE WHEN has_override THEN 'OVERRIDE_WORK' ELSE 'WORK' END;

            -- Each minute of an ambiguous fall-back interval must belong to one
            -- resolved work segment and avoid both occurrences of every break.
            IF NOT EXISTS (
                SELECT 1 FROM public.clinic_schedule_intervals i
                WHERE i.organization_id=org AND i.policy_version=version
                  AND i.interval_kind=period_kind
                  AND (CASE WHEN has_override THEN i.service_date=day
                            ELSE i.weekday=weekday END)
                  AND i.starts_at <= local_start AND i.ends_at >= local_end
                  AND NOT EXISTS (
                      SELECT 1 FROM pg_catalog.generate_series(
                          starts, ends - interval '1 microsecond', interval '1 minute'
                      ) AS minutes(instant)
                      WHERE (minutes.instant AT TIME ZONE 'Africa/Cairo')::time
                            < i.starts_at
                         OR (minutes.instant AT TIME ZONE 'Africa/Cairo')::time
                            >= i.ends_at
                  )
            ) OR EXISTS (
                SELECT 1 FROM public.clinic_schedule_intervals b
                WHERE b.organization_id=org AND b.policy_version=version
                  AND b.interval_kind='BREAK' AND b.weekday=weekday
                  AND EXISTS (
                      SELECT 1 FROM pg_catalog.generate_series(
                          starts, ends - interval '1 microsecond', interval '1 minute'
                      ) AS minutes(instant)
                      WHERE (minutes.instant AT TIME ZONE 'Africa/Cairo')::time
                            >= b.starts_at
                        AND (minutes.instant AT TIME ZONE 'Africa/Cairo')::time
                            < b.ends_at
                  )
            ) THEN
                RAISE EXCEPTION 'Timed interval outside resolved Cairo work periods'
                    USING ERRCODE = '23514';
            END IF;
        END $$;
        REVOKE ALL ON FUNCTION public.b10_assert_followup_schedule(uuid,jsonb)
            FROM PUBLIC, carepulse_runtime;
    """)


def downgrade() -> None:
    raise RuntimeError("T146 Cairo exact-schedule migration is forward-only")

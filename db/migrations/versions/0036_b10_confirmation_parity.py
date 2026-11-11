"""T148–T150: align protected Follow-up scheduling and price with confirmation."""

from alembic import op

revision = "0036_b10_confirmation_parity"
down_revision = "0035_t146_cairo_exact_schedule"
branch_labels = None
depends_on = None


def upgrade() -> None:
    _price_guard()
    _schedule_guard()


def _price_guard() -> None:
    op.execute(r"""
        CREATE OR REPLACE FUNCTION public.b10_assert_followup_price(org uuid, quote jsonb)
        RETURNS void LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog AS $$
        DECLARE
            active_version bigint;
            policy jsonb;
            base numeric;
            standard numeric;
            -- Match Python str.strip() while retaining the submitted evidence.
            reason_whitespace constant text :=
                U&'\0009\000A\000B\000C\000D\001C\001D\001E\001F\0020\0085\00A0\1680'
                || U&'\2000\2001\2002\2003\2004\2005\2006\2007\2008\2009\200A\2028\2029\202F\205F\3000';
        BEGIN
            SELECT h.current_version, p.payload INTO active_version, policy
            FROM public.clinic_policy_heads h JOIN public.clinic_policy_versions p
              ON (p.organization_id, p.kind, p.version) =
                 (h.organization_id, h.kind, h.current_version)
            WHERE h.organization_id = org AND h.kind = 'PRICE';
            IF jsonb_typeof(quote) IS DISTINCT FROM 'object'
               OR NOT quote ?& ARRAY['base_egp','discount_type','discount_value',
                                    'discount_reason','is_free','net_egp',
                                    'pricing_version','creation_standard_base_egp']
               OR (quote - 'base_egp' - 'discount_type' - 'discount_value'
                    - 'discount_reason' - 'is_free' - 'net_egp'
                    - 'pricing_version' - 'creation_standard_base_egp') <> '{}'::jsonb
               OR policy IS NULL THEN
                RAISE EXCEPTION 'Invalid follow-up price quote' USING ERRCODE = '23514';
            END IF;
            base := (policy->>'preferential_followup_egp')::numeric;
            standard := (policy->>'standard_new_exam_egp')::numeric;
            IF base IS NULL OR standard IS NULL
               OR (quote->>'base_egp')::numeric IS DISTINCT FROM base
               OR (quote->>'creation_standard_base_egp')::numeric IS DISTINCT FROM standard
               OR (quote->>'pricing_version')::bigint IS DISTINCT FROM active_version THEN
                RAISE EXCEPTION 'Follow-up price does not match active clinic policy'
                    USING ERRCODE = '23514';
            END IF;
            IF quote->>'discount_type' IN ('FIXED', 'PERCENT')
               AND (jsonb_typeof(quote->'discount_reason') IS DISTINCT FROM 'string'
                    OR btrim(quote->>'discount_reason', reason_whitespace) = '') THEN
                RAISE EXCEPTION 'Invalid follow-up discount reason'
                    USING ERRCODE = '23514';
            END IF;
            -- The immutable price-snapshot CHECK constraints enforce the discount/net formula.
        END $$;
        REVOKE ALL ON FUNCTION public.b10_assert_followup_price(uuid,jsonb)
            FROM PUBLIC, carepulse_runtime;
    """)


def _schedule_guard() -> None:
    op.execute(r"""
        CREATE OR REPLACE FUNCTION public.b10_schedule_without_offset_guard(
            org uuid, booking jsonb)
        RETURNS void LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog AS $$
        DECLARE
            day date := (booking->>'service_date')::date;
            mode text := booking->>'booking_mode';
            starts timestamptz;
            ends timestamptz;
            local_start time;
            local_end time;
            service_weekday integer := extract(isodow FROM day);
            version bigint;
            policy jsonb;
            on_holiday boolean;
            has_override boolean;
            open_override boolean;
            weekly_enabled boolean;
            manual_date boolean;
            manual_slot boolean;
            generated_slot boolean;
            period_kind text;
            duration integer;
            limit_count integer;
            occupancy bigint;
            -- PostgreSQL can normalize leap seconds and 24:00 during the cast.
            endpoint_pattern constant text :=
                '^[0-9]{4}-[0-9]{2}-[0-9]{2}T([01][0-9]|2[0-3]):[0-5][0-9]:[0-5][0-9]'
                '(\.[0-9]+)?(Z|[+-]([01][0-9]|2[0-3]):[0-5][0-9])$';
        BEGIN
            SELECT h.current_version, p.payload INTO version, policy
            FROM public.clinic_policy_heads h JOIN public.clinic_policy_versions p
              ON (p.organization_id, p.kind, p.version) =
                 (h.organization_id, h.kind, h.current_version)
            WHERE h.organization_id = org AND h.kind = 'SCHEDULE';
            IF policy IS NULL OR mode IS DISTINCT FROM policy->>'booking_mode'
               OR day IS NULL OR NOT isfinite(day)
               OR day < (clock_timestamp() AT TIME ZONE 'Africa/Cairo')::date THEN
                RAISE EXCEPTION 'Follow-up schedule unavailable' USING ERRCODE = '23514';
            END IF;
            SELECT EXISTS (
                SELECT 1 FROM public.clinic_schedule_holidays
                WHERE organization_id=org AND policy_version=version AND service_date=day
            ) INTO on_holiday;
            SELECT EXISTS (
                SELECT 1 FROM public.clinic_schedule_intervals
                WHERE organization_id=org AND policy_version=version
                  AND interval_kind='OVERRIDE_WORK' AND service_date=day
            ) INTO has_override;
            open_override := COALESCE(
                policy->'open_override_dates' @> to_jsonb(ARRAY[day::text]), false);
            IF policy ? 'working_weekdays' THEN
                weekly_enabled := policy->'working_weekdays' @> to_jsonb(ARRAY[service_weekday]);
            ELSE
                SELECT EXISTS (
                    SELECT 1 FROM public.clinic_schedule_intervals
                    WHERE organization_id=org AND policy_version=version
                      AND interval_kind='WORK' AND weekday=service_weekday
                ) INTO weekly_enabled;
            END IF;
            IF NOT ((NOT on_holiday AND (has_override OR open_override OR weekly_enabled))
                    OR (policy->>'holiday_booking_enabled')::boolean) THEN
                RAISE EXCEPTION 'Follow-up service date closed' USING ERRCODE = '23514';
            END IF;
            IF mode = 'OPEN' THEN
                IF booking->>'starts_at' IS NOT NULL
                   OR booking->>'ends_at' IS NOT NULL THEN
                    RAISE EXCEPTION 'Open booking cannot carry a timed interval'
                        USING ERRCODE = '23514';
                END IF;
                RETURN;
            END IF;
            IF COALESCE(booking->>'starts_at', '') !~ endpoint_pattern
               OR COALESCE(booking->>'ends_at', '') !~ endpoint_pattern THEN
                RAISE EXCEPTION 'Timed booking requires explicit RFC3339 offsets'
                    USING ERRCODE = '23514';
            END IF;
            starts := (booking->>'starts_at')::timestamptz;
            ends := (booking->>'ends_at')::timestamptz;
            IF starts IS NULL OR ends IS NULL OR NOT isfinite(starts)
               OR NOT isfinite(ends) OR starts <= clock_timestamp()
               OR starts >= ends
               OR (starts AT TIME ZONE 'Africa/Cairo')::date <> day
               OR (ends AT TIME ZONE 'Africa/Cairo')::date <> day THEN
                RAISE EXCEPTION 'Invalid Cairo booking interval' USING ERRCODE = '23514';
            END IF;
            local_start := (starts AT TIME ZONE 'Africa/Cairo')::time;
            local_end := (ends AT TIME ZONE 'Africa/Cairo')::time;
            IF local_start >= local_end THEN
                RAISE EXCEPTION 'Invalid local booking interval' USING ERRCODE = '23514';
            END IF;
            period_kind := CASE WHEN has_override THEN 'OVERRIDE_WORK'
                WHEN NOT on_holiday AND NOT open_override AND weekly_enabled
                    THEN 'WORK' ELSE NULL END;
            IF period_kind IS NULL OR NOT EXISTS (
                SELECT 1 FROM public.clinic_schedule_intervals i
                WHERE i.organization_id=org AND i.policy_version=version
                  AND i.interval_kind=period_kind
                  AND (CASE WHEN has_override THEN i.service_date=day
                            ELSE i.weekday=service_weekday END)
                  AND i.starts_at <= local_start AND i.ends_at >= local_end
            ) OR EXISTS (
                SELECT 1 FROM public.clinic_schedule_intervals i
                WHERE i.organization_id=org AND i.policy_version=version
                  AND i.interval_kind='BREAK' AND i.weekday=service_weekday
                  AND i.starts_at < local_end AND i.ends_at > local_start
            ) THEN
                RAISE EXCEPTION 'Timed interval outside working periods'
                    USING ERRCODE = '23514';
            END IF;
            manual_date := COALESCE(
                policy->'manual_slot_dates' @> to_jsonb(ARRAY[day::text]), false)
                OR EXISTS (
                    SELECT 1 FROM public.clinic_schedule_intervals
                    WHERE organization_id=org AND policy_version=version
                      AND interval_kind='MANUAL_SLOT' AND service_date=day
                );
            SELECT EXISTS (
                SELECT 1 FROM public.clinic_schedule_intervals i
                WHERE i.organization_id=org AND i.policy_version=version
                  AND i.interval_kind='MANUAL_SLOT'
                  AND (CASE WHEN manual_date THEN i.service_date=day
                            ELSE i.weekday=service_weekday AND i.service_date IS NULL END)
                  AND i.starts_at=local_start AND i.ends_at=local_end
            ) INTO manual_slot;
            IF NOT manual_slot THEN
                duration := (policy->>'duration_minutes')::integer;
                SELECT EXISTS (
                    SELECT 1 FROM public.clinic_schedule_intervals i
                    WHERE i.organization_id=org AND i.policy_version=version
                      AND i.interval_kind=period_kind
                      AND (CASE WHEN has_override THEN i.service_date=day
                                ELSE i.weekday=service_weekday END)
                      AND i.starts_at <= local_start AND local_end <= i.ends_at
                      AND duration > 0 AND duration < 1440
                      AND ends-starts=make_interval(mins => duration)
                      AND local_end-local_start=make_interval(mins => duration)
                      AND mod(extract(epoch FROM (local_start-i.starts_at))::numeric,
                              duration * 60) = 0
                ) INTO generated_slot;
                IF NOT generated_slot OR EXISTS (
                    WITH cairo_offsets AS (
                        -- Probe either side of the service day to retain both fold offsets.
                        SELECT DISTINCT
                            ((probe AT TIME ZONE 'UTC') AT TIME ZONE 'Africa/Cairo')
                                - probe AS utc_offset
                        FROM (VALUES (day - 1 + time '12:00'),
                                     (day + 1 + time '12:00')) AS probes(probe)
                    ), pauses AS (
                        SELECT starts_at, ends_at FROM public.clinic_schedule_intervals
                        WHERE organization_id=org AND policy_version=version
                          AND interval_kind='BREAK' AND weekday=service_weekday
                    )
                    SELECT 1 FROM public.clinic_schedule_intervals i
                    JOIN public.clinic_schedule_intervals work
                      ON work.organization_id=org AND work.policy_version=version
                     AND work.interval_kind=period_kind
                     AND (CASE WHEN has_override THEN work.service_date=day
                               ELSE work.weekday=service_weekday END)
                     AND work.starts_at <= i.starts_at AND work.ends_at >= i.ends_at
                    CROSS JOIN cairo_offsets start_offset
                    CROSS JOIN cairo_offsets end_offset
                    CROSS JOIN LATERAL (
                        SELECT ((day + i.starts_at) AT TIME ZONE 'UTC')
                                   - start_offset.utc_offset AS starts_at,
                               ((day + i.ends_at) AT TIME ZONE 'UTC')
                                   - end_offset.utc_offset AS ends_at
                    ) candidate
                    WHERE i.organization_id=org AND i.policy_version=version
                      AND i.interval_kind='MANUAL_SLOT'
                      AND (CASE WHEN manual_date THEN i.service_date=day
                                ELSE i.weekday=service_weekday AND i.service_date IS NULL END)
                      AND candidate.starts_at < candidate.ends_at
                      AND candidate.starts_at < ends AND candidate.ends_at > starts
                      AND candidate.starts_at AT TIME ZONE 'Africa/Cairo' = day + i.starts_at
                      AND candidate.ends_at AT TIME ZONE 'Africa/Cairo' = day + i.ends_at
                      AND NOT EXISTS (
                          SELECT 1 FROM pauses b
                          WHERE b.starts_at < i.ends_at AND b.ends_at > i.starts_at
                      )
                      AND NOT EXISTS (
                          SELECT 1 FROM generate_series(
                              candidate.starts_at, candidate.ends_at - interval '1 microsecond',
                              interval '1 minute'
                          ) AS minutes(instant)
                          CROSS JOIN LATERAL (
                              SELECT (minutes.instant AT TIME ZONE 'Africa/Cairo')::time
                                  AS wall_time
                          ) minute_wall
                          WHERE minute_wall.wall_time < work.starts_at
                             OR minute_wall.wall_time >= work.ends_at
                             OR EXISTS (
                                 SELECT 1 FROM pauses b
                                 WHERE b.starts_at <= minute_wall.wall_time
                                   AND minute_wall.wall_time < b.ends_at
                             )
                      )
                ) THEN
                    RAISE EXCEPTION 'Timed interval is not an offered slot'
                        USING ERRCODE = '23514';
                END IF;
            END IF;
            IF NOT (policy->>'allow_overlap')::boolean THEN
                IF EXISTS (
                    SELECT 1 FROM public.appointments a
                    WHERE a.organization_id=org AND a.service_date=day
                      AND a.booking_mode='EXACT_TIME'
                      AND a.status IN ('Booked','Waiting','In Examination','Completed')
                      AND a.starts_at < ends AND a.ends_at > starts
                ) THEN
                    RAISE EXCEPTION 'Timed slot occupied' USING ERRCODE = '23514';
                END IF;
                RETURN;
            ELSE
                SELECT (v.payload->>'capacity')::integer INTO limit_count
                FROM public.clinic_policy_heads h JOIN public.clinic_policy_versions v
                  ON (v.organization_id,v.kind,v.version)=
                     (h.organization_id,h.kind,h.current_version)
                WHERE h.organization_id=org AND h.kind='CAPACITY';
            END IF;
            WITH starts_on_day AS (
                SELECT starts AS instant
                UNION
                SELECT a.starts_at FROM public.appointments a
                WHERE a.organization_id=org AND a.service_date=day
                  AND a.booking_mode='EXACT_TIME'
                  AND a.status IN ('Booked','Waiting','In Examination','Completed')
            ), counted AS (
                SELECT instant, (
                    SELECT count(*) FROM public.appointments a
                    WHERE a.organization_id=org AND a.service_date=day
                      AND a.booking_mode='EXACT_TIME'
                      AND a.status IN ('Booked','Waiting','In Examination','Completed')
                      AND a.starts_at <= instant AND a.ends_at > instant
                ) AS existing FROM starts_on_day
            )
            SELECT COALESCE(MAX(
                existing + CASE WHEN starts <= instant AND ends > instant THEN 1 ELSE 0 END
            ), 0) INTO occupancy FROM counted;
            IF limit_count IS NULL OR occupancy > limit_count THEN
                RAISE EXCEPTION 'Timed slot capacity exceeded' USING ERRCODE = '23514';
            END IF;
        END $$;
        REVOKE ALL ON FUNCTION public.b10_schedule_without_offset_guard(uuid,jsonb)
            FROM PUBLIC, carepulse_runtime;
    """)


def downgrade() -> None:
    raise RuntimeError("B-10 confirmation parity migration is forward-only")

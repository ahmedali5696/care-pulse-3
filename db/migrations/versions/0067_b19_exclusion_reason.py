"""Bind B-19 exclusion explanations to the actual ordered eligibility guards."""

import re

from alembic import op

revision = "0067_b19_exclusion_reason"
down_revision = "0066_b19_bulk_reschedule"
branch_labels = None
depends_on = None


def _replace_once(definition: str, marker: str, replacement: str) -> str:
    if definition.count(marker) != 1:
        raise RuntimeError(f"Unexpected B-19 exclusion guard marker: {marker}")
    return definition.replace(marker, replacement)


def _definition(signature: str) -> str:
    return (
        op.get_bind()
        .exec_driver_sql(f"SELECT pg_get_functiondef('{signature}'::regprocedure)")
        .scalar_one()
    )


def _execute(definition: str) -> None:
    op.get_bind().exec_driver_sql(definition, execution_options={"no_parameters": True})


def _slot_only_guard() -> None:
    # The inherited validator checks elapsed time before offered-slot validity
    # and capacity before the full Cairo fold. The explanation needs the reverse
    # precedence, without changing any predecessor's booking/mutation guard.
    definition = _definition("public.b10_schedule_without_offset_guard(uuid,jsonb)")
    definition = _replace_once(
        definition,
        "FUNCTION public.b10_schedule_without_offset_guard(",
        "FUNCTION public.f01_b19_explanation_slot_base(",
    )
    definition = _replace_once(definition, "SECURITY DEFINER", "SECURITY INVOKER")
    definition = _replace_once(definition, " OR starts <= clock_timestamp()", "")
    marker = "IF NOT (policy->>'allow_overlap')::boolean THEN"
    if definition.count(marker) != 1:
        raise RuntimeError("Unexpected B-19 slot-only capacity marker")
    ending = re.search(r"\bEND\s+\$function\$\s*$", definition)
    if ending is None or ending.start() <= definition.index(marker):
        raise RuntimeError("Unexpected B-19 slot-only function ending")
    _execute(definition[: definition.index(marker)] + definition[ending.start() :])

    for old, new, called, replacement in (
        (
            "b10_schedule_without_exact_guard",
            "f01_b19_explanation_slot_offset",
            "b10_schedule_without_offset_guard",
            "f01_b19_explanation_slot_base",
        ),
        (
            "b10_assert_followup_schedule",
            "f01_b19_explanation_slot",
            "b10_schedule_without_exact_guard",
            "f01_b19_explanation_slot_offset",
        ),
    ):
        definition = _definition(f"public.{old}(uuid,jsonb)")
        definition = _replace_once(
            definition, f"FUNCTION public.{old}(", f"FUNCTION public.{new}("
        )
        definition = _replace_once(definition, "SECURITY DEFINER", "SECURITY INVOKER")
        _execute(
            _replace_once(definition, f"public.{called}(", f"public.{replacement}(")
        )
    op.execute("""
        REVOKE ALL ON FUNCTION public.f01_b19_explanation_slot_base(uuid,jsonb),
            public.f01_b19_explanation_slot_offset(uuid,jsonb),
            public.f01_b19_explanation_slot(uuid,jsonb) FROM PUBLIC,carepulse_api,
            carepulse_web,carepulse_worker,carepulse_gate;
    """)


def _reason_guard() -> None:
    op.execute("""
        CREATE FUNCTION public.f01_b19_explanation_date(
            p_org uuid,p_day date,p_policy jsonb)
        RETURNS boolean LANGUAGE sql VOLATILE SET search_path=pg_catalog,pg_temp AS $$
            WITH intervals AS (
                SELECT i.* FROM public.clinic_schedule_intervals i
                JOIN public.clinic_policy_heads h ON h.organization_id=i.organization_id
                    AND h.kind='SCHEDULE' AND h.current_version=i.policy_version
                WHERE i.organization_id=p_org AND i.policy_kind='SCHEDULE'
            ), calendar AS (
                SELECT EXISTS (SELECT 1 FROM public.clinic_schedule_holidays d
                    JOIN public.clinic_policy_heads h ON h.organization_id=d.organization_id
                        AND h.kind='SCHEDULE' AND h.current_version=d.policy_version
                    WHERE d.organization_id=p_org AND d.service_date=p_day) AS holiday,
                    EXISTS (SELECT 1 FROM intervals i WHERE i.interval_kind='OVERRIDE_WORK'
                        AND i.service_date=p_day) AS overridden,
                    coalesce(p_policy->'open_override_dates' @> to_jsonb(ARRAY[p_day::text]),false) AS open_override,
                    CASE WHEN p_policy ? 'working_weekdays' THEN
                        p_policy->'working_weekdays' @> to_jsonb(ARRAY[extract(isodow FROM p_day)::integer])
                    ELSE EXISTS (SELECT 1 FROM intervals i WHERE i.interval_kind='WORK'
                        AND i.weekday=extract(isodow FROM p_day)) END AS weekly,
                    coalesce(p_policy->'manual_slot_dates' @> to_jsonb(ARRAY[p_day::text]),false)
                        OR EXISTS (SELECT 1 FROM intervals i WHERE i.interval_kind='MANUAL_SLOT'
                            AND i.service_date=p_day) AS dated_manual,
                    CASE WHEN (p_policy->>'duration_minutes')::numeric BETWEEN 1 AND 1439
                        THEN (p_policy->>'duration_minutes')::numeric::integer END AS duration
            ), periods AS (
                SELECT i.starts_at,i.ends_at FROM intervals i,calendar c
                WHERE (c.overridden AND i.interval_kind='OVERRIDE_WORK' AND i.service_date=p_day)
                    OR (NOT c.overridden AND NOT c.holiday AND NOT c.open_override AND c.weekly
                        AND i.interval_kind='WORK' AND i.weekday=extract(isodow FROM p_day))
            ), local_slots AS (
                SELECT i.starts_at,i.ends_at FROM intervals i,calendar c
                WHERE i.interval_kind='MANUAL_SLOT'
                    AND (CASE WHEN c.dated_manual THEN i.service_date=p_day
                        ELSE i.service_date IS NULL AND i.weekday=extract(isodow FROM p_day) END)
                UNION ALL
                SELECT t.instant::time,(t.instant+make_interval(mins=>c.duration))::time
                FROM periods p CROSS JOIN calendar c CROSS JOIN LATERAL generate_series(
                    p_day+p.starts_at,p_day+p.ends_at-make_interval(mins=>c.duration),
                    make_interval(mins=>c.duration)) t(instant)
            )
            SELECT p_day >= (clock_timestamp() AT TIME ZONE 'Africa/Cairo')::date
                AND ((NOT c.holiday AND (c.overridden OR c.open_override OR c.weekly))
                    OR (p_policy->>'holiday_booking_enabled')::boolean)
                AND (p_policy->>'booking_mode'='OPEN' OR (
                    (c.overridden OR (NOT c.holiday AND (c.open_override OR c.weekly)))
                    AND EXISTS (SELECT 1 FROM local_slots s JOIN periods p
                        ON p.starts_at<=s.starts_at AND s.ends_at<=p.ends_at
                        WHERE NOT EXISTS (SELECT 1 FROM intervals b WHERE b.interval_kind='BREAK'
                            AND b.weekday=extract(isodow FROM p_day)
                            AND b.starts_at<s.ends_at AND b.ends_at>s.starts_at))))
            FROM calendar c;
        $$;

        CREATE FUNCTION public.f01_b19_exclusion_reason(
            p_org uuid,p_day date,p_row jsonb,p_policy jsonb,p_capacity integer)
        RETURNS text LANGUAGE plpgsql SET search_path=pg_catalog,pg_temp AS $$
        DECLARE starts timestamptz; ends timestamptz; booking jsonb;
        BEGIN
            IF p_row->>'status' NOT IN ('Booked','Waiting') THEN
                RETURN 'appointment not eligible';
            ELSIF p_row->>'status'='Waiting' AND p_row->'queue_active' IS DISTINCT FROM 'true'::jsonb THEN
                RETURN 'waiting queue evidence unavailable';
            ELSIF p_row->>'classification'<>'ROUTINE' OR p_row->>'lane'<>'MAIN' THEN
                RETURN 'service not eligible';
            ELSIF p_row->'destination_patient_conflict'='true'::jsonb THEN
                RETURN 'patient already booked on destination day';
            ELSIF p_row->>'service_type'='FOLLOW_UP' AND (
                p_row->>'claim_state' IS DISTINCT FROM 'ACTIVE'
                OR p_row->'source_eligible' IS DISTINCT FROM 'true'::jsonb) THEN
                RETURN 'follow-up entitlement unavailable';
            ELSIF p_row->>'service_type'='FOLLOW_UP' AND (
                p_row->>'approved_deadline' IS NULL OR (p_day>(p_row->>'approved_deadline')::date
                    AND p_row->>'extension_event_id' IS NULL)) THEN
                RETURN 'confirmed follow-up extension required';
            ELSIF public.f01_b19_explanation_date(p_org,p_day,p_policy) IS NOT TRUE THEN
                RETURN 'destination schedule unavailable';
            ELSIF p_row->>'booking_mode' IS DISTINCT FROM p_policy->>'booking_mode' THEN
                RETURN 'booking mode changed';
            ELSIF p_row->>'booking_mode'='OPEN' THEN
                RETURN NULL;
            END IF;

            starts:=public.f01_b19_carried(p_day,((p_row->>'starts_at')::timestamptz AT TIME ZONE 'Africa/Cairo')::time);
            ends:=public.f01_b19_carried(p_day,((p_row->>'ends_at')::timestamptz AT TIME ZONE 'Africa/Cairo')::time);
            IF starts IS NULL OR ends IS NULL OR starts>=ends THEN
                RETURN 'destination local time unavailable';
            END IF;
            booking:=jsonb_build_object('service_date',p_day,'booking_mode','EXACT_TIME',
                'starts_at',starts,'ends_at',ends);
            BEGIN
                PERFORM public.f01_b19_explanation_slot(p_org,booking);
            EXCEPTION WHEN check_violation THEN
                RETURN 'destination slot unavailable';
            END;
            IF starts<=clock_timestamp() THEN
                RETURN 'destination time elapsed';
            END IF;
            IF (p_row->>'id')::uuid=ANY(public.f01_b19_joint_conflicts(
                p_org,p_day,jsonb_build_object(p_row->>'id',booking),p_capacity)) THEN
                RETURN 'destination capacity conflict';
            END IF;
            RETURN NULL;
        END $$;
        REVOKE ALL ON FUNCTION public.f01_b19_explanation_date(uuid,date,jsonb),
            public.f01_b19_exclusion_reason(uuid,date,jsonb,jsonb,integer)
            FROM PUBLIC,carepulse_api,carepulse_web,carepulse_worker,carepulse_gate;
    """)


def upgrade() -> None:
    _slot_only_guard()
    _reason_guard()
    definition = _definition("public.f01_b19_assert_snapshot(jsonb)")
    marker = """coalesce(item->>'reason','') NOT IN ('appointment not eligible','waiting queue evidence unavailable','service not eligible',
                        'patient already booked on destination day','follow-up entitlement unavailable',
                        'confirmed follow-up extension required','destination schedule unavailable','booking mode changed',
                        'destination local time unavailable','destination slot unavailable','destination time elapsed',
                        'destination capacity conflict')
                    OR (candidate IS NOT NULL AND item->>'reason'<>'destination capacity conflict')"""
    _execute(
        _replace_once(
            definition,
            marker,
            """(CASE WHEN candidate IS NOT NULL THEN 'destination capacity conflict'
                        ELSE public.f01_b19_exclusion_reason(a.organization_id,destination,row_item,schedule,capacity)
                        END) IS DISTINCT FROM item->>'reason'
                    OR item->>'reason' IS NULL""",
        )
    )


def downgrade() -> None:
    raise RuntimeError("B-19 exclusion reason migration is forward-only")

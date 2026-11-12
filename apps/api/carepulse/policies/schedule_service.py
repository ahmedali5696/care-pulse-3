"""Authorized calendar staging, conflict review and serialized publication."""

import json
import re
from collections import defaultdict
from dataclasses import replace
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal
from uuid import uuid4
from zoneinfo import ZoneInfo

from sqlalchemy import Connection, text

from carepulse.identity.authorization import (
    AuthorizedStaff,
    can_manage_operational_settings,
)
from carepulse.identity.contracts import (
    ContractError,
    normalized_request_digest,
    require_matching_revision,
)
from carepulse.identity.receipts import (
    Receipt,
    TenantReceiptQuery,
    insert_tenant_receipt,
    resolve_tenant_receipt,
)
from carepulse.notifications.evidence import append_sensitive_audit
from carepulse.policies.schedule import (
    DatedSlot,
    LocalSlot,
    NoShowCutoff,
    OverlapCapacityConfirmation,
    ScheduleConfig,
    ScheduleInvalid,
    WeekdaySlot,
    date_rules,
    effective_manual_slots,
    is_working_day,
    validate_schedule,
)
from carepulse.policies.schedule_contracts import (
    ScheduleActivate,
    ScheduleConfigInput,
    SchedulePatch,
)
from carepulse.policies.versions import (
    StalePolicyRevision,
    append_version,
    lock_policy_heads,
    lock_schedule_barrier,
)

CAIRO = ZoneInfo("Africa/Cairo")
COUNTED = ("Booked", "Waiting", "In Examination", "Completed")


def _local_slot(slot) -> LocalSlot:
    return LocalSlot(
        time.fromisoformat(slot.starts_at), time.fromisoformat(slot.ends_at)
    )


def _to_config(body: ScheduleConfigInput, confirmation=None) -> ScheduleConfig:
    approval = (
        OverlapCapacityConfirmation(
            confirmation.capacity, confirmation.capacity_revision
        )
        if confirmation is not None
        else None
    )
    return ScheduleConfig(
        booking_mode=body.booking_mode,
        duration_minutes=int(Decimal(body.duration_minutes))
        if body.duration_minutes is not None
        else None,
        holiday_booking_enabled=body.holiday_booking_enabled,
        allow_overlap=body.allow_overlap,
        no_show_cutoff=NoShowCutoff(
            body.no_show_cutoff.enabled,
            time.fromisoformat(body.no_show_cutoff.local_time)
            if body.no_show_cutoff.local_time
            else None,
        ),
        working_weekdays=frozenset(body.working_weekdays),
        weekly_work_periods=tuple(
            WeekdaySlot(*_slot_pair(slot), slot.weekday)
            for slot in body.weekly_work_periods
        ),
        weekly_breaks=tuple(
            WeekdaySlot(*_slot_pair(slot), slot.weekday) for slot in body.weekly_breaks
        ),
        weekly_manual_slots=tuple(
            WeekdaySlot(*_slot_pair(slot), slot.weekday)
            for slot in body.weekly_manual_slots
        ),
        open_override_dates=frozenset(
            date.fromisoformat(day) for day in body.open_override_dates
        ),
        override_work_periods=tuple(
            DatedSlot(*_slot_pair(slot), date.fromisoformat(slot.service_date))
            for slot in body.override_work_periods
        ),
        manual_slots_by_date={
            date.fromisoformat(day): tuple(_local_slot(slot) for slot in slots)
            for day, slots in body.manual_slots_by_date.items()
        },
        holidays=frozenset(date.fromisoformat(day) for day in body.holidays),
        overlap_capacity_confirmation=approval,
    )


def _slot_pair(slot):
    return time.fromisoformat(slot.starts_at), time.fromisoformat(slot.ends_at)


def _payload_config(config: ScheduleConfig) -> dict:
    def weekly(slots):
        return [
            {
                "weekday": slot.weekday,
                "starts_at": slot.starts_at.strftime("%H:%M"),
                "ends_at": slot.ends_at.strftime("%H:%M"),
            }
            for slot in sorted(slots, key=lambda s: (s.weekday, s.starts_at, s.ends_at))
        ]

    def local(slots):
        return [
            {
                "starts_at": slot.starts_at.strftime("%H:%M"),
                "ends_at": slot.ends_at.strftime("%H:%M"),
            }
            for slot in sorted(slots, key=lambda s: (s.starts_at, s.ends_at))
        ]

    return {
        "booking_mode": config.booking_mode,
        "duration_minutes": format(Decimal(config.duration_minutes), "f")
        if config.duration_minutes is not None
        else None,
        "holiday_booking_enabled": config.holiday_booking_enabled,
        "allow_overlap": config.allow_overlap,
        "no_show_cutoff": {
            "enabled": config.no_show_cutoff.enabled,
            "local_time": config.no_show_cutoff.local_time.strftime("%H:%M")
            if config.no_show_cutoff.local_time
            else None,
        },
        "working_weekdays": sorted(config.working_weekdays),
        "weekly_work_periods": weekly(config.weekly_work_periods),
        "weekly_breaks": weekly(config.weekly_breaks),
        "weekly_manual_slots": weekly(config.weekly_manual_slots),
        "open_override_dates": sorted(
            day.isoformat() for day in config.open_override_dates
        ),
        "override_work_periods": [
            {
                "service_date": slot.service_date.isoformat(),
                "starts_at": slot.starts_at.strftime("%H:%M"),
                "ends_at": slot.ends_at.strftime("%H:%M"),
            }
            for slot in sorted(
                config.override_work_periods,
                key=lambda s: (s.service_date, s.starts_at, s.ends_at),
            )
        ],
        "manual_slots_by_date": {
            day.isoformat(): local(slots)
            for day, slots in sorted(config.manual_slots_by_date.items())
        },
        "holidays": sorted(day.isoformat() for day in config.holidays),
    }


def _schedule_head(connection: Connection, tenant):
    row = (
        connection.execute(
            text("""
            SELECT h.revision, h.current_version, v.payload::text AS payload
            FROM public.clinic_policy_heads AS h
            JOIN public.clinic_policy_versions AS v
              ON (v.organization_id,v.kind,v.version) =
                 (h.organization_id,h.kind,h.current_version)
            WHERE h.organization_id=:tenant AND h.kind='SCHEDULE'
        """),
            {"tenant": tenant},
        )
        .mappings()
        .one_or_none()
    )
    if row is None:
        raise ContractError(409)
    return {
        **row,
        "payload": json.loads(
            row["payload"],
            parse_float=Decimal,
            parse_int=lambda digits: int(Decimal(digits)),
        ),
    }


def _capacity_state(connection: Connection, tenant) -> dict:
    row = (
        connection.execute(
            text("""
            SELECT h.revision, ((v.payload->>'capacity')::numeric)::integer AS capacity
            FROM public.clinic_policy_heads h
            JOIN public.clinic_policy_versions v
              ON (h.organization_id,h.kind,h.current_version) =
                 (v.organization_id,v.kind,v.version)
            WHERE h.organization_id=:tenant AND h.kind='CAPACITY'
        """),
            {"tenant": tenant},
        )
        .mappings()
        .one_or_none()
    )
    if row is None:
        raise ContractError(409)
    return dict(row)


def load_current_schedule(connection: Connection, tenant) -> ScheduleConfig:
    """Reconstruct the active calendar from immutable version children."""
    head = _schedule_head(connection, tenant)
    intervals = (
        connection.execute(
            text("""
            SELECT interval_kind, weekday, service_date, starts_at, ends_at
            FROM public.clinic_schedule_intervals
            WHERE organization_id=:tenant AND policy_kind='SCHEDULE'
              AND policy_version=:version
        """),
            {"tenant": tenant, "version": head["current_version"]},
        )
        .mappings()
        .all()
    )
    holidays = connection.scalars(
        text("""
            SELECT service_date FROM public.clinic_schedule_holidays
            WHERE organization_id=:tenant AND policy_version=:version
        """),
        {"tenant": tenant, "version": head["current_version"]},
    ).all()
    payload = head["payload"]
    duration = payload["duration_minutes"]
    if duration is not None:
        if (
            type(duration) not in (int, Decimal)
            or (isinstance(duration, Decimal) and not duration.is_finite())
            or duration <= 0
            or (
                isinstance(duration, Decimal)
                and duration != duration.to_integral_value()
            )
        ):
            raise ContractError(409)
        duration = int(duration)
    fields = {
        "booking_mode": payload["booking_mode"],
        "duration_minutes": format(Decimal(duration), "f")
        if duration is not None
        else None,
        "holiday_booking_enabled": payload["holiday_booking_enabled"],
        "allow_overlap": payload["allow_overlap"],
        "no_show_cutoff": payload["no_show_cutoff"],
        "working_weekdays": payload.get(
            "working_weekdays",
            sorted(
                {row["weekday"] for row in intervals if row["interval_kind"] == "WORK"}
            ),
        ),
        "weekly_work_periods": [],
        "weekly_breaks": [],
        "weekly_manual_slots": [],
        "open_override_dates": payload.get("open_override_dates", []),
        "override_work_periods": [],
        "manual_slots_by_date": {
            day: [] for day in payload.get("manual_slot_dates", [])
        },
        "holidays": [day.isoformat() for day in holidays],
    }
    kinds = {
        "WORK": "weekly_work_periods",
        "BREAK": "weekly_breaks",
        "OVERRIDE_WORK": "override_work_periods",
    }
    for row in intervals:
        if any(
            row[column].second or row[column].microsecond
            for column in ("starts_at", "ends_at")
        ):
            raise ContractError(409)
        entry = {
            "starts_at": row["starts_at"].strftime("%H:%M"),
            "ends_at": row["ends_at"].strftime("%H:%M"),
        }
        if row["interval_kind"] == "MANUAL_SLOT":
            if row["service_date"] is None:
                fields["weekly_manual_slots"].append(
                    {**entry, "weekday": row["weekday"]}
                )
            else:
                fields["manual_slots_by_date"].setdefault(
                    row["service_date"].isoformat(), []
                ).append(entry)
        else:
            field = kinds[row["interval_kind"]]
            index = "weekday" if row["service_date"] is None else "service_date"
            fields[field].append(
                {
                    **entry,
                    index: row["weekday"]
                    if index == "weekday"
                    else row["service_date"].isoformat(),
                }
            )
    return _to_config(ScheduleConfigInput.model_validate(fields))


def read_schedule(connection: Connection, actor: AuthorizedStaff) -> dict:
    _require_schedule_manager(actor)
    try:
        lock_schedule_barrier(connection, actor.organization_id, shared=True)
        lock_policy_heads(connection, actor.organization_id, {"CAPACITY": "SHARE"})
    except StalePolicyRevision as invalid:
        raise ContractError(409, "STALE_REVISION") from invalid
    head = _schedule_head(connection, actor.organization_id)
    capacity = _capacity_state(connection, actor.organization_id)
    return {
        "config": _payload_config(
            load_current_schedule(connection, actor.organization_id)
        ),
        "routine_walk_in_enabled": head["payload"]["routine_walk_in_enabled"],
        "schedule_revision": head["revision"],
        "capacity": capacity["capacity"],
        "capacity_revision": capacity["revision"],
    }


def _require_schedule_manager(actor: AuthorizedStaff) -> None:
    if not can_manage_operational_settings(actor):
        raise ContractError(403)


def _schedule_rows(config: ScheduleConfig):
    rows = []
    for kind, slots in (
        ("WORK", config.weekly_work_periods),
        ("BREAK", config.weekly_breaks),
        ("MANUAL_SLOT", config.weekly_manual_slots),
        ("OVERRIDE_WORK", config.override_work_periods),
    ):
        for slot in slots:
            rows.append(
                {
                    "interval_kind": kind,
                    "weekday": getattr(slot, "weekday", None),
                    "service_date": getattr(slot, "service_date", None),
                    "starts_at": slot.starts_at,
                    "ends_at": slot.ends_at,
                }
            )
    for day, slots in config.manual_slots_by_date.items():
        for slot in slots:
            rows.append(
                {
                    "interval_kind": "MANUAL_SLOT",
                    "weekday": None,
                    "service_date": day,
                    "starts_at": slot.starts_at,
                    "ends_at": slot.ends_at,
                }
            )
    return rows


def _version_payload(config: ScheduleConfig, routine_walk_in: bool) -> str:
    payload = {
        "booking_mode": config.booking_mode,
        "duration_minutes": _payload_config(config)["duration_minutes"],
        "holiday_booking_enabled": config.holiday_booking_enabled,
        "allow_overlap": config.allow_overlap,
        "routine_walk_in_enabled": routine_walk_in,
        "no_show_cutoff": _payload_config(config)["no_show_cutoff"],
        "working_weekdays": sorted(config.working_weekdays),
        "open_override_dates": sorted(
            day.isoformat() for day in config.open_override_dates
        ),
        "manual_slot_dates": sorted(
            day.isoformat() for day in config.manual_slots_by_date
        ),
    }
    # JSONB stores duration as a number; Python's int JSON serializer rejects >4300 digits.
    encoded = json.dumps(payload, allow_nan=False)
    duration = payload["duration_minutes"]
    if duration is not None:
        encoded = encoded.replace(
            f'"duration_minutes": "{duration}"',
            f'"duration_minutes": {duration}',
            1,
        )
    return encoded


def _day_signature(config: ScheduleConfig, day: date):
    rules = date_rules(config, day)
    return (
        is_working_day(config, day),
        day in config.holidays,
        config.holiday_booking_enabled,
        rules.eligible,
        rules.periods,
        rules.breaks,
        effective_manual_slots(config, day),
        config.duration_minutes,
        config.booking_mode,
        config.allow_overlap,
        config.no_show_cutoff,
    )


def _timed_effects(appointment, config: ScheduleConfig) -> list[str]:
    if appointment["starts_at"] is None:
        return []
    rules = date_rules(config, appointment["service_date"])
    cursor = appointment["starts_at"].astimezone(UTC)
    end = appointment["ends_at"].astimezone(UTC)
    within_period = [True] * len(rules.periods)
    in_break = False
    # Walk real UTC minutes: Cairo's repeated fall hour must be checked twice.
    while cursor < end:
        local = cursor.astimezone(CAIRO)
        wall = local.time().replace(tzinfo=None)
        same_day = local.date() == appointment["service_date"]
        if not same_day:
            within_period = [False] * len(rules.periods)
            break
        for index, period in enumerate(rules.periods):
            within_period[index] &= period.starts_at <= wall < period.ends_at
        in_break |= any(
            pause.starts_at <= wall < pause.ends_at for pause in rules.breaks
        )
        if not any(within_period) and in_break:
            break
        cursor = cursor.replace(second=0, microsecond=0) + timedelta(minutes=1)
    effects = []
    if not any(within_period):
        effects.append("OUTSIDE_PERIOD")
    if in_break:
        effects.append("IN_BREAK")
    return effects


def _contributing_peak(
    appointments: list[dict], effective_limit: int
) -> tuple[int, set[str]]:
    events = []
    for appointment in appointments:
        if appointment["starts_at"] is None:
            continue
        identifier = str(appointment["id"])
        events.append((appointment["starts_at"], 1, identifier))
        events.append((appointment["ends_at"], 0, identifier))
    events.sort()
    active: set[str] = set()
    highest, contributing = 0, set()
    for _, event_kind, identifier in events:
        if event_kind == 0:
            active.remove(identifier)
        else:
            active.add(identifier)
            highest = max(highest, len(active))
            if len(active) > effective_limit:
                contributing.update(active)
    return highest, contributing


def compute_affected(
    connection: Connection,
    tenant,
    current_config: ScheduleConfig,
    candidate_config: ScheduleConfig,
    saved_capacity: int,
    today: date,
) -> tuple[list[dict], dict[str, int]]:
    """Review future confirmed appointments, including dates with no Booked row."""
    appointments = [
        dict(row)
        for row in connection.execute(
            text("""
                SELECT id, service_date, status, booking_mode,
                       starts_at, ends_at, revision
                FROM public.appointments
                WHERE organization_id = :tenant AND service_date >= :today
                  AND status IN ('Booked','Waiting','In Examination','Completed')
                ORDER BY service_date, id
            """),
            {"tenant": tenant, "today": today},
        ).mappings()
    ]
    by_date = defaultdict(list)
    for appointment in appointments:
        by_date[appointment["service_date"]].append(appointment)
    affected: dict[str, dict] = {}
    revisions: dict[str, int] = {}

    def record(appointment, code: str | None, blocking: bool):
        identifier = str(appointment["id"])
        if identifier not in affected:
            affected[identifier] = {
                "appointment_id": identifier,
                "service_date": appointment["service_date"].isoformat(),
                "effects": [],
                "blocking": False,
            }
        if code is not None and code not in affected[identifier]["effects"]:
            affected[identifier]["effects"].append(code)
        affected[identifier]["blocking"] |= blocking
        revisions[identifier] = appointment["revision"]

    limit = saved_capacity if candidate_config.allow_overlap else 1
    for day, records in by_date.items():
        changed = _day_signature(current_config, day) != _day_signature(
            candidate_config, day
        )
        candidate_rules = date_rules(candidate_config, day)
        # A preserved Open booking needs only the holiday date toggle; new timed slots need hours.
        date_eligible = (
            is_working_day(candidate_config, day)
            or candidate_config.holiday_booking_enabled
        )
        if changed:
            for appointment in records:
                if appointment["status"] not in ("Booked", "Waiting"):
                    continue
                # Keep the changed date's confirmed row in the revision-bound review
                # even when the proposed edit does not make this booking invalid.
                record(appointment, None, False)
                if not date_eligible:
                    record(appointment, "DATE_CLOSED", True)
                else:
                    for effect in _timed_effects(appointment, candidate_config):
                        record(appointment, effect, True)
                if current_config.booking_mode != candidate_config.booking_mode:
                    record(appointment, "MODE_CHANGED", False)
                prior_rules = date_rules(current_config, day)
                slot_changed = (
                    prior_rules.periods != candidate_rules.periods
                    or prior_rules.breaks != candidate_rules.breaks
                    or effective_manual_slots(current_config, day)
                    != effective_manual_slots(candidate_config, day)
                    or current_config.duration_minutes
                    != candidate_config.duration_minutes
                )
                if slot_changed and appointment["starts_at"] is not None:
                    record(appointment, "SLOTS_CHANGED", False)
                if (
                    appointment["status"] == "Booked"
                    and current_config.no_show_cutoff != candidate_config.no_show_cutoff
                    and candidate_config.no_show_cutoff.enabled
                ):
                    record(appointment, "CUTOFF_EXPOSURE", False)
        occupancy, contributors = _contributing_peak(records, limit)
        if occupancy > limit:
            for appointment in records:
                if str(appointment["id"]) in contributors:
                    record(appointment, "OVERLAP_LIMIT_EXCEEDED", True)
    return [
        {**effect, "effects": sorted(effect["effects"])}
        for _, effect in sorted(affected.items())
    ], dict(sorted(revisions.items()))


def _receipt_query(actor, command, key: str, operation: str) -> TenantReceiptQuery:
    if not re.fullmatch(r"[A-Za-z0-9._:-]{1,128}", key):
        raise ContractError(422)
    return TenantReceiptQuery(actor, operation, key, normalized_request_digest(command))


def _store_receipt(
    connection, query: TenantReceiptQuery, projection: dict, revision: int
):
    insert_tenant_receipt(
        connection,
        Receipt(
            actor_id=query.actor.staff_id,
            operation=query.operation,
            idempotency_key=query.idempotency_key,
            request_digest=query.request_digest,
            result_projection=projection,
            result_revision=revision,
            organization_id=query.actor.organization_id,
            staff_id=query.actor.staff_id,
        ),
    )


def stage_or_preview(
    connection: Connection,
    actor: AuthorizedStaff,
    command: SchedulePatch,
    idempotency_key: str,
    operation: str,
) -> dict:
    _require_schedule_manager(actor)
    if operation not in ("settings.schedule.stage", "settings.schedule.preview"):
        raise ValueError("Unsupported calendar operation")
    query = _receipt_query(actor, command, idempotency_key, operation)
    stored = resolve_tenant_receipt(connection, query)
    if stored is not None:
        return stored[0]
    tenant = actor.organization_id
    try:
        lock_schedule_barrier(connection, tenant, shared=True)
        lock_policy_heads(connection, tenant, {"CAPACITY": "SHARE"})
    except StalePolicyRevision as invalid:
        raise ContractError(409, "STALE_REVISION") from invalid
    stored = resolve_tenant_receipt(connection, query)
    if stored is not None:
        return stored[0]
    head = _schedule_head(connection, tenant)
    require_matching_revision(command.expected_schedule_revision, head["revision"])
    capacity = _capacity_state(connection, tenant)
    proposed = _to_config(command.config, command.overlap_capacity_confirmation)
    if proposed.allow_overlap:
        approval = command.overlap_capacity_confirmation
        if approval is None:
            raise ContractError(422)
        require_matching_revision(approval.capacity_revision, capacity["revision"])
        if approval.capacity != capacity["capacity"]:
            raise ContractError(409, "STALE_REVISION")
    try:
        validate_schedule(proposed, capacity["capacity"])
    except ScheduleInvalid as invalid:
        raise ContractError(422) from invalid
    current = load_current_schedule(connection, tenant)
    today = datetime.now(CAIRO).date()
    effects, records = compute_affected(
        connection, tenant, current, proposed, capacity["capacity"], today
    )
    preview_id = uuid4()
    candidate = {
        "config": command.config.model_dump(mode="json"),
        "overlap_capacity_confirmation": command.overlap_capacity_confirmation.model_dump(
            mode="json"
        )
        if command.overlap_capacity_confirmation
        else None,
        "capacity_revision": capacity["revision"],
    }
    connection.execute(
        text("""
            INSERT INTO public.clinic_policy_previews
                (id, organization_id, policy_kind, policy_version, policy_revision,
                 service_day_revision, actor_staff_id, actor_security_revision,
                 selected_refs, affected_refs, record_revisions, candidate_payload)
            VALUES (:id, :tenant, 'SCHEDULE', :version, :revision, 0, :actor, :security_revision,
                    CAST(:selected AS jsonb), CAST(:affected AS jsonb),
                    CAST(:records AS jsonb), CAST(:candidate AS jsonb))
        """),
        {
            "id": preview_id,
            "tenant": tenant,
            "version": head["current_version"],
            "revision": head["revision"],
            "actor": actor.staff_id,
            "security_revision": actor.security_revision,
            "selected": "[]",
            "affected": json.dumps(effects),
            "records": json.dumps(records),
            "candidate": json.dumps(candidate),
        },
    )
    projection = {
        "preview_id": str(preview_id),
        "affected": effects,
        "excluded": [],
        "revisions": {
            "schedule": head["revision"],
            "capacity": capacity["revision"],
            "active_schedule": head["revision"],
            "proposed_schedule": head["revision"] + 1,
        },
    }
    _store_receipt(connection, query, projection, head["revision"])
    return projection


def activate(
    connection: Connection,
    actor: AuthorizedStaff,
    command: ScheduleActivate,
    idempotency_key: str,
) -> dict:
    _require_schedule_manager(actor)
    query = _receipt_query(
        actor, command, idempotency_key, "settings.schedule.activate"
    )
    stored = resolve_tenant_receipt(connection, query)
    if stored is not None:
        return stored[0]
    tenant = actor.organization_id
    try:
        lock_schedule_barrier(connection, tenant, shared=False)
        lock_policy_heads(connection, tenant, {"CAPACITY": "SHARE"})
    except StalePolicyRevision as invalid:
        raise ContractError(409, "STALE_REVISION") from invalid
    stored = resolve_tenant_receipt(connection, query)
    if stored is not None:
        return stored[0]
    preview = (
        connection.execute(
            text("""
            SELECT policy_version, policy_revision, actor_staff_id, actor_security_revision,
                   affected_refs,
                   record_revisions, candidate_payload
            FROM public.clinic_policy_previews
            WHERE organization_id=:tenant AND policy_kind='SCHEDULE' AND id=:id
        """),
            {"tenant": tenant, "id": command.preview_id},
        )
        .mappings()
        .one_or_none()
    )
    if preview is None or preview["actor_staff_id"] != actor.staff_id:
        raise ContractError(404)
    if preview["actor_security_revision"] is None:
        raise ContractError(409, "STALE_REVISION")
    require_matching_revision(
        preview["actor_security_revision"], actor.security_revision
    )
    head = _schedule_head(connection, tenant)
    require_matching_revision(command.expected_schedule_revision, head["revision"])
    require_matching_revision(preview["policy_revision"], head["revision"])
    if preview["policy_version"] != head["current_version"]:
        raise ContractError(409, "STALE_REVISION")
    capacity = _capacity_state(connection, tenant)
    candidate = preview["candidate_payload"]
    require_matching_revision(candidate["capacity_revision"], capacity["revision"])
    if command.expected_capacity_revision is not None:
        require_matching_revision(
            command.expected_capacity_revision, capacity["revision"]
        )
    parsed = ScheduleConfigInput.model_validate(candidate["config"])
    approval = candidate["overlap_capacity_confirmation"]
    confirmation = None
    if approval is not None:
        confirmation = OverlapCapacityConfirmation(
            approval["capacity"], approval["capacity_revision"]
        )
    proposed = _to_config(parsed)
    proposed = replace(proposed, overlap_capacity_confirmation=confirmation)
    try:
        validate_schedule(proposed, capacity["capacity"])
    except ScheduleInvalid as invalid:
        raise ContractError(422) from invalid
    current = load_current_schedule(connection, tenant)
    effects, records = compute_affected(
        connection,
        tenant,
        current,
        proposed,
        capacity["capacity"],
        datetime.now(CAIRO).date(),
    )
    if effects != preview["affected_refs"] or records != preview["record_revisions"]:
        raise ContractError(409, "STALE_REVISION")
    if any(entry["blocking"] for entry in effects):
        raise ContractError(409)
    if _payload_config(current) == _payload_config(proposed):
        raise ContractError(422)
    try:
        append_version(
            connection,
            tenant,
            "SCHEDULE",
            _version_payload(proposed, head["payload"]["routine_walk_in_enabled"]),
            head["revision"],
            actor_staff_id=str(actor.staff_id),
            intervals=_schedule_rows(proposed),
            holidays=sorted(proposed.holidays),
        )
    except StalePolicyRevision as invalid:
        raise ContractError(409, "STALE_REVISION") from invalid
    new_revision = head["revision"] + 1
    append_sensitive_audit(
        connection,
        {
            "organization_id": tenant,
            "actor_staff_id": actor.staff_id,
            "actor_role": actor.role,
            "action": "settings.schedule.activate",
            "reason": command.reason,
            "before_state": {"schedule_revision": head["revision"]},
            "after_state": {
                "schedule_revision": new_revision,
                "preview_id": str(command.preview_id),
            },
            "correlation_id": str(uuid4()),
        },
    )
    projection = read_schedule(connection, actor)
    _store_receipt(connection, query, projection, new_revision)
    return projection

"""Immutable, signed previews for same-original-day Routine bulk rescheduling."""

import json
from collections import defaultdict
from datetime import UTC, date, datetime
from typing import Literal
from uuid import UUID
from zoneinfo import ZoneInfo

from pydantic import BaseModel, ConfigDict, StrictStr, field_validator
from sqlalchemy import Connection

from carepulse.appointments.booking_contracts import CanonicalServiceDate
from carepulse.identity.authorization import AuthorizedStaff
from carepulse.identity.contracts import ContractError
from carepulse.identity.runtime_identity import current_request
from carepulse.policies.intervals import Interval
from carepulse.policies.schedule import date_rules
from carepulse.policies.schedule_service import (
    _capacity_state,
    _schedule_head,
    load_current_schedule,
)
from carepulse.policies.slots import candidates_for_date, resolve_cairo_local_time
from carepulse.queue.admission_control import _call, _signed_identity
from carepulse.visits.eligibility_contracts import _reason

CAIRO = ZoneInfo("Africa/Cairo")
_PREVIEW_PURPOSE = "APPOINTMENT_BULK_PREVIEW"
_REVISION_KEYS = (
    "revision",
    "service_type",
    "classification",
    "lane",
    "status",
    "patient_id",
    "doctor_staff_id",
    "booking_mode",
    "starts_at",
    "ends_at",
    "patient_revision",
    "queue_day_revision",
    "claim_revision",
    "source_decision_revision",
    "visit_revision",
    "followup_revision",
    "authorization_revision",
)


class BulkPreviewCommand(BaseModel):
    model_config = ConfigDict(extra="forbid")

    original_service_date: CanonicalServiceDate
    destination_date: CanonicalServiceDate
    appointment_ids: list[UUID]
    reason: StrictStr

    @field_validator("appointment_ids")
    @classmethod
    def unique_nonempty_selection(cls, ids: list[UUID]) -> list[UUID]:
        if not ids or len(ids) != len(set(ids)):
            raise ValueError("Bulk selection must contain distinct Appointment IDs")
        return ids

    @field_validator("reason")
    @classmethod
    def valid_reason(cls, reason: str) -> str:
        return _reason(reason)


class BulkCommitCommand(BaseModel):
    model_config = ConfigDict(extra="forbid")

    preview_id: UUID
    confirmation: Literal["RESCHEDULE"]


def joint_capacity_conflicts(
    existing: list[Interval], candidates: dict[str, Interval], capacity: int
) -> set[str]:
    """Mark every candidate present in any oversubscribed half-open segment."""
    if capacity < 1:
        raise ValueError("Capacity must be positive")
    existing_events: dict[datetime, int] = defaultdict(int)
    candidate_starts: dict[datetime, set[str]] = defaultdict(set)
    candidate_ends: dict[datetime, set[str]] = defaultdict(set)
    for interval in existing:
        existing_events[interval.starts_at] += 1
        existing_events[interval.ends_at] -= 1
    for identifier, interval in candidates.items():
        candidate_starts[interval.starts_at].add(identifier)
        candidate_ends[interval.ends_at].add(identifier)
    endpoints = sorted(
        existing_events.keys() | candidate_starts.keys() | candidate_ends.keys()
    )
    active: set[str] = set()
    conflicted: set[str] = set()
    existing_load = 0
    for index, instant in enumerate(endpoints[:-1]):
        existing_load += existing_events[instant]
        active.difference_update(candidate_ends[instant])
        active.update(candidate_starts[instant])
        if existing_load + len(active) > capacity:
            conflicted.update(active)
    return conflicted


def _source_clocks(row: dict) -> dict:
    if row["booking_mode"] != "EXACT_TIME":
        return {"source_local_start": None, "source_local_end": None}
    return {
        "source_local_start": datetime.fromisoformat(row["starts_at"])
        .astimezone(CAIRO)
        .time()
        .isoformat(),
        "source_local_end": datetime.fromisoformat(row["ends_at"])
        .astimezone(CAIRO)
        .time()
        .isoformat(),
    }


def _destination_interval(row: dict, destination: date) -> Interval:
    start = datetime.fromisoformat(row["starts_at"]).astimezone(CAIRO).time()
    end = datetime.fromisoformat(row["ends_at"]).astimezone(CAIRO).time()
    return Interval(
        resolve_cairo_local_time(destination, start),
        resolve_cairo_local_time(destination, end),
    )


def _candidate_exclusion(
    row: dict, context: dict
) -> tuple[str | None, Interval | None]:
    if row["status"] not in ("Booked", "Waiting"):
        return "appointment not eligible", None
    if row["status"] == "Waiting" and not row.get("queue_active"):
        return "waiting queue evidence unavailable", None
    if row["classification"] != "ROUTINE" or row["lane"] != "MAIN":
        return "service not eligible", None
    if row.get("destination_patient_conflict"):
        return "patient already booked on destination day", None
    if row["service_type"] == "FOLLOW_UP":
        if row.get("claim_state") != "ACTIVE" or not row.get("source_eligible"):
            return "follow-up entitlement unavailable", None
        frozen_deadline = row.get("approved_deadline")
        if (
            frozen_deadline is None
            or context["destination"] > date.fromisoformat(frozen_deadline)
            and not row.get("extension_event_id")
        ):
            return "confirmed follow-up extension required", None
    if not context["destination_eligible"]:
        return "destination schedule unavailable", None
    if row["booking_mode"] != context["booking_mode"]:
        return "booking mode changed", None
    if row["booking_mode"] == "OPEN":
        return None, None
    try:
        interval = _destination_interval(row, context["destination"])
    except (ValueError, OverflowError, TypeError):
        return "destination local time unavailable", None
    if (interval.starts_at, interval.ends_at) not in context["slots"]:
        return "destination slot unavailable", None
    if interval.starts_at <= context["now"]:
        return "destination time elapsed", None
    if joint_capacity_conflicts(
        context["existing"], {row["id"]: interval}, context["capacity"]
    ):
        return "destination capacity conflict", None
    return None, interval


def _bound_reschedule_command(
    row: dict, interval: Interval | None, context: dict
) -> dict:
    command = {
        "service_date": context["destination"].isoformat(),
        "reason": context["reason"],
        "expected_appointment_revision": row["revision"],
        "expected_schedule_revision": context["schedule_revision"],
    }
    if row["status"] == "Waiting":
        command["expected_queue_day_revision"] = row["queue_day_revision"]
    if interval is not None:
        command["starts_at"] = interval.starts_at.isoformat()
        command["ends_at"] = interval.ends_at.isoformat()
        if context["allow_overlap"]:
            command["expected_capacity_revision"] = context["capacity_revision"]
    if row["service_type"] == "FOLLOW_UP":
        command.update(
            expected_claim_revision=row["claim_revision"],
            expected_source_decision_revision=row["source_decision_revision"],
            expected_visit_revision=row["visit_revision"],
            expected_followup_revision=row["followup_revision"],
            expected_authorization_revision=row["authorization_revision"],
        )
        if row.get("extension_event_id") and context[
            "destination"
        ] > date.fromisoformat(row["approved_deadline"]):
            command["extension_event_id"] = row["extension_event_id"]
    return command


def _operational_row(row: dict, interval: Interval | None) -> dict:
    return {
        "appointment_id": row["id"],
        "status": row["status"],
        "appointment_revision": row["revision"],
        **_source_clocks(row),
        "starts_at": interval.starts_at.isoformat() if interval else None,
        "ends_at": interval.ends_at.isoformat() if interval else None,
    }


def _existing_intervals(
    connection: Connection, tenant: UUID, destination: date
) -> list[Interval]:
    registered = current_request.get()
    if registered is None or registered.identity.purpose not in (
        _PREVIEW_PURPOSE,
        "APPOINTMENT_BULK_COMMIT",
    ):
        raise ContractError(403)
    operation = (
        "preview" if registered.identity.purpose == _PREVIEW_PURPOSE else "commit"
    )
    from sqlalchemy import text

    return [
        Interval(row.starts_at.astimezone(UTC), row.ends_at.astimezone(UTC))
        for row in connection.execute(
            text(
                f"SELECT starts_at, ends_at FROM public.f01_b19_{operation}_intervals(:tenant,:day)"
            ),
            {"tenant": tenant, "day": destination},
        )
    ]


def evaluate_cohort(
    connection: Connection, actor: AuthorizedStaff, source: dict
) -> dict:
    """Derive partitions without reserving numbers or claiming destination capacity."""
    destination = date.fromisoformat(source["destination_date"])
    original = date.fromisoformat(source["original_service_date"])
    if destination == original:
        raise ContractError(409)
    rows = _call(connection, "SELECT public.f01_b19_rows()")
    if not isinstance(rows, list) or {row["id"] for row in rows} != set(
        source["appointment_ids"]
    ):
        raise ContractError(404)
    if any(row["service_date"] != original.isoformat() for row in rows):
        raise ContractError(409)
    schedule = load_current_schedule(connection, actor.organization_id)
    now = datetime.now(UTC)
    capacity = _capacity_state(connection, actor.organization_id)
    context = {
        "destination": destination,
        "reason": source["reason"],
        "booking_mode": schedule.booking_mode,
        "allow_overlap": schedule.allow_overlap,
        "destination_eligible": destination >= now.astimezone(CAIRO).date()
        and date_rules(schedule, destination).eligible,
        "slots": {
            (slot.starts_at, slot.ends_at)
            for slot in candidates_for_date(schedule, destination)
        },
        "capacity": capacity["capacity"] if schedule.allow_overlap else 1,
        "capacity_revision": capacity["revision"],
        "schedule_revision": _schedule_head(connection, actor.organization_id)[
            "revision"
        ],
        "existing": _existing_intervals(connection, actor.organization_id, destination),
        "now": now,
    }
    rows_by_id = {row["id"]: row for row in rows}
    tentative: dict[str, Interval] = {}
    exclusions: dict[str, str] = {}
    for identifier in source["appointment_ids"]:
        row = rows_by_id[identifier]
        denial, interval = _candidate_exclusion(row, context)
        if denial:
            exclusions[identifier] = denial
        elif interval is not None:
            tentative[identifier] = interval
    for identifier in joint_capacity_conflicts(
        context["existing"], tentative, context["capacity"]
    ):
        exclusions[identifier] = "destination capacity conflict"
    included, excluded = [], []
    for identifier in source["appointment_ids"]:
        row = rows_by_id[identifier]
        evidence = _operational_row(row, tentative.get(identifier))
        if identifier in exclusions:
            excluded.append({**evidence, "reason": exclusions[identifier]})
        else:
            included.append(
                {
                    **evidence,
                    "command": _bound_reschedule_command(
                        row, tentative.get(identifier), context
                    ),
                }
            )
    return {
        "original_service_date": original.isoformat(),
        "destination_date": destination.isoformat(),
        "appointment_ids": source["appointment_ids"],
        "reason": source["reason"],
        "eligible_count": len(included),
        "excluded_count": len(excluded),
        "included": included,
        "excluded": excluded,
        "security_revision": actor.security_revision,
        "schedule_revision": context["schedule_revision"],
        "capacity_revision": context["capacity_revision"],
        "row_evidence": [
            {key: rows_by_id[identifier].get(key) for key in ("id", *_REVISION_KEYS)}
            for identifier in source["appointment_ids"]
        ],
    }


def preview_bulk_reschedule(
    connection: Connection,
    actor: AuthorizedStaff,
    command: BulkPreviewCommand,
    key: str,
) -> tuple[dict, int]:
    _signed_identity(_PREVIEW_PURPOSE, key)
    if command.original_service_date == command.destination_date:
        raise ContractError(409)
    replay = _call(connection, "SELECT public.f01_b19_preview_replay()")
    if replay is not None:
        return replay, int(replay["revision"])
    selection = {
        "appointment_ids": [str(identifier) for identifier in command.appointment_ids],
        "original_service_date": command.original_service_date.isoformat(),
        "destination_date": command.destination_date.isoformat(),
        "reason": command.reason,
    }
    evaluation = evaluate_cohort(connection, actor, selection)
    preview = _call(
        connection,
        "SELECT public.f01_b19_store_preview(CAST(:snapshot AS jsonb))",
        {"snapshot": json.dumps(evaluation, allow_nan=False)},
    )
    if not isinstance(preview, dict):
        raise ContractError(503)
    return preview, int(preview["revision"])


def recover_bulk_preview(
    connection: Connection,
    actor: AuthorizedStaff,
    command: BulkPreviewCommand,
    key: str,
) -> tuple[dict, int]:
    _signed_identity(_PREVIEW_PURPOSE, key)
    preview = _call(connection, "SELECT public.f01_b19_preview_replay()")
    if not isinstance(preview, dict):
        raise ContractError(409, "IDEMPOTENCY_CONFLICT")
    return preview, int(preview["revision"])

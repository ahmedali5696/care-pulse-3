"""Tenant-scoped advisory slots and locked exact-time booking validation."""

from datetime import UTC, date, datetime
from zoneinfo import ZoneInfo

from sqlalchemy import Connection, text

from carepulse.identity.authorization import AuthorizedStaff
from carepulse.identity.contracts import ContractError, require_matching_revision
from carepulse.policies.intervals import Interval, intersects, peak_occupancy
from carepulse.policies.schedule import date_rules
from carepulse.policies.schedule_service import (
    _capacity_state,
    _schedule_head,
    load_current_schedule,
)
from carepulse.policies.slots import Candidate, candidates_for_date
from carepulse.policies.versions import (
    StalePolicyRevision,
    lock_policy_heads,
    lock_schedule_barrier,
)

CAIRO = ZoneInfo("Africa/Cairo")


def _counted_intervals(
    connection: Connection, tenant, service_date: date
) -> list[Interval]:
    return [
        Interval(row.starts_at.astimezone(UTC), row.ends_at.astimezone(UTC))
        for row in connection.execute(
            text("""
                SELECT starts_at, ends_at FROM public.appointments
                WHERE organization_id = :tenant AND service_date = :service_date
                  AND booking_mode = 'EXACT_TIME'
                  AND status IN ('Booked','Waiting','In Examination','Completed')
            """),
            {"tenant": tenant, "service_date": service_date},
        )
    ]


def _available(
    existing: list[Interval], candidate: Candidate, allow_overlap: bool, capacity: int
) -> bool:
    if not allow_overlap:
        return not any(intersects(candidate, booked) for booked in existing)
    return peak_occupancy([*existing, candidate]) <= capacity


def list_slots(
    connection: Connection, actor: AuthorizedStaff, service_date: date
) -> dict:
    """Read-only availability; the next booking must rescan after day serialization."""
    if actor.role not in ("DOCTOR_OWNER", "RECEPTIONIST"):
        raise ContractError(403)
    tenant = actor.organization_id
    try:
        lock_schedule_barrier(connection, tenant, shared=True)
    except StalePolicyRevision as invalid:
        raise ContractError(409, "STALE_REVISION") from invalid
    schedule = load_current_schedule(connection, tenant)
    capacity = None
    if schedule.allow_overlap:
        try:
            lock_policy_heads(connection, tenant, {"CAPACITY": "SHARE"})
        except StalePolicyRevision as invalid:
            raise ContractError(409, "STALE_REVISION") from invalid
        capacity = _capacity_state(connection, tenant)
    candidates = candidates_for_date(schedule, service_date)
    existing = (
        _counted_intervals(connection, tenant, service_date) if candidates else []
    )
    now = datetime.now(UTC)
    return {
        "service_date": service_date.isoformat(),
        "booking_mode": schedule.booking_mode,
        "open_eligible": schedule.booking_mode == "OPEN"
        and service_date >= now.astimezone(CAIRO).date()
        and date_rules(schedule, service_date).eligible,
        "candidates": [
            {
                "starts_at": candidate.starts_at.isoformat(),
                "ends_at": candidate.ends_at.isoformat(),
                "origin": candidate.origin,
                "available": candidate.starts_at > now
                and _available(
                    existing,
                    candidate,
                    schedule.allow_overlap,
                    capacity["capacity"] if capacity else 1,
                ),
            }
            for candidate in candidates
        ],
        "schedule_revision": _schedule_head(connection, tenant)["revision"],
        **({"capacity_revision": capacity["revision"]} if capacity else {}),
    }


def validate_timed_booking(
    connection: Connection,
    tenant,
    service_date: date,
    starts_at: datetime,
    ends_at: datetime,
    now: datetime,
    expected_capacity_revision: int | None,
) -> None:
    """Caller holds schedule/policy locks and the clinic/day number-counter lock."""
    if any(
        endpoint.tzinfo is None or endpoint.utcoffset() is None
        for endpoint in (starts_at, ends_at, now)
    ):
        raise ContractError(422)
    if service_date < now.astimezone(CAIRO).date() or starts_at <= now:
        raise ContractError(409)
    if (
        starts_at.astimezone(CAIRO).date() != service_date
        or ends_at.astimezone(CAIRO).date() != service_date
    ):
        raise ContractError(422)
    schedule = load_current_schedule(connection, tenant)
    if (starts_at, ends_at) not in {
        (slot.starts_at, slot.ends_at)
        for slot in candidates_for_date(schedule, service_date)
    }:
        raise ContractError(409)
    capacity = _capacity_state(connection, tenant) if schedule.allow_overlap else None
    if capacity:
        require_matching_revision(expected_capacity_revision, capacity["revision"])
    booked = _counted_intervals(connection, tenant, service_date)
    if not _available(
        booked,
        Candidate(starts_at.astimezone(UTC), ends_at.astimezone(UTC), "VALIDATION"),
        schedule.allow_overlap,
        capacity["capacity"] if capacity else 1,
    ):
        raise ContractError(409)

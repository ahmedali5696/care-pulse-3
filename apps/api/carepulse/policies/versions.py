"""Internal D-04 version writes. Authenticated Owner commands belong to B-06/B-07."""

import json
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy import text
from sqlalchemy.engine import Connection

CAIRO = ZoneInfo("Africa/Cairo")
KINDS = frozenset({"PRICE", "SCHEDULE", "QUEUE", "ADMISSION", "CAPACITY", "FOLLOWUP"})


class StalePolicyRevision(ValueError):
    """The requested policy base has been superseded."""


def _next_operating_day(
    connection: Connection, tenant: str, today: date
) -> date | None:
    """Absent operating periods mean pending queue effectiveness, never tomorrow."""
    schedule = connection.execute(
        text("""
        SELECT v.version FROM public.clinic_policy_heads h
        JOIN public.clinic_policy_versions v
          ON (v.organization_id, v.kind, v.version) =
             (h.organization_id, h.kind, h.current_version)
        WHERE h.organization_id = :tenant AND h.kind = 'SCHEDULE'
        FOR SHARE OF h
    """),
        {"tenant": tenant},
    ).scalar_one()
    rows = connection.execute(
        text("""
        SELECT interval_kind, weekday, service_date
        FROM public.clinic_schedule_intervals
        WHERE organization_id = :tenant AND policy_kind = 'SCHEDULE'
          AND policy_version = :version AND interval_kind IN ('WORK', 'OVERRIDE_WORK')
    """),
        {"tenant": tenant, "version": schedule},
    ).all()
    weekdays = {row.weekday for row in rows if row.interval_kind == "WORK"}
    overrides = {
        row.service_date for row in rows if row.interval_kind == "OVERRIDE_WORK"
    }
    holidays = set(
        connection.scalars(
            text("""
        SELECT service_date FROM public.clinic_schedule_holidays
        WHERE organization_id = :tenant AND policy_version = :version
    """),
            {"tenant": tenant, "version": schedule},
        ).all()
    )
    for offset in range(1, 367):
        candidate = today + timedelta(days=offset)
        if candidate not in holidays and (
            candidate in overrides or candidate.isoweekday() in weekdays
        ):
            return candidate
    return None


def append_version(
    connection: Connection,
    tenant: str,
    kind: str,
    payload: dict,
    expected_revision: int,
    *,
    actor_staff_id: str | None = None,
    today: date | None = None,
    intervals: list[dict] | None = None,
    holidays: list[date] | None = None,
) -> int:
    """Caller owns transaction, live authorization and tenant-scoped SET LOCAL.

    Head FOR UPDATE serializes writers. Immutable versions retain all previous
    references; future queue policy remains pending if no operating day exists.
    """
    if kind not in KINDS:
        raise ValueError("Unsupported policy kind")
    if kind != "SCHEDULE" and (intervals or holidays):
        raise ValueError("Schedule entries require a schedule version")
    effective_on = None
    if kind == "QUEUE":
        # Lock the calendar before the queue revision so concurrent calendar
        # replacements cannot change the chosen next operating day.
        effective_on = _next_operating_day(
            connection, tenant, today or datetime.now(CAIRO).date()
        )
    head = connection.execute(
        text("""
        SELECT current_version, revision FROM public.clinic_policy_heads
        WHERE organization_id = :tenant AND kind = :kind FOR UPDATE
    """),
        {"tenant": tenant, "kind": kind},
    ).one_or_none()
    if head is None or head.revision != expected_revision:
        raise StalePolicyRevision("Policy revision changed")
    new_version = head.current_version + 1
    connection.execute(
        text("""
        INSERT INTO public.clinic_policy_versions
            (organization_id, kind, version, payload, created_by_staff_id,
             effective_on, pending_effective)
        VALUES (:tenant, :kind, :version, CAST(:payload AS jsonb), :actor,
                :effective, :pending)
    """),
        {
            "tenant": tenant,
            "kind": kind,
            "version": new_version,
            "payload": json.dumps(payload, allow_nan=False),
            "actor": actor_staff_id,
            "effective": effective_on,
            "pending": kind == "QUEUE" and effective_on is None,
        },
    )
    if kind == "SCHEDULE":
        for item in intervals or []:
            connection.execute(
                text("""
                INSERT INTO public.clinic_schedule_intervals
                    (organization_id, policy_version, interval_kind, weekday,
                     service_date, starts_at, ends_at)
                VALUES (:tenant, :version, :interval_kind, :weekday,
                        :service_date, :starts_at, :ends_at)
            """),
                {"tenant": tenant, "version": new_version, **item},
            )
        for service_date in holidays or []:
            connection.execute(
                text("""
                INSERT INTO public.clinic_schedule_holidays
                    (organization_id, policy_version, service_date)
                VALUES (:tenant, :version, :service_date)
            """),
                {
                    "tenant": tenant,
                    "version": new_version,
                    "service_date": service_date,
                },
            )
    connection.execute(
        text("""
        UPDATE public.clinic_policy_heads
        SET current_version = :version, revision = revision + 1,
            queue_effective_on = :effective,
            queue_pending = :pending
        WHERE organization_id = :tenant AND kind = :kind
    """),
        {
            "tenant": tenant,
            "kind": kind,
            "version": new_version,
            "effective": effective_on,
            "pending": kind == "QUEUE" and effective_on is None,
        },
    )
    return new_version

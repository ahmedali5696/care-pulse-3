"""Cairo-aware UTC candidates for an exact-time schedule."""

import re
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from itertools import product
from zoneinfo import ZoneInfo

from carepulse.policies.intervals import Interval, intersects
from carepulse.policies.schedule import (
    DateRules,
    LocalSlot,
    ScheduleConfig,
    _fits,
    date_rules,
)

CAIRO = ZoneInfo("Africa/Cairo")
RFC3339 = re.compile(
    r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})"
)


@dataclass(frozen=True)
class Candidate(Interval):
    origin: str


def _cairo_instants(service_date: date, wall_time: time) -> tuple[datetime, ...]:
    instants = set()
    for fold in (0, 1):
        local = datetime.combine(service_date, wall_time, CAIRO).replace(fold=fold)
        instant = local.astimezone(UTC)
        roundtrip = instant.astimezone(CAIRO)
        if roundtrip.date() == service_date and roundtrip.time() == wall_time:
            instants.add(instant)
    return tuple(sorted(instants))


def resolve_cairo_local_time(
    service_date: date, wall_time: time, explicit_offset: timedelta | None = None
) -> datetime:
    matches = tuple(
        instant
        for instant in _cairo_instants(service_date, wall_time)
        if explicit_offset is None
        or instant.astimezone(CAIRO).utcoffset() == explicit_offset
    )
    if len(matches) != 1:
        raise ValueError("Nonexistent or ambiguous Cairo local time")
    return matches[0]


def parse_timed_endpoint(text: str, intended_service_date: date) -> datetime:
    if not isinstance(text, str) or not RFC3339.fullmatch(text):
        raise ValueError("Timed endpoint requires RFC3339 with an explicit offset")
    supplied = datetime.fromisoformat(text)
    instant = supplied.astimezone(UTC)
    local = instant.astimezone(CAIRO)
    if supplied.utcoffset() != timedelta(0) and (
        supplied.date() != local.date()
        or supplied.time() != local.time()
        or supplied.utcoffset() != local.utcoffset()
    ):
        raise ValueError("Timed endpoint offset does not match Cairo")
    if (
        local.date() != intended_service_date
        or resolve_cairo_local_time(
            intended_service_date, local.time(), local.utcoffset()
        )
        != instant
    ):
        raise ValueError("Timed endpoint is outside the Cairo service date")
    return instant


def _variants(
    service_date: date, slot: LocalSlot, origin: str, duration: timedelta | None = None
) -> tuple[Candidate, ...]:
    return tuple(
        Candidate(start, end, origin)
        for start, end in product(
            _cairo_instants(service_date, slot.starts_at),
            _cairo_instants(service_date, slot.ends_at),
        )
        if start < end and (duration is None or end - start == duration)
    )


def _generated_candidates(
    rules: DateRules, service_date: date, duration_minutes: int | None
) -> tuple[Candidate, ...]:
    generated = []
    if duration_minutes is not None and 0 < duration_minutes < 24 * 60:
        duration = timedelta(minutes=duration_minutes)
        for period in rules.periods:
            cursor = datetime.combine(service_date, period.starts_at)
            period_end = datetime.combine(service_date, period.ends_at)
            while cursor + duration <= period_end:
                slot = LocalSlot(cursor.time(), (cursor + duration).time())
                if _fits(slot, (period,), rules.breaks):
                    generated.extend(
                        _variants(service_date, slot, "GENERATED", duration)
                    )
                cursor += duration
    return tuple(generated)


def candidates_for_date(
    config: ScheduleConfig, service_date: date
) -> tuple[Candidate, ...]:
    if config.booking_mode != "EXACT_TIME":
        return ()
    rules = date_rules(config, service_date)
    if not rules.eligible:
        return ()

    manual = tuple(
        variant
        for slot in rules.manual_slots
        if _fits(slot, rules.periods, rules.breaks)
        for variant in _variants(service_date, slot, "MANUAL")
    )
    generated = _generated_candidates(rules, service_date, config.duration_minutes)
    offered = [*manual]
    offered.extend(
        candidate
        for candidate in generated
        if not any(intersects(candidate, selected) for selected in manual)
    )

    return tuple(
        sorted(
            offered,
            key=lambda candidate: (
                candidate.starts_at,
                candidate.ends_at,
                candidate.origin,
            ),
        )
    )

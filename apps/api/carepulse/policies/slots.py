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
    r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.(?P<fraction>\d+))?"
    r"(?:Z|[+-](?:[01]\d|2[0-3]):[0-5]\d)"
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
    match = RFC3339.fullmatch(text) if isinstance(text, str) else None
    if match is None or any(digit != "0" for digit in (match["fraction"] or "")[6:]):
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


def _fits_resolved(
    candidate: Candidate,
    periods: tuple[Candidate, ...],
    breaks: tuple[Candidate, ...],
) -> bool:
    return any(
        period.starts_at <= candidate.starts_at and candidate.ends_at <= period.ends_at
        for period in periods
    ) and not any(intersects(candidate, pause) for pause in breaks)


def _resolved_segments(
    service_date: date, slot: LocalSlot, origin: str
) -> tuple[Candidate, ...]:
    # HH:MM schedule boundaries describe each real minute, including both folds.
    minute = timedelta(minutes=1)
    cursor = datetime.combine(service_date, slot.starts_at)
    end = datetime.combine(service_date, slot.ends_at)
    instants = []
    while cursor < end:
        instants.extend(_cairo_instants(service_date, cursor.time()))
        cursor += minute
    segments = []
    for instant in sorted(instants):
        if segments and segments[-1].ends_at == instant:
            previous = segments[-1]
            segments[-1] = Candidate(previous.starts_at, instant + minute, origin)
        else:
            segments.append(Candidate(instant, instant + minute, origin))
    return tuple(segments)


def candidates_for_date(
    config: ScheduleConfig, service_date: date
) -> tuple[Candidate, ...]:
    if config.booking_mode != "EXACT_TIME":
        return ()
    rules = date_rules(config, service_date)
    if not rules.eligible:
        return ()

    periods = tuple(
        variant
        for period in rules.periods
        for variant in _resolved_segments(service_date, period, "PERIOD")
    )
    breaks = tuple(
        variant
        for pause in rules.breaks
        for variant in _resolved_segments(service_date, pause, "BREAK")
    )
    manual = tuple(
        variant
        for slot in rules.manual_slots
        if _fits(slot, rules.periods, rules.breaks)
        for variant in _variants(service_date, slot, "MANUAL")
        if _fits_resolved(variant, periods, breaks)
    )
    generated = tuple(
        candidate
        for candidate in _generated_candidates(
            rules, service_date, config.duration_minutes
        )
        if _fits_resolved(candidate, periods, breaks)
    )
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

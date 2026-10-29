"""Pure calendar resolution and configuration validation for B-07."""

from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from itertools import pairwise


class ScheduleInvalid(ValueError):
    """Unprocessable proposed schedule (HTTP 422 at the service boundary)."""


@dataclass(frozen=True)
class LocalSlot:
    starts_at: time
    ends_at: time


@dataclass(frozen=True)
class WeekdaySlot(LocalSlot):
    weekday: int


@dataclass(frozen=True)
class DatedSlot(LocalSlot):
    service_date: date


@dataclass(frozen=True)
class NoShowCutoff:
    enabled: bool = False
    local_time: time | None = None


@dataclass(frozen=True)
class OverlapCapacityConfirmation:
    capacity: int
    capacity_revision: int


@dataclass(frozen=True)
class ScheduleConfig:
    booking_mode: str = "OPEN"
    duration_minutes: int | None = None
    holiday_booking_enabled: bool = False
    allow_overlap: bool = False
    no_show_cutoff: NoShowCutoff = NoShowCutoff()
    # None marks legacy v1 metadata: derive weekdays from stored WORK rows.
    working_weekdays: frozenset[int] | None = None
    weekly_work_periods: tuple[WeekdaySlot, ...] = ()
    weekly_breaks: tuple[WeekdaySlot, ...] = ()
    weekly_manual_slots: tuple[WeekdaySlot, ...] = ()
    open_override_dates: frozenset[date] = frozenset()
    override_work_periods: tuple[DatedSlot, ...] = ()
    manual_slots_by_date: dict[date, tuple[LocalSlot, ...]] = field(
        default_factory=dict
    )
    holidays: frozenset[date] = frozenset()
    overlap_capacity_confirmation: OverlapCapacityConfirmation | None = None


@dataclass(frozen=True)
class DateRules:
    eligible: bool
    periods: tuple[LocalSlot, ...]
    breaks: tuple[LocalSlot, ...]
    manual_slots: tuple[LocalSlot, ...]


def effective_working_weekdays(config: ScheduleConfig) -> frozenset[int]:
    if config.working_weekdays is not None:
        return config.working_weekdays
    return frozenset(period.weekday for period in config.weekly_work_periods)


def resolved_periods(
    config: ScheduleConfig, service_date: date
) -> tuple[LocalSlot, ...]:
    overrides = tuple(
        LocalSlot(period.starts_at, period.ends_at)
        for period in config.override_work_periods
        if period.service_date == service_date
    )
    if overrides:
        return tuple(sorted(overrides, key=lambda slot: slot.starts_at))
    if service_date in config.holidays or service_date in config.open_override_dates:
        return ()
    if service_date.isoweekday() not in effective_working_weekdays(config):
        return ()
    return tuple(
        LocalSlot(period.starts_at, period.ends_at)
        for period in config.weekly_work_periods
        if period.weekday == service_date.isoweekday()
    )


def is_working_day(config: ScheduleConfig, service_date: date) -> bool:
    return service_date not in config.holidays and (
        any(
            period.service_date == service_date
            for period in config.override_work_periods
        )
        or service_date in config.open_override_dates
        or service_date.isoweekday() in effective_working_weekdays(config)
    )


def effective_manual_slots(
    config: ScheduleConfig, service_date: date
) -> tuple[LocalSlot, ...]:
    if service_date in config.manual_slots_by_date:
        return config.manual_slots_by_date[service_date]
    return tuple(
        LocalSlot(slot.starts_at, slot.ends_at)
        for slot in config.weekly_manual_slots
        if slot.weekday == service_date.isoweekday()
    )


def _breaks(config: ScheduleConfig, service_date: date) -> tuple[LocalSlot, ...]:
    return tuple(
        LocalSlot(slot.starts_at, slot.ends_at)
        for slot in config.weekly_breaks
        if slot.weekday == service_date.isoweekday()
    )


def _intersects(first: LocalSlot, second: LocalSlot) -> bool:
    return first.starts_at < second.ends_at and second.starts_at < first.ends_at


def _fits(
    slot: LocalSlot, periods: tuple[LocalSlot, ...], breaks: tuple[LocalSlot, ...]
) -> bool:
    return any(
        period.starts_at <= slot.starts_at and slot.ends_at <= period.ends_at
        for period in periods
    ) and not any(_intersects(slot, pause) for pause in breaks)


def _has_generated_slot(
    periods: tuple[LocalSlot, ...], breaks: tuple[LocalSlot, ...], duration: int | None
) -> bool:
    if duration is None or type(duration) is not int or duration <= 0:
        return False
    if duration >= 24 * 60:
        return False
    for period in periods:
        candidate = datetime.combine(date.min, period.starts_at)
        end = datetime.combine(date.min, period.ends_at)
        while candidate + timedelta(minutes=duration) <= end:
            slot = LocalSlot(
                candidate.time(), (candidate + timedelta(minutes=duration)).time()
            )
            if _fits(slot, (period,), breaks):
                return True
            candidate += timedelta(minutes=duration)
    return False


def date_rules(config: ScheduleConfig, service_date: date) -> DateRules:
    periods = resolved_periods(config, service_date)
    breaks = _breaks(config, service_date)
    manual_slots = effective_manual_slots(config, service_date)
    working = is_working_day(config, service_date)
    permitted = working or config.holiday_booking_enabled
    if config.booking_mode == "OPEN":
        eligible = permitted
    else:
        override = any(
            slot.service_date == service_date for slot in config.override_work_periods
        )
        eligible = (
            permitted
            and (working or override)
            and (
                any(_fits(slot, periods, breaks) for slot in manual_slots)
                or _has_generated_slot(periods, breaks, config.duration_minutes)
            )
        )
    return DateRules(
        eligible=eligible, periods=periods, breaks=breaks, manual_slots=manual_slots
    )


def _valid_time(wall_time: time) -> bool:
    return (
        isinstance(wall_time, time)
        and wall_time.tzinfo is None
        and wall_time.second == 0
        and wall_time.microsecond == 0
    )


def _validate_slots(slots: tuple[LocalSlot, ...]) -> None:
    for slot in slots:
        if (
            not _valid_time(slot.starts_at)
            or not _valid_time(slot.ends_at)
            or slot.starts_at >= slot.ends_at
        ):
            raise ScheduleInvalid("Periods and slots require positive HH:MM ranges")


def _validate_nonoverlapping(slots: tuple[LocalSlot, ...]) -> None:
    _validate_slots(slots)
    ordered = sorted(slots, key=lambda slot: slot.starts_at)
    if any(_intersects(left, right) for left, right in pairwise(ordered)):
        raise ScheduleInvalid("Overlapping periods or breaks")


def _validate_weekly(config: ScheduleConfig, weekdays: frozenset[int]) -> None:
    weekly = (
        *config.weekly_work_periods,
        *config.weekly_breaks,
        *config.weekly_manual_slots,
    )
    if any(
        type(slot.weekday) is not int or slot.weekday not in weekdays for slot in weekly
    ):
        raise ScheduleInvalid("Weekly slots require a configured working weekday")
    for weekday in weekdays:
        work = tuple(
            slot for slot in config.weekly_work_periods if slot.weekday == weekday
        )
        breaks = tuple(slot for slot in config.weekly_breaks if slot.weekday == weekday)
        manual = tuple(
            slot for slot in config.weekly_manual_slots if slot.weekday == weekday
        )
        _validate_nonoverlapping(work)
        _validate_nonoverlapping(breaks)
        _validate_nonoverlapping(manual)
        if any(not _fits(pause, work, ()) for pause in breaks):
            raise ScheduleInvalid("Break outside a work period")
        if any(not _fits(slot, work, breaks) for slot in manual):
            raise ScheduleInvalid("Weekly manual slot outside work periods or in break")


def _validate_dated(config: ScheduleConfig) -> None:
    override_dates = {period.service_date for period in config.override_work_periods}
    for service_date in override_dates:
        _validate_nonoverlapping(
            tuple(
                period
                for period in config.override_work_periods
                if period.service_date == service_date
            )
        )
    for service_date, manual in config.manual_slots_by_date.items():
        _validate_nonoverlapping(manual)
        if any(
            not _fits(
                slot,
                resolved_periods(config, service_date),
                _breaks(config, service_date),
            )
            for slot in manual
        ):
            raise ScheduleInvalid("Date manual slot outside work periods or in break")


def _validate_overlap(config: ScheduleConfig, saved_capacity: int) -> None:
    if not config.allow_overlap:
        return
    confirmation = config.overlap_capacity_confirmation
    if config.booking_mode != "EXACT_TIME" or (
        config.duration_minutes is None
        and not config.weekly_manual_slots
        and not any(config.manual_slots_by_date.values())
    ):
        raise ScheduleInvalid("Overlap requires configured Exact-Time slots")
    if (
        confirmation is None
        or type(confirmation.capacity) is not int
        or confirmation.capacity < 2
        or confirmation.capacity != saved_capacity
    ):
        raise ScheduleInvalid("Overlap capacity confirmation required")


def validate_schedule(config: ScheduleConfig, saved_capacity: int) -> None:
    if config.booking_mode not in ("OPEN", "EXACT_TIME"):
        raise ScheduleInvalid("Invalid booking mode")
    if config.duration_minutes is not None and (
        type(config.duration_minutes) is not int or config.duration_minutes <= 0
    ):
        raise ScheduleInvalid("Duration must be a positive integer")
    if config.no_show_cutoff.local_time is not None and not _valid_time(
        config.no_show_cutoff.local_time
    ):
        raise ScheduleInvalid("Cutoff must be HH:MM")
    if config.no_show_cutoff.enabled and config.no_show_cutoff.local_time is None:
        # enabling requires a valid specified time (no product-invented default)
        raise ScheduleInvalid("Enabled cutoff requires a time")
    weekdays = effective_working_weekdays(config)
    if any(type(weekday) is not int or not 1 <= weekday <= 7 for weekday in weekdays):
        raise ScheduleInvalid("Invalid working weekday")
    if config.holidays & config.open_override_dates:
        raise ScheduleInvalid("Holiday conflicts with an Open override")
    _validate_weekly(config, weekdays)
    _validate_dated(config)
    _validate_overlap(config, saved_capacity)

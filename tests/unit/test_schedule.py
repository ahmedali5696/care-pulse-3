"""B-07 pure schedule eligibility and validation regressions."""

import json
from dataclasses import replace
from datetime import date, time, timedelta
from decimal import Decimal

import pytest
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
    resolved_periods,
    validate_schedule,
)
from carepulse.policies.schedule_contracts import ScheduleConfigInput
from carepulse.policies.schedule_service import (
    _payload_config,
    _to_config,
    _version_payload,
)
from pydantic import ValidationError

MONDAY = date(2026, 11, 2)
TUESDAY = MONDAY + timedelta(days=1)


def _clock(hour: int, minute: int = 0) -> time:
    return time(hour, minute)


def _slot(start: int, end: int) -> LocalSlot:
    return LocalSlot(_clock(start), _clock(end))


def _week(start: int, end: int, weekday: int = 1) -> WeekdaySlot:
    return WeekdaySlot(_clock(start), _clock(end), weekday)


def _date_slot(start: int, end: int, service_date: date = MONDAY) -> DatedSlot:
    return DatedSlot(_clock(start), _clock(end), service_date)


def _weekly_config() -> ScheduleConfig:
    return ScheduleConfig(
        working_weekdays=frozenset({1}),
        weekly_work_periods=(_week(9, 12),),
        duration_minutes=30,
        booking_mode="EXACT_TIME",
    )


def test_default_open_calendar_has_no_invented_hours_or_cutoff():
    config = ScheduleConfig()
    validate_schedule(config, saved_capacity=1)
    assert config.booking_mode == "OPEN"
    assert config.duration_minutes is None
    assert not config.allow_overlap
    assert not config.holiday_booking_enabled
    assert config.no_show_cutoff == NoShowCutoff(enabled=False, local_time=None)
    assert not is_working_day(config, MONDAY)
    assert resolved_periods(config, MONDAY) == ()
    assert not date_rules(config, MONDAY).eligible


def test_open_working_day_needs_no_hours_but_exact_time_needs_candidates():
    open_config = ScheduleConfig(working_weekdays=frozenset({1}))
    validate_schedule(open_config, 1)
    assert date_rules(open_config, MONDAY).eligible
    assert resolved_periods(open_config, MONDAY) == ()
    assert not date_rules(
        replace(open_config, booking_mode="EXACT_TIME"), MONDAY
    ).eligible
    assert date_rules(_weekly_config(), MONDAY).eligible


def test_duration_longer_than_a_day_produces_no_candidate_without_an_artificial_limit():
    config = replace(_weekly_config(), duration_minutes=10**30)
    validate_schedule(config, 1)
    assert not date_rules(config, MONDAY).eligible


@pytest.mark.parametrize("duration", ["9007199254740993", "9" * 4301])
def test_duration_wire_and_published_numeric_payload_preserve_exact_digits(duration):
    config = ScheduleConfig(
        duration_minutes=int(Decimal(duration)), working_weekdays=frozenset()
    )
    wire = _payload_config(config)
    assert wire["duration_minutes"] == duration
    parsed = ScheduleConfigInput.model_validate(wire)
    assert _to_config(parsed).duration_minutes == config.duration_minutes
    published = _version_payload(config, False)
    assert json.loads(published, parse_int=Decimal)["duration_minutes"] == Decimal(
        duration
    )
    assert f'"duration_minutes": {duration}' in published


@pytest.mark.parametrize("invalid", [30, 30.0, True, "0", "01", "1.0", "+1", "-1"])
def test_duration_wire_rejects_numbers_and_noncanonical_text(invalid):
    wire = _payload_config(ScheduleConfig(working_weekdays=frozenset()))
    with pytest.raises(ValidationError):
        ScheduleConfigInput.model_validate({**wire, "duration_minutes": invalid})


def test_legacy_weekdays_derive_from_work_periods_only_when_metadata_absent():
    legacy = ScheduleConfig(weekly_work_periods=(_week(9, 10),))
    validate_schedule(legacy, 1)
    assert is_working_day(legacy, MONDAY)
    assert not is_working_day(replace(legacy, working_weekdays=frozenset()), MONDAY)
    with pytest.raises(ScheduleInvalid):
        validate_schedule(replace(legacy, working_weekdays=frozenset()), 1)


def test_date_override_beats_weekly_and_weekly_breaks_apply():
    config = replace(
        _weekly_config(),
        weekly_breaks=(_week(10, 11),),
        override_work_periods=(_date_slot(10, 12),),
    )
    validate_schedule(config, 1)
    assert resolved_periods(config, MONDAY) == (_slot(10, 12),)
    assert date_rules(config, MONDAY).breaks == (_slot(10, 11),)
    assert date_rules(config, MONDAY).eligible


@pytest.mark.parametrize(
    ("date_", "holiday_enabled", "mode", "has_override", "expected"),
    [
        (MONDAY, False, "OPEN", False, True),
        (TUESDAY, False, "OPEN", False, False),
        (TUESDAY, True, "OPEN", False, True),
        (TUESDAY, True, "EXACT_TIME", False, False),
        (MONDAY, False, "OPEN", True, False),
        (MONDAY, True, "OPEN", True, True),
        (MONDAY, False, "EXACT_TIME", True, False),
        (MONDAY, True, "EXACT_TIME", True, True),
    ],
)
def test_holiday_and_working_day_eligibility(
    date_, holiday_enabled, mode, has_override, expected
):
    # a closed holiday cannot borrow weekly hours as an implicit opening
    config = replace(
        _weekly_config(),
        booking_mode=mode,
        holiday_booking_enabled=holiday_enabled,
        holidays=frozenset({MONDAY}) if has_override else frozenset(),
        override_work_periods=(_date_slot(10, 12),) if has_override else (),
    )
    assert date_rules(config, date_).eligible is expected
    if has_override:
        assert not is_working_day(config, MONDAY)


def test_open_override_date_opens_without_hours_and_suppresses_weekly_periods():
    config = replace(
        _weekly_config(),
        booking_mode="OPEN",
        open_override_dates=frozenset({MONDAY, TUESDAY}),
    )
    assert is_working_day(config, TUESDAY)
    assert date_rules(config, TUESDAY).eligible
    assert date_rules(config, MONDAY).periods == ()


def test_empty_date_manual_set_replaces_weekly_instead_of_inheriting():
    config = replace(
        _weekly_config(),
        weekly_manual_slots=(_week(9, 10),),
        manual_slots_by_date={MONDAY: ()},
    )
    validate_schedule(config, 1)
    assert effective_manual_slots(config, MONDAY) == ()
    assert effective_manual_slots(replace(config, manual_slots_by_date={}), MONDAY) == (
        _slot(9, 10),
    )


@pytest.mark.parametrize(
    "config",
    [
        ScheduleConfig(working_weekdays=frozenset({0})),
        ScheduleConfig(working_weekdays=frozenset({8})),
        ScheduleConfig(
            weekly_work_periods=(_week(9, 10),), working_weekdays=frozenset()
        ),
        ScheduleConfig(working_weekdays=frozenset({1}), weekly_breaks=(_week(9, 10),)),
        replace(_weekly_config(), weekly_work_periods=(_week(9, 11), _week(10, 12))),
        replace(_weekly_config(), weekly_work_periods=(_week(9, 9),)),
        replace(_weekly_config(), weekly_breaks=(_week(8, 10),)),
        replace(_weekly_config(), weekly_breaks=(_week(9, 11), _week(10, 12))),
        replace(_weekly_config(), weekly_manual_slots=(_week(8, 10),)),
        replace(
            _weekly_config(),
            weekly_breaks=(_week(10, 11),),
            weekly_manual_slots=(_week(10, 11),),
        ),
        replace(_weekly_config(), manual_slots_by_date={MONDAY: (_slot(8, 10),)}),
        replace(
            _weekly_config(),
            override_work_periods=(_date_slot(9, 11), _date_slot(10, 12)),
        ),
        replace(
            _weekly_config(),
            holidays=frozenset({MONDAY}),
            open_override_dates=frozenset({MONDAY}),
        ),
        replace(_weekly_config(), booking_mode="UNKNOWN"),
        replace(_weekly_config(), duration_minutes=0),
        replace(_weekly_config(), duration_minutes=True),
    ],
)
def test_invalid_schedule_period_break_manual_and_mode_matrix(config):
    with pytest.raises(ScheduleInvalid):
        validate_schedule(config, 1)


def test_cutoff_needs_explicit_valid_time_and_is_never_period_end():
    config = _weekly_config()
    assert config.no_show_cutoff.local_time is None
    with pytest.raises(ScheduleInvalid):
        validate_schedule(replace(config, no_show_cutoff=NoShowCutoff(enabled=True)), 1)
    with pytest.raises(ScheduleInvalid):
        validate_schedule(
            replace(config, no_show_cutoff=NoShowCutoff(True, time(12, 0, 1))), 1
        )
    validate_schedule(replace(config, no_show_cutoff=NoShowCutoff(True, _clock(16))), 1)


@pytest.mark.parametrize(
    ("config", "saved_capacity", "valid"),
    [
        (_weekly_config(), 1, True),
        (replace(_weekly_config(), allow_overlap=True), 2, False),
        (
            replace(
                _weekly_config(),
                allow_overlap=True,
                overlap_capacity_confirmation=OverlapCapacityConfirmation(1, 1),
            ),
            1,
            False,
        ),
        (
            replace(
                _weekly_config(),
                allow_overlap=True,
                overlap_capacity_confirmation=OverlapCapacityConfirmation(2, 1),
            ),
            3,
            False,
        ),
        (
            replace(
                _weekly_config(),
                allow_overlap=True,
                overlap_capacity_confirmation=OverlapCapacityConfirmation(2, 1),
            ),
            2,
            True,
        ),
        (
            ScheduleConfig(
                booking_mode="EXACT_TIME",
                allow_overlap=True,
                overlap_capacity_confirmation=OverlapCapacityConfirmation(2, 1),
            ),
            2,
            False,
        ),
    ],
)
def test_overlap_requires_slots_and_current_confirmed_capacity(
    config, saved_capacity, valid
):
    if valid:
        validate_schedule(config, saved_capacity)
    else:
        with pytest.raises(ScheduleInvalid):
            validate_schedule(config, saved_capacity)

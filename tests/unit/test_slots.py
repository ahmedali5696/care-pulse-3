"""B-08 exact-time UTC candidate and Cairo DST regressions."""

from dataclasses import replace
from datetime import UTC, date, datetime, time, timedelta
from zoneinfo import ZoneInfo

import pytest
from carepulse.policies.schedule import (
    DatedSlot,
    LocalSlot,
    ScheduleConfig,
    WeekdaySlot,
)
from carepulse.policies.slots import (
    candidates_for_date,
    parse_timed_endpoint,
    resolve_cairo_local_time,
)

CAIRO = ZoneInfo("Africa/Cairo")
MONDAY = date(2026, 11, 2)


def _time(hour: int, minute: int = 0) -> time:
    return time(hour, minute)


def _local(start: tuple[int, int], end: tuple[int, int]) -> LocalSlot:
    return LocalSlot(_time(*start), _time(*end))


def _schedule(
    day: date = MONDAY,
    periods: tuple[LocalSlot, ...] = (_local((9, 0), (11, 20)),),
    duration: int | None = 30,
) -> ScheduleConfig:
    return ScheduleConfig(
        booking_mode="EXACT_TIME",
        duration_minutes=duration,
        working_weekdays=frozenset({day.isoweekday()}),
        weekly_work_periods=tuple(
            WeekdaySlot(slot.starts_at, slot.ends_at, day.isoweekday())
            for slot in periods
        ),
    )


def _offered(config: ScheduleConfig, day: date = MONDAY) -> list[tuple[str, str, str]]:
    return [
        (
            slot.starts_at.astimezone(CAIRO).strftime("%H:%M"),
            slot.ends_at.astimezone(CAIRO).strftime("%H:%M"),
            slot.origin,
        )
        for slot in candidates_for_date(config, day)
    ]


@pytest.fixture(scope="module")
def cairo_transitions() -> dict[str, tuple[date, time]]:
    transitions = {}
    instant = datetime(2026, 1, 1, tzinfo=UTC)
    year_end = instant.replace(year=2027)
    while instant < year_end:
        following = instant + timedelta(hours=1)
        earlier_offset = instant.astimezone(CAIRO).utcoffset()
        later_offset = following.astimezone(CAIRO).utcoffset()
        if earlier_offset != later_offset:
            transition = next(
                instant + timedelta(minutes=minute)
                for minute in range(1, 61)
                if (instant + timedelta(minutes=minute)).astimezone(CAIRO).utcoffset()
                != earlier_offset
            )
            local = transition.astimezone(CAIRO).replace(tzinfo=None)
            if later_offset > earlier_offset:
                local -= later_offset - earlier_offset
                transitions["gap"] = (local.date(), local.time())
            else:
                transitions["fold"] = (local.date(), local.time())
        instant = following
    assert set(transitions) == {"gap", "fold"}
    return transitions


def test_grid_omits_break_and_remainder_without_shifting_anchor():
    config = replace(
        _schedule(),
        weekly_breaks=(WeekdaySlot(_time(9, 40), _time(9, 50), 1),),
    )
    assert _offered(config) == [
        ("09:00", "09:30", "GENERATED"),
        ("10:00", "10:30", "GENERATED"),
        ("10:30", "11:00", "GENERATED"),
    ]


def test_each_period_anchors_its_own_grid_with_exact_fit():
    config = _schedule(periods=(_local((9, 0), (10, 0)), _local((10, 10), (11, 10))))
    assert [start for start, _, _ in _offered(config)] == [
        "09:00",
        "09:30",
        "10:10",
        "10:40",
    ]
    assert _offered(config)[-1][1] == "11:10"


@pytest.mark.parametrize(
    ("manual_start", "manual_end", "generated_starts"),
    [
        ((9, 15), (9, 45), ["10:00", "10:30"]),
        ((9, 30), (10, 0), ["09:00", "10:00", "10:30"]),
    ],
)
def test_manual_precedence_suppresses_every_intersection_but_not_touching(
    manual_start, manual_end, generated_starts
):
    config = replace(
        _schedule(periods=(_local((9, 0), (11, 0)),)),
        weekly_manual_slots=(WeekdaySlot(_time(*manual_start), _time(*manual_end), 1),),
    )
    offered = _offered(config)
    assert [
        start for start, _, origin in offered if origin == "GENERATED"
    ] == generated_starts
    assert [origin for _, _, origin in offered].count("MANUAL") == 1


def test_explicit_empty_and_nonempty_date_manual_sets_replace_weekly():
    next_monday = MONDAY + timedelta(days=7)
    inherited_monday = MONDAY + timedelta(days=14)
    config = replace(
        _schedule(periods=(_local((9, 0), (11, 0)),)),
        weekly_manual_slots=(WeekdaySlot(_time(9, 15), _time(9, 45), 1),),
        manual_slots_by_date={
            MONDAY: (_local((10, 15), (10, 45)),),
            next_monday: (),
        },
    )
    assert [start for start, _, origin in _offered(config) if origin == "MANUAL"] == [
        "10:15"
    ]
    assert [
        start for start, _, origin in _offered(config) if origin == "GENERATED"
    ] == ["09:00", "09:30"]
    assert [origin for _, _, origin in _offered(config, next_monday)] == [
        "GENERATED"
    ] * 4
    assert [
        start
        for start, _, origin in _offered(config, inherited_monday)
        if origin == "MANUAL"
    ] == ["09:15"]


def test_manual_slots_outside_period_or_crossing_break_are_never_offered():
    config = replace(
        _schedule(periods=(_local((9, 0), (11, 0)),)),
        weekly_breaks=(WeekdaySlot(_time(9, 40), _time(9, 50), 1),),
        manual_slots_by_date={
            MONDAY: (
                _local((8, 45), (9, 15)),
                _local((9, 30), (10, 0)),
                _local((10, 0), (10, 15)),
            )
        },
    )
    assert [start for start, _, origin in _offered(config) if origin == "MANUAL"] == [
        "10:00"
    ]


def test_holiday_requires_permission_and_explicit_hours_for_timed_slots():
    config = replace(_schedule(), holidays=frozenset({MONDAY}))
    assert _offered(config) == []
    assert _offered(replace(config, holiday_booking_enabled=True)) == []
    override = DatedSlot(_time(9), _time(10), MONDAY)
    assert _offered(
        replace(config, holiday_booking_enabled=True, override_work_periods=(override,))
    ) == [
        ("09:00", "09:30", "GENERATED"),
        ("09:30", "10:00", "GENERATED"),
    ]
    assert (
        _offered(replace(config, booking_mode="OPEN", holiday_booking_enabled=True))
        == []
    )


def test_extremely_large_valid_duration_yields_no_generated_grid():
    assert (
        candidates_for_date(replace(_schedule(), duration_minutes=10**4300), MONDAY)
        == ()
    )


def test_gap_time_and_offset_forgery_rejected(cairo_transitions):
    gap_day, gap_time = cairo_transitions["gap"]
    with pytest.raises(ValueError):
        resolve_cairo_local_time(gap_day, gap_time)
    with pytest.raises(ValueError):
        parse_timed_endpoint(f"{gap_day}T{gap_time:%H:%M}:00+02:00", gap_day)
    valid_day = date(2026, 1, 12)
    with pytest.raises(ValueError):
        parse_timed_endpoint(f"{valid_day}T09:00:00+04:00", valid_day)


def test_gap_candidates_are_omitted(cairo_transitions):
    gap_day, gap_time = cairo_transitions["gap"]
    period = _local((gap_time.hour, gap_time.minute), (2, 0))
    config = _schedule(day=gap_day, periods=(period,))
    offered = _offered(config, gap_day)
    assert all(start != gap_time.strftime("%H:%M") for start, _, _ in offered)
    assert offered


def test_fold_rejects_unqualified_local_and_accepts_explicit_offsets_and_utc(
    cairo_transitions,
):
    fold_day, fold_time = cairo_transitions["fold"]
    with pytest.raises(ValueError):
        resolve_cairo_local_time(fold_day, fold_time)
    instants = [
        datetime.combine(fold_day, fold_time, CAIRO).replace(fold=fold).astimezone(UTC)
        for fold in (0, 1)
    ]
    assert instants[0] != instants[1]
    for instant in instants:
        local = instant.astimezone(CAIRO)
        assert (
            resolve_cairo_local_time(fold_day, fold_time, local.utcoffset()) == instant
        )
        assert parse_timed_endpoint(local.isoformat(), fold_day) == instant
        assert (
            parse_timed_endpoint(instant.isoformat().replace("+00:00", "Z"), fold_day)
            == instant
        )
        with pytest.raises(ValueError):
            parse_timed_endpoint(local.replace(tzinfo=None).isoformat(), fold_day)
        with pytest.raises(ValueError):
            parse_timed_endpoint(
                instant.isoformat().replace("+00:00", "Z"), fold_day + timedelta(days=1)
            )


def test_fold_generated_variants_keep_true_elapsed_duration_only(cairo_transitions):
    fold_day, fold_time = cairo_transitions["fold"]
    end_local = (datetime.combine(fold_day, fold_time) + timedelta(minutes=30)).time()
    config = _schedule(day=fold_day, periods=(LocalSlot(fold_time, end_local),))
    generated = candidates_for_date(config, fold_day)
    assert len(generated) == 2
    assert len({candidate.starts_at for candidate in generated}) == 2
    assert all(
        candidate.ends_at - candidate.starts_at == timedelta(minutes=30)
        for candidate in generated
    )
    assert all(candidate.origin == "GENERATED" for candidate in generated)
    mixed_start = min(candidate.starts_at for candidate in generated)
    mixed_end = max(candidate.ends_at for candidate in generated)
    assert mixed_end - mixed_start == timedelta(minutes=90)
    assert all(
        (candidate.starts_at, candidate.ends_at) != (mixed_start, mixed_end)
        for candidate in generated
    )
    manual = replace(
        config,
        duration_minutes=None,
        manual_slots_by_date={fold_day: (LocalSlot(fold_time, end_local),)},
    )
    # The mixed 90-minute interval leaves the configured 30-minute work window.
    manual_offered = candidates_for_date(manual, fold_day)
    assert len(manual_offered) == 2
    assert all(
        candidate.ends_at - candidate.starts_at == timedelta(minutes=30)
        for candidate in manual_offered
    )


def test_narrow_fold_work_period_does_not_cover_mixed_manual_interval(
    cairo_transitions,
):
    fold_day, repeated = cairo_transitions["fold"]
    config = replace(
        _schedule(
            day=fold_day,
            periods=(_local((repeated.hour, 20), (repeated.hour, 40)),),
            duration=None,
        ),
        manual_slots_by_date={
            fold_day: (_local((repeated.hour, 25), (repeated.hour, 35)),)
        },
    )
    offered = candidates_for_date(config, fold_day)
    assert len(offered) == 2
    assert all(candidate.origin == "MANUAL" for candidate in offered)
    assert {
        candidate.starts_at.astimezone(CAIRO).utcoffset() for candidate in offered
    } == {timedelta(hours=2), timedelta(hours=3)}
    assert all(
        candidate.ends_at - candidate.starts_at == timedelta(minutes=10)
        for candidate in offered
    )
    mixed = (
        min(slot.starts_at for slot in offered),
        max(slot.ends_at for slot in offered),
    )
    assert mixed[1] - mixed[0] == timedelta(minutes=70)
    assert mixed not in {(slot.starts_at, slot.ends_at) for slot in offered}


def test_fold_manual_variants_crossing_real_break_are_not_offered(cairo_transitions):
    fold_day, repeated = cairo_transitions["fold"]
    weekday = fold_day.isoweekday()
    config = replace(
        _schedule(
            day=fold_day,
            periods=(_local((repeated.hour - 1, 0), (repeated.hour, 59)),),
            duration=None,
        ),
        weekly_breaks=(
            WeekdaySlot(_time(repeated.hour, 20), _time(repeated.hour, 40), weekday),
        ),
        manual_slots_by_date={
            fold_day: (_local((repeated.hour, 45), (repeated.hour, 55)),)
        },
    )
    offered = candidates_for_date(config, fold_day)
    assert len(offered) == 2
    assert all(candidate.origin == "MANUAL" for candidate in offered)
    assert {
        candidate.starts_at.astimezone(CAIRO).utcoffset() for candidate in offered
    } == {timedelta(hours=2), timedelta(hours=3)}
    assert all(
        candidate.starts_at.astimezone(CAIRO).utcoffset()
        == candidate.ends_at.astimezone(CAIRO).utcoffset()
        for candidate in offered
    )


@pytest.mark.parametrize(
    "timestamp",
    [
        "2027-07-12T09:00:00+02:60",
        "2027-07-12T09:00:00.0000009+03:00",
    ],
)
def test_noncanonical_offset_and_nonzero_submicrosecond_instant_rejected(timestamp):
    with pytest.raises(ValueError):
        parse_timed_endpoint(timestamp, date(2027, 7, 12))


def test_zero_submicrosecond_tail_and_equivalent_utc_instant_accepted():
    service_date = date(2027, 7, 12)
    expected = datetime(2027, 7, 12, 6, tzinfo=UTC)
    assert (
        parse_timed_endpoint("2027-07-12T09:00:00.000000000+03:00", service_date)
        == expected
    )
    assert parse_timed_endpoint("2027-07-12T06:00:00Z", service_date) == expected

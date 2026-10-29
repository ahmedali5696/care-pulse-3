"""B-07 interval and peak occupancy regressions."""

from datetime import UTC, datetime, timedelta, timezone

import pytest
from carepulse.policies.intervals import Interval, intersects, peak_occupancy


def _at(hour: int, minute: int = 0) -> datetime:
    return datetime(2026, 11, 2, hour, minute, tzinfo=UTC)


def test_touching_endpoints_do_not_overlap():
    first = Interval(_at(9), _at(9, 30))
    adjacent = Interval(_at(9, 30), _at(10))
    overlapping = Interval(_at(9, 29), _at(10))

    assert not intersects(first, adjacent)
    assert not intersects(adjacent, first)
    assert intersects(first, overlapping)


def test_peak_sweep_counts_simultaneous_not_pairwise_intersections():
    # an added 09:00–10:00 interval can coexist with separate 09:00–09:30 and 09:30–10:00 reservations: peak is two, not three
    intervals = [
        Interval(_at(9), _at(10)),
        Interval(_at(9), _at(9, 30)),
        Interval(_at(9, 30), _at(10)),
    ]

    assert peak_occupancy(intervals) == 2
    assert peak_occupancy(reversed(intervals)) == 2
    assert peak_occupancy([]) == 0


@pytest.mark.parametrize(
    ("start", "end"),
    [
        (_at(9), _at(9)),
        (_at(10), _at(9)),
        (_at(9).replace(tzinfo=None), _at(10)),
        (_at(9), _at(10).replace(tzinfo=None)),
        (_at(9).astimezone(timezone(timedelta(hours=3))), _at(10)),
    ],
)
def test_invalid_or_non_utc_intervals_are_rejected(start, end):
    with pytest.raises(ValueError):
        Interval(start, end)

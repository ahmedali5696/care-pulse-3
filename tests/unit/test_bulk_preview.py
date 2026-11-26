"""B-19 joint-capacity decisions use actual half-open occupancy segments."""

from datetime import UTC, datetime, timedelta

from carepulse.appointments.bulk_preview import joint_capacity_conflicts
from carepulse.policies.intervals import Interval


def _interval(start: int, end: int) -> Interval:
    beginning = datetime(2026, 12, 1, 9, tzinfo=UTC)
    return Interval(
        beginning + timedelta(minutes=start), beginning + timedelta(minutes=end)
    )


def test_joint_capacity_excludes_every_candidate_in_an_oversubscribed_segment():
    occupied = [_interval(0, 60)]
    candidates = {"a": _interval(0, 60), "b": _interval(30, 90)}
    assert joint_capacity_conflicts(occupied, candidates, 2) == {"a", "b"}
    assert joint_capacity_conflicts(occupied, dict(reversed(list(candidates.items()))), 2) == {
        "a",
        "b",
    }


def test_joint_capacity_preserves_touching_endpoints_and_capacity_two():
    occupied = [_interval(0, 30)]
    candidates = {"a": _interval(0, 60), "b": _interval(30, 60)}
    assert joint_capacity_conflicts(occupied, candidates, 2) == set()
    assert joint_capacity_conflicts(occupied, {"a": _interval(0, 30), "b": _interval(30, 60)}, 1) == {
        "a"
    }


def test_preexisting_conflict_does_not_exclude_unrelated_candidate():
    occupied = [_interval(0, 60), _interval(0, 60), _interval(0, 60)]
    assert joint_capacity_conflicts(occupied, {"unrelated": _interval(60, 90)}, 2) == set()

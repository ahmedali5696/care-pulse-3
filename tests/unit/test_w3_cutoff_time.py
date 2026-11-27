"""Selected-day cutoff resolution at Cairo clock transitions."""

from datetime import UTC, date, datetime, time, timedelta

import pytest
from carepulse.appointments.cutoff import cutoff_due, resolve_cutoff
from carepulse.policies.schedule import NoShowCutoff


@pytest.mark.parametrize(
    ("day", "clock", "expected"),
    [
        (date(2026, 4, 24), time(0, 30), datetime(2026, 4, 23, 22, tzinfo=UTC)),
        (date(2026, 10, 29), time(23, 30), datetime(2026, 10, 29, 21, 30, tzinfo=UTC)),
        (date(2026, 11, 2), time(9), datetime(2026, 11, 2, 7, tzinfo=UTC)),
    ],
)
def test_cutoff_uses_first_valid_gap_or_later_fold(day, clock, expected):
    policy = NoShowCutoff(enabled=True, local_time=clock)
    assert resolve_cutoff(day, policy) == expected
    assert not cutoff_due(day, policy, expected - timedelta(microseconds=1))
    assert cutoff_due(day, policy, expected)


@pytest.mark.parametrize("policy", [NoShowCutoff(), NoShowCutoff(True, None)])
def test_disabled_or_missing_cutoff_never_becomes_due(policy):
    assert resolve_cutoff(date(2026, 11, 2), policy) is None
    assert not cutoff_due(date(2026, 11, 2), policy, datetime(2026, 11, 3, tzinfo=UTC))


def test_naive_comparison_clock_is_rejected():
    with pytest.raises(ValueError, match="aware instant"):
        cutoff_due(date(2026, 11, 2), NoShowCutoff(), datetime(2026, 11, 2))

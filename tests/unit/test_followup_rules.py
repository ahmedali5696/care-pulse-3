"""B-10 Doctor deadlines use Cairo calendar days, not elapsed hours."""

from datetime import UTC, date, datetime, timedelta

import pytest
from carepulse.appointments.followup_rules import (
    cairo_completion_date,
    default_deadline,
    extension_must_not_shorten,
    intended_date_allowed,
    validate_exception_deadline,
)

COMPLETION = date(2026, 11, 1)


@pytest.mark.parametrize(
    ("intended", "allowed"),
    [
        (date(2026, 10, 31), False),
        (date(2026, 11, 1), True),
        (date(2026, 11, 8), True),
        (date(2026, 11, 9), False),
    ],
)
def test_default_followup_window_is_inclusive_calendar_days(intended, allowed):
    assert default_deadline(COMPLETION) == date(2026, 11, 8)
    assert (
        intended_date_allowed(COMPLETION, intended, default_deadline(COMPLETION))
        is allowed
    )


@pytest.mark.parametrize(
    "deadline",
    [
        COMPLETION,
        COMPLETION + timedelta(days=3),
        COMPLETION + timedelta(days=8),
        COMPLETION + timedelta(days=3650),
    ],
)
def test_justified_doctor_deadline_can_shorten_or_extend_without_horizon(deadline):
    assert (
        validate_exception_deadline(COMPLETION, deadline, "Clinical reason") == deadline
    )


@pytest.mark.parametrize(
    ("deadline", "reason"),
    [(date(2026, 10, 31), "Clinical reason"), (COMPLETION, "  ")],
)
def test_invalid_doctor_deadline_or_blank_reason_rejected(deadline, reason):
    with pytest.raises(ValueError):
        validate_exception_deadline(COMPLETION, deadline, reason)


def test_extension_cannot_shorten_confirmed_deadline():
    assert extension_must_not_shorten(COMPLETION, COMPLETION) == COMPLETION
    assert extension_must_not_shorten(COMPLETION, date(2026, 11, 10)) == date(
        2026, 11, 10
    )
    with pytest.raises(ValueError):
        extension_must_not_shorten(date(2026, 11, 8), date(2026, 11, 7))


def test_cairo_completion_day_uses_history_instant_after_midnight():
    original_service_day = date(2026, 10, 31)
    completed_at = datetime(2026, 10, 31, 22, 30, tzinfo=UTC)
    assert original_service_day != cairo_completion_date(completed_at)
    assert cairo_completion_date(completed_at) == COMPLETION
    assert default_deadline(cairo_completion_date(completed_at)) == date(2026, 11, 8)


def test_naive_completion_instant_is_not_assumed_to_be_cairo_time():
    with pytest.raises(ValueError):
        cairo_completion_date(datetime(2026, 11, 1, 0, 30))

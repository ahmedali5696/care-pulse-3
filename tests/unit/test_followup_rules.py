"""B-10 Doctor deadlines use Cairo calendar days, not elapsed hours."""

from datetime import UTC, date, datetime, timedelta
from uuid import uuid4

import pytest
from carepulse.appointments.booking_contracts import BookAppointment
from carepulse.appointments.followup_rules import (
    cairo_completion_date,
    default_deadline,
    extension_must_not_shorten,
    intended_date_allowed,
    validate_exception_deadline,
)
from carepulse.appointments.followups import _approved_deadline
from pydantic import ValidationError

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
    assert default_deadline(COMPLETION, 7) == date(2026, 11, 8)
    assert (
        intended_date_allowed(COMPLETION, intended, default_deadline(COMPLETION, 7))
        is allowed
    )


@pytest.mark.parametrize("last_day_inclusive", [0, 10, 365])
def test_default_deadline_uses_configured_inclusive_window(last_day_inclusive):
    deadline = COMPLETION + timedelta(days=last_day_inclusive)
    assert default_deadline(COMPLETION, last_day_inclusive) == deadline
    assert intended_date_allowed(COMPLETION, deadline, deadline)
    assert not intended_date_allowed(COMPLETION, deadline + timedelta(days=1), deadline)


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
    assert default_deadline(cairo_completion_date(completed_at), 7) == date(2026, 11, 8)


def test_naive_completion_instant_is_not_assumed_to_be_cairo_time():
    with pytest.raises(ValueError):
        cairo_completion_date(datetime(2026, 11, 1, 0, 30))


@pytest.mark.parametrize("days", [0, 4, 10])
def test_missing_source_decision_uses_current_followup_policy(days):
    expected = COMPLETION + timedelta(days=days)
    assert _approved_deadline(COMPLETION, None, days) == expected
    assert _approved_deadline(COMPLETION, {"kind": "DEFAULT"}, days) == expected


def _followup_request(**changes):
    return {
        "patient_id": str(uuid4()),
        "service_date": "2026-11-10",
        "requested_service_type": "FOLLOW_UP",
        "source_visit_id": str(uuid4()),
        "discount": {"discount_type": "NONE", "discount_value": "0"},
        "expected_base_egp": "30.00",
        "expected_net_egp": "30.00",
        "expected_price_revision": 2,
        "expected_followup_revision": 1,
        "expected_schedule_revision": 2,
        **changes,
    }


def test_signed_followup_revision_families_remain_distinct():
    original = BookAppointment.model_validate(
        _followup_request(expected_source_revision=1)
    )
    successor = BookAppointment.model_validate(
        _followup_request(
            expected_source_decision_revision=0,
            expected_visit_revision=1,
        )
    )
    assert original.source_family == "v1"
    assert successor.source_family == "v2"
    assert original.model_dump(mode="json", exclude_unset=True) != successor.model_dump(
        mode="json", exclude_unset=True
    )


@pytest.mark.parametrize(
    "invalid",
    [
        {"expected_source_decision_revision": 0},
        {"expected_visit_revision": 0},
        {
            "expected_source_revision": 1,
            "expected_source_decision_revision": 0,
            "expected_visit_revision": 1,
        },
        {
            "expected_source_revision": None,
            "expected_source_decision_revision": 0,
            "expected_visit_revision": 1,
        },
        {
            "expected_source_decision_revision": 0,
            "expected_visit_revision": 1,
            "new_booking_date_exception": None,
        },
        {
            "expected_source_decision_revision": 0,
            "expected_visit_revision": 1,
            "new_booking_date_exception": {"selected_service_date": "2026-11-11"},
        },
        {
            "expected_source_decision_revision": 0,
            "expected_visit_revision": 1,
            "new_booking_date_exception": {
                "selected_service_date": "2026-11-10",
                "reason": "   ",
            },
        },
    ],
)
def test_incomplete_hybrid_or_implicit_new_date_declarations_are_invalid(invalid):
    with pytest.raises(ValidationError):
        BookAppointment.model_validate(_followup_request(**invalid))

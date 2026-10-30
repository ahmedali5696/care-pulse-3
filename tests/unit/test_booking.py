"""B-09 booking intent and no-authority projection boundaries."""

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from uuid import uuid4
from zoneinfo import ZoneInfo

import pytest
from carepulse.appointments.book import validate_booking_date
from carepulse.appointments.booking_contracts import BookAppointment
from carepulse.appointments.queries import _projection
from carepulse.identity.contracts import ContractError
from pydantic import ValidationError

CAIRO = ZoneInfo("Africa/Cairo")


def _valid_command():
    return {
        "patient_id": str(uuid4()),
        "service_date": "2026-11-01",
        "requested_service_type": "NEW_EXAMINATION",
        "discount": {"discount_type": "NONE", "discount_value": "0"},
        "expected_base_egp": "100.00",
        "expected_net_egp": "100.00",
        "expected_price_revision": 1,
        "expected_schedule_revision": 1,
    }


@pytest.mark.parametrize(
    "invalid_fields",
    [
        {"status": "Waiting"},
        {"lane": "EMERGENCY"},
        {"doctor_staff_id": str(uuid4())},
        {"organization_id": str(uuid4())},
        {"number": 200},
        {"price_snapshot": {"net_egp": "0.00"}},
        {"expected_base_egp": 100.0},
        {"expected_net_egp": 100},
        {"expected_source_revision": True},
        {"expected_schedule_revision": -1},
        {"requested_service_type": "FOLLOW_UP"},
        {"source_visit_id": str(uuid4())},
        {"starts_at": "2026-11-01T09:00:00+02:00"},
    ],
)
def test_forbidden_authority_and_unqualified_booking_intents_rejected(invalid_fields):
    with pytest.raises(ValidationError):
        BookAppointment.model_validate({**_valid_command(), **invalid_fields})


def test_untimed_and_complete_timed_intents_have_distinct_fields():
    open_booking = BookAppointment.model_validate(_valid_command())
    assert open_booking.starts_at is open_booking.ends_at is None
    timed = BookAppointment.model_validate(
        {
            **_valid_command(),
            "starts_at": "2026-11-01T09:00:00+02:00",
            "ends_at": "2026-11-01T09:30:00+02:00",
        }
    )
    assert timed.starts_at is not None and timed.ends_at is not None


def test_booking_date_has_no_arbitrary_future_horizon():
    now = datetime(2026, 10, 30, 10, 0, tzinfo=UTC)
    today = now.astimezone(CAIRO).date()
    with pytest.raises(ContractError) as invalid:
        validate_booking_date(today - timedelta(days=1), now)
    assert invalid.value.status_code == 409
    for day in (today, today + timedelta(days=366), today + timedelta(days=3650)):
        validate_booking_date(day, now)


def test_operational_projection_excludes_clinical_visit_content_and_preserves_percent():
    row = {
        "id": uuid4(),
        "patient_id": uuid4(),
        "doctor_staff_id": uuid4(),
        "service_date": date(2026, 11, 1),
        "status": "Booked",
        "service_type": "NEW_EXAMINATION",
        "classification": "ROUTINE",
        "lane": "MAIN",
        "booking_mode": "OPEN",
        "starts_at": None,
        "ends_at": None,
        "current_number": 8,
        "revision": 0,
        "base_egp": Decimal("100.00"),
        "discount_type": "PERCENT",
        "discount_value": Decimal("12.345"),
        "discount_reason": "Authorized",
        "is_free": False,
        "net_egp": Decimal("87.66"),
        "pricing_version": 2,
        "creation_standard_base_egp": Decimal("100.00"),
    }
    projection = _projection(row)
    assert projection["discount_value"] == "12.345"
    assert projection["net_egp"] == "87.66"
    assert projection["number"] == 8
    assert set(projection) == set(row) - {"current_number"} | {"number"}
    assert "visit" not in projection and "diagnosis" not in projection

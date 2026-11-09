"""B-09 booking intent and no-authority projection boundaries."""

import asyncio
import hashlib
import json
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from urllib.parse import urlencode
from uuid import uuid4
from zoneinfo import ZoneInfo

import pytest
from carepulse.appointments.book import validate_booking_date
from carepulse.appointments.booking_contracts import AppointmentQuery, BookAppointment
from carepulse.appointments.queries import _projection
from carepulse.appointments.routes import router as appointment_router
from carepulse.identity.contracts import (
    ContractError,
    contract_exception_handler,
    error_response,
    normalized_request_digest,
)
from fastapi import FastAPI
from fastapi.exceptions import RequestValidationError
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


@pytest.mark.parametrize("numeric", [1, 1.0])
def test_fallback_reaffirmation_requires_json_boolean_true(numeric):
    body = {
        **_valid_command(),
        "requested_service_type": "FOLLOW_UP",
        "expected_followup_revision": 1,
        "fallback_acknowledgment": {
            "service_type": "NEW_EXAMINATION",
            "discount_reaffirmed": numeric,
        },
    }
    with pytest.raises(ValidationError):
        BookAppointment.model_validate_json(json.dumps(body))
    body["fallback_acknowledgment"]["discount_reaffirmed"] = True
    assert (
        BookAppointment.model_validate_json(json.dumps(body)).source_family == "common"
    )


@pytest.mark.parametrize(
    "source_tokens",
    [
        {"expected_source_revision": 0, "expected_source_decision_revision": None},
        {"expected_source_revision": 0, "expected_visit_revision": None},
        {"expected_source_revision": 0, "new_booking_date_exception": None},
        {
            "expected_source_decision_revision": 0,
            "expected_visit_revision": 0,
            "expected_source_revision": None,
        },
    ],
)
def test_referenced_source_rejects_null_foreign_family_keys(source_tokens):
    body = {
        **_valid_command(),
        "requested_service_type": "FOLLOW_UP",
        "source_visit_id": str(uuid4()),
        "expected_followup_revision": 1,
        **source_tokens,
    }
    with pytest.raises(ValidationError):
        BookAppointment.model_validate(body)


def test_common_source_null_tokens_preserve_original_request_digests():
    original = {
        **_valid_command(),
        "requested_service_type": "FOLLOW_UP",
        "expected_followup_revision": 1,
        "fallback_acknowledgment": {
            "service_type": "NEW_EXAMINATION",
            "discount_reaffirmed": True,
        },
    }
    explicit_null = {
        **original,
        "source_visit_id": None,
        "expected_source_revision": None,
    }
    v2_null = {
        **original,
        "source_visit_id": None,
        "expected_source_decision_revision": None,
        "expected_visit_revision": None,
    }
    for body in (original, explicit_null, v2_null):
        command = BookAppointment.model_validate(body)
        assert command.source_family == "common"
        expected = hashlib.sha256(
            json.dumps(body, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
        assert normalized_request_digest(command) == expected
    assert normalized_request_digest(BookAppointment.model_validate(original)) != (
        normalized_request_digest(BookAppointment.model_validate(explicit_null))
    )


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


@pytest.mark.parametrize(
    "invalid_date",
    [
        "2026-11-01T00:00:00",
        "2026-11-01T00:00:00+02:00",
        1793491200,
        "1793491200",
        "2026-11-1",
        "2026-02-30",
        date(2026, 11, 1),
    ],
)
def test_booking_and_appointment_query_reject_noncanonical_dates(invalid_date):
    with pytest.raises(ValidationError):
        BookAppointment.model_validate(
            {**_valid_command(), "service_date": invalid_date}
        )
    with pytest.raises(ValidationError):
        AppointmentQuery.model_validate({"service_date": invalid_date})


@pytest.mark.parametrize(
    "invalid_date",
    ["2026-11-01T00:00:00", "2026-11-01T00:00:00+02:00", 1793491200, "2026-11-1"],
)
def test_booking_and_appointment_query_reject_noncanonical_dates_with_http_422(
    invalid_date,
):
    app = FastAPI()
    app.include_router(appointment_router)
    app.add_exception_handler(ContractError, contract_exception_handler)
    app.add_exception_handler(
        RequestValidationError, lambda _request, _error: error_response(422)
    )

    def request(method, *, body=None, query=None):
        payload = json.dumps(body).encode() if body is not None else b""
        messages = []

        async def receive():
            return {"type": "http.request", "body": payload, "more_body": False}

        async def send(message):
            messages.append(message)

        asyncio.run(
            app(
                {
                    "type": "http",
                    "asgi": {"version": "3.0"},
                    "http_version": "1.1",
                    "scheme": "http",
                    "method": method,
                    "path": "/v1/appointments",
                    "raw_path": b"/v1/appointments",
                    "query_string": urlencode(query or {}).encode(),
                    "root_path": "",
                    "headers": [(b"content-type", b"application/json")],
                    "client": ("synthetic", 1),
                    "server": ("api", 8000),
                },
                receive,
                send,
            )
        )
        return messages[0]["status"], json.loads(
            b"".join(message.get("body", b"") for message in messages[1:])
        )

    for status, response in (
        request("POST", body={**_valid_command(), "service_date": invalid_date}),
        request("GET", query={"service_date": str(invalid_date)}),
    ):
        assert status == 422
        assert response["error"]["code"] == "INVALID_INPUT"


def test_booking_and_appointment_query_parse_canonical_date():
    day = date(2026, 11, 1)
    assert BookAppointment.model_validate(_valid_command()).service_date == day
    assert (
        AppointmentQuery.model_validate({"service_date": day.isoformat()}).service_date
        == day
    )


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

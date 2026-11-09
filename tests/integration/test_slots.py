"""B-08 signed slots reads on disposable PostgreSQL."""

import asyncio
import json
from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
from hashlib import sha256
from hmac import new
from urllib.parse import urlencode
from uuid import uuid4

import pytest
from carepulse.db import transaction
from carepulse.identity.signing import canonical_payload, canonical_query
from carepulse.main import app
from sqlalchemy import text
from test_booking import (
    booking_body,
    booking_effects,
    booking_request,
    new_patient,
)
from test_booking import (
    booking_clinic as booking_clinic,  # noqa: PLC0414
)
from test_patients import patient_backend as patient_backend  # noqa: PLC0414
from test_request_boundary import boundary_data as boundary_data  # noqa: PLC0414
from test_schedule import SERVICE_DAY, _actor, _booking, _config, _publish


def _slots_request(clinic, actor, day, *, other_fields=None):
    query = {
        "service_date": day.isoformat() if isinstance(day, date) else day,
        **(other_fields or {}),
    }
    envelope = replace(
        actor,
        method="GET",
        path="/v1/slots",
        query=canonical_query(urlencode(query)),
        body_sha256=sha256(b"").hexdigest(),
        purpose="SLOTS",
        timestamp=str(int(datetime.now(UTC).timestamp() * 1000)),
        nonce=f"synthetic_{uuid4().hex}",
        idempotency_key="",
    )
    fields = (
        "protocol_version",
        "key_id",
        "service",
        "organization_id",
        "staff_id",
        "session_id",
        "role",
        "security_revision",
        "timestamp",
        "nonce",
        "purpose",
        "idempotency_key",
    )
    headers = [
        (
            f"x-carepulse-{field.replace('_', '-')}".encode(),
            str(getattr(envelope, field)).encode(),
        )
        for field in fields
    ] + [
        (b"x-carepulse-service-key", clinic.config.service_key.encode()),
        (
            b"x-carepulse-signature",
            new(clinic.config.service_key.encode(), canonical_payload(envelope), sha256)
            .hexdigest()
            .encode(),
        ),
    ]
    scope = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "scheme": "http",
        "method": "GET",
        "path": "/v1/slots",
        "raw_path": b"/v1/slots",
        "query_string": envelope.query.encode(),
        "headers": headers,
        "client": ("synthetic", 1),
        "server": ("api", 8000),
    }
    outgoing = []

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message):
        outgoing.append(message)

    asyncio.run(app(scope, receive, send))
    response = b"".join(message.get("body", b"") for message in outgoing[1:])
    return outgoing[0]["status"], json.loads(response)


def test_both_roles_see_occupied_candidates_without_settings_or_reservation(
    patient_backend,
):
    clinic = patient_backend
    app.state.service_config, app.state.db_engine = clinic.config, clinic.runtime
    with transaction(clinic.runtime, clinic.tenant) as connection:
        _publish(connection, _actor(clinic.tenant, clinic.staff), _config())
    with transaction(clinic.runtime, clinic.tenant) as connection:
        booked = _booking(connection, clinic.tenant, clinic.staff, start=9, end=10)
    for actor in (clinic.owner, clinic.receptionist):
        status, response = _slots_request(clinic, actor, SERVICE_DAY)
        assert status == 200
        slots = response["data"]
        assert slots["booking_mode"] == "EXACT_TIME"
        assert slots["schedule_revision"] == 2
        assert "capacity_revision" not in slots
        assert slots["open_eligible"] is False
        assert len(slots["candidates"]) == 6
        assert [slot["available"] for slot in slots["candidates"]] == [
            False,
            False,
            True,
            True,
            True,
            True,
        ]
        assert set(slots) == {
            "service_date",
            "booking_mode",
            "open_eligible",
            "candidates",
            "schedule_revision",
        }
        assert all(
            set(slot) == {"starts_at", "ends_at", "origin", "available"}
            for slot in slots["candidates"]
        )
    with transaction(clinic.runtime, clinic.tenant) as connection:
        assert connection.scalar(text("SELECT count(*) FROM public.appointments")) == 1
        assert (
            connection.scalar(
                text("SELECT status FROM public.appointments WHERE id=:id"),
                {"id": booked["id"]},
            )
            == "Booked"
        )


def test_open_mode_long_horizon_and_unknown_query_are_handled(patient_backend):
    clinic = patient_backend
    app.state.service_config, app.state.db_engine = clinic.config, clinic.runtime
    with transaction(clinic.runtime, clinic.tenant) as connection:
        _publish(connection, _actor(clinic.tenant, clinic.staff), _config(mode="OPEN"))
    status, response = _slots_request(clinic, clinic.receptionist, SERVICE_DAY)
    assert status == 200 and response["data"]["open_eligible"] is True
    assert response["data"]["candidates"] == []
    status, response = _slots_request(
        clinic, clinic.owner, SERVICE_DAY - timedelta(days=56)
    )
    assert status == 200 and response["data"]["open_eligible"] is False
    for distance in (366, 3650):
        status, response = _slots_request(
            clinic, clinic.owner, SERVICE_DAY + timedelta(days=distance)
        )
        assert status == 200
        assert (
            response["data"]["service_date"]
            == (SERVICE_DAY + timedelta(days=distance)).isoformat()
        )
    status, _ = _slots_request(
        clinic, clinic.owner, SERVICE_DAY, other_fields={"tenant_id": "forbidden"}
    )
    assert status == 422


def test_foreign_tenant_identity_cannot_read_slots(patient_backend):
    clinic = patient_backend
    app.state.service_config, app.state.db_engine = clinic.config, clinic.runtime
    forged = replace(clinic.owner, organization_id=str(clinic.other))
    status, _ = _slots_request(clinic, forged, SERVICE_DAY)
    assert status in (401, 403, 404)


@pytest.mark.parametrize(
    "raw_date",
    [
        "1793577600",
        "1793577600000",
        "2026-11-02T00:00:00Z",
        "2026-11-02T00:00:00+02:00",
        "2026-11-02 00:00:00",
        "20261102",
        "2026-11-2",
        "2026-W45-1",
        "2026-11-02Z",
        "2026-02-30",
        " 2026-11-02",
    ],
)
def test_signed_slots_rejects_noncanonical_service_dates(patient_backend, raw_date):
    clinic = patient_backend
    app.state.service_config, app.state.db_engine = clinic.config, clinic.runtime
    status, response = _slots_request(clinic, clinic.owner, raw_date)
    assert status == 422
    assert response["error"]["code"] == "INVALID_INPUT"


def test_fold_break_excludes_mixed_manual_interval_from_preview_and_confirmation(
    booking_clinic,
):
    clinic = booking_clinic
    fold_day = date(2030, 10, 31)
    weekday = fold_day.isoweekday()
    config = _config(
        duration_minutes=None,
        working_weekdays=[weekday],
        weekly_work_periods=[
            {"weekday": weekday, "starts_at": "22:00", "ends_at": "23:59"}
        ],
        weekly_breaks=[{"weekday": weekday, "starts_at": "23:20", "ends_at": "23:40"}],
        manual_slots_by_date={
            fold_day.isoformat(): [{"starts_at": "23:45", "ends_at": "23:55"}]
        },
    )
    with transaction(clinic.runtime, clinic.tenant) as connection:
        _publish(
            connection,
            _actor(clinic.tenant, clinic.staff),
            config,
            revision=2,
        )
    status, response = _slots_request(clinic, clinic.owner, fold_day)
    assert status == 200, response
    offered = response["data"]["candidates"]
    assert len(offered) == 2
    assert all(slot["available"] and slot["origin"] == "MANUAL" for slot in offered)
    starts = [datetime.fromisoformat(slot["starts_at"]) for slot in offered]
    ends = [datetime.fromisoformat(slot["ends_at"]) for slot in offered]
    assert len(set(starts)) == 2
    assert all(end - start == timedelta(minutes=10) for start, end in zip(starts, ends))

    before = booking_effects(clinic)
    status, denied = booking_request(
        clinic,
        clinic.owner,
        body=booking_body(
            new_patient(clinic),
            day=fold_day,
            expected_schedule_revision=3,
            starts_at=min(starts).isoformat(),
            ends_at=max(ends).isoformat(),
        ),
        key=str(uuid4()),
    )
    assert status == 409, denied
    assert booking_effects(clinic) == before

    for slot in offered:
        status, confirmed = booking_request(
            clinic,
            clinic.owner,
            body=booking_body(
                new_patient(clinic),
                day=fold_day,
                expected_schedule_revision=3,
                starts_at=slot["starts_at"],
                ends_at=slot["ends_at"],
            ),
            key=str(uuid4()),
        )
        assert status == 201, confirmed

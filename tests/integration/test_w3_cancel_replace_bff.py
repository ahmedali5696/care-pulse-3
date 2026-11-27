"""B-17 authenticated browser routes, signed authority and committed evidence."""

from datetime import datetime
from uuid import uuid4

from test_booking import booking_body
from test_patients import PATIENT, browser_request
from test_patients import invited_patients as invited_patients  # noqa: PLC0414
from test_pricing import _patch as pricing_patch
from test_schedule import CAIRO, _config
from test_schedule import _patch as schedule_patch
from test_sessions import _database
from test_w3_bff import _denied
from test_w3_visit_bff import _proofs


def _state(tenant):
    with _database() as connection:
        return {
            table: [
                row[0]
                for row in connection.execute(
                    f"SELECT to_jsonb(r) FROM public.{table} r WHERE organization_id=%s ORDER BY to_jsonb(r)::text",
                    (tenant,),
                ).fetchall()
            ]
            for table in (
                "appointments",
                "appointment_day_counters",
                "appointment_number_assignments",
                "appointment_price_snapshots",
                "appointment_history",
                "queue_days",
                "queue_entries",
                "followup_claims",
                "visits",
                "sensitive_audit",
                "outbox_events",
                "idempotency_receipts",
            )
        }


def _configure(owner):
    config = _config(
        mode="OPEN",
        working_weekdays=list(range(1, 8)),
        weekly_work_periods=[
            {"weekday": day, "starts_at": "00:00", "ends_at": "23:59"}
            for day in range(1, 8)
        ],
    )
    status, preview = browser_request(
        owner,
        "/api/settings/schedule/preview",
        method="POST",
        body=schedule_patch(config).model_dump(mode="json"),
        key=str(uuid4()),
    )
    assert status == 200, preview
    plan = preview["data"]
    status, published = browser_request(
        owner,
        "/api/settings/schedule/activate",
        method="POST",
        body={
            "preview_id": plan["preview_id"],
            "expected_schedule_revision": plan["revisions"]["schedule"],
            "expected_capacity_revision": plan["revisions"]["capacity"],
            "reason": "Synthetic calendar",
        },
        key=str(uuid4()),
    )
    assert status == 200, published
    status, pricing = browser_request(
        owner,
        "/api/settings/pricing",
        method="PATCH",
        body=pricing_patch(
            standard_new_exam_egp="100.00",
            urgent_base_egp="150.00",
            urgent_enabled=True,
        ),
        key=str(uuid4()),
    )
    assert status == 200, pricing
    return pricing["revision"], published["revision"]


def _book(staff, name, price_revision, schedule_revision):
    status, patient = browser_request(
        staff,
        method="POST",
        body={**PATIENT, "name_parts": ["Synthetic", "B17", name]},
        key=str(uuid4()),
    )
    assert status == 201, patient
    status, booked = browser_request(
        staff,
        "/api/appointments",
        method="POST",
        body=booking_body(
            patient["data"]["id"],
            day=datetime.now(CAIRO).date(),
            expected_price_revision=price_revision,
            expected_schedule_revision=schedule_revision,
        ),
        key=str(uuid4()),
    )
    assert status == 201, booked
    return booked["data"]["id"]


def test_browser_cancel_replace_bound_purpose_recovery_and_actor(invited_patients):
    sessions = invited_patients
    owner, staff = sessions.owner, sessions.receptionist
    price_revision, schedule_revision = _configure(owner)
    original = _book(staff, "Replace", price_revision, schedule_revision)
    other = _book(staff, "Cancel", price_revision, schedule_revision)
    path = f"/api/appointments/{original}/replace-urgent"
    key, cancel_key = str(uuid4()), str(uuid4())
    ordinary = {"expected_appointment_revision": 0, "reason": "Synthetic cancellation"}
    command = {
        **ordinary,
        "confirmation": "REPLACE",
        "physical_presence_confirmed": True,
        "discount": {
            "discount_type": "NONE",
            "discount_value": "0",
            "discount_reason": None,
        },
        "expected_base_egp": "150.00",
        "expected_net_egp": "150.00",
        "expected_price_revision": price_revision,
        "expected_schedule_revision": schedule_revision,
    }
    before = _state(sessions.clinic)
    for cookie, payload, code, reason in (
        (None, command, 401, "UNAUTHENTICATED"),
        (owner, command, 403, "FORBIDDEN"),
        (sessions.other, command, 403, "FORBIDDEN"),
        (staff, {**command, "confirmation": "YES"}, 422, "INVALID_REQUEST"),
    ):
        response = browser_request(cookie, path, method="POST", body=payload, key=key)
        if code == 422:
            assert response[0] == 422 and set(response[1]) == {"error"}
        else:
            _denied(response, code, reason)
    assert _state(sessions.clinic) == before
    _denied(
        browser_request(
            sessions.other,
            f"/api/appointments/{other}/cancel",
            method="POST",
            body=ordinary,
            key=cancel_key,
        ),
        404,
        "NOT_FOUND",
    )

    status, created = browser_request(staff, path, method="POST", body=command, key=key)
    assert status == 201, created
    projection = created["data"]
    new = projection["replacement_appointment_id"]
    assert new != original
    assert projection["status"] == "Cancelled" and projection["new_status"] == "Waiting"
    committed = _state(sessions.clinic)
    assert len(committed["appointments"]) == len(before["appointments"]) + 1
    assert {row["id"]: row["status"] for row in committed["appointments"]} == {
        original: "Cancelled",
        other: "Booked",
        new: "Waiting",
    }
    assert not any(
        row["event_type"] == "DOCTOR_CANCELLED_APPOINTMENT"
        for row in committed["outbox_events"]
    )
    status, replayed = browser_request(
        staff, path, method="POST", body=command, key=key
    )
    assert status == 200 and replayed == created
    _denied(
        browser_request(
            staff, path, method="POST", body={**command, "reason": "Changed"}, key=key
        ),
        409,
        "IDEMPOTENCY_CONFLICT",
    )
    _denied(
        browser_request(
            staff,
            f"/api/appointments/{other}/replace-urgent",
            method="POST",
            body=command,
            key=key,
        ),
        409,
        "IDEMPOTENCY_CONFLICT",
    )
    assert _state(sessions.clinic) == committed
    assert {purpose for purpose, _digest in _proofs(sessions.clinic, key)} == {
        "APPOINTMENT_REPLACE_URGENT"
    }

    status, cancelled = browser_request(
        owner,
        f"/api/appointments/{other}/cancel",
        method="POST",
        body=ordinary,
        key=cancel_key,
    )
    assert status == 200, cancelled
    assert cancelled["data"]["status"] == "Cancelled"
    final = _state(sessions.clinic)
    review = [
        row
        for row in final["outbox_events"]
        if row["event_type"] == "DOCTOR_CANCELLED_APPOINTMENT"
    ]
    assert len(review) == 1
    assert {purpose for purpose, _digest in _proofs(sessions.clinic, cancel_key)} == {
        "APPOINTMENT_CANCEL"
    }
    assert len(final["appointment_number_assignments"]) == len(
        committed["appointment_number_assignments"]
    )
    assert final["visits"] == []

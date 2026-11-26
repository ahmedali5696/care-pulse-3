"""B-18 browser command, closed signed purpose and same-ID receipt recovery."""

from datetime import datetime, timedelta
from uuid import uuid4

from test_patients import browser_request
from test_patients import invited_patients as invited_patients  # noqa: PLC0414
from test_schedule import CAIRO
from test_w3_bff import _denied
from test_w3_cancel_replace_bff import _book, _configure, _state
from test_w3_visit_bff import _proofs


def test_browser_reschedule_bound_actor_and_receipt(invited_patients):
    sessions = invited_patients
    status, identity = browser_request(sessions.owner, "/api/session")
    assert status == 200, identity
    price, schedule = _configure(sessions.owner)
    target = _book(sessions.receptionist, "Move", price, schedule)
    other = _book(sessions.receptionist, "Other", price, schedule)
    path = f"/api/appointments/{target}/reschedule"
    day = datetime.now(CAIRO).date() + timedelta(days=1)
    body = {
        "service_date": day.isoformat(),
        "reason": "Synthetic browser move",
        "expected_appointment_revision": 0,
        "expected_schedule_revision": schedule,
    }
    key = uuid4().hex
    before = _state(sessions.clinic)
    _denied(
        browser_request(None, path, method="POST", body=body, key=key),
        401,
        "UNAUTHENTICATED",
    )
    _denied(
        browser_request(sessions.other, path, method="POST", body=body, key=key),
        404,
        "NOT_FOUND",
    )
    assert (
        browser_request(
            sessions.owner,
            path,
            method="POST",
            body={**body, "actor_staff_id": str(uuid4())},
            key=key,
        )[0]
        == 422
    )
    assert _state(sessions.clinic) == before
    status, reply = browser_request(
        sessions.owner, path, method="POST", body=body, key=key
    )
    assert status == 200, reply
    assert reply["data"]["appointment_id"] == target and reply["revision"] == 1
    committed = _state(sessions.clinic)
    (history,) = [
        row
        for row in committed["appointment_history"]
        if row not in before["appointment_history"]
    ]
    assert history["actor_staff_id"] == identity["data"]["id"]
    assert history["actor_role"] == "DOCTOR_OWNER"
    assert history["appointment_id"] == target
    assert len(committed["appointments"]) == len(before["appointments"])
    assert (
        committed["appointment_price_snapshots"]
        == before["appointment_price_snapshots"]
    )
    assert browser_request(sessions.owner, path, method="POST", body=body, key=key) == (
        200,
        reply,
    )
    _denied(
        browser_request(
            sessions.owner,
            f"/api/appointments/{other}/reschedule",
            method="POST",
            body=body,
            key=key,
        ),
        409,
        "IDEMPOTENCY_CONFLICT",
    )
    assert _state(sessions.clinic) == committed
    assert {purpose for purpose, _ in _proofs(sessions.clinic, key)} == {
        "APPOINTMENT_RESCHEDULE"
    }
    review = [
        r
        for r in committed["outbox_events"]
        if r["event_type"] == "DOCTOR_RESCHEDULED_APPOINTMENT"
    ]
    assert len(review) == 1 and review[0]["aggregate_id"] == target

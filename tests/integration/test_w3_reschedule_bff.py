"""B-18 browser command, closed signed purpose and same-ID receipt recovery."""

from datetime import date, datetime, timedelta
from uuid import uuid4

from test_patients import browser_request
from test_patients import invited_patients as invited_patients  # noqa: PLC0414
from test_schedule import CAIRO
from test_w3_bff import _denied
from test_w3_cancel_replace_bff import _book, _configure, _state
from test_w3_visit_bff import _proofs


def _browser_bulk_reschedule(sessions, target, actor_id):
    before = _state(sessions.clinic)
    original = next(row for row in before["appointments"] if row["id"] == target)
    destination = (
        date.fromisoformat(original["service_date"]) + timedelta(days=2)
    ).isoformat()
    path = "/api/appointments/bulk-reschedule"
    preview_path, commit_path = f"{path}/preview", f"{path}/commit"
    preview_key, commit_key = uuid4().hex, uuid4().hex
    body = {
        "appointment_ids": [target],
        "original_service_date": original["service_date"],
        "destination_date": destination,
        "reason": "Synthetic browser bulk move",
    }
    _denied(browser_request(sessions.owner, preview_path), 403, "FORBIDDEN")
    _denied(
        browser_request(
            sessions.owner, f"{path}/cancel", method="POST", body=body, key=preview_key
        ),
        403,
        "FORBIDDEN",
    )
    _denied(
        browser_request(None, preview_path, method="POST", body=body, key=preview_key),
        401,
        "UNAUTHENTICATED",
    )
    _denied(
        browser_request(
            sessions.other, preview_path, method="POST", body=body, key=preview_key
        ),
        404,
        "NOT_FOUND",
    )
    assert _state(sessions.clinic) == before
    status, preview = browser_request(
        sessions.owner, preview_path, method="POST", body=body, key=preview_key
    )
    assert status == 200, preview
    snapshot = preview["data"]
    assert snapshot["eligible_count"] == 1 and snapshot["excluded_count"] == 0
    assert [row["appointment_id"] for row in snapshot["included"]] == [target]
    assert snapshot["excluded"] == []
    previewed = _state(sessions.clinic)
    assert {k: v for k, v in previewed.items() if k != "idempotency_receipts"} == {
        k: v for k, v in before.items() if k != "idempotency_receipts"
    }
    (receipt,) = [
        row
        for row in previewed["idempotency_receipts"]
        if row not in before["idempotency_receipts"]
    ]
    assert (
        receipt["id"],
        receipt["actor_id"],
        receipt["operation"],
        receipt["result_projection"],
    ) == (
        snapshot["preview_id"],
        actor_id,
        "appointments.bulk_reschedule.preview",
        snapshot,
    )
    assert browser_request(
        sessions.owner, preview_path, method="POST", body=body, key=preview_key
    ) == (200, preview)
    assert _state(sessions.clinic) == previewed
    command = {"preview_id": snapshot["preview_id"], "confirmation": "RESCHEDULE"}
    _denied(
        browser_request(
            sessions.receptionist,
            commit_path,
            method="POST",
            body=command,
            key=commit_key,
        ),
        404,
        "NOT_FOUND",
    )
    _denied(
        browser_request(
            sessions.owner,
            commit_path,
            method="POST",
            body={**command, "destination_date": destination},
            key=commit_key,
        ),
        422,
        "INVALID_INPUT",
    )
    assert _state(sessions.clinic) == previewed
    status, committed = browser_request(
        sessions.owner, commit_path, method="POST", body=command, key=commit_key
    )
    assert status == 200, committed
    assert [row["appointment_id"] for row in committed["data"]["moved"]] == [target]
    after = _state(sessions.clinic)
    assert len(after["appointments"]) == len(before["appointments"])
    moved = next(row for row in after["appointments"] if row["id"] == target)
    assert (
        moved["service_date"] == destination
        and moved["revision"] == original["revision"] + 1
    )
    assert after["appointment_price_snapshots"] == before["appointment_price_snapshots"]
    (history,) = [
        row
        for row in after["appointment_history"]
        if row not in before["appointment_history"]
    ]
    assert (
        history["appointment_id"],
        history["actor_staff_id"],
        history["actor_role"],
    ) == (target, actor_id, "DOCTOR_OWNER")
    (receipt,) = [
        row
        for row in after["idempotency_receipts"]
        if row not in previewed["idempotency_receipts"]
    ]
    assert (
        receipt["actor_id"],
        receipt["operation"],
        receipt["result_projection"],
    ) == (actor_id, "appointments.bulk_reschedule.commit", committed["data"])
    (review,) = [
        row
        for row in after["outbox_events"]
        if row not in before["outbox_events"]
        and row["event_type"] == "DOCTOR_RESCHEDULED_APPOINTMENT"
    ]
    assert review["aggregate_id"] == target
    assert browser_request(
        sessions.owner, commit_path, method="POST", body=command, key=commit_key
    ) == (200, committed)
    assert _state(sessions.clinic) == after
    assert {purpose for purpose, _ in _proofs(sessions.clinic, preview_key)} == {
        "APPOINTMENT_BULK_PREVIEW"
    }
    assert {purpose for purpose, _ in _proofs(sessions.clinic, commit_key)} == {
        "APPOINTMENT_BULK_COMMIT"
    }


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
    _browser_bulk_reschedule(sessions, other, identity["data"]["id"])

"""B-20 authenticated browser reconciliation and immutable effects."""

from datetime import datetime, timedelta
from types import SimpleNamespace
from uuid import UUID, uuid4

from sqlalchemy import create_engine
from test_patients import PATIENT, browser_request
from test_patients import invited_patients as invited_patients  # noqa: PLC0414
from test_schedule import CAIRO
from test_sessions import _database
from test_w3_bff import _denied
from test_w3_cancel_replace_bff import _book, _configure, _state
from test_w3_checkin import _seed_booked
from test_w3_visit_bff import _proofs


def _seed_prior(sessions, day):
    status, patient = browser_request(
        sessions.receptionist, method="POST", body=PATIENT, key=str(uuid4())
    )
    assert status == 201, patient
    status, actor = browser_request(sessions.owner, "/api/session")
    assert status == 200, actor

    def migration_connection():
        connection = _database()
        connection.commit()
        return connection

    engine = create_engine("postgresql+psycopg://", creator=migration_connection)
    try:
        clinic = SimpleNamespace(
            migration=engine,
            tenant=sessions.clinic,
            owner=SimpleNamespace(staff_id=actor["data"]["id"]),
        )
        return _seed_booked(clinic, UUID(patient["data"]["id"]), day)
    finally:
        engine.dispose()


def _day(sessions, day):
    status, reply = browser_request(sessions.owner, f"/api/reconciliation?date={day}")
    assert status == 200, reply
    return reply["data"]


def _review_state(tenant):
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
                "day_reviews",
                "day_review_history",
                "sensitive_audit",
                "idempotency_receipts",
            )
        }


def test_authenticated_manual_absence_is_scoped_and_replay_safe(invited_patients):
    sessions = invited_patients
    day = datetime.now(CAIRO).date() - timedelta(days=1)
    _configure(sessions.owner)
    target = _seed_prior(sessions, day)
    path = f"/api/appointments/{target['id']}/manual-no-show"
    key = str(uuid4())
    body = {
        "verified_absence": True,
        "reason": "Verified synthetic absence",
        "expected_appointment_revision": 0,
        "expected_claim_revision": None,
    }
    before = _state(sessions.clinic)
    _denied(
        browser_request(
            sessions.owner, "/api/days/cutoff", method="POST", body={}, key=key
        ),
        403,
        "FORBIDDEN",
    )
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
            sessions.receptionist,
            path,
            method="POST",
            body={**body, "verified_absence": False},
            key=key,
        )[0]
        == 422
    )
    assert _state(sessions.clinic) == before
    view = _day(sessions, day)
    entry = next(
        row for row in view["entries"] if row["appointment_id"] == target["id"]
    )
    assert entry["overdue"] and entry["requires_review"] and entry["status"] == "Booked"
    status, reply = browser_request(
        sessions.receptionist, path, method="POST", body=body, key=key
    )
    assert status == 200, reply
    assert reply["data"]["status"] == "No-show"
    after = _state(sessions.clinic)
    assert browser_request(
        sessions.receptionist, path, method="POST", body=body, key=key
    ) == (status, reply)
    assert (
        browser_request(
            sessions.receptionist,
            path,
            method="POST",
            body={**body, "reason": "Changed"},
            key=key,
        )[0]
        == 409
    )
    assert _state(sessions.clinic) == after
    row = next(row for row in after["appointments"] if row["id"] == target["id"])
    assert (row["status"], row["revision"]) == ("No-show", 1)
    for table in (
        "appointment_history",
        "sensitive_audit",
        "outbox_events",
        "idempotency_receipts",
    ):
        assert len(after[table]) == len(before[table]) + 1, table
    history = next(
        row
        for row in after["appointment_history"]
        if row not in before["appointment_history"]
    )
    audit = next(
        row for row in after["sensitive_audit"] if row not in before["sensitive_audit"]
    )
    event = next(
        row for row in after["outbox_events"] if row not in before["outbox_events"]
    )
    receipt = next(
        row
        for row in after["idempotency_receipts"]
        if row not in before["idempotency_receipts"]
    )
    actor = browser_request(sessions.receptionist, "/api/session")[1]["data"]["id"]
    assert (
        history["appointment_id"],
        history["actor_staff_id"],
        history["new_status"],
    ) == (target["id"], actor, "No-show")
    assert (audit["action"], audit["actor_staff_id"], audit["reason"]) == (
        "APPOINTMENT_MARKED_NO_SHOW",
        actor,
        body["reason"],
    )
    assert (event["event_type"], event["aggregate_id"]) == (
        "APPOINTMENT_MARKED_NO_SHOW",
        target["id"],
    )
    assert (
        receipt["operation"],
        receipt["idempotency_key"],
        receipt["result_projection"],
    ) == ("appointments.manual_no_show", key, reply["data"])
    for table in (
        "queue_days",
        "queue_entries",
        "visits",
        "followup_claims",
        "appointment_price_snapshots",
    ):
        assert after[table] == before[table], table
    assert {purpose for purpose, _ in _proofs(sessions.clinic, key)} == {
        "APPOINTMENT_MANUAL_NO_SHOW"
    }


def test_review_close_enforces_role_revisions_and_waiting(invited_patients):
    sessions = invited_patients
    price, schedule = _configure(sessions.owner)
    target = _book(sessions.receptionist, "Waiting", price, schedule)
    day = datetime.now(CAIRO).date().isoformat()
    queue = browser_request(sessions.owner, f"/api/queue?date={day}&lane=MAIN")[1]
    status, checked = browser_request(
        sessions.receptionist,
        f"/api/appointments/{target}/check-in",
        method="POST",
        body={
            "physical_presence_confirmed": True,
            "expected_appointment_revision": 0,
            "expected_queue_day_revision": queue["revision"],
        },
        key=str(uuid4()),
    )
    assert status == 200, checked
    view = _day(sessions, day)
    close = {
        "expected_review_revision": view["review_revision"],
        "expected_main_queue_day_revision": view["main_queue_day_revision"],
        "expected_emergency_queue_day_revision": view["emergency_queue_day_revision"],
        "notes": None,
        "reason": None,
    }
    before = _review_state(sessions.clinic)
    assert (
        browser_request(
            sessions.owner,
            f"/api/days/{day}/close",
            method="POST",
            body=close,
            key=str(uuid4()),
        )[0]
        == 409
    )
    assert _review_state(sessions.clinic) == before
    review = {
        "expected_review_revision": 0,
        "notes": "Synthetic review",
        "reason": None,
    }
    key = str(uuid4())
    status, reply = browser_request(
        sessions.receptionist,
        f"/api/days/{day}/review",
        method="POST",
        body=review,
        key=key,
    )
    assert status == 200, reply
    after = _review_state(sessions.clinic)
    assert browser_request(
        sessions.receptionist,
        f"/api/days/{day}/review",
        method="POST",
        body=review,
        key=key,
    ) == (status, reply)
    amendment = {
        "expected_review_revision": 1,
        "notes": "Amended",
        "reason": "Synthetic amendment",
    }
    assert browser_request(
        sessions.receptionist,
        f"/api/days/{day}/review",
        method="POST",
        body=amendment,
        key=str(uuid4()),
    )[0] in (403, 409)
    assert _review_state(sessions.clinic) == after
    owner_key = str(uuid4())
    status, amended = browser_request(
        sessions.owner,
        f"/api/days/{day}/review",
        method="POST",
        body=amendment,
        key=owner_key,
    )
    assert status == 200, amended
    assert amended["data"]["revision"] == 2
    assert {purpose for purpose, _ in _proofs(sessions.clinic, key)} == {"DAY_REVIEW"}
    assert {purpose for purpose, _ in _proofs(sessions.clinic, owner_key)} == {
        "DAY_REVIEW"
    }


def test_empty_day_close_replays_with_exact_review_evidence(invited_patients):
    sessions = invited_patients
    day = (datetime.now(CAIRO).date() - timedelta(days=2)).isoformat()
    view = _day(sessions, day)
    command = {
        "expected_review_revision": view["review_revision"],
        "expected_main_queue_day_revision": view["main_queue_day_revision"],
        "expected_emergency_queue_day_revision": view["emergency_queue_day_revision"],
        "notes": "Synthetic close",
        "reason": None,
    }
    path, key = f"/api/days/{day}/close", str(uuid4())
    before = _review_state(sessions.clinic)
    status, reply = browser_request(
        sessions.receptionist, path, method="POST", body=command, key=key
    )
    assert status == 200, reply
    assert reply["data"]["disposition"] == "FINAL_CLOSED"
    after = _review_state(sessions.clinic)
    for table in (
        "day_reviews",
        "day_review_history",
        "sensitive_audit",
        "idempotency_receipts",
    ):
        assert len(after[table]) == len(before[table]) + 1, table
    assert browser_request(
        sessions.receptionist, path, method="POST", body=command, key=key
    ) == (status, reply)
    assert _review_state(sessions.clinic) == after
    assert {purpose for purpose, _ in _proofs(sessions.clinic, key)} == {"DAY_CLOSE"}

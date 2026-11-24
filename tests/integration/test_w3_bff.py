"""B-12 real authenticated browser-to-BFF-to-API-to-PostgreSQL acceptance."""

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from threading import Barrier
from uuid import UUID, uuid4

from test_booking import booking_body
from test_patients import PATIENT, browser_request
from test_patients import invited_patients as invited_patients  # noqa: PLC0414
from test_pricing import _patch as pricing_patch
from test_schedule import SERVICE_DAY, _config
from test_schedule import _patch as schedule_patch
from test_sessions import _database


def _domain_state(tenant):
    """Observe committed domain and successful command effects, independent of HTTP."""
    tables = {
        "appointments": "",
        "appointment_day_counters": "",
        "appointment_number_assignments": "",
        "appointment_price_snapshots": "",
        "appointment_history": "",
        "queue_days": "",
        "queue_entries": "",
        "visits": "",
        "sensitive_audit": "AND action IN ('appointments.book', 'appointments.check_in')",
        "outbox_events": "AND event_type IN ('APPOINTMENT_BOOKED', 'PATIENT_CHECKED_IN')",
        "idempotency_receipts": "AND operation IN ('appointments.book', 'appointments.check_in')",
    }
    with _database() as connection:
        return {
            table: [
                row[0]
                for row in connection.execute(
                    f"SELECT to_jsonb(r) FROM public.{table} r "
                    f"WHERE organization_id=%s {condition} ORDER BY to_jsonb(r)::text",
                    (tenant,),
                ).fetchall()
            ]
            for table, condition in tables.items()
        }


def _denied(response, status, code):
    actual, body = response
    assert actual == status, body
    assert set(body) == {"error"}
    assert body["error"]["code"] == code


def test_invited_browser_checkin_replay_and_tenant_session_isolation(invited_patients):
    sessions = invited_patients
    owner, receptionist, foreign = (
        sessions.owner,
        sessions.receptionist,
        sessions.other,
    )
    status, identity = browser_request(receptionist, "/api/session")
    assert status == 200
    actor = identity["data"]
    assert actor["role"] == "RECEPTIONIST"
    assert actor["organization_id"] == str(sessions.clinic)
    status, identity = browser_request(foreign, "/api/session")
    assert status == 200
    foreign_tenant = UUID(identity["data"]["organization_id"])
    assert foreign_tenant != sessions.clinic

    status, pricing = browser_request(
        owner,
        "/api/settings/pricing",
        method="PATCH",
        body=pricing_patch(standard_new_exam_egp="100.00"),
        key=str(uuid4()),
    )
    assert status == 200, pricing
    assert pricing["revision"] == 2
    status, preview = browser_request(
        owner,
        "/api/settings/schedule/preview",
        method="POST",
        body=schedule_patch(_config(mode="OPEN")).model_dump(mode="json"),
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
            "reason": "Synthetic reviewed calendar",
        },
        key=str(uuid4()),
    )
    assert status == 200, published
    assert published["revision"] == 2

    status, patient = browser_request(
        receptionist, method="POST", body=PATIENT, key=str(uuid4())
    )
    assert status == 201, patient
    patient_id = patient["data"]["id"]
    booking_key = str(uuid4())
    status, booked = browser_request(
        receptionist,
        "/api/appointments",
        method="POST",
        body=booking_body(patient_id),
        key=booking_key,
    )
    assert status == 201, booked
    appointment = booked["data"]
    appointment_id = appointment["id"]
    assert (appointment["status"], appointment["number"], booked["revision"]) == (
        "Booked",
        1,
        0,
    )
    day = SERVICE_DAY.isoformat()
    path = f"/api/appointments/{appointment_id}/check-in"
    queue_path = f"/api/queue?date={day}&lane=MAIN"
    command = {
        "physical_presence_confirmed": True,
        "expected_appointment_revision": 0,
        "expected_queue_day_revision": 0,
    }
    key = str(uuid4())

    before = _domain_state(sessions.clinic)
    foreign_before = _domain_state(foreign_tenant)
    assert len(before["appointments"]) == 1
    assert before["queue_days"] == before["queue_entries"] == before["visits"] == []
    assert len(before["appointment_history"]) == 1
    assert len(before["idempotency_receipts"]) == 1

    _denied(
        browser_request(
            receptionist,
            path,
            method="POST",
            body={**command, "physical_presence_confirmed": False},
            key=str(uuid4()),
        ),
        422,
        "INVALID_INPUT",
    )
    _denied(browser_request(receptionist, path), 403, "FORBIDDEN")
    _denied(
        browser_request(None, path, method="POST", body=command, key=key),
        401,
        "UNAUTHENTICATED",
    )
    _denied(
        browser_request(receptionist, path, method="POST", body=command),
        422,
        "INVALID_INPUT",
    )
    for stale_field in ("expected_appointment_revision", "expected_queue_day_revision"):
        _denied(
            browser_request(
                receptionist,
                path,
                method="POST",
                body={**command, stale_field: 1},
                key=str(uuid4()),
            ),
            409,
            "STALE_REVISION",
        )
    _denied(
        browser_request(foreign, path, method="POST", body=command, key=key),
        404,
        "NOT_FOUND",
    )
    assert _domain_state(sessions.clinic) == before
    assert _domain_state(foreign_tenant) == foreign_before

    start = Barrier(2)

    def submit():
        start.wait(timeout=15)
        return browser_request(receptionist, path, method="POST", body=command, key=key)

    with ThreadPoolExecutor(max_workers=2) as pool:
        first, second = pool.submit(submit), pool.submit(submit)
        status, checked = first.result(timeout=30)
        assert second.result(timeout=30) == (status, checked)
    assert status == 200, checked
    entry = checked["data"]
    assert checked["revision"] == 1
    assert {
        field: entry[field]
        for field in (
            "appointment_id",
            "patient_id",
            "service_date",
            "status",
            "lane",
            "booking_number",
            "appointment_revision",
            "queue_day_revision",
            "queue_entry_revision",
            "arrival_sequence",
            "routine_rank",
            "position",
            "routine_cursor",
            "attested_by_staff_id",
        )
    } == {
        "appointment_id": appointment_id,
        "patient_id": patient_id,
        "service_date": day,
        "status": "Waiting",
        "lane": "MAIN",
        "booking_number": 1,
        "appointment_revision": 1,
        "queue_day_revision": 1,
        "queue_entry_revision": 1,
        "arrival_sequence": 1,
        "routine_rank": 1,
        "position": 1,
        "routine_cursor": 0,
        "attested_by_staff_id": actor["id"],
    }

    after = _domain_state(sessions.clinic)
    assert len(after["appointments"]) == 1
    assert datetime.fromisoformat(
        after["appointments"][0]["updated_at"]
    ) >= datetime.fromisoformat(before["appointments"][0]["updated_at"])
    assert after["appointments"][0] == {
        **before["appointments"][0],
        "status": "Waiting",
        "revision": 1,
        "updated_at": after["appointments"][0]["updated_at"],
    }
    for table in (
        "appointment_day_counters",
        "appointment_number_assignments",
        "appointment_price_snapshots",
    ):
        assert len(before[table]) == 1
        assert after[table] == before[table]
    assert after["visits"] == []
    (queue_day,) = after["queue_days"]
    assert (
        queue_day["id"],
        queue_day["service_date"],
        queue_day["lane"],
        queue_day["revision"],
        queue_day["arrival_counter"],
        queue_day["routine_cursor"],
    ) == (entry["queue_day_id"], day, "MAIN", 1, 1, 0)
    (queue_entry,) = after["queue_entries"]
    assert (
        queue_entry["id"],
        queue_entry["queue_day_id"],
        queue_entry["appointment_id"],
        queue_entry["patient_id"],
        queue_entry["disposition"],
        queue_entry["arrival_sequence"],
        queue_entry["routine_rank"],
        queue_entry["revision"],
        queue_entry["attested_by_staff_id"],
    ) == (
        entry["queue_entry_id"],
        entry["queue_day_id"],
        appointment_id,
        patient_id,
        "ACTIVE",
        1,
        1,
        1,
        actor["id"],
    )
    created, checkin_history = sorted(
        after["appointment_history"], key=lambda row: row["appointment_revision"]
    )
    assert created == before["appointment_history"][0]
    assert (
        checkin_history["appointment_id"],
        checkin_history["appointment_revision"],
        checkin_history["action"],
        checkin_history["old_status"],
        checkin_history["new_status"],
        checkin_history["old_service_date"],
        checkin_history["new_service_date"],
        checkin_history["old_number"],
        checkin_history["new_number"],
        checkin_history["actor_staff_id"],
        checkin_history["actor_role"],
    ) == (
        appointment_id,
        1,
        "CHECKED_IN",
        "Booked",
        "Waiting",
        day,
        day,
        1,
        1,
        actor["id"],
        "RECEPTIONIST",
    )
    booking_audit, checkin_audit = sorted(
        after["sensitive_audit"], key=lambda row: row["action"]
    )
    assert booking_audit == before["sensitive_audit"][0]
    assert (
        checkin_audit["action"],
        checkin_audit["actor_staff_id"],
        checkin_audit["actor_role"],
        checkin_audit["correlation_id"],
        checkin_audit["after_state"]["queue_entry_id"],
    ) == (
        "appointments.check_in",
        actor["id"],
        "RECEPTIONIST",
        appointment_id,
        entry["queue_entry_id"],
    )
    booked_event, checkin_event = sorted(
        after["outbox_events"], key=lambda row: row["event_type"]
    )
    assert booked_event == before["outbox_events"][0]
    assert (checkin_event["event_type"], checkin_event["aggregate_id"]) == (
        "PATIENT_CHECKED_IN",
        appointment_id,
    )
    assert checkin_event["payload"]["queue_entry_id"] == entry["queue_entry_id"]
    booked_receipt, checkin_receipt = sorted(
        after["idempotency_receipts"], key=lambda row: row["operation"]
    )
    assert booked_receipt == before["idempotency_receipts"][0]
    assert booked_receipt["idempotency_key"] == booking_key
    assert (
        checkin_receipt["operation"],
        checkin_receipt["idempotency_key"],
        checkin_receipt["actor_id"],
        checkin_receipt["staff_id"],
        checkin_receipt["result_revision"],
        checkin_receipt["result_projection"],
    ) == ("appointments.check_in", key, actor["id"], actor["id"], 1, entry)

    for cookie in (owner, receptionist):
        status, queue = browser_request(cookie, queue_path)
        assert status == 200, queue
        assert queue["revision"] == 1
        view = queue["data"]
        assert view["queue_day_id"] == entry["queue_day_id"]
        assert view["total_waiting"] == view["arrival_counter"] == 1
        assert view["examination_capacity"] == 1
        assert view["active_in_examination_count"] == 0
        (item,) = view["entries"]
        assert item["appointment_id"] == appointment_id
        assert item["queue_entry_id"] == entry["queue_entry_id"]
        assert item["attested_by_staff_id"] == actor["id"]
        assert item["position"] == 1 and item["status"] == "Waiting"

    assert browser_request(
        receptionist, path, method="POST", body=command, key=key
    ) == (200, checked)
    _denied(
        browser_request(
            receptionist,
            path,
            method="POST",
            body={**command, "expected_appointment_revision": 1},
            key=key,
        ),
        409,
        "IDEMPOTENCY_CONFLICT",
    )
    _denied(
        browser_request(foreign, path, method="POST", body=command, key=key),
        404,
        "NOT_FOUND",
    )
    status, foreign_queue = browser_request(foreign, queue_path)
    assert status == 200, foreign_queue
    assert foreign_queue["data"]["entries"] == []
    assert foreign_queue["data"]["queue_day_id"] is None
    assert _domain_state(sessions.clinic) == after
    assert _domain_state(foreign_tenant) == foreign_before

    # Same visible tenant, different target, identical signed body and key.
    status, other_patient = browser_request(
        receptionist,
        method="POST",
        body={**PATIENT, "name_parts": ["Synthetic", "B12", "Other"]},
        key=str(uuid4()),
    )
    assert status == 201, other_patient
    status, other_booking = browser_request(
        receptionist,
        "/api/appointments",
        method="POST",
        body=booking_body(other_patient["data"]["id"]),
        key=str(uuid4()),
    )
    assert status == 201, other_booking
    other_path = f"/api/appointments/{other_booking['data']['id']}/check-in"
    after_other_booking = _domain_state(sessions.clinic)
    _denied(
        browser_request(receptionist, other_path, method="POST", body=command, key=key),
        409,
        "IDEMPOTENCY_CONFLICT",
    )
    assert _domain_state(sessions.clinic) == after_other_booking

    assert browser_request(receptionist, "/api/auth/logout", method="POST")[0] == 200
    _denied(
        browser_request(receptionist, path, method="POST", body=command, key=key),
        401,
        "UNAUTHENTICATED",
    )
    _denied(browser_request(receptionist, queue_path), 401, "UNAUTHENTICATED")
    assert _domain_state(sessions.clinic) == after_other_booking
    assert _domain_state(foreign_tenant) == foreign_before

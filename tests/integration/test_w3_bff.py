"""B-11–B-15 authenticated browser-to-BFF-to-API-to-PostgreSQL acceptance."""

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from threading import Barrier
from uuid import UUID, uuid4

from carepulse.db import transaction
from sqlalchemy import create_engine
from test_appointment_persistence import _admit_saved
from test_booking import booking_body
from test_patients import PATIENT, browser_request
from test_patients import invited_patients as invited_patients  # noqa: PLC0414
from test_pricing import _patch as pricing_patch
from test_schedule import CAIRO, SERVICE_DAY, _config
from test_schedule import _patch as schedule_patch
from test_sessions import _database
from test_w3_queue_policy import placement_body


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
        "sensitive_audit": "AND action IN ('appointments.book', 'appointments.check_in', 'appointments.intake_routine', 'appointments.intake_emergency', 'appointments.intake_urgent', 'queue.admit')",
        "outbox_events": "AND event_type IN ('APPOINTMENT_BOOKED', 'PATIENT_CHECKED_IN', 'APPOINTMENT_INTAKE', 'APPOINTMENT_ADMITTED')",
        "idempotency_receipts": "AND operation IN ('appointments.book', 'appointments.check_in', 'appointments.intake_routine', 'appointments.intake_emergency', 'appointments.intake_urgent', 'queue.admit')",
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


def _seed_browser_active_exam(tenant, doctor, appointment):
    # Independent B-14 setup uses the existing migration-only lifecycle fixture.
    def migration_connection():
        connection = _database()
        connection.commit()
        return connection

    engine = create_engine("postgresql+psycopg://", creator=migration_connection)
    try:
        with transaction(engine, tenant) as connection:
            _admit_saved(connection, tenant, UUID(doctor), appointment)
    finally:
        engine.dispose()


def _browser_book_new_patient(cookie, name, *, day=SERVICE_DAY):
    status, patient = browser_request(
        cookie,
        method="POST",
        body={**PATIENT, "name_parts": ["Synthetic", "B14", name]},
        key=str(uuid4()),
    )
    assert status == 201, patient
    status, booked = browser_request(
        cookie,
        "/api/appointments",
        method="POST",
        body=booking_body(patient["data"]["id"], day=day),
        key=str(uuid4()),
    )
    assert status == 201, booked
    return booked["data"]


def _confirmation_effects(tenant):
    with _database() as connection:
        return {
            table: connection.execute(
                f"SELECT to_jsonb(r) FROM public.{table} r WHERE organization_id=%s ORDER BY to_jsonb(r)::text",
                (tenant,),
            ).fetchall()
            for table in (
                "doctor_admission_confirmations",
                "sensitive_audit",
                "outbox_events",
                "idempotency_receipts",
            )
        }


def _browser_queue_policy_and_placement(sessions, queue_path):
    owner, receptionist = sessions.owner, sessions.receptionist
    policy_path, placement_path = "/api/settings/queue", "/api/queue/placement"
    status, initial = browser_request(owner, policy_path)
    assert status == 200, initial
    status, queue = browser_request(receptionist, queue_path)
    assert status == 200, queue
    assert queue["data"]["total_waiting"] == 2
    publish = {
        "routine_order": "ARRIVAL_FIFO",
        "late_depth": 0,
        "expected_policy_revision": initial["revision"],
    }
    place = placement_body(queue["data"])
    policy_key, placement_key = str(uuid4()), str(uuid4())
    before = _domain_state(sessions.clinic)
    evidence = _confirmation_effects(sessions.clinic)
    _denied(browser_request(receptionist, policy_path), 403, "FORBIDDEN")
    _denied(
        browser_request(
            receptionist, policy_path, method="PATCH", body=publish, key=policy_key
        ),
        403,
        "FORBIDDEN",
    )
    _denied(
        browser_request(
            owner, placement_path, method="POST", body=place, key=placement_key
        ),
        403,
        "FORBIDDEN",
    )
    _denied(browser_request(receptionist, placement_path), 403, "FORBIDDEN")
    _denied(
        browser_request(
            None, placement_path, method="POST", body=place, key=placement_key
        ),
        401,
        "UNAUTHENTICATED",
    )
    assert _domain_state(sessions.clinic) == before
    assert _confirmation_effects(sessions.clinic) == evidence
    status, published = browser_request(
        owner, policy_path, method="PATCH", body=publish, key=policy_key
    )
    assert status == 200, published
    assert published["revision"] == initial["revision"] + 1
    assert published["data"]["routine_order"] == "ARRIVAL_FIFO"
    assert published["data"]["late_depth"] == 0
    assert browser_request(owner, policy_path) == (200, published)
    assert browser_request(receptionist, queue_path) == (200, queue)
    status, placed = browser_request(
        receptionist, placement_path, method="POST", body=place, key=placement_key
    )
    assert status == 200, placed
    assert placed["revision"] == queue["revision"] + 1
    status, current = browser_request(receptionist, queue_path)
    assert status == 200, current
    assert [row["queue_entry_id"] for row in current["data"]["entries"]] == [
        row["queue_entry_id"] for row in place["entries"]
    ]
    assert current["data"]["routine_cursor"] == queue["data"]["routine_cursor"]
    assert (
        current["data"]["queue_policy_version"] == queue["data"]["queue_policy_version"]
    )
    committed = _confirmation_effects(sessions.clinic)
    assert browser_request(
        owner, policy_path, method="PATCH", body=publish, key=policy_key
    ) == (200, published)
    assert browser_request(
        receptionist, placement_path, method="POST", body=place, key=placement_key
    ) == (200, placed)
    _denied(
        browser_request(
            receptionist,
            placement_path,
            method="POST",
            body={**place, "entries": list(reversed(place["entries"]))},
            key=placement_key,
        ),
        409,
        "IDEMPOTENCY_CONFLICT",
    )
    assert _confirmation_effects(sessions.clinic) == committed
    for operation, key, result, cookie in (
        ("settings.queue.update", policy_key, published, owner),
        ("queue.placement", placement_key, placed, receptionist),
    ):
        status, identity = browser_request(cookie, "/api/session")
        assert status == 200, identity
        actor_id = identity["data"]["id"]
        with _database() as connection:
            assert connection.execute(
                "SELECT actor_id::text, result_projection, result_revision FROM public.idempotency_receipts WHERE organization_id=%s AND operation=%s AND idempotency_key=%s",
                (sessions.clinic, operation, key),
            ).fetchall() == [(actor_id, result["data"], result["revision"])]
            assert connection.execute(
                "SELECT actor_staff_id::text, after_state FROM public.sensitive_audit WHERE organization_id=%s AND action=%s",
                (sessions.clinic, operation),
            ).fetchall() == [(actor_id, result["data"])]


def test_independent_browser_sessions_issue_and_retrieve_exact_confirmation(
    invited_patients,
):
    sessions = invited_patients
    owner, receptionist, foreign = sessions.owner, sessions.receptionist, sessions.other
    assert owner != receptionist
    status, doctor = browser_request(owner, "/api/session")
    assert status == 200 and doctor["data"]["role"] == "DOCTOR_OWNER"
    status, staff = browser_request(receptionist, "/api/session")
    assert status == 200 and staff["data"]["role"] == "RECEPTIONIST"
    assert doctor["data"]["id"] != staff["data"]["id"]
    assert (
        doctor["data"]["organization_id"]
        == staff["data"]["organization_id"]
        == str(sessions.clinic)
    )

    status, pricing = browser_request(
        owner,
        "/api/settings/pricing",
        method="PATCH",
        body=pricing_patch(standard_new_exam_egp="100.00"),
        key=str(uuid4()),
    )
    assert status == 200, pricing
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

    today = datetime.now(CAIRO).date()
    day = today.isoformat()
    queue_path = f"/api/queue?date={day}&lane=MAIN"
    status, queue = browser_request(owner, queue_path)
    assert status == 200, queue
    status, capacity = browser_request(
        owner,
        "/api/queue/capacity",
        method="PATCH",
        body={
            "capacity": 3,
            "expected_capacity_revision": queue["data"]["capacity_policy_version"],
            "expected_control_revision": queue["data"]["control_revision"],
        },
        key=str(uuid4()),
    )
    assert status == 200, capacity
    status, mode = browser_request(owner, "/api/settings/admission")
    assert status == 200, mode
    status, mode = browser_request(
        owner,
        "/api/settings/admission",
        method="PATCH",
        body={
            "mode": "RECEPTIONIST",
            "expected_admission_revision": mode["revision"],
        },
        key=str(uuid4()),
    )
    assert status == 200, mode
    active = _browser_book_new_patient(receptionist, "Active", day=today)
    _seed_browser_active_exam(sessions.clinic, doctor["data"]["id"], active)
    target = _browser_book_new_patient(receptionist, "Target", day=today)
    other = _browser_book_new_patient(receptionist, "Other", day=today)
    status, queue = browser_request(receptionist, queue_path)
    assert status == 200, queue
    assert queue["data"]["active_in_examination_count"] == 1
    status, checked = browser_request(
        receptionist,
        f"/api/appointments/{target['id']}/check-in",
        method="POST",
        body={
            "physical_presence_confirmed": True,
            "expected_appointment_revision": 0,
            "expected_queue_day_revision": queue["revision"],
        },
        key=str(uuid4()),
    )
    assert status == 200, checked
    entry = checked["data"]
    command = {
        "appointment_id": target["id"],
        "patient_id": target["patient_id"],
        "date": day,
        "lane": "MAIN",
        "expected_appointment_revision": entry["appointment_revision"],
        "expected_queue_day_revision": entry["queue_day_revision"],
        "expected_control_revision": capacity["data"]["control_revision"],
        "expected_capacity_revision": capacity["data"]["capacity_revision"],
        "expected_admission_revision": mode["revision"],
    }
    path, key = "/api/queue/admission-confirmations", str(uuid4())
    before = _confirmation_effects(sessions.clinic)
    domain = _domain_state(sessions.clinic)
    _denied(
        browser_request(receptionist, path, method="POST", body=command, key=key),
        403,
        "FORBIDDEN",
    )
    assert _confirmation_effects(sessions.clinic) == before
    status, issued = browser_request(owner, path, method="POST", body=command, key=key)
    assert status == 201, issued
    assert issued["data"]["state"] == "ISSUED"
    after = _confirmation_effects(sessions.clinic)
    for table in before:
        assert len(after[table]) == len(before[table]) + 1, table
        assert all(row in after[table] for row in before[table])
    (saved,) = after["doctor_admission_confirmations"]
    assert saved[0]["id"] == issued["data"]["id"]
    assert saved[0]["appointment_id"] == target["id"]
    assert saved[0]["patient_id"] == target["patient_id"]
    assert saved[0]["issuing_doctor_staff_id"] == doctor["data"]["id"]
    (audit,) = [
        row[0]
        for row in after["sensitive_audit"]
        if row not in before["sensitive_audit"]
    ]
    assert (audit["actor_staff_id"], audit["actor_role"], audit["after_state"]) == (
        doctor["data"]["id"],
        "DOCTOR_OWNER",
        issued["data"],
    )
    (receipt,) = [
        row[0]
        for row in after["idempotency_receipts"]
        if row not in before["idempotency_receipts"]
    ]
    assert (
        receipt["actor_id"],
        receipt["idempotency_key"],
        receipt["result_projection"],
    ) == (
        doctor["data"]["id"],
        key,
        issued["data"],
    )
    assert browser_request(owner, path, method="POST", body=command, key=key) == (
        200,
        issued,
    )
    usable_path = f"{path}/usable?appointment_id={target['id']}&date={day}&lane=MAIN"
    status, usable = browser_request(receptionist, usable_path)
    assert status == 200, usable
    assert usable["data"] == {
        k: v for k, v in issued["data"].items() if k not in ("state", "issued_at")
    }
    assert usable["revision"] == issued["revision"]
    for cookie, candidate in (
        (owner, usable_path),
        (foreign, usable_path),
        (receptionist, usable_path.replace(target["id"], other["id"])),
        (receptionist, usable_path.replace("lane=MAIN", "lane=EMERGENCY")),
        (
            receptionist,
            usable_path.replace(day, (SERVICE_DAY + timedelta(days=1)).isoformat()),
        ),
    ):
        _denied(browser_request(cookie, candidate), 404, "NOT_FOUND")
    _denied(
        browser_request(
            receptionist,
            f"{path}/{issued['data']['id']}/revoke",
            method="POST",
            body={
                "expected_control_revision": capacity["data"]["control_revision"],
                "revoke_reason": "Synthetic denial",
            },
            key=str(uuid4()),
        ),
        403,
        "FORBIDDEN",
    )
    assert _confirmation_effects(sessions.clinic) == after
    assert _domain_state(sessions.clinic) == domain
    status, current_queue = browser_request(receptionist, queue_path)
    assert status == 200, current_queue
    (selected,) = current_queue["data"]["entries"]
    admit_command = {
        **command,
        "expected_queue_entry_revision": selected["queue_entry_revision"],
        "expected_head_appointment_id": target["id"],
        "confirmation_id": usable["data"]["id"],
        "physical_presence_confirmed": True,
    }
    admit_path, admit_key = "/api/queue/admit", str(uuid4())
    _denied(
        browser_request(
            owner, admit_path, method="POST", body=admit_command, key=admit_key
        ),
        403,
        "FORBIDDEN",
    )
    assert _domain_state(sessions.clinic) == domain
    status, admitted = browser_request(
        receptionist, admit_path, method="POST", body=admit_command, key=admit_key
    )
    assert status == 200, admitted
    assert admitted["data"]["appointment_id"] == target["id"]
    assert admitted["data"]["confirmation_id"] == usable["data"]["id"]
    assert admitted["data"]["status"] == "In Examination"
    final = _domain_state(sessions.clinic)
    assert len(final["visits"]) == len(domain["visits"]) + 1
    assert len(final["appointment_history"]) == len(domain["appointment_history"]) + 1
    for table in ("sensitive_audit", "outbox_events", "idempotency_receipts"):
        assert len(final[table]) == len(domain[table]) + 1
    (new_audit,) = [
        row for row in final["sensitive_audit"] if row not in domain["sensitive_audit"]
    ]
    assert new_audit["actor_staff_id"] == staff["data"]["id"]
    assert new_audit["actor_role"] == "RECEPTIONIST"
    (consumed,) = _confirmation_effects(sessions.clinic)[
        "doctor_admission_confirmations"
    ]
    assert consumed[0]["state"] == "CONSUMED"
    assert browser_request(
        receptionist, admit_path, method="POST", body=admit_command, key=admit_key
    ) == (200, admitted)
    assert _domain_state(sessions.clinic) == final
    _denied(browser_request(receptionist, usable_path), 404, "NOT_FOUND")
    assert browser_request(receptionist, "/api/auth/logout", method="POST")[0] == 200
    _denied(browser_request(receptionist, usable_path), 401, "UNAUTHENTICATED")


def test_invited_browser_admission_controls_are_purpose_and_role_bound(
    invited_patients,
):
    sessions = invited_patients
    owner, receptionist = sessions.owner, sessions.receptionist
    path = "/api/settings/admission"
    status, initial = browser_request(owner, path)
    assert status == 200, initial
    initial_revision = initial["revision"]
    command = {"mode": "RECEPTIONIST", "expected_admission_revision": initial_revision}
    _denied(browser_request(receptionist, path), 403, "FORBIDDEN")
    _denied(
        browser_request(
            receptionist, path, method="PATCH", body=command, key=str(uuid4())
        ),
        403,
        "FORBIDDEN",
    )
    key = str(uuid4())
    status, changed = browser_request(
        owner, path, method="PATCH", body=command, key=key
    )
    assert status == 200, changed
    assert changed["data"] == {
        "mode": "RECEPTIONIST",
        "admission_revision": initial_revision + 1,
    }
    assert browser_request(owner, path, method="PATCH", body=command, key=key) == (
        200,
        changed,
    )
    queue_path = f"/api/queue?date={SERVICE_DAY.isoformat()}&lane=MAIN"
    status, queue = browser_request(receptionist, queue_path)
    assert status == 200, queue
    control = queue["data"]
    pause = {"paused": True, "expected_control_revision": control["control_revision"]}
    _denied(
        browser_request(
            receptionist,
            "/api/queue/pause",
            method="POST",
            body=pause,
            key=str(uuid4()),
        ),
        403,
        "FORBIDDEN",
    )
    status, paused = browser_request(
        owner, "/api/queue/pause", method="POST", body=pause, key=str(uuid4())
    )
    assert status == 200, paused
    assert paused["data"]["paused"] is True
    # B-15 route is enabled, but the malformed command must not reach a writer.
    _denied(
        browser_request(
            owner, "/api/queue/admit", method="POST", body={}, key=str(uuid4())
        ),
        422,
        "INVALID_INPUT",
    )
    with _database() as connection:
        assert connection.execute(
            "SELECT count(*) FROM public.idempotency_receipts WHERE organization_id=%s AND operation='settings.admission.update' AND idempotency_key=%s",
            (sessions.clinic, key),
        ).fetchone() == (1,)


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

    status, checked_other = browser_request(
        receptionist,
        other_path,
        method="POST",
        body={**command, "expected_queue_day_revision": 1},
        key=str(uuid4()),
    )
    assert status == 200, checked_other
    _browser_queue_policy_and_placement(sessions, queue_path)
    after_other_booking = _domain_state(sessions.clinic)

    assert browser_request(receptionist, "/api/auth/logout", method="POST")[0] == 200
    _denied(
        browser_request(receptionist, path, method="POST", body=command, key=key),
        401,
        "UNAUTHENTICATED",
    )
    _denied(browser_request(receptionist, queue_path), 401, "UNAUTHENTICATED")
    assert _domain_state(sessions.clinic) == after_other_booking
    assert _domain_state(foreign_tenant) == foreign_before


def test_invited_browser_emergency_intake_is_atomic_and_replay_safe(invited_patients):
    sessions = invited_patients
    owner, receptionist, foreign = sessions.owner, sessions.receptionist, sessions.other
    status, pricing = browser_request(
        owner,
        "/api/settings/pricing",
        method="PATCH",
        body=pricing_patch(standard_new_exam_egp="100.00"),
        key=str(uuid4()),
    )
    assert status == 200, pricing
    status, patient = browser_request(
        receptionist, method="POST", body=PATIENT, key=str(uuid4())
    )
    assert status == 201, patient
    body = {
        "patient_id": patient["data"]["id"],
        "physical_presence_confirmed": True,
        "discount": {
            "discount_type": "NONE",
            "discount_value": "0",
            "discount_reason": None,
        },
        "expected_base_egp": "100.00",
        "expected_net_egp": "100.00",
        "expected_price_revision": pricing["revision"],
    }
    path = "/api/intake/emergency"
    key = str(uuid4())
    before = _domain_state(sessions.clinic)
    _denied(
        browser_request(None, path, method="POST", body=body, key=key),
        401,
        "UNAUTHENTICATED",
    )
    _denied(
        browser_request(foreign, path, method="POST", body=body, key=key),
        404,
        "NOT_FOUND",
    )
    _denied(
        browser_request(
            receptionist,
            path,
            method="POST",
            body={**body, "role": "DOCTOR_OWNER"},
            key=key,
        ),
        422,
        "INVALID_INPUT",
    )
    _denied(
        browser_request(
            owner,
            "/api/intake/urgent",
            method="POST",
            body={**body, "expected_schedule_revision": 1},
            key=key,
        ),
        403,
        "FORBIDDEN",
    )
    assert _domain_state(sessions.clinic) == before

    status, created = browser_request(
        receptionist, path, method="POST", body=body, key=key
    )
    assert status == 201, created
    assert created["revision"] == 0
    entry = created["data"]
    assert (
        entry["status"],
        entry["classification"],
        entry["lane"],
        entry["number"],
    ) == ("Waiting", "EMERGENCY", "EMERGENCY", 1)
    after = _domain_state(sessions.clinic)
    for table in before:
        assert len(after[table]) == len(before[table]) + (
            0 if table == "visits" else 1
        ), table
    assert after["appointment_history"][-1]["new_status"] == "Waiting"
    assert after["outbox_events"][-1]["event_type"] == "APPOINTMENT_INTAKE"
    status, recovered = browser_request(
        receptionist, path, method="POST", body=body, key=key
    )
    assert status == 200 and recovered == created
    assert _domain_state(sessions.clinic) == after

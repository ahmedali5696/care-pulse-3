"""T009: B-12 check-in, dated queue ordering, FR-019 capacity visibility, and query plan."""

import asyncio
import json
from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
from hashlib import sha256
from hmac import new
from urllib.parse import urlencode
from uuid import UUID, uuid4

import pytest
from carepulse.db import transaction
from carepulse.identity.signing import canonical_payload, canonical_query
from carepulse.main import app
from carepulse.policies.versions import append_version
from carepulse.queue.contracts import QueueQuery
from carepulse.queue.queries import dated_queue_projection
from runtime_identity_fixture import db_proof_headers, signed_test_transaction
from sqlalchemy import event, text
from sqlalchemy.exc import DBAPIError
from test_appointment_persistence import _admit_saved
from test_booking import booking_body, booking_request
from test_booking import booking_clinic as booking_clinic  # noqa: PLC0414
from test_database import admin as admin  # noqa: PLC0414
from test_database import disposable_project as disposable_project  # noqa: PLC0414
from test_database import empty_database as empty_database  # noqa: PLC0414
from test_patients import create_patient, private_request
from test_patients import patient_backend as patient_backend  # noqa: PLC0414
from test_pricing import _fresh_receptionist_session, _owner_changes_manager
from test_request_boundary import boundary_data as boundary_data  # noqa: PLC0414
from test_schedule import CAIRO, SERVICE_DAY

NEXT_DAY = SERVICE_DAY + timedelta(days=1)
PRIOR_DAY = SERVICE_DAY - timedelta(days=1)


def queue_request(
    clinic,
    actor,
    *,
    method: str,
    path: str,
    purpose: str,
    body=None,
    key: str = "",
    query=None,
):
    encoded = (
        json.dumps(body, separators=(",", ":")).encode() if body is not None else b""
    )
    signed = replace(
        actor,
        method=method,
        path=path,
        query=canonical_query(urlencode(query or {}, doseq=True)),
        body_sha256=sha256(encoded).hexdigest(),
        purpose=purpose,
        timestamp=str(int(datetime.now(UTC).timestamp() * 1000)),
        nonce=f"synthetic_{uuid4().hex}",
        idempotency_key=key,
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
            str(getattr(signed, field)).encode(),
        )
        for field in fields
    ] + [
        (b"content-type", b"application/json"),
        (b"x-carepulse-service-key", clinic.config.service_key.encode()),
        (
            b"x-carepulse-signature",
            new(clinic.config.service_key.encode(), canonical_payload(signed), sha256)
            .hexdigest()
            .encode(),
        ),
    ]
    headers.extend(db_proof_headers(signed))
    scope = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "scheme": "http",
        "method": method,
        "path": path,
        "raw_path": path.encode(),
        "query_string": signed.query.encode(),
        "root_path": "",
        "headers": headers,
        "client": ("synthetic", 1),
        "server": ("api", 8000),
    }
    messages = []
    sent = False

    async def receive():
        nonlocal sent
        if not sent:
            sent = True
            return {"type": "http.request", "body": encoded, "more_body": False}
        return {"type": "http.disconnect"}

    async def send(message):
        messages.append(message)

    asyncio.run(app(scope, receive, send))
    start = next(m for m in messages if m["type"] == "http.response.start")
    raw = b"".join(
        m.get("body", b"") for m in messages if m["type"] == "http.response.body"
    )
    return start["status"], json.loads(raw.decode())


def _new_patient(clinic, actor, name: str) -> str:
    return create_patient(clinic, actor, name_parts=["Synthetic", "B12", name])["id"]


def _book(clinic, actor, patient_id: str, service_date: date = SERVICE_DAY) -> dict:
    status, reply = booking_request(
        clinic,
        actor,
        body=booking_body(patient_id, day=service_date),
        key=f"book-{uuid4().hex}",
    )
    assert status == 201, reply
    return reply["data"]


def _seed_booked(
    clinic,
    patient_id: UUID,
    service_date: date,
    *,
    classification: str = "ROUTINE",
    lane: str = "MAIN",
) -> dict:
    """Seed a legitimate pre-existing Booked appointment (including Urgent/Emergency/prior-day)."""
    appointment_id = uuid4()
    assignment_id = uuid4()
    doctor_id = UUID(clinic.owner.staff_id)
    with transaction(clinic.migration, clinic.tenant) as conn:
        number = int(
            conn.scalar(
                text("""
                    INSERT INTO public.appointment_day_counters
                        (organization_id, service_date, last_number)
                    VALUES (:tenant, :day, 1)
                    ON CONFLICT (organization_id, service_date)
                    DO UPDATE SET last_number = public.appointment_day_counters.last_number + 1
                    RETURNING last_number
                """),
                {"tenant": clinic.tenant, "day": service_date},
            )
        )
        conn.execute(
            text("""
                INSERT INTO public.appointments
                    (id, organization_id, patient_id, doctor_staff_id, service_date,
                     status, service_type, classification, lane, booking_mode,
                     current_assignment_id, current_number, revision)
                VALUES (:id, :tenant, :patient, :doctor, :day,
                        'Booked', 'NEW_EXAMINATION', :classification, :lane, 'OPEN',
                        :assignment_id, :number, 0)
            """),
            {
                "id": appointment_id,
                "tenant": clinic.tenant,
                "patient": patient_id,
                "doctor": doctor_id,
                "day": service_date,
                "classification": classification,
                "lane": lane,
                "assignment_id": assignment_id,
                "number": number,
            },
        )
        conn.execute(
            text("""
                INSERT INTO public.appointment_number_assignments
                    (id, organization_id, appointment_id, service_date, number)
                VALUES (:assignment_id, :tenant, :id, :day, :number)
            """),
            {
                "assignment_id": assignment_id,
                "tenant": clinic.tenant,
                "id": appointment_id,
                "day": service_date,
                "number": number,
            },
        )
        base = "150.00" if classification == "URGENT" else "100.00"
        conn.execute(
            text("""
                INSERT INTO public.appointment_price_snapshots
                    (organization_id, appointment_id, service_type, classification,
                     base_egp, net_egp, discount_type, discount_value, is_free,
                     pricing_version, creation_standard_base_egp)
                VALUES (:tenant, :id, 'NEW_EXAMINATION', :classification,
                        :base, :base, 'NONE', 0, false, 2, '100.00')
            """),
            {
                "tenant": clinic.tenant,
                "id": appointment_id,
                "classification": classification,
                "base": base,
            },
        )
        conn.execute(
            text("""
                INSERT INTO public.appointment_history
                    (id, organization_id, appointment_id, appointment_revision, action,
                     old_status, new_status, old_service_date, new_service_date,
                     old_number, new_number, actor_staff_id, actor_role,
                     price_snapshot_appointment_id)
                VALUES (:hid, :tenant, :id, 0, 'CREATED',
                        NULL, 'Booked', NULL, :day,
                        NULL, :number, :doctor, 'DOCTOR_OWNER', :id)
            """),
            {
                "hid": uuid4(),
                "tenant": clinic.tenant,
                "id": appointment_id,
                "day": service_date,
                "number": number,
                "doctor": doctor_id,
            },
        )
    return {
        "id": str(appointment_id),
        "number": number,
        "revision": 0,
        "service_date": service_date.isoformat(),
    }


def test_checkin_roles_archived_prebooking_replay_and_no_visit(booking_clinic) -> None:
    clinic = booking_clinic
    p_doc = _new_patient(clinic, clinic.owner, "Doc Checkin")
    p_rec = _new_patient(clinic, clinic.owner, "Rec Checkin")
    p_mgr = _new_patient(clinic, clinic.owner, "Mgr Checkin")
    p_arch = _new_patient(clinic, clinic.owner, "Archived Checkin")

    a_doc = _book(clinic, clinic.owner, p_doc)
    a_rec = _book(clinic, clinic.owner, p_rec)
    a_mgr = _book(clinic, clinic.owner, p_mgr)
    a_arch = _book(clinic, clinic.owner, p_arch)

    # Archive p_arch AFTER booking — existing-ID check-in MUST still succeed.
    arch_status, _ = private_request(
        clinic,
        clinic.owner,
        f"/v1/patients/{p_arch}/archive",
        method="POST",
        body={"reason": "Archived before check-in", "expected_lifecycle_revision": 0},
    )
    assert arch_status == 200

    # 1) Doctor check-in + replay safety + target conflict.
    key_doc = f"checkin-doc-{uuid4().hex}"
    s1, r1 = queue_request(
        clinic,
        clinic.owner,
        method="POST",
        path=f"/v1/appointments/{a_doc['id']}/check-in",
        purpose="QUEUE_CHECK_IN",
        body={"physical_presence_confirmed": True, "expected_appointment_revision": 0},
        key=key_doc,
    )
    assert s1 == 200, r1
    assert r1["data"]["status"] == "Waiting"
    assert r1["data"]["booking_number"] == a_doc["number"]
    assert r1["data"]["arrival_sequence"] == 1
    assert r1["revision"] == 1

    # Replay with same actor/key/body/target returns original result with zero duplicates.
    s1_rep, r1_rep = queue_request(
        clinic,
        clinic.owner,
        method="POST",
        path=f"/v1/appointments/{a_doc['id']}/check-in",
        purpose="QUEUE_CHECK_IN",
        body={"physical_presence_confirmed": True, "expected_appointment_revision": 0},
        key=key_doc,
    )
    assert s1_rep == 200 and r1_rep == r1

    # Same actor/key/body on a second visible target MUST conflict (T003/T004).
    s_conf, r_conf = queue_request(
        clinic,
        clinic.owner,
        method="POST",
        path=f"/v1/appointments/{a_rec['id']}/check-in",
        purpose="QUEUE_CHECK_IN",
        body={"physical_presence_confirmed": True, "expected_appointment_revision": 0},
        key=key_doc,
    )
    assert s_conf == 409 and r_conf["error"]["code"] == "IDEMPOTENCY_CONFLICT"

    # 2) Ordinary Receptionist check-in.
    s2, r2 = queue_request(
        clinic,
        clinic.receptionist,
        method="POST",
        path=f"/v1/appointments/{a_rec['id']}/check-in",
        purpose="QUEUE_CHECK_IN",
        body={"physical_presence_confirmed": True, "expected_appointment_revision": 0},
        key=f"checkin-rec-{uuid4().hex}",
    )
    assert s2 == 200, r2
    assert r2["data"]["attested_by_staff_id"] == clinic.receptionist.staff_id

    # 3) Manager check-in.
    _owner_changes_manager(clinic, True, 0)
    mgr = _fresh_receptionist_session(clinic, 1)
    s3, r3 = queue_request(
        clinic,
        mgr,
        method="POST",
        path=f"/v1/appointments/{a_mgr['id']}/check-in",
        purpose="QUEUE_CHECK_IN",
        body={"physical_presence_confirmed": True, "expected_appointment_revision": 0},
        key=f"checkin-mgr-{uuid4().hex}",
    )
    assert s3 == 200, r3

    # 4) Archived patient existing-ID check-in succeeds without creating any Visit.
    s4, r4 = queue_request(
        clinic,
        mgr,
        method="POST",
        path=f"/v1/appointments/{a_arch['id']}/check-in",
        purpose="QUEUE_CHECK_IN",
        body={"physical_presence_confirmed": True, "expected_appointment_revision": 0},
        key=f"checkin-arch-{uuid4().hex}",
    )
    assert s4 == 200, r4
    assert r4["data"]["status"] == "Waiting"
    assert r4["data"]["booking_number"] == a_arch["number"]

    # Negative: false presence denies with 422.
    p_neg = _new_patient(clinic, clinic.owner, "No Presence")
    a_neg = _book(clinic, clinic.owner, p_neg)
    s_neg, _ = queue_request(
        clinic,
        clinic.owner,
        method="POST",
        path=f"/v1/appointments/{a_neg['id']}/check-in",
        purpose="QUEUE_CHECK_IN",
        body={"physical_presence_confirmed": False, "expected_appointment_revision": 0},
        key=f"checkin-neg-{uuid4().hex}",
    )
    assert s_neg == 422

    # Verify DB invariants: 4 Waiting entries, 4 CHECKED_IN history rows, 4 PATIENT_CHECKED_IN events, 0 Visits.
    with clinic.migration.begin() as conn:
        assert (
            conn.scalar(
                text(
                    "SELECT count(*) FROM public.queue_entries WHERE organization_id=:t AND disposition='ACTIVE'"
                ),
                {"t": clinic.tenant},
            )
            == 4
        )
        assert (
            conn.scalar(
                text(
                    "SELECT count(*) FROM public.appointment_history WHERE organization_id=:t AND action='CHECKED_IN'"
                ),
                {"t": clinic.tenant},
            )
            == 4
        )
        assert (
            conn.scalar(
                text(
                    "SELECT count(*) FROM public.outbox_events WHERE organization_id=:t AND event_type='PATIENT_CHECKED_IN'"
                ),
                {"t": clinic.tenant},
            )
            == 4
        )
        assert (
            conn.scalar(
                text("SELECT count(*) FROM public.visits WHERE organization_id=:t"),
                {"t": clinic.tenant},
            )
            == 0
        )


def test_ordering_late_depth_urgent_emergency_and_old_day_isolation(
    booking_clinic,
) -> None:
    clinic = booking_clinic
    # Set QUEUE policy with BOOKED_NUMBER and late_depth=1 effective on SERVICE_DAY.
    with transaction(clinic.migration, clinic.tenant) as conn:
        append_version(
            conn,
            str(clinic.tenant),
            "QUEUE",
            {"routine_order": "BOOKED_NUMBER", "late_depth": 1},
            1,
            actor_staff_id=clinic.owner.staff_id,
            today=PRIOR_DAY,
        )

    p1 = UUID(_new_patient(clinic, clinic.owner, "P1"))
    p2 = UUID(_new_patient(clinic, clinic.owner, "P2"))
    p3 = UUID(_new_patient(clinic, clinic.owner, "P3"))
    p4 = UUID(_new_patient(clinic, clinic.owner, "P4"))
    p_urg = UUID(_new_patient(clinic, clinic.owner, "PUrg"))
    p_emg1 = UUID(_new_patient(clinic, clinic.owner, "PEmg1"))
    p_emg2 = UUID(_new_patient(clinic, clinic.owner, "PEmg2"))
    p_prior = UUID(_new_patient(clinic, clinic.owner, "PPrior"))

    # Numbers 1, 2, 3, 4 on SERVICE_DAY (#1 and #3 remain unarrived Booked).
    _seed_booked(clinic, p1, SERVICE_DAY)
    a2 = _seed_booked(clinic, p2, SERVICE_DAY)
    _seed_booked(clinic, p3, SERVICE_DAY)
    a4 = _seed_booked(clinic, p4, SERVICE_DAY)
    a_urg = _seed_booked(
        clinic, p_urg, SERVICE_DAY, classification="URGENT", lane="MAIN"
    )
    a_emg1 = _seed_booked(
        clinic, p_emg1, SERVICE_DAY, classification="EMERGENCY", lane="EMERGENCY"
    )
    a_emg2 = _seed_booked(
        clinic, p_emg2, SERVICE_DAY, classification="EMERGENCY", lane="EMERGENCY"
    )
    overdue_day = datetime.now(CAIRO).date() - timedelta(days=1)
    a_prior = _seed_booked(clinic, p_prior, overdue_day)

    # Check in #4 then #2 (cursor=0 -> NOT late -> booked-number order: #2 ahead of #4!).
    assert (
        queue_request(
            clinic,
            clinic.owner,
            method="POST",
            path=f"/v1/appointments/{a4['id']}/check-in",
            purpose="QUEUE_CHECK_IN",
            body={
                "physical_presence_confirmed": True,
                "expected_appointment_revision": 0,
            },
            key=f"ord-{uuid4().hex}",
        )[0]
        == 200
    )
    assert (
        queue_request(
            clinic,
            clinic.owner,
            method="POST",
            path=f"/v1/appointments/{a2['id']}/check-in",
            purpose="QUEUE_CHECK_IN",
            body={
                "physical_presence_confirmed": True,
                "expected_appointment_revision": 0,
            },
            key=f"ord-{uuid4().hex}",
        )[0]
        == 200
    )
    # Check in Urgent on MAIN -> must sit at position 1 ahead of Routine #2 and #4.
    assert (
        queue_request(
            clinic,
            clinic.owner,
            method="POST",
            path=f"/v1/appointments/{a_urg['id']}/check-in",
            purpose="QUEUE_CHECK_IN",
            body={
                "physical_presence_confirmed": True,
                "expected_appointment_revision": 0,
            },
            key=f"ord-{uuid4().hex}",
        )[0]
        == 200
    )

    # Check in Emergency E1 then E2 -> independent EMERGENCY FIFO.
    for appt in (a_emg1, a_emg2):
        assert (
            queue_request(
                clinic,
                clinic.owner,
                method="POST",
                path=f"/v1/appointments/{appt['id']}/check-in",
                purpose="QUEUE_CHECK_IN",
                body={
                    "physical_presence_confirmed": True,
                    "expected_appointment_revision": 0,
                },
                key=f"emg-{uuid4().hex}",
            )[0]
            == 200
        )

    # Check in overdue prior-day Booked -> stays on its original day.
    assert (
        queue_request(
            clinic,
            clinic.owner,
            method="POST",
            path=f"/v1/appointments/{a_prior['id']}/check-in",
            purpose="QUEUE_CHECK_IN",
            body={
                "physical_presence_confirmed": True,
                "expected_appointment_revision": 0,
            },
            key=f"prior-{uuid4().hex}",
        )[0]
        == 200
    )

    # Verify MAIN lane order: Urgent (pos 1), Routine #2 (pos 2), Routine #4 (pos 3).
    # Unarrived #1 and #3 do NOT appear! Prior-day Waiting does NOT appear!
    s_main, main_q = queue_request(
        clinic,
        clinic.owner,
        method="GET",
        path="/v1/queue",
        purpose="QUEUE_READ",
        query={"date": SERVICE_DAY.isoformat(), "lane": "MAIN"},
    )
    assert s_main == 200, main_q
    entries = main_q["data"]["entries"]
    assert [
        (e["appointment_id"], e["position"], e["booking_number"]) for e in entries
    ] == [
        (a_urg["id"], 1, a_urg["number"]),
        (a2["id"], 2, a2["number"]),
        (a4["id"], 3, a4["number"]),
    ]
    assert main_q["data"]["total_waiting"] == 3

    # Verify EMERGENCY lane is independent FIFO.
    s_emg, emg_q = queue_request(
        clinic,
        clinic.owner,
        method="GET",
        path="/v1/queue",
        purpose="QUEUE_READ",
        query={"date": SERVICE_DAY.isoformat(), "lane": "EMERGENCY"},
    )
    assert s_emg == 200, emg_q
    assert [e["appointment_id"] for e in emg_q["data"]["entries"]] == [
        a_emg1["id"],
        a_emg2["id"],
    ]

    # Verify the overdue day's lane has only a_prior.
    s_pr, pr_q = queue_request(
        clinic,
        clinic.owner,
        method="GET",
        path="/v1/queue",
        purpose="QUEUE_READ",
        query={"date": overdue_day.isoformat(), "lane": "MAIN"},
    )
    assert s_pr == 200
    assert pr_q["data"]["service_date"] == overdue_day.isoformat()
    assert [e["appointment_id"] for e in pr_q["data"]["entries"]] == [a_prior["id"]]


def test_fr019_capacity_visibility_and_query_plan(booking_clinic) -> None:
    clinic = booking_clinic
    p_wait = UUID(_new_patient(clinic, clinic.owner, "Wait Patient"))
    p_exam = UUID(_new_patient(clinic, clinic.owner, "Exam Patient"))

    a_wait = _seed_booked(clinic, p_wait, SERVICE_DAY)
    assert (
        queue_request(
            clinic,
            clinic.owner,
            method="POST",
            path=f"/v1/appointments/{a_wait['id']}/check-in",
            purpose="QUEUE_CHECK_IN",
            body={
                "physical_presence_confirmed": True,
                "expected_appointment_revision": 0,
            },
            key=f"fr019-{uuid4().hex}",
        )[0]
        == 200
    )

    # Configure shared examination capacity = 3 and seed 1 active In Examination on ANOTHER date/lane.
    a_exam = _seed_booked(
        clinic, p_exam, PRIOR_DAY, classification="EMERGENCY", lane="EMERGENCY"
    )
    with transaction(clinic.migration, clinic.tenant) as conn:
        cap_v = append_version(
            conn,
            str(clinic.tenant),
            "CAPACITY",
            {"capacity": 3, "paused": False},
            1,
            actor_staff_id=clinic.owner.staff_id,
        )
        conn.execute(
            text("""
                INSERT INTO public.doctor_controls
                    (organization_id, doctor_staff_id, revision, capacity_policy_kind, capacity_policy_version)
                VALUES (:tenant, :doctor, 1, 'CAPACITY', :cap_v)
                ON CONFLICT (organization_id, doctor_staff_id)
                DO UPDATE SET revision = public.doctor_controls.revision + 1,
                              capacity_policy_version = :cap_v
            """),
            {
                "tenant": clinic.tenant,
                "doctor": UUID(clinic.owner.staff_id),
                "cap_v": cap_v,
            },
        )
        # Transition a_exam to In Examination on PRIOR_DAY EMERGENCY lane to prove cross-date/lane count.
        _admit_saved(conn, clinic.tenant, UUID(clinic.owner.staff_id), a_exam)

    # FR-019 positive: ordinary Receptionist sees configured shared capacity (3) AND active In Examination count (1).
    s_rec, rec_q = queue_request(
        clinic,
        clinic.receptionist,
        method="GET",
        path="/v1/queue",
        purpose="QUEUE_READ",
        query={"date": SERVICE_DAY.isoformat(), "lane": "MAIN"},
    )
    assert s_rec == 200, rec_q
    data = rec_q["data"]
    assert data["examination_capacity"] == 3
    assert data["active_in_examination_count"] == 1
    assert data["paused"] is False
    assert data["admission_mode"] == "BOTH"
    # No Doctor clinical narrative leaked to Receptionist DTO.
    for entry in data["entries"]:
        assert not (
            set(entry)
            & {
                "diagnosis",
                "findings",
                "examination",
                "medical_notes",
                "treatment",
                "tests",
            }
        )

    # FR-019 negative: QUEUE_READ grants ZERO capacity-settings mutation rights to ordinary Receptionist.
    with (
        signed_test_transaction(
            clinic,
            clinic.receptionist,
            method="GET",
            path="/v1/queue",
            purpose="QUEUE_READ",
            query=f"date={SERVICE_DAY.isoformat()}&lane=MAIN",
        ) as (conn, _actor),
        pytest.raises(DBAPIError),
    ):
        conn.execute(
            text("""
                UPDATE public.doctor_controls
                SET revision = revision + 1
                WHERE organization_id = :tenant
            """),
            {"tenant": clinic.tenant},
        )

    _owner_changes_manager(clinic, True, 0)
    manager = _fresh_receptionist_session(clinic, 1)
    for actor in (clinic.owner, manager):
        status, payload = queue_request(
            clinic,
            actor,
            method="GET",
            path="/v1/queue",
            purpose="QUEUE_READ",
            query={"date": SERVICE_DAY.isoformat(), "lane": "MAIN"},
        )
        assert status == 200 and payload["data"] == data
    with (
        signed_test_transaction(
            clinic,
            manager,
            method="GET",
            path="/v1/queue",
            purpose="QUEUE_READ",
            query=f"date={SERVICE_DAY.isoformat()}&lane=MAIN",
        ) as (conn, _actor),
        pytest.raises(DBAPIError),
    ):
        conn.execute(
            text(
                "UPDATE public.doctor_controls SET revision=revision+1 WHERE organization_id=:tenant"
            ),
            {"tenant": clinic.tenant},
        )


def _check_in(clinic, appointment):
    status, payload = queue_request(
        clinic,
        clinic.owner,
        method="POST",
        path=f"/v1/appointments/{appointment['id']}/check-in",
        purpose="QUEUE_CHECK_IN",
        body={"physical_presence_confirmed": True, "expected_appointment_revision": 0},
        key=str(uuid4()),
    )
    assert status == 200, payload
    return payload["data"]


@pytest.mark.parametrize(
    "depth,order,expected",
    [
        (0, "BOOKED_NUMBER", [0, 2, 3, 4]),
        (1, "BOOKED_NUMBER", [2, 0, 3, 4]),
        (5, "BOOKED_NUMBER", [2, 3, 4, 0]),
        (5, "ARRIVAL_FIFO", [4, 3, 2, 0]),
    ],
)
def test_actual_late_depth_and_fifo(booking_clinic, depth, order, expected):
    clinic = booking_clinic
    with transaction(clinic.migration, clinic.tenant) as conn:
        append_version(
            conn,
            str(clinic.tenant),
            "QUEUE",
            {"routine_order": order, "late_depth": depth},
            1,
            actor_staff_id=clinic.owner.staff_id,
            today=PRIOR_DAY,
        )
    appointments = [
        _book(clinic, clinic.owner, _new_patient(clinic, clinic.owner, f"Ordering{i}"))
        for i in range(5)
    ]
    # Seed a genuine prior ordinary admission via D-06/D-07 fixtures, not a new B-15 API.
    with transaction(clinic.migration, clinic.tenant) as conn:
        _admit_saved(
            conn,
            clinic.tenant,
            UUID(clinic.owner.staff_id),
            appointments[1],
            ordinary_routine_head=True,
        )
    for index in (4, 3, 2, 0):
        _check_in(clinic, appointments[index])
    status, payload = queue_request(
        clinic,
        clinic.owner,
        method="GET",
        path="/v1/queue",
        purpose="QUEUE_READ",
        query={"date": SERVICE_DAY.isoformat(), "lane": "MAIN"},
    )
    assert status == 200, payload
    data = payload["data"]
    assert data["routine_cursor"] == appointments[1]["number"]
    assert data["routine_order"] == order and data["late_depth"] == depth
    assert [entry["appointment_id"] for entry in data["entries"]] == [
        appointments[i]["id"] for i in expected
    ]
    assert [entry["position"] for entry in data["entries"]] == [1, 2, 3, 4]
    with transaction(clinic.migration, clinic.tenant) as conn:
        assert (
            conn.scalar(
                text(
                    "SELECT count(*) FROM public.visits WHERE organization_id=:tenant"
                ),
                {"tenant": clinic.tenant},
            )
            == 1
        )
        assert (
            conn.scalar(
                text(
                    "SELECT count(*) FROM public.outbox_events WHERE organization_id=:tenant AND event_type='PATIENT_CHECKED_IN'"
                ),
                {"tenant": clinic.tenant},
            )
            == 4
        )


def test_representative_production_queue_query_plan(booking_clinic):
    clinic = booking_clinic
    chosen = []
    for index in range(75):
        day = SERVICE_DAY if index < 25 else PRIOR_DAY
        lane = "EMERGENCY" if index >= 50 else "MAIN"
        appointment = _seed_booked(
            clinic,
            UUID(_new_patient(clinic, clinic.owner, f"Plan{index}")),
            day,
            classification="EMERGENCY" if lane == "EMERGENCY" else "ROUTINE",
            lane=lane,
        )
        _check_in(clinic, appointment)
        if index < 25:
            chosen.append(appointment["id"])
    with clinic.migration.begin() as conn:
        conn.execute(
            text("ANALYZE public.queue_entries, public.appointments, public.patients")
        )
    statement = []

    def capture(_conn, _cursor, sql, params, _context, _executemany):
        if "WITH ordered AS" in sql:
            statement.append((sql, params))

    query = QueueQuery(date=SERVICE_DAY, lane="MAIN", limit=10, offset=5)
    with signed_test_transaction(
        clinic,
        clinic.receptionist,
        method="GET",
        path="/v1/queue",
        purpose="QUEUE_READ",
        query=canonical_query(urlencode(query.model_dump(mode="json"))),
    ) as (conn, actor):
        event.listen(conn, "before_cursor_execute", capture)
        try:
            data, _ = dated_queue_projection(conn, actor, query)
        finally:
            event.remove(conn, "before_cursor_execute", capture)
        assert data["total_waiting"] == len(chosen)
        assert [entry["appointment_id"] for entry in data["entries"]] == chosen[5:15]
        assert len(statement) == 1
        sql, params = statement[0]
        plan = conn.exec_driver_sql(
            "EXPLAIN (ANALYZE, BUFFERS, FORMAT JSON) " + sql, params
        ).scalar_one()[0]["Plan"]
    assert plan["Node Type"] == "Limit" and plan["Actual Rows"] == 10
    assert "Shared Hit Blocks" in plan

    def summary(node):
        return {
            **{
                key: node[key]
                for key in (
                    "Node Type",
                    "Relation Name",
                    "Index Name",
                    "Actual Rows",
                    "Actual Loops",
                    "Shared Hit Blocks",
                    "Shared Read Blocks",
                )
                if key in node
            },
            "Plans": [summary(child) for child in node.get("Plans", [])],
        }

    print("B12_QUEUE_PLAN", json.dumps(summary(plan), sort_keys=True))

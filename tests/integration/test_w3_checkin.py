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
from carepulse.identity.signing import canonical_payload, canonical_query
from carepulse.main import app
from carepulse.policies.versions import append_version
from runtime_identity_fixture import db_proof_headers, signed_test_transaction
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from test_booking import booking_clinic as booking_clinic  # noqa: PLC0414
from test_booking import booking_request
from test_database import admin as admin  # noqa: PLC0414
from test_database import disposable_project as disposable_project  # noqa: PLC0414
from test_database import empty_database as empty_database  # noqa: PLC0414
from test_patients import create_patient, private_request
from test_patients import patient_backend as patient_backend  # noqa: PLC0414
from test_pricing import _fresh_receptionist_session, _owner_changes_manager
from test_request_boundary import boundary_data as boundary_data  # noqa: PLC0414
from test_schedule import SERVICE_DAY

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


def _book(clinic, actor, patient_id: str, service_date: date = SERVICE_DAY) -> dict:
    status, reply = booking_request(
        clinic,
        actor,
        body={
            "patient_id": patient_id,
            "service_date": service_date.isoformat(),
            "service_type": "NEW_EXAMINATION",
            "expected_schedule_revision": 2,
            "expected_price_revision": 2,
            "expected_followup_revision": 1,
        },
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
    with clinic.migration.begin() as conn:
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
                     base_egp, net_egp, is_free, pricing_version, creation_standard_base_egp)
                VALUES (:tenant, :id, 'NEW_EXAMINATION', :classification,
                        :base, :base, false, 1, '100.00')
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
    return {"id": str(appointment_id), "number": number, "revision": 0}


def test_checkin_roles_archived_prebooking_replay_and_no_visit(booking_clinic) -> None:
    clinic = booking_clinic
    p_doc = create_patient(clinic, clinic.owner, name="Doc Checkin")
    p_rec = create_patient(clinic, clinic.owner, name="Rec Checkin")
    p_mgr = create_patient(clinic, clinic.owner, name="Mgr Checkin")
    p_arch = create_patient(clinic, clinic.owner, name="Archived Checkin")

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
    _owner_changes_manager(clinic, True)
    mgr = _fresh_receptionist_session(clinic)
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
    p_neg = create_patient(clinic, clinic.owner, name="No Presence")
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
    with clinic.migration.begin() as conn:
        append_version(
            conn,
            str(clinic.tenant),
            "QUEUE",
            {"routine_order": "BOOKED_NUMBER", "late_depth": 1},
            1,
            actor_staff_id=clinic.owner.staff_id,
            today=PRIOR_DAY,
        )

    p1 = UUID(create_patient(clinic, clinic.owner, name="P1"))
    p2 = UUID(create_patient(clinic, clinic.owner, name="P2"))
    p3 = UUID(create_patient(clinic, clinic.owner, name="P3"))
    p4 = UUID(create_patient(clinic, clinic.owner, name="P4"))
    p_urg = UUID(create_patient(clinic, clinic.owner, name="PUrg"))
    p_emg1 = UUID(create_patient(clinic, clinic.owner, name="PEmg1"))
    p_emg2 = UUID(create_patient(clinic, clinic.owner, name="PEmg2"))
    p_prior = UUID(create_patient(clinic, clinic.owner, name="PPrior"))

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
    a_prior = _seed_booked(clinic, p_prior, PRIOR_DAY)

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

    # Check in overdue prior-day Booked -> stays on PRIOR_DAY, never enters SERVICE_DAY.
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

    # Verify PRIOR_DAY lane has only a_prior.
    s_pr, pr_q = queue_request(
        clinic,
        clinic.owner,
        method="GET",
        path="/v1/queue",
        purpose="QUEUE_READ",
        query={"date": PRIOR_DAY.isoformat(), "lane": "MAIN"},
    )
    assert s_pr == 200
    assert [e["appointment_id"] for e in pr_q["data"]["entries"]] == [a_prior["id"]]


def test_fr019_capacity_visibility_and_query_plan(booking_clinic) -> None:
    clinic = booking_clinic
    p_wait = UUID(create_patient(clinic, clinic.owner, name="Wait Patient"))
    p_exam = UUID(create_patient(clinic, clinic.owner, name="Exam Patient"))

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
    with clinic.migration.begin() as conn:
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
        conn.execute(
            text("""
                UPDATE public.appointments
                SET status = 'In Examination', revision = 1
                WHERE organization_id = :tenant AND id = :id
            """),
            {"tenant": clinic.tenant, "id": UUID(a_exam["id"])},
        )

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

    # Representative PostgreSQL query plan inspection for bounded tenant/date/lane GET /queue projection.
    with clinic.migration.begin() as conn:
        plan_json = conn.scalar(
            text("""
                EXPLAIN (FORMAT JSON)
                WITH ordered AS (
                    SELECT e.id, e.appointment_id, e.routine_rank, e.arrival_sequence,
                           ROW_NUMBER() OVER (
                               ORDER BY
                                   CASE WHEN e.lane = 'MAIN' AND a.classification = 'URGENT' THEN 0 ELSE 1 END ASC,
                                   CASE WHEN e.lane = 'MAIN' AND a.classification = 'ROUTINE' THEN e.routine_rank ELSE e.arrival_sequence END ASC,
                                   e.id ASC
                           ) AS position
                    FROM public.queue_entries e
                    JOIN public.appointments a
                      ON a.organization_id = e.organization_id AND a.id = e.appointment_id
                    JOIN public.patients p
                      ON p.organization_id = e.organization_id AND p.id = e.patient_id
                    WHERE e.organization_id = :tenant
                      AND e.service_date = :day
                      AND e.lane = 'MAIN'
                      AND e.disposition = 'ACTIVE'
                      AND a.service_date = :day
                      AND a.status = 'Waiting'
                )
                SELECT * FROM ordered ORDER BY position ASC LIMIT 50 OFFSET 0
            """),
            {"tenant": clinic.tenant, "day": SERVICE_DAY},
        )
    assert isinstance(plan_json, list) and plan_json[0]["Plan"]["Node Type"] == "Limit"

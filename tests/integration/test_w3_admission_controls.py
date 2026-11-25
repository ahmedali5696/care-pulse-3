"""T022: B-14 signed role controls and exact-case confirmation visibility."""

import json
from dataclasses import replace
from datetime import timedelta
from uuid import UUID, uuid4

import pytest
from carepulse.db import transaction
from runtime_identity_fixture import signed_test_transaction
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from test_appointment_persistence import _admit_saved
from test_booking import booking_clinic as booking_clinic  # noqa: PLC0414
from test_patients import patient_backend as patient_backend  # noqa: PLC0414
from test_pricing import _fresh_receptionist_session, _owner_changes_manager
from test_request_boundary import boundary_data as boundary_data  # noqa: PLC0414
from test_w3_checkin import (
    PRIOR_DAY,
    SERVICE_DAY,
    _new_patient,
    _seed_booked,
    queue_request,
)
from test_w3_receipt_boundary import _foreign_appointment


def control_request(
    clinic, actor, operation, *, body=None, key=None, confirmation_id=None, query=None
):
    method, path, purpose = {
        "mode_read": ("GET", "/v1/settings/admission", "ADMISSION_SETTINGS_READ"),
        "mode": ("PATCH", "/v1/settings/admission", "ADMISSION_MODE_UPDATE"),
        "capacity": ("PATCH", "/v1/queue/capacity", "QUEUE_CAPACITY_UPDATE"),
        "pause": ("POST", "/v1/queue/pause", "QUEUE_PAUSE"),
        "issue": (
            "POST",
            "/v1/queue/admission-confirmations",
            "QUEUE_CONFIRMATION_ISSUE",
        ),
        "revoke": (
            "POST",
            f"/v1/queue/admission-confirmations/{confirmation_id}/revoke",
            "QUEUE_CONFIRMATION_REVOKE",
        ),
        "usable": (
            "GET",
            "/v1/queue/admission-confirmations/usable",
            "QUEUE_CONFIRMATION_USABLE",
        ),
    }[operation]
    return queue_request(
        clinic,
        actor,
        method=method,
        path=path,
        purpose=purpose,
        body=body,
        query=query,
        key=key if key is not None else (str(uuid4()) if method != "GET" else ""),
    )


def control_revisions(clinic):
    with clinic.migration.connect() as conn:
        rows = dict(
            conn.execute(
                text("""
            SELECT kind, revision FROM public.clinic_policy_heads
            WHERE organization_id=:tenant AND kind IN ('CAPACITY','ADMISSION')
        """),
                {"tenant": clinic.tenant},
            )
            .tuples()
            .all()
        )
        control = conn.scalar(
            text(
                "SELECT revision FROM public.doctor_controls WHERE organization_id=:tenant"
            ),
            {"tenant": clinic.tenant},
        )
    return {
        "expected_control_revision": control,
        "expected_capacity_revision": rows["CAPACITY"],
        "expected_admission_revision": rows["ADMISSION"],
    }


def seed_active_exam(clinic):
    saved = _seed_booked(
        clinic,
        UUID(_new_patient(clinic, clinic.owner, "ActiveExam")),
        PRIOR_DAY,
        classification="EMERGENCY",
        lane="EMERGENCY",
    )
    with transaction(clinic.migration, clinic.tenant) as conn:
        _admit_saved(conn, clinic.tenant, UUID(clinic.owner.staff_id), saved)
    return saved


def seed_waiting(clinic, *, day=SERVICE_DAY, lane="MAIN"):
    saved = _seed_booked(
        clinic,
        UUID(_new_patient(clinic, clinic.owner, "WaitingCase")),
        day,
        classification="EMERGENCY" if lane == "EMERGENCY" else "ROUTINE",
        lane=lane,
    )
    with clinic.migration.connect() as conn:
        revision = conn.scalar(
            text(
                "SELECT revision FROM public.queue_days WHERE organization_id=:tenant AND service_date=:day AND lane=:lane"
            ),
            {"tenant": clinic.tenant, "day": day, "lane": lane},
        )
    status, reply = queue_request(
        clinic,
        clinic.owner,
        method="POST",
        path=f"/v1/appointments/{saved['id']}/check-in",
        purpose="QUEUE_CHECK_IN",
        body={
            "physical_presence_confirmed": True,
            "expected_appointment_revision": 0,
            "expected_queue_day_revision": revision if revision is not None else 0,
        },
        key=str(uuid4()),
    )
    assert status == 200, reply
    return reply["data"]


def issue_body(clinic, waiting):
    with clinic.migration.connect() as conn:
        appointment, queue_day = conn.execute(
            text("""
            SELECT a.revision, q.revision FROM public.appointments a
            JOIN public.queue_days q ON q.organization_id=a.organization_id AND q.doctor_staff_id=a.doctor_staff_id
                AND q.service_date=a.service_date AND q.lane=a.lane
            WHERE a.organization_id=:tenant AND a.id=:id
        """),
            {"tenant": clinic.tenant, "id": UUID(waiting["appointment_id"])},
        ).one()
    return {
        "appointment_id": waiting["appointment_id"],
        "patient_id": waiting["patient_id"],
        "date": waiting["service_date"],
        "lane": waiting["lane"],
        "expected_appointment_revision": appointment,
        "expected_queue_day_revision": queue_day,
        **control_revisions(clinic),
    }


def _capacity_body(clinic, capacity):
    revisions = control_revisions(clinic)
    return {
        "capacity": capacity,
        "expected_control_revision": revisions["expected_control_revision"],
        "expected_capacity_revision": revisions["expected_capacity_revision"],
    }


def _mode_body(clinic, mode):
    return {
        "mode": mode,
        "expected_admission_revision": control_revisions(clinic)[
            "expected_admission_revision"
        ],
    }


def _pause_body(clinic, paused):
    return {
        "paused": paused,
        "expected_control_revision": control_revisions(clinic)[
            "expected_control_revision"
        ],
    }


def _revoke_body(clinic):
    return {
        "revoke_reason": "Synthetic authority withdrawn",
        "expected_control_revision": control_revisions(clinic)[
            "expected_control_revision"
        ],
    }


def _query(waiting):
    return {
        "appointment_id": waiting["appointment_id"],
        "date": waiting["service_date"],
        "lane": waiting["lane"],
    }


def _effects(clinic, *, tenant=None):
    with clinic.migration.connect() as conn:
        return tuple(
            tuple(
                conn.scalars(
                    text(
                        f"SELECT to_jsonb(t) FROM public.{table} t WHERE organization_id=:tenant ORDER BY to_jsonb(t)::text"
                    ),
                    {"tenant": tenant if tenant is not None else clinic.tenant},
                )
            )
            for table in (
                "clinic_policy_heads",
                "clinic_policy_versions",
                "doctor_controls",
                "doctor_admission_confirmations",
                "appointments",
                "queue_days",
                "queue_entries",
                "visits",
                "sensitive_audit",
                "outbox_events",
                "idempotency_receipts",
            )
        )


def _success(
    clinic, actor, operation, *, body, key=None, confirmation_id=None, status=200
):
    key = key or str(uuid4())
    code, reply = control_request(
        clinic, actor, operation, body=body, key=key, confirmation_id=confirmation_id
    )
    assert code == status, reply
    action, event_name = {
        "mode": ("settings.admission.update", "ADMISSION_MODE_CHANGED"),
        "capacity": ("queue.capacity", "EXAMINATION_CAPACITY_CHANGED"),
        "pause": ("queue.pause", "DOCTOR_PAUSE_CHANGED"),
        "issue": ("queue.confirmation.issue", "DOCTOR_CONFIRMATION_ISSUED"),
        "revoke": ("queue.confirmation.revoke", "DOCTOR_CONFIRMATION_REVOKED"),
    }[operation]
    with clinic.migration.connect() as conn:
        receipt = conn.execute(
            text("""
            SELECT actor_id::text, staff_id::text, result_projection, result_revision
            FROM public.idempotency_receipts WHERE organization_id=:tenant AND operation=:action AND idempotency_key=:key
        """),
            {"tenant": clinic.tenant, "action": action, "key": key},
        ).one()
        assert receipt == (
            actor.staff_id,
            actor.staff_id,
            reply["data"],
            reply["revision"],
        )
        audit = conn.execute(
            text("""
            SELECT actor_staff_id::text, actor_role, after_state FROM public.sensitive_audit
            WHERE organization_id=:tenant AND action=:action AND after_state=CAST(:projection AS jsonb)
        """),
            {
                "tenant": clinic.tenant,
                "action": action,
                "projection": json.dumps(reply["data"]),
            },
        ).all()
        assert audit == [(actor.staff_id, actor.role, reply["data"])]
        assert (
            conn.scalar(
                text("""
            SELECT count(*) FROM public.outbox_events WHERE organization_id=:tenant AND event_type=:event
                AND payload=CAST(:projection AS jsonb)
        """),
                {
                    "tenant": clinic.tenant,
                    "event": event_name,
                    "projection": json.dumps(reply["data"]),
                },
            )
            == 1
        )
    return reply


def _denied(clinic, actor, operation, status, **kwargs):
    before = _effects(clinic)
    code, reply = control_request(clinic, actor, operation, **kwargs)
    assert code == status, reply
    assert "data" not in reply
    assert _effects(clinic) == before


def test_signed_mode_manager_revocation_and_owner_controls(booking_clinic):
    clinic = booking_clinic
    owner, receptionist = clinic.owner, clinic.receptionist
    status, initial = control_request(clinic, owner, "mode_read")
    assert status == 200 and initial["data"]["mode"] == "BOTH"
    _denied(clinic, receptionist, "mode_read", 403)
    _denied(clinic, receptionist, "mode", 403, body=_mode_body(clinic, "DOCTOR"))
    _owner_changes_manager(clinic, True, 0)
    manager = _fresh_receptionist_session(clinic, 1)
    body = _mode_body(clinic, "RECEPTIONIST")
    key = str(uuid4())
    updated = _success(clinic, manager, "mode", body=body, key=key)
    assert updated["data"] == {
        "mode": "RECEPTIONIST",
        "admission_revision": body["expected_admission_revision"] + 1,
    }
    assert control_request(clinic, manager, "mode_read") == (200, updated)
    before = _effects(clinic)
    assert control_request(clinic, manager, "mode", body=body, key=key) == (
        200,
        updated,
    )
    status, conflict = control_request(
        clinic, manager, "mode", body={**body, "mode": "DOCTOR"}, key=key
    )
    assert status == 409 and conflict["error"]["code"] == "IDEMPOTENCY_CONFLICT"
    assert _effects(clinic) == before
    for operation, forbidden in (
        ("capacity", _capacity_body(clinic, 3)),
        ("pause", _pause_body(clinic, True)),
    ):
        _denied(clinic, manager, operation, 403, body=forbidden)
    _owner_changes_manager(clinic, False, 1)
    _denied(clinic, manager, "mode", 401, body=body, key=key)
    current_receptionist = _fresh_receptionist_session(clinic, 2)
    _denied(clinic, current_receptionist, "mode", 403, body=body, key=key)
    _denied(
        clinic, current_receptionist, "capacity", 403, body=_capacity_body(clinic, 3)
    )
    _denied(clinic, current_receptionist, "pause", 403, body=_pause_body(clinic, True))
    capacity = _capacity_body(clinic, 3)
    changed = _success(clinic, owner, "capacity", body=capacity)
    assert changed["data"] == {
        "capacity": 3,
        "paused": False,
        "capacity_revision": capacity["expected_capacity_revision"] + 1,
        "control_revision": capacity["expected_control_revision"] + 1,
    }
    _denied(clinic, owner, "capacity", 409, body=capacity)
    paused = _success(clinic, owner, "pause", body=_pause_body(clinic, True))
    assert paused["data"]["paused"] is True and paused["data"]["capacity"] == 3
    resumed = _success(clinic, owner, "pause", body=_pause_body(clinic, False))
    assert resumed["data"]["paused"] is False


def test_exact_case_confirmation_read_and_write_scope(booking_clinic):
    clinic = booking_clinic
    owner, receptionist = clinic.owner, clinic.receptionist
    _success(clinic, owner, "capacity", body=_capacity_body(clinic, 3))
    seed_active_exam(clinic)
    waiting = seed_waiting(clinic)
    other = seed_waiting(clinic, lane="EMERGENCY")
    _success(clinic, owner, "mode", body=_mode_body(clinic, "RECEPTIONIST"))
    body = issue_body(clinic, waiting)
    _denied(clinic, receptionist, "issue", 403, body=body)
    _denied(
        clinic, owner, "issue", 404, body={**body, "patient_id": other["patient_id"]}
    )
    issued = _success(clinic, owner, "issue", body=body, status=201)
    confirmation = issued["data"]["id"]
    assert issued["data"]["state"] == "ISSUED"
    assert owner.session_id != receptionist.session_id
    query = _query(waiting)
    before = _effects(clinic)
    status, usable = control_request(clinic, receptionist, "usable", query=query)
    assert status == 200, usable
    assert usable["data"] == {
        k: v for k, v in issued["data"].items() if k not in ("state", "issued_at")
    }
    assert usable["revision"] == issued["revision"]
    assert _effects(clinic) == before
    _denied(clinic, owner, "usable", 404, query=query)
    for mismatch in (
        _query(other),
        {**query, "lane": "EMERGENCY"},
        {**query, "date": (SERVICE_DAY + timedelta(days=1)).isoformat()},
        {**query, "appointment_id": str(uuid4())},
    ):
        _denied(clinic, receptionist, "usable", 404, query=mismatch)
    _denied(
        clinic,
        replace(receptionist, security_revision="999"),
        "usable",
        401,
        query=query,
    )
    _denied(
        clinic,
        replace(receptionist, organization_id=str(clinic.other)),
        "usable",
        403,
        query=query,
    )
    foreign = _foreign_appointment(clinic)
    foreign_session = f"synthetic-b14-foreign-{uuid4()}"
    with clinic.migration.begin() as conn:
        foreign_staff, foreign_user, foreign_revision = conn.execute(
            text(
                "SELECT id, auth_user_id, security_revision FROM public.staff_accounts WHERE organization_id=:tenant AND role='DOCTOR_OWNER'"
            ),
            {"tenant": clinic.other},
        ).one()
        conn.execute(
            text("""
                INSERT INTO public."session" (id, "userId", token, "expiresAt", "createdAt", "updatedAt",
                    "securityRevision", "absoluteDeadline", "lastActivity")
                VALUES (:session, :user, :token, now()+interval '1 hour', now(), now(), :revision,
                    now()+interval '1 hour', now())
            """),
            {
                "session": foreign_session,
                "user": foreign_user,
                "token": f"synthetic-{uuid4()}",
                "revision": foreign_revision,
            },
        )
    foreign_actor = replace(
        owner,
        organization_id=str(clinic.other),
        staff_id=str(foreign_staff),
        session_id=foreign_session,
        security_revision=str(foreign_revision),
    )
    assert control_request(clinic, foreign_actor, "mode_read")[0] == 200
    foreign_before = _effects(clinic, tenant=clinic.other)
    _denied(clinic, foreign_actor, "usable", 404, query=query)
    _denied(clinic, foreign_actor, "issue", 404, body=body)
    assert _effects(clinic, tenant=clinic.other) == foreign_before
    _denied(
        clinic,
        receptionist,
        "usable",
        404,
        query={**query, "appointment_id": str(foreign)},
    )
    _denied(clinic, owner, "issue", 404, body={**body, "appointment_id": str(foreign)})
    _denied(
        clinic,
        receptionist,
        "revoke",
        403,
        body=_revoke_body(clinic),
        confirmation_id=confirmation,
    )
    _denied(
        clinic,
        owner,
        "revoke",
        404,
        body=_revoke_body(clinic),
        confirmation_id=str(uuid4()),
    )
    with signed_test_transaction(
        clinic,
        receptionist,
        method="GET",
        path="/v1/queue/admission-confirmations/usable",
        purpose="QUEUE_CONFIRMATION_USABLE",
        query=f"appointment_id={query['appointment_id']}&date={query['date']}&lane={query['lane']}",
    ) as (conn, _actor):
        assert conn.scalar(text("SELECT session_user")) == "carepulse_api"
        assert (
            conn.scalar(text("SELECT public.f01_b14_usable()"))["projection"]
            == usable["data"]
        )
        for statement in (
            "SELECT public.f01_b14_mutate(5)",
            "SELECT public.f01_b14_replay()",
            "SELECT public.f01_b14_settings()",
            "SELECT * FROM public.doctor_admission_confirmations",
            "UPDATE public.doctor_admission_confirmations SET state='REVOKED'",
        ):
            with pytest.raises(DBAPIError) as denied, conn.begin_nested():
                conn.execute(text(statement))
            assert denied.value.orig.sqlstate == "42501"
    assert _effects(clinic) == before
    with clinic.runtime.connect() as conn:
        for statement in (
            "SELECT public.f01_b14_usable()",
            "SELECT public.f01_b14_mutate(5)",
        ):
            with pytest.raises(DBAPIError) as denied, conn.begin_nested():
                conn.execute(text(statement))
            assert denied.value.orig.sqlstate == "42501"
    _owner_changes_manager(clinic, True, 0)
    manager = _fresh_receptionist_session(clinic, 1)
    _denied(clinic, manager, "issue", 403, body=body)
    _denied(
        clinic,
        manager,
        "revoke",
        403,
        body=_revoke_body(clinic),
        confirmation_id=confirmation,
    )
    assert control_request(clinic, manager, "usable", query=query) == (200, usable)
    with clinic.migration.begin() as conn:
        # An ACTIVE clinic must retain its verified Owner. Suspend before
        # invalidating the issuer, preserving the inherited database invariant.
        conn.execute(
            text("UPDATE public.organizations SET state='SUSPENDED' WHERE id=:tenant"),
            {"tenant": clinic.tenant},
        )
        conn.execute(
            text('UPDATE public."user" SET "emailVerified"=false WHERE id=:user'),
            {"user": clinic.native_user},
        )
    _denied(clinic, manager, "usable", 403, query=query)
    with clinic.migration.begin() as conn:
        conn.execute(
            text('UPDATE public."user" SET "emailVerified"=true WHERE id=:user'),
            {"user": clinic.native_user},
        )
        conn.execute(
            text("UPDATE public.organizations SET state='ACTIVE' WHERE id=:tenant"),
            {"tenant": clinic.tenant},
        )
    revoked = _success(
        clinic, owner, "revoke", body=_revoke_body(clinic), confirmation_id=confirmation
    )
    assert revoked["data"]["state"] == "REVOKED"
    _denied(clinic, manager, "usable", 404, query=query)

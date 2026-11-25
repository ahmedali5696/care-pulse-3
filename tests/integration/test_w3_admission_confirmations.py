"""T023: signed single-case Doctor confirmation lifecycle and concurrency."""

from contextlib import contextmanager
from dataclasses import replace
from datetime import datetime, timedelta
from uuid import UUID, uuid4

import pytest
from carepulse.db import transaction
from carepulse.identity.contracts import ContractError
from carepulse.main import app
from carepulse.queue.admission_contracts import IssueConfirmationCommand
from carepulse.queue.admission_control import issue_confirmation
from runtime_identity_fixture import signed_test_transaction
from sqlalchemy import text
from test_appointment_persistence import _admit_saved
from test_booking import booking_clinic as booking_clinic  # noqa: PLC0414
from test_patients import patient_backend as patient_backend  # noqa: PLC0414
from test_request_boundary import boundary_data as boundary_data  # noqa: PLC0414
from test_w3_admission_controls import (
    SERVICE_DAY,
    _capacity_body,
    _denied,
    _effects,
    _mode_body,
    _new_patient,
    _pause_body,
    _query,
    _revoke_body,
    _seed_booked,
    _success,
    control_request,
    issue_body,
    seed_active_exam,
    seed_waiting,
)
from test_w3_checkin_races import _ordered_race


def _prepare(clinic, capacity=3):
    _success(clinic, clinic.owner, "capacity", body=_capacity_body(clinic, capacity))
    seed_active_exam(clinic)
    return seed_waiting(clinic)


def _issue(clinic, waiting, *, actor=None, key=None):
    return _success(
        clinic,
        actor or clinic.owner,
        "issue",
        body=issue_body(clinic, waiting),
        key=key,
        status=201,
    )["data"]


def _confirmation_rows(clinic):
    with clinic.migration.connect() as connection:
        return connection.execute(
            text("""
                SELECT id::text, state, issuer_security_revision, issued_at, expires_at
                FROM public.doctor_admission_confirmations WHERE organization_id=:tenant
                ORDER BY issued_at, id
            """),
            {"tenant": clinic.tenant},
        ).all()


def _assert_issue_effects(before, after, expected=1):
    for index, (old, new) in enumerate(zip(before, after, strict=True)):
        if index in (3, 8, 9, 10):
            assert len(new) == len(old) + expected
            assert all(row in new for row in old)
        else:
            assert new == old


def test_configured_expiry_and_missing_config(booking_clinic, monkeypatch):
    clinic = booking_clinic
    waiting = _prepare(clinic)
    # Use the smallest valid positive synthetic configuration, not a product default.
    config = replace(
        clinic.config,
        policy=replace(
            clinic.config.policy,
            doctor_confirmation_expiry_minutes=1,
        ),
    )
    monkeypatch.setattr(app.state, "service_config", config)
    body, key = issue_body(clinic, waiting), str(uuid4())
    before = _effects(clinic)
    with clinic.migration.connect() as connection:
        earliest = connection.scalar(text("SELECT clock_timestamp()"))
    issued = _success(clinic, clinic.owner, "issue", body=body, key=key, status=201)[
        "data"
    ]
    rows = _confirmation_rows(clinic)
    assert len(rows) == 1
    identifier, state, issuer_revision, issued_at, expires_at = rows[0]
    with clinic.migration.connect() as connection:
        observed = connection.scalar(text("SELECT clock_timestamp()"))
    assert identifier == issued["id"] and state == "ISSUED"
    assert issuer_revision == int(clinic.owner.security_revision)
    assert earliest <= issued_at <= observed < expires_at
    assert expires_at - issued_at == timedelta(minutes=1)
    assert datetime.fromisoformat(issued["issued_at"]) == issued_at
    assert datetime.fromisoformat(issued["expires_at"]) == expires_at
    _assert_issue_effects(before, _effects(clinic))
    query = _query(waiting)
    status, usable = control_request(clinic, clinic.receptionist, "usable", query=query)
    assert status == 200 and usable["data"]["id"] == identifier

    # The API must fail closed when its mandatory configured lifetime is absent
    # or invalid, even if a currently valid approval is stored.
    for invalid in (None, 0):
        monkeypatch.setattr(
            app.state,
            "service_config",
            replace(
                config,
                policy=replace(
                    config.policy, doctor_confirmation_expiry_minutes=invalid
                ),
            ),
        )
        _denied(clinic, clinic.owner, "issue", 503, body=body)
        _denied(clinic, clinic.receptionist, "usable", 503, query=query)
    monkeypatch.setattr(app.state, "service_config", config)
    # Wait for the exact persisted DB-clock deadline, not an arbitrary sleep.
    # PostgreSQL may wake slightly after equality; the denial is at or after it.
    with clinic.migration.connect() as connection:
        connection.execute(
            text("SELECT pg_sleep_until(CAST(:deadline AS timestamptz))"),
            {"deadline": expires_at},
        )
        assert connection.scalar(text("SELECT clock_timestamp()")) >= expires_at
    _denied(clinic, clinic.receptionist, "usable", 404, query=query)
    snapshot = _effects(clinic)
    status, replay = control_request(clinic, clinic.owner, "issue", body=body, key=key)
    assert status == 200 and replay["data"] == issued
    assert _effects(clinic) == snapshot
    renewed = _issue(clinic, waiting)
    assert renewed["id"] != identifier
    assert _confirmation_rows(clinic)[0] == rows[0]
    _assert_issue_effects(snapshot, _effects(clinic))


def test_replay_revocation_and_bound_revision_reissue_while_paused(booking_clinic):
    clinic = booking_clinic
    waiting = _prepare(clinic)
    owner = clinic.owner
    _success(clinic, owner, "mode", body=_mode_body(clinic, "RECEPTIONIST"))
    body, issue_key = issue_body(clinic, waiting), str(uuid4())
    issued = _success(clinic, owner, "issue", body=body, key=issue_key, status=201)[
        "data"
    ]
    snapshot = _effects(clinic)
    status, replay = control_request(clinic, owner, "issue", body=body, key=issue_key)
    assert status == 200 and replay["data"] == issued
    assert _effects(clinic) == snapshot
    _denied(clinic, owner, "issue", 409, body=body)
    revoke = _revoke_body(clinic)
    _success(clinic, owner, "pause", body=_pause_body(clinic, True))
    _denied(clinic, owner, "revoke", 409, body=revoke, confirmation_id=issued["id"])
    _denied(clinic, clinic.receptionist, "usable", 404, query=_query(waiting))
    revoke, revoke_key = _revoke_body(clinic), str(uuid4())
    before_revoke = _effects(clinic)
    revoked = _success(
        clinic,
        owner,
        "revoke",
        body=revoke,
        key=revoke_key,
        confirmation_id=issued["id"],
    )["data"]
    assert revoked["state"] == "REVOKED" and revoked["id"] == issued["id"]
    snapshot = _effects(clinic)
    assert snapshot[7] == before_revoke[7]
    status, replay = control_request(
        clinic,
        owner,
        "revoke",
        body=revoke,
        key=revoke_key,
        confirmation_id=issued["id"],
    )
    assert status == 200 and replay["data"] == revoked
    assert _effects(clinic) == snapshot
    _denied(clinic, owner, "revoke", 409, body=revoke, confirmation_id=issued["id"])
    # Issuance remains possible while paused and with Receptionist-only mode.
    fresh = _issue(clinic, waiting)
    assert fresh["id"] != issued["id"]
    _denied(clinic, owner, "issue", 409, body=issue_body(clinic, waiting))
    _success(clinic, owner, "mode", body=_mode_body(clinic, "BOTH"))
    stale = _confirmation_rows(clinic)
    another = _issue(clinic, waiting)
    assert another["id"] not in (issued["id"], fresh["id"])
    assert _confirmation_rows(clinic)[:2] == stale
    _denied(clinic, clinic.receptionist, "usable", 404, query=_query(waiting))
    _success(
        clinic,
        owner,
        "revoke",
        body=_revoke_body(clinic),
        confirmation_id=another["id"],
    )
    assert _effects(clinic)[7] == before_revoke[7]


def test_issue_denies_every_revision_and_ineligible_target(booking_clinic):
    clinic = booking_clinic
    owner = clinic.owner
    # Seed a real terminal/closed-entry appointment before the active examination.
    patient = UUID(_new_patient(clinic, owner, "Terminal"))
    terminal = _seed_booked(clinic, patient, SERVICE_DAY)
    with transaction(clinic.migration, clinic.tenant) as connection:
        _admit_saved(
            connection, clinic.tenant, UUID(owner.staff_id), terminal, complete=True
        )
    terminal_case = {
        "appointment_id": terminal["id"],
        "patient_id": str(patient),
        "service_date": terminal["service_date"],
        "lane": "MAIN",
    }
    waiting = seed_waiting(clinic)
    _denied(
        clinic, owner, "issue", 409, body=issue_body(clinic, waiting)
    )  # capacity one
    _success(clinic, owner, "capacity", body=_capacity_body(clinic, 3))
    _denied(
        clinic, owner, "issue", 409, body=issue_body(clinic, waiting)
    )  # zero active
    seed_active_exam(clinic)
    _success(clinic, owner, "capacity", body=_capacity_body(clinic, 1))
    _denied(clinic, owner, "issue", 409, body=issue_body(clinic, waiting))
    _success(clinic, owner, "capacity", body=_capacity_body(clinic, 3))
    other_patient = UUID(_new_patient(clinic, owner, "Unarrived"))
    booked = _seed_booked(clinic, other_patient, SERVICE_DAY)
    booked_case = {
        "appointment_id": booked["id"],
        "patient_id": str(other_patient),
        "service_date": booked["service_date"],
        "lane": "MAIN",
    }
    body = issue_body(clinic, waiting)
    for revision, value in body.items():
        if revision.startswith("expected_"):
            _denied(clinic, owner, "issue", 409, body={**body, revision: value + 1})
    _denied(
        clinic, owner, "issue", 404, body={**body, "patient_id": str(other_patient)}
    )
    _denied(
        clinic,
        owner,
        "issue",
        404,
        body={
            **body,
            "date": (
                datetime.fromisoformat(body["date"]).date() + timedelta(days=1)
            ).isoformat(),
        },
    )
    _denied(clinic, owner, "issue", 404, body={**body, "lane": "EMERGENCY"})
    _denied(clinic, owner, "issue", 409, body=issue_body(clinic, booked_case))
    _denied(clinic, owner, "issue", 409, body=issue_body(clinic, terminal_case))
    with clinic.migration.connect() as connection:
        assert (
            connection.scalar(
                text("""
            SELECT disposition FROM public.queue_entries WHERE appointment_id=:id
        """),
                {"id": UUID(terminal["id"])},
            )
            == "CLOSED"
        )
    _denied(clinic, clinic.receptionist, "issue", 403, body=body)
    _issue(clinic, waiting)


@contextmanager
def _issue_transaction(clinic, body, key, *, retain_denial=False):
    command = IssueConfirmationCommand.model_validate(body)
    with signed_test_transaction(
        clinic,
        clinic.owner,
        method="POST",
        path="/v1/queue/admission-confirmations",
        purpose="QUEUE_CONFIRMATION_ISSUE",
        body=body,
        key=key,
    ) as (connection, actor):

        def execute():
            def issue():
                return issue_confirmation(
                    connection,
                    actor,
                    command,
                    key,
                    ttl_minutes=clinic.config.policy.doctor_confirmation_expiry_minutes,
                )

            if retain_denial:
                # The savepoint rolls back only the denied command; F-01's
                # acquired organization lock remains until the outer commit.
                with pytest.raises(ContractError) as denied, connection.begin_nested():
                    issue()
                assert denied.value.status_code == 409
                return None
            return issue()

        yield connection, execute


@pytest.mark.parametrize("already_valid", [False, True])
def test_distinct_keys_compete_at_real_f01_lock(booking_clinic, already_valid):
    clinic = booking_clinic
    waiting = _prepare(clinic)
    if already_valid:
        _issue(clinic, waiting)
    body = issue_body(clinic, waiting)
    before = _effects(clinic)
    first_key, second_key = str(uuid4()), str(uuid4())

    def checkpoint(_observer):
        assert _effects(clinic) == before

    first, second = _ordered_race(
        clinic,
        lambda: _issue_transaction(
            clinic, body, first_key, retain_denial=already_valid
        ),
        lambda: _issue_transaction(clinic, body, second_key),
        denial="CONFLICT",
        checkpoint=checkpoint,
    )
    assert second is None
    if already_valid:
        assert first is None
        assert _effects(clinic) == before
    else:
        assert first[0]["state"] == "ISSUED" and first[2] is False
        _assert_issue_effects(before, _effects(clinic))
    assert len(_confirmation_rows(clinic)) == 1
    with clinic.migration.connect() as connection:
        keys = connection.scalars(
            text("""
            SELECT idempotency_key FROM public.idempotency_receipts
            WHERE organization_id=:tenant AND operation='queue.confirmation.issue'
        """),
            {"tenant": clinic.tenant},
        ).all()
    assert len(keys) == 1 and second_key not in keys
    assert (first_key in keys) is not already_valid


def test_issuer_security_revision_survives_disable_reenable(booking_clinic):
    clinic = booking_clinic
    waiting = _prepare(clinic)
    issued = _issue(clinic, waiting)
    old_owner = clinic.owner
    fields = {"tenant": clinic.tenant, "doctor": UUID(old_owner.staff_id)}
    with clinic.migration.begin() as connection:
        connection.execute(
            text("UPDATE public.organizations SET state='SUSPENDED' WHERE id=:tenant"),
            fields,
        )
        connection.execute(
            text("""
            UPDATE public.staff_accounts SET enabled=false,security_revision=security_revision+1
            WHERE organization_id=:tenant AND id=:doctor
        """),
            fields,
        )
    _denied(clinic, clinic.receptionist, "usable", 403, query=_query(waiting))
    session = f"synthetic-b14-new-owner-{uuid4()}"
    with clinic.migration.begin() as connection:
        revision = connection.scalar(
            text("""
            UPDATE public.staff_accounts SET enabled=true,security_revision=security_revision+1
            WHERE organization_id=:tenant AND id=:doctor RETURNING security_revision
        """),
            fields,
        )
        connection.execute(
            text("UPDATE public.organizations SET state='ACTIVE' WHERE id=:tenant"),
            fields,
        )
        connection.execute(
            text("""
            INSERT INTO public."session" (id,"userId",token,"expiresAt","createdAt","updatedAt",
                "securityRevision","absoluteDeadline","lastActivity")
            SELECT :session,auth_user_id,:token,now()+interval '1 hour',now(),now(),
                security_revision,now()+interval '1 hour',now()
            FROM public.staff_accounts WHERE organization_id=:tenant AND id=:doctor
        """),
            {**fields, "session": session, "token": str(uuid4())},
        )
    assert revision == int(old_owner.security_revision) + 2
    _denied(clinic, clinic.receptionist, "usable", 404, query=_query(waiting))
    _denied(clinic, old_owner, "issue", 401, body=issue_body(clinic, waiting))
    new_owner = replace(old_owner, session_id=session, security_revision=str(revision))
    fresh = _issue(clinic, waiting, actor=new_owner)
    assert fresh["id"] != issued["id"]
    rows = _confirmation_rows(clinic)
    assert [(r[0], r[1], r[2]) for r in rows] == [
        (issued["id"], "ISSUED", int(old_owner.security_revision)),
        (fresh["id"], "ISSUED", revision),
    ]
    code, response = control_request(
        clinic, clinic.receptionist, "usable", query=_query(waiting)
    )
    assert code == 200 and response["data"]["id"] == fresh["id"]


def _seed_consumed_admission(clinic, waiting, issued):
    """Legitimate B-15 prerequisite only: matching history, Visit and closed entry."""
    fields = {
        "tenant": clinic.tenant,
        "doctor": UUID(clinic.owner.staff_id),
        "appointment": UUID(waiting["appointment_id"]),
        "confirmation": UUID(issued["id"]),
        "day": waiting["service_date"],
        "lane": waiting["lane"],
        "revision": issued["appointment_revision"] + 1,
        "history": uuid4(),
        "visit": uuid4(),
    }
    with transaction(clinic.migration, clinic.tenant) as connection:
        connection.execute(
            text("""
            SELECT * FROM public.lock_doctor_admission_capacity(:tenant,:doctor,CAST(:day AS date),:lane)
        """),
            fields,
        ).one()
        connection.execute(
            text("""
            UPDATE public.appointments SET status='In Examination',revision=:revision
            WHERE organization_id=:tenant AND id=:appointment
        """),
            fields,
        )
        connection.execute(
            text("""
            INSERT INTO public.appointment_history
                (id,organization_id,appointment_id,appointment_revision,action,old_status,new_status,
                old_service_date,new_service_date,old_number,new_number,actor_staff_id,actor_role,
                reason,price_snapshot_appointment_id)
            SELECT :history,organization_id,id,:revision,'STATUS','Waiting','In Examination',
                service_date,service_date,current_number,current_number,:doctor,'DOCTOR_OWNER',
                'Synthetic B-15 prerequisite',id FROM public.appointments WHERE id=:appointment
        """),
            fields,
        )
        connection.execute(
            text("""
            UPDATE public.queue_entries SET disposition='CLOSED',closure_history_revision=:revision,
                revision=revision+1 WHERE organization_id=:tenant AND appointment_id=:appointment
                AND disposition='ACTIVE'
        """),
            fields,
        )
        connection.execute(
            text("""
            UPDATE public.queue_days SET revision=revision+1 WHERE organization_id=:tenant
                AND id=(SELECT queue_day_id FROM public.queue_entries WHERE appointment_id=:appointment)
        """),
            fields,
        )
        connection.execute(
            text("""
            INSERT INTO public.visits
                (id,organization_id,appointment_id,patient_id,doctor_staff_id,service_date,
                admission_history_revision,admitted_at)
            SELECT :visit,organization_id,id,patient_id,doctor_staff_id,service_date,:revision,clock_timestamp()
            FROM public.appointments WHERE id=:appointment
        """),
            fields,
        )
        connection.execute(
            text("""
            UPDATE public.doctor_admission_confirmations SET state='CONSUMED',
                consuming_admission_revision=:revision,consumed_at=clock_timestamp()
            WHERE organization_id=:tenant AND id=:confirmation
        """),
            fields,
        )


def test_full_capacity_and_consumed_approval_denials(booking_clinic):
    clinic = booking_clinic
    waiting = _prepare(clinic, capacity=2)
    issued = _issue(clinic, waiting)
    _seed_consumed_admission(clinic, waiting, issued)
    assert _confirmation_rows(clinic)[0][1] == "CONSUMED"
    _denied(
        clinic,
        clinic.owner,
        "revoke",
        409,
        body=_revoke_body(clinic),
        confirmation_id=issued["id"],
    )
    _denied(clinic, clinic.receptionist, "usable", 404, query=_query(waiting))
    candidate = seed_waiting(clinic)
    _denied(clinic, clinic.owner, "issue", 409, body=issue_body(clinic, candidate))
    _denied(clinic, clinic.owner, "capacity", 409, body=_capacity_body(clinic, 1))
    with clinic.migration.connect() as connection:
        assert (
            connection.scalar(
                text("""
            SELECT count(*) FROM public.appointments WHERE organization_id=:tenant
            AND status='In Examination'
        """),
                {"tenant": clinic.tenant},
            )
            == 2
        )

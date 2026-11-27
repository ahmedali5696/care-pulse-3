"""T028: actual signed B-15 admission, evidence and authority boundaries."""

import json
from contextlib import contextmanager
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from uuid import UUID, uuid4

import pytest
from carepulse.identity.authorization import protected_transaction
from carepulse.identity.contracts import ContractError
from carepulse.main import app
from carepulse.queue.admission_contracts import AdmitCommand
from carepulse.queue.admit import recover_admission
from runtime_identity_fixture import registered_test_request, signed_test_transaction
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from test_booking import booking_clinic as booking_clinic  # noqa: PLC0414
from test_patients import patient_backend as patient_backend  # noqa: PLC0414
from test_pricing import _fresh_receptionist_session, _owner_changes_manager
from test_request_boundary import boundary_data as boundary_data  # noqa: PLC0414
from test_schedule import CAIRO
from test_w3_admission_confirmations import _foreign_confirmation, _issue
from test_w3_admission_controls import (
    _capacity_body,
    _mode_body,
    _pause_body,
    _query,
    _revoke_body,
    _success,
    control_request,
    control_revisions,
    seed_waiting,
)
from test_w3_checkin import _new_patient, _seed_booked, queue_request
from test_w3_checkin_races import _archive


def today():
    return datetime.now(CAIRO).date()


def waiting_case(clinic, *, day=None, lane="MAIN", classification=None):
    if day is None:
        day = today()
    if classification is None:
        return seed_waiting(clinic, day=day, lane=lane)
    booked = _seed_booked(
        clinic,
        UUID(_new_patient(clinic, clinic.owner, classification)),
        day,
        classification=classification,
        lane=lane,
    )
    status, queue = queue_request(
        clinic,
        clinic.owner,
        method="GET",
        path="/v1/queue",
        purpose="QUEUE_READ",
        query={"date": day.isoformat(), "lane": lane},
    )
    assert status == 200, queue
    status, reply = queue_request(
        clinic,
        clinic.owner,
        method="POST",
        path=f"/v1/appointments/{booked['id']}/check-in",
        purpose="QUEUE_CHECK_IN",
        body={
            "physical_presence_confirmed": True,
            "expected_appointment_revision": 0,
            "expected_queue_day_revision": queue["revision"],
        },
        key=str(uuid4()),
    )
    assert status == 200, reply
    return reply["data"]


def admit_body(clinic, waiting, **changes):
    status, reply = queue_request(
        clinic,
        clinic.owner,
        method="GET",
        path="/v1/queue",
        purpose="QUEUE_READ",
        query={"date": waiting["service_date"], "lane": waiting["lane"]},
    )
    assert status == 200, reply
    queue = reply["data"]
    entry = next(
        row
        for row in queue["entries"]
        if row["appointment_id"] == waiting["appointment_id"]
    )
    return {
        "appointment_id": entry["appointment_id"],
        "patient_id": entry["patient_id"],
        "date": entry["service_date"],
        "lane": entry["lane"],
        "expected_appointment_revision": entry["appointment_revision"],
        "expected_queue_entry_revision": entry["queue_entry_revision"],
        "expected_queue_day_revision": queue["queue_day_revision"],
        "expected_head_appointment_id": queue["entries"][0]["appointment_id"],
        **control_revisions(clinic),
        **changes,
    }


def admit_request(clinic, actor, body, key=None):
    return queue_request(
        clinic,
        actor,
        method="POST",
        path="/v1/queue/admit",
        purpose="QUEUE_ADMIT",
        body=body,
        key=key or str(uuid4()),
    )


def admission_effects(clinic, *, tenant=None):
    tables = (
        "patients",
        "clinic_policy_heads",
        "clinic_policy_versions",
        "doctor_controls",
        "doctor_admission_confirmations",
        "appointments",
        "appointment_history",
        "appointment_number_assignments",
        "appointment_day_counters",
        "appointment_price_snapshots",
        "queue_days",
        "queue_entries",
        "visits",
        "followup_claims",
        "sensitive_audit",
        "outbox_events",
        "idempotency_receipts",
    )
    with clinic.migration.connect() as connection:
        return {
            table: list(
                connection.scalars(
                    text(
                        f"SELECT to_jsonb(r) FROM public.{table} r WHERE organization_id=:tenant ORDER BY to_jsonb(r)::text"
                    ),
                    {"tenant": tenant or clinic.tenant},
                )
            )
            for table in tables
        }


def assert_admitted(clinic, actor, body, key, projection):
    state = admission_effects(clinic)
    target = body["appointment_id"]
    (appointment,) = [row for row in state["appointments"] if row["id"] == target]
    (visit,) = [row for row in state["visits"] if row["appointment_id"] == target]
    (entry,) = [
        row for row in state["queue_entries"] if row["appointment_id"] == target
    ]
    (history,) = [
        row
        for row in state["appointment_history"]
        if row["appointment_id"] == target and row["action"] == "ADMITTED"
    ]
    (day,) = [row for row in state["queue_days"] if row["id"] == entry["queue_day_id"]]
    revision = body["expected_appointment_revision"] + 1
    assert (
        appointment["status"],
        appointment["revision"],
        appointment["service_date"],
    ) == (
        "In Examination",
        revision,
        body["date"],
    )
    assert (
        visit["id"],
        visit["patient_id"],
        visit["doctor_staff_id"],
        visit["service_date"],
        visit["admission_history_revision"],
    ) == (
        projection["visit_id"],
        body["patient_id"],
        clinic.owner.staff_id,
        body["date"],
        revision,
    )
    assert visit["admitted_at"] == projection["admitted_at"]
    assert (
        history["actor_staff_id"],
        history["actor_role"],
        history["reason"],
        history["old_status"],
        history["new_status"],
        history["appointment_revision"],
    ) == (
        actor.staff_id,
        actor.role,
        body.get("non_head_reason"),
        "Waiting",
        "In Examination",
        revision,
    )
    assert history["old_service_date"] == history["new_service_date"] == body["date"]
    assert (
        history["old_number"] == history["new_number"] == appointment["current_number"]
    )
    assert (
        entry["disposition"],
        entry["closure_history_revision"],
        entry["closure_queue_revision"],
        entry["revision"],
    ) == (
        "CLOSED",
        revision,
        body["expected_queue_day_revision"],
        body["expected_queue_entry_revision"] + 1,
    )
    assert entry["closure_transaction_id"] is not None
    ordinary = (
        body["lane"] == "MAIN"
        and appointment["classification"] == "ROUTINE"
        and body["expected_head_appointment_id"] == target
    )
    assert entry["ordinary_head_admitted"] is ordinary
    cursor = (
        max(entry["closure_routine_cursor"], appointment["current_number"])
        if ordinary
        else entry["closure_routine_cursor"]
    )
    assert (day["revision"], day["routine_cursor"]) == (
        projection["queue_day_revision"],
        cursor,
    )
    assert projection["routine_cursor"] == cursor
    assert projection["appointment_revision"] == revision
    for name in ("control", "capacity", "admission"):
        assert projection[f"{name}_revision"] == body[f"expected_{name}_revision"]
    evidence = {
        **projection,
        "head_appointment_id": body["expected_head_appointment_id"],
        "non_head_reason": body.get("non_head_reason"),
        "physical_presence_confirmed": body.get("physical_presence_confirmed", False),
        "actor_staff_id": actor.staff_id,
        "actor_role": actor.role,
    }
    (audit,) = [
        row
        for row in state["sensitive_audit"]
        if row["action"] == "queue.admit" and row["correlation_id"] == target
    ]
    assert audit["after_state"] == evidence
    assert (audit["actor_staff_id"], audit["actor_role"]) == (
        actor.staff_id,
        actor.role,
    )
    (event,) = [
        row
        for row in state["outbox_events"]
        if row["event_type"] == "APPOINTMENT_ADMITTED" and row["aggregate_id"] == target
    ]
    assert event["payload"] == evidence
    (receipt,) = [
        row
        for row in state["idempotency_receipts"]
        if row["operation"] == "queue.admit" and row["idempotency_key"] == key
    ]
    assert (
        receipt["actor_id"],
        receipt["staff_id"],
        receipt["result_projection"],
        receipt["result_revision"],
    ) == (
        actor.staff_id,
        actor.staff_id,
        projection,
        revision,
    )
    if body.get("confirmation_id"):
        (confirmation,) = [
            row
            for row in state["doctor_admission_confirmations"]
            if row["id"] == body["confirmation_id"]
        ]
        assert confirmation["state"] == "CONSUMED"
        assert confirmation["consuming_admission_revision"] == revision
        assert confirmation["consumed_at"] < confirmation["expires_at"]


def _deny(clinic, actor, body, expected, *, key=None):
    before = admission_effects(clinic)
    status, reply = admit_request(clinic, actor, body, key)
    assert status == expected, reply
    assert set(reply) == {"error"}
    assert admission_effects(clinic) == before


def _admit(clinic, actor, body, *, key=None):
    key = key or str(uuid4())
    before = admission_effects(clinic)
    status, reply = admit_request(clinic, actor, body, key)
    assert status == 200, reply
    assert_admitted(clinic, actor, body, key, reply["data"])
    after = admission_effects(clinic)
    for table in (
        "visits",
        "appointment_history",
        "sensitive_audit",
        "outbox_events",
        "idempotency_receipts",
    ):
        assert len(after[table]) == len(before[table]) + 1, table
        assert all(row in after[table] for row in before[table])
    for table in (
        "patients",
        "clinic_policy_heads",
        "clinic_policy_versions",
        "doctor_controls",
        "appointment_number_assignments",
        "appointment_day_counters",
        "appointment_price_snapshots",
        "followup_claims",
    ):
        assert after[table] == before[table], table
    for table in ("appointments", "queue_entries", "queue_days"):
        assert len(after[table]) == len(before[table])
        assert sum(row not in after[table] for row in before[table]) == 1
    assert admit_request(clinic, actor, body, key) == (200, reply)
    assert admission_effects(clinic) == after
    return reply["data"]


@pytest.mark.parametrize("role", ["owner", "receptionist", "manager"])
@pytest.mark.parametrize(
    "lane,exception",
    [("EMERGENCY", False), ("EMERGENCY", True), ("MAIN", False), ("MAIN", True)],
)
def test_selected_lane_head_and_explicit_exception(
    booking_clinic, role, lane, exception
):
    clinic = booking_clinic
    if role == "manager":
        _owner_changes_manager(clinic, True, 0)
        actor = _fresh_receptionist_session(clinic, 1)
    else:
        actor = getattr(clinic, role)
    # A second lane never decides the selected lane's head.
    waiting_case(clinic, lane="MAIN" if lane == "EMERGENCY" else "EMERGENCY")
    if lane == "MAIN":
        routine = waiting_case(clinic)
        head = waiting_case(clinic, classification="URGENT")
        tail = routine
    else:
        head, tail = waiting_case(clinic, lane=lane), waiting_case(clinic, lane=lane)
    selected = tail if exception else head
    body = admit_body(clinic, selected, physical_presence_confirmed=True)
    assert body["expected_head_appointment_id"] == head["appointment_id"]
    if exception:
        _deny(clinic, actor, body, 409)
        body["non_head_reason"] = "Synthetic justified within-lane exception"
    else:
        _deny(
            clinic,
            actor,
            {**body, "expected_head_appointment_id": tail["appointment_id"]},
            409,
        )
    _admit(clinic, actor, body)
    _deny(clinic, actor, body, 409)


def test_original_day_presence_archive_and_denial_guards(booking_clinic):
    clinic = booking_clinic
    prior = waiting_case(clinic, day=today() - timedelta(days=1))
    current = waiting_case(clinic)
    actor = clinic.receptionist
    body = admit_body(clinic, prior)
    _deny(clinic, actor, body, 409)
    for changes, expected in (
        ({"physical_presence_confirmed": False}, 422),
        ({"actor_staff_id": clinic.owner.staff_id}, 422),
        ({"visit_id": str(uuid4())}, 422),
        ({"date": current["service_date"]}, 404),
        ({"lane": "EMERGENCY"}, 404),
        ({"patient_id": current["patient_id"]}, 404),
        ({"expected_appointment_revision": 0}, 409),
        ({"expected_queue_day_revision": body["expected_queue_day_revision"] + 1}, 409),
        (
            {
                "expected_queue_entry_revision": body["expected_queue_entry_revision"]
                + 1
            },
            409,
        ),
        ({"confirmation_id": str(uuid4())}, 409),
    ):
        _deny(
            clinic,
            actor,
            {**body, "physical_presence_confirmed": True, **changes},
            expected,
        )
    _success(clinic, clinic.owner, "pause", body=_pause_body(clinic, True))
    _deny(
        clinic, actor, admit_body(clinic, prior, physical_presence_confirmed=True), 409
    )
    _success(clinic, clinic.owner, "pause", body=_pause_body(clinic, False))
    with _archive(clinic, UUID(prior["patient_id"])) as (_, execute):
        assert execute()["archived"] is True
    _admit(clinic, actor, admit_body(clinic, prior, physical_presence_confirmed=True))
    state = admission_effects(clinic)
    (today_queue,) = [
        row
        for row in state["queue_days"]
        if row["service_date"] == current["service_date"]
    ]
    assert today_queue["routine_cursor"] == 0


def test_independent_actor_confirmation_handoff_and_scope(booking_clinic, monkeypatch):
    clinic = booking_clinic
    owner, receptionist = clinic.owner, clinic.receptionist
    assert owner.session_id != receptionist.session_id
    _success(clinic, owner, "capacity", body=_capacity_body(clinic, 3))
    first = waiting_case(clinic, lane="EMERGENCY")
    _admit(clinic, owner, admit_body(clinic, first, physical_presence_confirmed=True))
    _success(clinic, owner, "mode", body=_mode_body(clinic, "RECEPTIONIST"))
    target, other = waiting_case(clinic), waiting_case(clinic)
    approval = _issue(clinic, target)
    unrelated = _issue(clinic, other)
    foreign, foreign_approval = _foreign_confirmation(clinic)
    foreign_before = admission_effects(clinic, tenant=clinic.other)
    code, usable = control_request(clinic, receptionist, "usable", query=_query(target))
    assert code == 200 and usable["data"]["id"] == approval["id"]
    body = admit_body(clinic, target, physical_presence_confirmed=True)
    for invalid in (None, 0):
        monkeypatch.setattr(
            app.state,
            "service_config",
            replace(
                clinic.config,
                policy=replace(
                    clinic.config.policy, doctor_confirmation_expiry_minutes=invalid
                ),
            ),
        )
        _deny(clinic, receptionist, {**body, "confirmation_id": approval["id"]}, 503)
    monkeypatch.setattr(app.state, "service_config", clinic.config)
    for candidate in (None, str(uuid4()), unrelated["id"], foreign_approval["id"]):
        _deny(clinic, receptionist, {**body, "confirmation_id": candidate}, 409)
    _deny(clinic, owner, {**body, "confirmation_id": approval["id"]}, 403)
    _deny(
        clinic,
        receptionist,
        {
            **body,
            "appointment_id": foreign["appointment_id"],
            "patient_id": foreign["patient_id"],
        },
        404,
    )
    assert admission_effects(clinic, tenant=clinic.other) == foreign_before
    before = admission_effects(clinic)
    assert (
        control_request(
            clinic,
            receptionist,
            "revoke",
            confirmation_id=approval["id"],
            body=_revoke_body(clinic),
        )[0]
        == 403
    )
    assert (
        control_request(
            clinic,
            receptionist,
            "issue",
            body={
                k: v
                for k, v in body.items()
                if k
                not in (
                    "expected_queue_entry_revision",
                    "expected_head_appointment_id",
                    "physical_presence_confirmed",
                )
            },
        )[0]
        == 403
    )
    assert admission_effects(clinic) == before
    key = str(uuid4())
    _admit(
        clinic, receptionist, {**body, "confirmation_id": usable["data"]["id"]}, key=key
    )
    _deny(
        clinic,
        receptionist,
        {
            **admit_body(clinic, other, physical_presence_confirmed=True),
            "confirmation_id": unrelated["id"],
        },
        409,
    )
    _deny(clinic, receptionist, {**body, "confirmation_id": approval["id"]}, 409)
    _deny(
        clinic,
        receptionist,
        {**admit_body(clinic, other), "confirmation_id": unrelated["id"]},
        409,
        key=key,
    )


def test_database_admission_functions_require_exact_signed_stage(booking_clinic):
    clinic = booking_clinic
    waiting = waiting_case(clinic)
    body = admit_body(clinic, waiting)
    before = admission_effects(clinic)
    with clinic.runtime.connect() as connection:
        assert connection.scalar(text("SELECT session_user")) == "carepulse_api"
        for function in ("f01_b15_admit", "f01_b15_replay"):
            with pytest.raises(DBAPIError) as denied, connection.begin_nested():
                connection.scalar(text(f"SELECT public.{function}()"))
            assert denied.value.orig.sqlstate == "42501"
    with signed_test_transaction(
        clinic,
        clinic.owner,
        method="GET",
        path="/v1/queue",
        purpose="QUEUE_READ",
        query=f"date={body['date']}&lane=MAIN",
    ) as (connection, _):
        for function in ("f01_b15_admit", "f01_b15_replay"):
            with pytest.raises(DBAPIError) as denied, connection.begin_nested():
                connection.scalar(text(f"SELECT public.{function}()"))
            assert denied.value.orig.sqlstate == "42501"
    assert admission_effects(clinic) == before


def test_signed_database_admission_and_http_replay(booking_clinic):
    clinic = booking_clinic
    waiting = waiting_case(clinic)
    body = admit_body(clinic, waiting, physical_presence_confirmed=True)
    key = str(uuid4())
    with signed_test_transaction(
        clinic,
        clinic.owner,
        method="POST",
        path="/v1/queue/admit",
        purpose="QUEUE_ADMIT",
        body=body,
        key=key,
    ) as (connection, _):
        assert connection.scalar(text("SELECT session_user")) == "carepulse_api"
        result = connection.scalar(text("SELECT public.f01_b15_admit()"))
        assert result["replayed"] is False
    assert_admitted(clinic, clinic.owner, body, key, result["projection"])
    after = admission_effects(clinic)
    status, reply = admit_request(clinic, clinic.owner, body, key)
    assert status == 200
    assert reply["data"] == result["projection"]
    assert admission_effects(clinic) == after


@pytest.mark.parametrize("disable_reenable", [False, True])
def test_admission_rejects_stale_issuer_with_otherwise_current_guards(
    booking_clinic, disable_reenable
):
    clinic = booking_clinic
    _success(clinic, clinic.owner, "capacity", body=_capacity_body(clinic, 2))
    first = waiting_case(clinic, lane="EMERGENCY")
    _admit(clinic, clinic.owner, admit_body(clinic, first))
    _success(clinic, clinic.owner, "mode", body=_mode_body(clinic, "RECEPTIONIST"))
    target = waiting_case(clinic)
    issued = _issue(clinic, target)
    actor = clinic.receptionist
    status, usable = control_request(clinic, actor, "usable", query=_query(target))
    assert status == 200 and usable["data"]["id"] == issued["id"]
    body = admit_body(clinic, target, confirmation_id=issued["id"])
    before = admission_effects(clinic)
    fields = {"tenant": clinic.tenant, "doctor": UUID(clinic.owner.staff_id)}
    if disable_reenable:
        with clinic.migration.begin() as connection:
            connection.execute(
                text(
                    "UPDATE public.organizations SET state='SUSPENDED' WHERE id=:tenant"
                ),
                fields,
            )
            connection.execute(
                text(
                    "UPDATE public.staff_accounts SET enabled=false,security_revision=security_revision+1 WHERE organization_id=:tenant AND id=:doctor"
                ),
                fields,
            )
    session = f"synthetic-b15-new-owner-{uuid4()}"
    with clinic.migration.begin() as connection:
        revision = connection.scalar(
            text(
                "UPDATE public.staff_accounts SET enabled=true,security_revision=security_revision+1 WHERE organization_id=:tenant AND id=:doctor RETURNING security_revision"
            ),
            fields,
        )
        if disable_reenable:
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
    assert revision == int(clinic.owner.security_revision) + (
        2 if disable_reenable else 1
    )
    clinic.owner = replace(
        clinic.owner, session_id=session, security_revision=str(revision)
    )
    assert admission_effects(clinic) == before
    assert admit_body(clinic, target, confirmation_id=issued["id"]) == body
    with clinic.migration.connect() as connection:
        assert (
            connection.scalar(
                text("""
                SELECT state='ISSUED' AND expires_at>clock_timestamp()
                    AND issuer_security_revision<>:revision
                FROM public.doctor_admission_confirmations
                WHERE organization_id=:tenant AND id=:id
            """),
                {
                    "tenant": clinic.tenant,
                    "id": UUID(issued["id"]),
                    "revision": revision,
                },
            )
            is True
        )
    _deny(clinic, actor, body, 409)
    fresh = _issue(clinic, target)
    assert fresh["id"] != issued["id"]
    _admit(clinic, actor, {**body, "confirmation_id": fresh["id"]})
    confirmations = admission_effects(clinic)["doctor_admission_confirmations"]
    assert {row["id"]: row["state"] for row in confirmations} == {
        issued["id"]: "ISSUED",
        fresh["id"]: "CONSUMED",
    }


@contextmanager
def _aborted_admission_request(clinic, actor, body, key):
    raw = json.dumps(body, separators=(",", ":")).encode()
    identity = replace(
        actor,
        method="POST",
        path="/v1/queue/admit",
        purpose="QUEUE_ADMIT",
        query="",
        body_sha256=sha256(raw).hexdigest(),
        timestamp=str(int(datetime.now(UTC).timestamp() * 1000)),
        nonce=f"synthetic_{uuid4().hex}",
        idempotency_key=key,
    )
    with registered_test_request(clinic, identity, raw):
        with (
            pytest.raises(RuntimeError, match="synthetic rollback"),
            protected_transaction(
                clinic.runtime, identity, clinic.config.policy.session_idle_minutes
            ),
        ):
            raise RuntimeError("synthetic rollback")
        yield identity


def _recover(clinic, identity, body, key):
    with protected_transaction(
        clinic.runtime,
        identity,
        clinic.config.policy.session_idle_minutes,
        stage="RECEIPT_RECOVERY",
    ) as (connection, actor):
        assert connection.scalar(text("SELECT session_user")) == "carepulse_api"
        return recover_admission(
            connection, actor, AdmitCommand.model_validate(body), key
        )


def test_admission_receipt_recovery_and_target_authorization(booking_clinic):
    clinic = booking_clinic
    actor = clinic.receptionist
    target, peer = waiting_case(clinic), waiting_case(clinic)
    foreign, _ = _foreign_confirmation(clinic)
    body = admit_body(clinic, target)
    peer_body = admit_body(clinic, peer)
    key = str(uuid4())
    original = _admit(clinic, actor, body, key=key)
    before = admission_effects(clinic)
    foreign_before = admission_effects(clinic, tenant=clinic.other)
    with _aborted_admission_request(clinic, actor, body, key) as identity:
        assert _recover(clinic, identity, body, key) == (
            original,
            original["appointment_revision"],
        )
    for changed, expected, code in (
        (peer_body, 409, "IDEMPOTENCY_CONFLICT"),
        (
            {
                **body,
                "appointment_id": foreign["appointment_id"],
                "patient_id": foreign["patient_id"],
                "date": foreign["service_date"],
                "lane": foreign["lane"],
            },
            404,
            None,
        ),
        ({**body, "appointment_id": str(uuid4())}, 404, None),
    ):
        _deny(clinic, actor, changed, expected, key=key)
        with _aborted_admission_request(clinic, actor, changed, key) as identity:
            with pytest.raises(ContractError) as denied:
                _recover(clinic, identity, changed, key)
            assert (denied.value.status_code, denied.value.code) == (expected, code)
    assert admission_effects(clinic) == before
    assert admission_effects(clinic, tenant=clinic.other) == foreign_before
    # Even a registered request must recheck mode after its primary aborts.
    with _aborted_admission_request(clinic, actor, body, key) as identity:
        _success(clinic, clinic.owner, "mode", body=_mode_body(clinic, "DOCTOR"))
        excluded = admission_effects(clinic)
        with pytest.raises(ContractError) as denied:
            _recover(clinic, identity, body, key)
        assert denied.value.status_code == 403
        _deny(clinic, actor, body, 403, key=key)
        assert admission_effects(clinic) == excluded
    _success(clinic, clinic.owner, "mode", body=_mode_body(clinic, "BOTH"))
    before = admission_effects(clinic)
    with _aborted_admission_request(clinic, actor, body, key) as identity:
        assert _recover(clinic, identity, body, key) == (
            original,
            original["appointment_revision"],
        )
    assert admission_effects(clinic) == before
    with _aborted_admission_request(clinic, actor, body, key) as identity:
        with clinic.migration.begin() as connection:
            connection.execute(
                text('DELETE FROM public."session" WHERE id=:id'),
                {"id": actor.session_id},
            )
        with pytest.raises(ContractError) as denied:
            _recover(clinic, identity, body, key)
        assert denied.value.status_code == 401
    _deny(clinic, actor, body, 401, key=key)
    assert admission_effects(clinic) == before

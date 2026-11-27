"""T029: signed admission races with observed PostgreSQL lock ordering."""

from contextlib import contextmanager
from dataclasses import replace
from datetime import timedelta
from functools import partial
from uuid import UUID, uuid4

import pytest
from carepulse.identity.contracts import ContractError
from carepulse.main import app
from carepulse.queue.admission_contracts import (
    AdmissionModeCommand,
    AdmitCommand,
    CapacityCommand,
    RevokeConfirmationCommand,
)
from carepulse.queue.admission_control import (
    revoke_confirmation,
    set_admission_mode,
    set_capacity,
)
from carepulse.queue.admit import admit_patient
from runtime_identity_fixture import signed_test_transaction
from sqlalchemy import text
from test_booking import booking_clinic as booking_clinic  # noqa: PLC0414
from test_patients import patient_backend as patient_backend  # noqa: PLC0414
from test_pricing import _fresh_receptionist_session, _owner_changes_manager
from test_request_boundary import boundary_data as boundary_data  # noqa: PLC0414
from test_w3_admission import (
    admission_effects,
    admit_body,
    admit_request,
    assert_admitted,
    today,
    waiting_case,
)
from test_w3_admission_confirmations import _issue
from test_w3_admission_controls import (
    _capacity_body,
    _mode_body,
    _revoke_body,
    _success,
)
from test_w3_checkin_races import _ordered_race


@contextmanager
def _admission(clinic, actor, body, key):
    with signed_test_transaction(
        clinic,
        actor,
        method="POST",
        path="/v1/queue/admit",
        purpose="QUEUE_ADMIT",
        body=body,
        key=key,
    ) as (connection, authorized):
        yield (
            connection,
            lambda: admit_patient(
                connection, authorized, AdmitCommand.model_validate(body), key
            ),
        )


@contextmanager
def _revoke(clinic, identifier, body, key):
    with signed_test_transaction(
        clinic,
        clinic.owner,
        method="POST",
        path=f"/v1/queue/admission-confirmations/{identifier}/revoke",
        purpose="QUEUE_CONFIRMATION_REVOKE",
        body=body,
        key=key,
    ) as (connection, authorized):
        yield (
            connection,
            lambda: revoke_confirmation(
                connection,
                authorized,
                UUID(identifier),
                RevokeConfirmationCommand.model_validate(body),
                key,
            ),
        )


@contextmanager
def _policy(clinic, actor, body, key):
    if "capacity" in body:
        path, purpose, command, mutate = (
            "/v1/queue/capacity",
            "QUEUE_CAPACITY_UPDATE",
            CapacityCommand.model_validate(body),
            set_capacity,
        )
    else:
        path, purpose, command, mutate = (
            "/v1/settings/admission",
            "ADMISSION_MODE_UPDATE",
            AdmissionModeCommand.model_validate(body),
            set_admission_mode,
        )
    with signed_test_transaction(
        clinic,
        actor,
        method="PATCH",
        path=path,
        purpose=purpose,
        body=body,
        key=key,
    ) as (connection, authorized):
        yield connection, lambda: mutate(connection, authorized, command, key)


@contextmanager
def _denied(transaction, status, code=None):
    with transaction() as (connection, execute):

        def reject():
            with pytest.raises(ContractError) as error, connection.begin_nested():
                execute()
            assert error.value.status_code == status
            if code is not None:
                assert error.value.code == code

        yield connection, reject


@contextmanager
def _hold_only(transaction):
    with transaction() as (connection, _execute):
        yield connection, lambda: None


def _checkpoint(clinic, before):
    def observe(_observer):
        assert admission_effects(clinic) == before

    return observe


def _assert_delta(before, after, *, changed=(), appended=None):
    """All rows outside the specified command effects must remain identical."""
    appended = appended or {}
    for table, old in before.items():
        new = after[table]
        assert len(new) == len(old) + appended.get(table, 0), table
        assert sum(row not in new for row in old) == changed.count(table), table


def _admission_delta(before, after, *, confirmation=False):
    _assert_delta(
        before,
        after,
        changed=("appointments", "queue_days", "queue_entries")
        + (("doctor_admission_confirmations",) if confirmation else ()),
        appended={
            table: 1
            for table in (
                "visits",
                "appointment_history",
                "sensitive_audit",
                "outbox_events",
                "idempotency_receipts",
            )
        },
    )


def _case_row(state, table, identifier):
    return next(row for row in state[table] if row["id"] == identifier)


def _assert_waiting_unchanged(before, after, waiting):
    for table, identifier in (
        ("appointments", waiting["appointment_id"]),
        ("queue_entries", waiting["queue_entry_id"]),
    ):
        assert _case_row(after, table, identifier) == _case_row(
            before, table, identifier
        )


def _assert_admission(clinic, actor, body, key, outcome):
    projection, revision, replayed = outcome
    assert replayed is False
    assert revision == body["expected_appointment_revision"] + 1
    assert_admitted(clinic, actor, body, key, projection)
    return projection


def _body(clinic, waiting, **changes):
    return admit_body(clinic, waiting, physical_presence_confirmed=True, **changes)


def _active_exam(clinic, capacity):
    _success(clinic, clinic.owner, "capacity", body=_capacity_body(clinic, capacity))
    waiting = waiting_case(clinic, lane="EMERGENCY", day=today() - timedelta(days=1))
    body, key = _body(clinic, waiting), str(uuid4())
    status, reply = admit_request(clinic, clinic.owner, body, key)
    assert status == 200, reply
    assert_admitted(clinic, clinic.owner, body, key, reply["data"])


@pytest.mark.parametrize("cross_date", [False, True])
@pytest.mark.parametrize("reverse", [False, True])
def test_shared_capacity_across_lanes_and_dates_commits_only_first(
    booking_clinic, cross_date, reverse
):
    clinic = booking_clinic
    day = today()
    cases = [
        waiting_case(clinic, day=day, lane="MAIN"),
        waiting_case(
            clinic,
            day=day - timedelta(days=1) if cross_date else day,
            lane="MAIN" if cross_date else "EMERGENCY",
        ),
    ]
    actors = [clinic.owner, clinic.receptionist]
    if reverse:
        cases.reverse()
        actors.reverse()
    bodies = [_body(clinic, case) for case in cases]
    keys = [str(uuid4()), str(uuid4())]
    first, second = [
        partial(_admission, clinic, actor, body, key)
        for actor, body, key in zip(actors, bodies, keys, strict=True)
    ]
    before = admission_effects(clinic)
    winner, loser = _ordered_race(
        clinic,
        first,
        second,
        denial="CONFLICT",
        checkpoint=_checkpoint(clinic, before),
    )
    assert loser is None
    _assert_admission(clinic, actors[0], bodies[0], keys[0], winner)
    after = admission_effects(clinic)
    _admission_delta(before, after)
    _assert_waiting_unchanged(before, after, cases[1])
    assert _case_row(after, "queue_days", cases[1]["queue_day_id"]) == _case_row(
        before, "queue_days", cases[1]["queue_day_id"]
    )
    assert sum(row["status"] == "In Examination" for row in after["appointments"]) == 1


@pytest.mark.parametrize("reverse", [False, True])
@pytest.mark.parametrize("same_key", [False, True])
def test_duplicate_admission_cannot_create_a_second_visit(
    booking_clinic, reverse, same_key
):
    clinic = booking_clinic
    waiting = waiting_case(clinic)
    body = _body(clinic, waiting)
    actors = [clinic.owner, clinic.receptionist]
    if reverse:
        actors.reverse()
    key = str(uuid4())
    other_key = key if same_key else str(uuid4())
    other_actor = actors[0] if same_key else actors[1]
    before = admission_effects(clinic)
    winner, contender = _ordered_race(
        clinic,
        partial(_admission, clinic, actors[0], body, key),
        partial(_admission, clinic, other_actor, body, other_key),
        denial=None if same_key else "CONFLICT",
        checkpoint=_checkpoint(clinic, before),
    )
    _assert_admission(clinic, actors[0], body, key, winner)
    if same_key:
        assert contender == (winner[0], winner[1], True)
    else:
        assert contender is None
    _admission_delta(before, admission_effects(clinic))


@pytest.mark.parametrize("non_head_first", [False, True])
def test_same_lane_competition_rejects_stale_selected_head_and_approval(
    booking_clinic, non_head_first
):
    clinic = booking_clinic
    _active_exam(clinic, 3)
    cases = [waiting_case(clinic), waiting_case(clinic)]
    approvals = [_issue(clinic, case) for case in cases]
    bodies = [
        _body(clinic, cases[0], confirmation_id=approvals[0]["id"]),
        _body(
            clinic,
            cases[1],
            confirmation_id=approvals[1]["id"],
            non_head_reason="Synthetic within-lane exception",
        ),
    ]
    actors = [clinic.owner, clinic.receptionist]
    if non_head_first:
        cases.reverse()
        approvals.reverse()
        bodies.reverse()
        actors.reverse()
    keys = [str(uuid4()), str(uuid4())]
    before = admission_effects(clinic)
    winner, loser = _ordered_race(
        clinic,
        partial(_admission, clinic, actors[0], bodies[0], keys[0]),
        partial(_admission, clinic, actors[1], bodies[1], keys[1]),
        denial="STALE_REVISION",
        checkpoint=_checkpoint(clinic, before),
    )
    assert loser is None
    _assert_admission(clinic, actors[0], bodies[0], keys[0], winner)
    after = admission_effects(clinic)
    _admission_delta(before, after, confirmation=True)
    _assert_waiting_unchanged(before, after, cases[1])
    identifier = approvals[1]["id"]
    assert _case_row(after, "doctor_admission_confirmations", identifier) == _case_row(
        before, "doctor_admission_confirmations", identifier
    )
    assert sum(row["status"] == "In Examination" for row in after["appointments"]) == 2


def _assert_control_evidence(after, actor, key, outcome, action, event):
    projection, revision, replayed = outcome
    assert replayed is False
    (receipt,) = [
        row
        for row in after["idempotency_receipts"]
        if row["operation"] == action and row["idempotency_key"] == key
    ]
    assert (
        receipt["actor_id"],
        receipt["staff_id"],
        receipt["result_projection"],
        receipt["result_revision"],
    ) == (actor.staff_id, actor.staff_id, projection, revision)
    (audit,) = [
        row
        for row in after["sensitive_audit"]
        if row["action"] == action and row["after_state"] == projection
    ]
    assert (audit["actor_staff_id"], audit["actor_role"]) == (
        actor.staff_id,
        actor.role,
    )
    assert (
        sum(
            row["event_type"] == event and row["payload"] == projection
            for row in after["outbox_events"]
        )
        == 1
    )
    return projection


@pytest.mark.parametrize("revoke_first", [False, True])
def test_confirmation_consumption_and_revocation_commit_one_terminal_state(
    booking_clinic, revoke_first
):
    clinic = booking_clinic
    _active_exam(clinic, 2)
    waiting = waiting_case(clinic)
    issued = _issue(clinic, waiting)
    identifier = issued["id"]
    body, revoke_body = (
        _body(clinic, waiting, confirmation_id=identifier),
        _revoke_body(clinic),
    )
    admission_key, revoke_key = str(uuid4()), str(uuid4())
    admit = partial(_admission, clinic, clinic.receptionist, body, admission_key)
    revoke = partial(_revoke, clinic, identifier, revoke_body, revoke_key)
    before = admission_effects(clinic)
    first, second = (revoke, admit) if revoke_first else (admit, revoke)
    winner, loser = _ordered_race(
        clinic,
        first,
        second,
        denial="CONFLICT",
        checkpoint=_checkpoint(clinic, before),
    )
    assert loser is None
    after = admission_effects(clinic)
    confirmation = _case_row(after, "doctor_admission_confirmations", identifier)
    if revoke_first:
        projection = _assert_control_evidence(
            after,
            clinic.owner,
            revoke_key,
            winner,
            "queue.confirmation.revoke",
            "DOCTOR_CONFIRMATION_REVOKED",
        )
        assert projection["state"] == confirmation["state"] == "REVOKED"
        assert confirmation["revoked_by_staff_id"] == clinic.owner.staff_id
        assert confirmation["revoked_at"] == projection["revoked_at"]
        assert confirmation["consumed_at"] is None
        _assert_delta(
            before,
            after,
            changed=("doctor_admission_confirmations",),
            appended={
                table: 1
                for table in (
                    "sensitive_audit",
                    "outbox_events",
                    "idempotency_receipts",
                )
            },
        )
    else:
        _assert_admission(clinic, clinic.receptionist, body, admission_key, winner)
        assert (
            confirmation["state"] == "CONSUMED" and confirmation["revoked_at"] is None
        )
        _admission_delta(before, after, confirmation=True)


@pytest.mark.parametrize("expiry_first", [False, True])
def test_confirmation_expiry_at_database_deadline_during_observed_lock(
    booking_clinic, monkeypatch, expiry_first
):
    clinic = booking_clinic
    _active_exam(clinic, 2)
    waiting = waiting_case(clinic)
    monkeypatch.setattr(
        app.state,
        "service_config",
        replace(
            clinic.config,
            policy=replace(clinic.config.policy, doctor_confirmation_expiry_minutes=1),
        ),
    )
    issued = _issue(clinic, waiting)
    body = _body(clinic, waiting, confirmation_id=issued["id"])
    first_key, second_key = str(uuid4()), str(uuid4())
    first = partial(_admission, clinic, clinic.receptionist, body, first_key)
    second = partial(_admission, clinic, clinic.owner, body, second_key)
    before = admission_effects(clinic)

    def checkpoint(observer):
        assert admission_effects(clinic) == before
        assert observer.scalar(
            text("SELECT clock_timestamp() < CAST(:deadline AS timestamptz)"),
            {"deadline": issued["expires_at"]},
        )
        observer.execute(
            text("SELECT pg_sleep_until(CAST(:deadline AS timestamptz))"),
            {"deadline": issued["expires_at"]},
        )
        assert observer.scalar(
            text("SELECT clock_timestamp() >= CAST(:deadline AS timestamptz)"),
            {"deadline": issued["expires_at"]},
        )

    winner, loser = _ordered_race(
        clinic,
        partial(_hold_only, first) if expiry_first else first,
        second,
        denial="CONFLICT",
        checkpoint=checkpoint,
    )
    assert loser is None
    after = admission_effects(clinic)
    if expiry_first:
        assert winner is None and after == before
        assert (
            _case_row(after, "doctor_admission_confirmations", issued["id"])["state"]
            == "ISSUED"
        )
    else:
        _assert_admission(clinic, clinic.receptionist, body, first_key, winner)
        _admission_delta(before, after, confirmation=True)


def _assert_policy_versions(before, after, kind, projection):
    old_heads = {row["kind"]: row for row in before["clinic_policy_heads"]}
    new_heads = {row["kind"]: row for row in after["clinic_policy_heads"]}
    old, new = old_heads.pop(kind), new_heads.pop(kind)
    assert old_heads == new_heads
    assert new["current_version"] == old["current_version"] + 1
    assert new["revision"] == old["revision"] + 1
    (published,) = [
        row
        for row in after["clinic_policy_versions"]
        if row not in before["clinic_policy_versions"]
    ]
    assert (published["kind"], published["version"]) == (kind, new["current_version"])
    if kind == "CAPACITY":
        assert published["payload"]["capacity"] == projection["capacity"] == 1
        assert published["payload"]["paused"] == projection["paused"]
        old_control, new_control = (
            before["doctor_controls"][0],
            after["doctor_controls"][0],
        )
        assert new_control["capacity_policy_version"] == new["current_version"]
        assert (
            new_control["revision"]
            == old_control["revision"] + 1
            == projection["control_revision"]
        )
        assert new["revision"] == projection["capacity_revision"]
    else:
        assert published["payload"]["actors"] == projection["mode"]
        assert new["revision"] == projection["admission_revision"]
        assert before["doctor_controls"] == after["doctor_controls"]


@pytest.mark.parametrize("policy_first", [False, True])
@pytest.mark.parametrize(
    "policy_kind", ["CAPACITY", "ADMISSION_OWNER", "ADMISSION_MANAGER"]
)
def test_admission_and_policy_write_revalidate_committed_policy_in_both_orders(
    booking_clinic, policy_first, policy_kind
):
    clinic = booking_clinic
    _active_exam(clinic, 2)
    waiting = waiting_case(clinic)
    policy_actor, admission_actor = clinic.owner, clinic.receptionist
    if policy_kind == "ADMISSION_MANAGER":
        _owner_changes_manager(clinic, True, 0)
        policy_actor, admission_actor = (
            _fresh_receptionist_session(clinic, 1),
            clinic.owner,
        )
    issued = _issue(clinic, waiting)
    admission_body = _body(clinic, waiting, confirmation_id=issued["id"])
    capacity = policy_kind == "CAPACITY"
    policy_body = (
        _capacity_body(clinic, 1)
        if capacity
        else _mode_body(
            clinic, "RECEPTIONIST" if policy_kind == "ADMISSION_MANAGER" else "DOCTOR"
        )
    )
    admit_key, policy_key = str(uuid4()), str(uuid4())
    admit = partial(_admission, clinic, admission_actor, admission_body, admit_key)
    change = partial(_policy, clinic, policy_actor, policy_body, policy_key)
    before = admission_effects(clinic)
    if policy_first:
        policy_outcome, admission_outcome = _ordered_race(
            clinic,
            change,
            partial(
                _denied,
                admit,
                409 if capacity else 403,
                "STALE_REVISION" if capacity else None,
            ),
            checkpoint=_checkpoint(clinic, before),
        )
        assert admission_outcome is None
    else:
        admission_outcome, policy_outcome = _ordered_race(
            clinic,
            admit,
            change,
            denial="CONFLICT" if capacity else None,
            checkpoint=_checkpoint(clinic, before),
        )
        _assert_admission(
            clinic, admission_actor, admission_body, admit_key, admission_outcome
        )
    after = admission_effects(clinic)
    if policy_outcome is None:
        assert capacity and not policy_first
        _admission_delta(before, after, confirmation=True)
        return

    projection = _assert_control_evidence(
        after,
        policy_actor,
        policy_key,
        policy_outcome,
        "queue.capacity" if capacity else "settings.admission.update",
        "EXAMINATION_CAPACITY_CHANGED" if capacity else "ADMISSION_MODE_CHANGED",
    )
    _assert_policy_versions(
        before, after, "CAPACITY" if capacity else "ADMISSION", projection
    )
    changes = ("clinic_policy_heads",) + (("doctor_controls",) if capacity else ())
    appends = {
        "clinic_policy_versions": 1,
        "sensitive_audit": 1,
        "outbox_events": 1,
        "idempotency_receipts": 1,
    }
    if policy_first:
        assert _case_row(
            after, "doctor_admission_confirmations", issued["id"]
        ) == _case_row(before, "doctor_admission_confirmations", issued["id"])
    else:
        changes += (
            "appointments",
            "queue_days",
            "queue_entries",
            "doctor_admission_confirmations",
        )
        appends.update(
            visits=1,
            appointment_history=1,
            sensitive_audit=2,
            outbox_events=2,
            idempotency_receipts=2,
        )
    _assert_delta(before, after, changed=changes, appended=appends)

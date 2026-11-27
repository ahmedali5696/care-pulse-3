"""T042: actual signed terminal, clinical and B-10 contention in both orders."""

from contextlib import contextmanager
from functools import partial
from uuid import UUID, uuid4

import pytest
from carepulse.appointments.book import book_appointment
from carepulse.identity.contracts import ContractError
from carepulse.visits.clinical import patch_clinical
from carepulse.visits.clinical_contracts import (
    ClinicalPatch,
    CompleteVisit,
    ExceptionalCancel,
)
from carepulse.visits.complete import cancel_exceptional, complete_visit
from carepulse.visits.eligibility import record_followup_decision
from carepulse.visits.eligibility_contracts import FollowupDecisionPatchV2
from runtime_identity_fixture import signed_test_transaction
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from test_booking import booking_clinic as booking_clinic  # noqa: PLC0414
from test_followups_races import _command, _source_for
from test_followups_races import followup_clinic as followup_clinic  # noqa: PLC0414
from test_patients import patient_backend as patient_backend  # noqa: PLC0414
from test_request_boundary import boundary_data as boundary_data  # noqa: PLC0414
from test_w3_admission import _admit, admit_body
from test_w3_checkin import queue_request
from test_w3_checkin_races import _ordered_race
from test_w3_visits import (
    PURPOSES,
    admitted,
    detail,
    effects,
    terminal_body,
    visit_request,
)


@contextmanager
def _visit_command(clinic, actor, visit, operation, body, key):
    method, suffix, purpose = PURPOSES[operation]
    schema, execute = {
        "complete": (CompleteVisit, complete_visit),
        "cancel": (ExceptionalCancel, cancel_exceptional),
        "clinical": (ClinicalPatch, patch_clinical),
        "source": (FollowupDecisionPatchV2, record_followup_decision),
    }[operation]
    with signed_test_transaction(
        clinic,
        actor,
        method=method,
        path=f"/v1/visits/{visit}{suffix}",
        purpose=purpose,
        body=body,
        key=key,
    ) as (connection, authorized):
        yield (
            connection,
            lambda: execute(
                connection, authorized, visit, schema.model_validate(body), key
            ),
        )


@contextmanager
def _booking(clinic, command, key):
    with signed_test_transaction(
        clinic,
        clinic.receptionist,
        method="POST",
        path="/v1/appointments",
        purpose="APPOINTMENT_BOOKING",
        body=command.model_dump(mode="json", exclude_unset=True),
        key=key,
    ) as (connection, authorized):
        yield connection, lambda: book_appointment(connection, authorized, command, key)


@contextmanager
def _denied_holder(transaction):
    # Retain the real F-01 authority lock while rolling back the rejected domain command.
    with transaction() as (connection, execute):

        def deny():
            with pytest.raises(ContractError) as denied, connection.begin_nested():
                execute()
            assert denied.value.status_code == 409

        yield connection, deny


def _checkpoint(clinic, before, appointment_id):
    def observe(observer):
        assert effects(clinic) == before
        with pytest.raises(DBAPIError) as blocked, observer.begin_nested():
            observer.execute(
                text(
                    "SELECT id FROM public.organizations WHERE id=:id FOR UPDATE NOWAIT"
                ),
                {"id": clinic.tenant},
            )
        assert blocked.value.orig.sqlstate == "55P03"
        assert (
            observer.scalar(
                text("SELECT status FROM public.appointments WHERE id=:id"),
                {"id": UUID(appointment_id)},
            )
            == "In Examination"
        )

    return observe


def _row(snapshot, table, identity):
    return next(row for row in snapshot[table] if row["id"] == str(identity))


def _added(before, after, table, count):
    assert all(row in after[table] for row in before[table]), table
    rows = [row for row in after[table] if row not in before[table]]
    assert len(rows) == count, table
    return rows


def _assert_clinical(before, after, visit, actor, key, outcome, field, narrative):
    projection, revision, replayed = outcome
    assert replayed is False
    old = _row(before, "visits", visit)
    current = _row(after, "visits", visit)
    assert revision == current["clinical_revision"] == old["clinical_revision"] + 1
    assert current[field] == narrative
    (change,) = _added(before, after, "clinical_revisions", 1)
    assert (
        change["visit_id"],
        change["revision"],
        change["actor_staff_id"],
        change["actor_role"],
    ) == (str(visit), revision, actor.staff_id, actor.role)
    assert change["new_values"] == {**change["old_values"], field: narrative}
    assert change["old_values"][field] == old[field]
    (audit,) = _added(before, after, "clinical_audit", 1)
    assert (
        audit["correlation_id"],
        audit["doctor_staff_id"],
        audit["clinical_revision"],
        audit["occurred_at"],
    ) == (change["id"], actor.staff_id, revision, change["occurred_at"])
    (receipt,) = [
        row for row in after["idempotency_receipts"] if row["idempotency_key"] == key
    ]
    assert (
        receipt["actor_id"],
        receipt["operation"],
        receipt["result_projection"],
        receipt["result_revision"],
    ) == (actor.staff_id, "visits.clinical", projection, revision)


def _assert_terminal(
    before, after, visit, operation, key, actor, outcome, *, claim=False, booked=False
):
    original = _row(before, "visits", visit)
    appointment = original["appointment_id"]
    expected = "Completed" if operation == "complete" else "Cancelled"
    prior = _row(before, "appointments", appointment)
    current = _row(after, "appointments", appointment)
    projection, revision, replayed = outcome
    assert replayed is False
    assert current == {
        **prior,
        "status": expected,
        "revision": prior["revision"] + 1,
        "updated_at": current["updated_at"],
    }
    assert revision == current["revision"]
    assert projection["visit_id"] == str(visit)
    assert _row(after, "visits", visit) == original
    assert len(after["visits"]) == len(before["visits"])
    attestations = _added(
        before, after, "completion_attestations", operation == "complete"
    )
    if attestations:
        (attestation,) = attestations
        assert (
            attestation["visit_id"],
            attestation["appointment_id"],
            attestation["actor_staff_id"],
            attestation["actor_role"],
            attestation["examination_occurred"],
            attestation["completion_history_revision"],
        ) == (str(visit), appointment, actor.staff_id, actor.role, True, revision)
    for table in (
        "appointment_history",
        "sensitive_audit",
        "outbox_events",
        "idempotency_receipts",
    ):
        _added(before, after, table, 1 + booked)
    for table in (
        "appointments",
        "appointment_number_assignments",
        "appointment_price_snapshots",
    ):
        assert len(after[table]) == len(before[table]) + booked, table
    assert next(
        row
        for row in after["appointment_price_snapshots"]
        if row["appointment_id"] == appointment
    ) == next(
        row
        for row in before["appointment_price_snapshots"]
        if row["appointment_id"] == appointment
    )
    (receipt,) = [
        row for row in after["idempotency_receipts"] if row["idempotency_key"] == key
    ]
    assert (
        receipt["actor_id"],
        receipt["result_projection"],
        receipt["result_revision"],
    ) == (actor.staff_id, projection, revision)
    (history,) = [
        row
        for row in after["appointment_history"]
        if row["appointment_id"] == appointment
        and row["appointment_revision"] == revision
    ]
    assert (
        history["old_status"],
        history["new_status"],
        history["actor_staff_id"],
        history["actor_role"],
        history["old_number"],
        history["new_number"],
    ) == (
        "In Examination",
        expected,
        actor.staff_id,
        actor.role,
        prior["current_number"],
        prior["current_number"],
    )
    for table in (
        "clinical_revisions",
        "clinical_audit",
        "followup_source_decision_events",
        "followup_authorization_events",
        "queue_days",
        "queue_entries",
    ):
        assert after[table] == before[table], table
    if not booked:
        for table in (
            "appointment_day_counters",
            "appointment_number_assignments",
            "appointment_price_snapshots",
        ):
            assert after[table] == before[table], table
    else:
        _added(before, after, "appointment_number_assignments", 1)
        _added(before, after, "appointment_price_snapshots", 1)
    event_type = (
        "VISIT_EXAMINATION_COMPLETED"
        if operation == "complete"
        else "VISIT_EXAMINATION_CANCELLED"
    )
    (event,) = [
        row
        for row in after["outbox_events"]
        if row["event_type"] == event_type and row["aggregate_id"] == appointment
    ]
    assert event["payload"]["visit_id"] == str(visit)
    assert (
        event["payload"]["actor_staff_id"],
        event["payload"]["actor_role"],
        event["payload"]["appointment_revision"],
        event["payload"]["status"],
    ) == (actor.staff_id, actor.role, revision, expected)
    if operation == "cancel":
        assert event["payload"]["actor_role"] == "DOCTOR_OWNER"
        assert "reason" not in event["payload"]
        (review,) = [
            row
            for row in _added(before, after, "sensitive_audit", 1 + booked)
            if row["action"] == "VISIT_EXAMINATION_CANCELLED"
        ]
        assert (
            review["actor_staff_id"],
            review["actor_role"],
            review["correlation_id"],
            review["reason"],
            review["after_state"]["visit_id"],
        ) == (
            actor.staff_id,
            actor.role,
            history["id"],
            "Synthetic exceptional cancellation",
            str(visit),
        )
    if claim:
        (settled,) = [
            row
            for row in after["followup_claims"]
            if row["appointment_id"] == appointment
        ]
        assert settled["state"] == (
            "CONSUMED" if operation == "complete" else "RELEASED"
        )
        assert (
            settled["revision"]
            == next(
                row["revision"]
                for row in before["followup_claims"]
                if row["id"] == settled["id"]
            )
            + 1
        )
        assert settled["terminal_history_revision"] == receipt["result_revision"]
        previous = _row(before, "followup_claims", settled["id"])
        assert settled == {
            **previous,
            "state": settled["state"],
            "revision": previous["revision"] + 1,
            "terminal_history_revision": revision,
            "terminal_at": history["occurred_at"],
        }


@pytest.mark.parametrize("first", ["complete", "cancel"])
def test_completion_and_doctor_cancel_serialize(followup_clinic, first):
    clinic = followup_clinic
    _terminal_race(clinic, admitted(clinic), first)
    _, claimed = _claimed_visit(clinic)
    _terminal_race(clinic, claimed, first)


def _terminal_race(clinic, visit, first):
    projection = detail(clinic, visit)
    actors = {"complete": clinic.receptionist, "cancel": clinic.owner}
    second = "cancel" if first == "complete" else "complete"
    keys = {operation: str(uuid4()) for operation in ("complete", "cancel")}
    contenders = {
        operation: partial(
            _visit_command,
            clinic,
            actor,
            visit,
            operation,
            terminal_body(projection, cancel=operation == "cancel"),
            keys[operation],
        )
        for operation, actor in actors.items()
    }
    before = effects(clinic)
    winner, loser = _ordered_race(
        clinic,
        contenders[first],
        contenders[second],
        denial="STALE_REVISION",
        checkpoint=_checkpoint(clinic, before, projection["appointment_id"]),
    )
    after = effects(clinic)
    assert loser is None
    _assert_terminal(
        before,
        after,
        visit,
        first,
        keys[first],
        actors[first],
        winner,
        claim=projection["claim_revision"] is not None,
    )
    assert not any(
        row["idempotency_key"] == keys["cancel" if first == "complete" else "complete"]
        for row in after["idempotency_receipts"]
    )
    assert visit_request(
        clinic,
        actors[first],
        visit,
        first,
        terminal_body(projection, cancel=first == "cancel"),
        key=keys[first],
    ) == (200, {"data": winner[0], "revision": winner[1]})
    assert effects(clinic) == after


def _claimed_visit(clinic):
    source = _source_for(clinic)
    command = _command(source, 0)
    with _booking(clinic, command, str(uuid4())) as (_, execute):
        saved = execute()
    assert saved["service_type"] == "FOLLOW_UP"
    with clinic.migration.connect() as conn:
        revision = conn.scalar(
            text(
                "SELECT revision FROM public.queue_days WHERE organization_id=:t AND service_date=:d AND lane='MAIN'"
            ),
            {"t": clinic.tenant, "d": source["day"]},
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
            "expected_queue_day_revision": revision or 0,
        },
        key=str(uuid4()),
    )
    assert status == 200, reply
    waiting = reply["data"]
    result = _admit(
        clinic,
        clinic.owner,
        admit_body(clinic, waiting, physical_presence_confirmed=True),
    )
    return source, UUID(result["visit_id"])


@pytest.mark.parametrize("operation", ["complete", "cancel"])
@pytest.mark.parametrize("terminal_first", [True, False])
def test_claim_settlement_and_competing_booking(
    followup_clinic, operation, terminal_first
):
    clinic = followup_clinic
    source, visit = _claimed_visit(clinic)
    projection = detail(clinic, visit)
    key, booking_key = str(uuid4()), str(uuid4())
    terminal = partial(
        _visit_command,
        clinic,
        clinic.owner,
        visit,
        operation,
        terminal_body(projection, cancel=operation == "cancel"),
        key,
    )
    competing = partial(_booking, clinic, _command(source, 2), booking_key)
    before = effects(clinic)
    checkpoint = _checkpoint(clinic, before, projection["appointment_id"])
    booked = terminal_first and operation == "cancel"
    if terminal_first:
        winner, outcome = _ordered_race(
            clinic,
            terminal,
            competing,
            denial=None if booked else "CONFLICT",
            checkpoint=checkpoint,
        )
        if booked:
            assert outcome["service_type"] == "FOLLOW_UP"
        else:
            assert outcome is None
    else:
        rejected, winner = _ordered_race(
            clinic, partial(_denied_holder, competing), terminal, checkpoint=checkpoint
        )
        assert rejected is None
    after = effects(clinic)
    _assert_terminal(
        before,
        after,
        visit,
        operation,
        key,
        clinic.owner,
        winner,
        claim=True,
        booked=booked,
    )
    assert sum(
        row["state"] in ("ACTIVE", "CONSUMED")
        for row in after["followup_claims"]
        if row["source_visit_id"] == str(source["visit"])
    ) == (operation == "complete" or booked)
    assert (
        sum(
            row["idempotency_key"] == booking_key
            for row in after["idempotency_receipts"]
        )
        == booked
    )
    if booked:
        new = _row(after, "appointments", outcome["id"])
        (assignment,) = _added(before, after, "appointment_number_assignments", 1)
        (price,) = _added(before, after, "appointment_price_snapshots", 1)
        assert (
            assignment["appointment_id"],
            assignment["id"],
            assignment["number"],
            assignment["service_date"],
        ) == (
            new["id"],
            new["current_assignment_id"],
            new["current_number"],
            new["service_date"],
        )
        previous_counters = {
            row["service_date"]: row["last_number"]
            for row in before["appointment_day_counters"]
        }
        assert (
            new["current_number"] == previous_counters.get(new["service_date"], 0) + 1
        )
        assert {
            row["service_date"]: row["last_number"]
            for row in after["appointment_day_counters"]
        } == {**previous_counters, new["service_date"]: new["current_number"]}
        (fresh,) = [
            row
            for row in after["followup_claims"]
            if row["appointment_id"] == new["id"]
        ]
        assert (fresh["state"], fresh["source_visit_id"], fresh["revision"]) == (
            "ACTIVE",
            str(source["visit"]),
            1,
        )
        assert (
            price["appointment_id"],
            price["service_type"],
            price["base_egp"],
            price["net_egp"],
        ) == (new["id"], "FOLLOW_UP", 40, 40)
        assert len(after["followup_claims"]) == len(before["followup_claims"]) + 1
    else:
        assert len(after["followup_claims"]) == len(before["followup_claims"])
    assert visit_request(
        clinic,
        clinic.owner,
        visit,
        operation,
        terminal_body(projection, cancel=operation == "cancel"),
        key=key,
    ) == (200, {"data": winner[0], "revision": winner[1]})
    assert effects(clinic) == after


@pytest.mark.parametrize("clinical_first", [True, False])
def test_clinical_correction_and_source_restriction_serialize(
    followup_clinic, clinical_first
):
    clinic = followup_clinic
    source = _source_for(clinic)
    visit = source["visit"]
    clinical_key, source_key = str(uuid4()), str(uuid4())
    clinical = partial(
        _visit_command,
        clinic,
        clinic.owner,
        visit,
        "clinical",
        {"expected_visit_revision": 0, "recommendation": "Synthetic correction"},
        clinical_key,
    )
    decision = partial(
        _visit_command,
        clinic,
        clinic.owner,
        visit,
        "source",
        {
            "expected_visit_revision": 0,
            "expected_source_decision_revision": 0,
            "expected_followup_revision": 1,
            "followup_decision": {
                "kind": "OPTED_OUT",
                "reason": "Synthetic restriction",
            },
        },
        source_key,
    )
    before = effects(clinic)

    def checkpoint(observer):
        assert effects(clinic) == before
        with pytest.raises(DBAPIError) as blocked, observer.begin_nested():
            observer.execute(
                text("SELECT id FROM public.visits WHERE id=:id FOR UPDATE NOWAIT"),
                {"id": visit},
            )
        assert blocked.value.orig.sqlstate == "55P03"

    first, second = _ordered_race(
        clinic,
        clinical if clinical_first else decision,
        decision if clinical_first else clinical,
        denial="STALE_REVISION" if clinical_first else None,
        checkpoint=checkpoint,
    )
    after = effects(clinic)
    _assert_clinical(
        before,
        after,
        visit,
        clinic.owner,
        clinical_key,
        first if clinical_first else second,
        "recommendation",
        "Synthetic correction",
    )
    for table in (
        "appointment_number_assignments",
        "appointment_day_counters",
        "appointment_history",
        "completion_attestations",
        "outbox_events",
        "followup_authorization_events",
    ):
        assert after[table] == before[table], table
    _added(before, after, "idempotency_receipts", 1 + (not clinical_first))
    _added(before, after, "sensitive_audit", not clinical_first)
    events = _added(
        before, after, "followup_source_decision_events", not clinical_first
    )
    if clinical_first:
        assert second is None
        assert after["followup_source_state"] == before["followup_source_state"]
    else:
        (event,) = events
        source_projection, source_revision = first
        assert (
            event["source_visit_id"],
            event["source_revision"],
            event["clinical_revision"],
            event["actor_staff_id"],
            event["actor_role"],
            event["decision"],
            event["before_decision"],
        ) == (
            str(visit),
            1,
            0,
            clinic.owner.staff_id,
            clinic.owner.role,
            {"kind": "OPTED_OUT", "reason": "Synthetic restriction"},
            None,
        )
        assert (source_projection["event_id"], source_revision) == (event["id"], 1)
        source_audit = _row(after, "sensitive_audit", event["sensitive_audit_id"])
        assert (
            source_audit["actor_staff_id"],
            source_audit["actor_role"],
            source_audit["before_state"]["source_revision"],
            source_audit["after_state"]["source_revision"],
        ) == (clinic.owner.staff_id, clinic.owner.role, 0, 1)
        (receipt,) = [
            row
            for row in after["idempotency_receipts"]
            if row["idempotency_key"] == source_key
        ]
        assert (
            receipt["actor_id"],
            receipt["result_projection"],
            receipt["result_revision"],
        ) == (clinic.owner.staff_id, source_projection, source_revision)
    assert after["appointments"] == before["appointments"]
    assert after["followup_claims"] == before["followup_claims"]
    assert after["appointment_price_snapshots"] == before["appointment_price_snapshots"]
    assert len(after["clinical_revisions"]) == len(before["clinical_revisions"]) + 1
    assert _row(after, "visits", visit)["recommendation"] == "Synthetic correction"
    assert (
        sum(
            row["idempotency_key"] == clinical_key
            for row in after["idempotency_receipts"]
        )
        == 1
    )
    assert sum(
        row["idempotency_key"] == source_key for row in after["idempotency_receipts"]
    ) == (not clinical_first)


@pytest.mark.parametrize("clinical_first", [True, False])
def test_completion_and_clinical_revision_serialize(booking_clinic, clinical_first):
    clinic = booking_clinic
    visit = admitted(clinic)
    projection = detail(clinic, visit)
    clinical_key, terminal_key = str(uuid4()), str(uuid4())
    clinical = partial(
        _visit_command,
        clinic,
        clinic.owner,
        visit,
        "clinical",
        {"expected_visit_revision": 0, "diagnosis": "Synthetic correction"},
        clinical_key,
    )
    terminal = partial(
        _visit_command,
        clinic,
        clinic.receptionist,
        visit,
        "complete",
        terminal_body(projection),
        terminal_key,
    )
    before = effects(clinic)
    first, second = _ordered_race(
        clinic,
        clinical if clinical_first else terminal,
        terminal if clinical_first else clinical,
        denial="STALE_REVISION" if clinical_first else None,
        checkpoint=_checkpoint(clinic, before, projection["appointment_id"]),
    )
    after = effects(clinic)
    _assert_clinical(
        before,
        after,
        visit,
        clinic.owner,
        clinical_key,
        first if clinical_first else second,
        "diagnosis",
        "Synthetic correction",
    )
    for table in (
        "appointment_day_counters",
        "appointment_number_assignments",
        "appointment_price_snapshots",
        "followup_claims",
        "followup_source_decision_events",
    ):
        assert after[table] == before[table], table
    _added(before, after, "idempotency_receipts", 1 + (not clinical_first))
    for table in (
        "appointment_history",
        "sensitive_audit",
        "outbox_events",
        "completion_attestations",
    ):
        _added(before, after, table, not clinical_first)
    if clinical_first:
        assert second is None
        assert after["appointments"] == before["appointments"]
    else:
        projection, revision, replayed = first
        assert replayed is False
        (receipt,) = [
            row
            for row in after["idempotency_receipts"]
            if row["idempotency_key"] == terminal_key
        ]
        assert (
            receipt["actor_id"],
            receipt["result_projection"],
            receipt["result_revision"],
        ) == (clinic.receptionist.staff_id, projection, revision)
        (attestation,) = _added(before, after, "completion_attestations", 1)
        assert (
            attestation["visit_id"],
            attestation["actor_staff_id"],
            attestation["actor_role"],
            attestation["completion_history_revision"],
        ) == (
            str(visit),
            clinic.receptionist.staff_id,
            clinic.receptionist.role,
            revision,
        )
    assert _row(after, "appointments", projection["appointment_id"])["status"] == (
        "In Examination" if clinical_first else "Completed"
    )
    assert len(after["completion_attestations"]) == len(
        before["completion_attestations"]
    ) + (not clinical_first)
    assert len(after["clinical_revisions"]) == len(before["clinical_revisions"]) + 1
    assert _row(after, "visits", visit)["diagnosis"] == "Synthetic correction"
    assert sum(
        row["idempotency_key"] == terminal_key for row in after["idempotency_receipts"]
    ) == (not clinical_first)

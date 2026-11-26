"""T071: CON-03/04 real signed arrival versus ordinary admission races."""

from datetime import timedelta
from functools import partial
from uuid import uuid4

import pytest
from carepulse.identity.contracts import ContractError
from sqlalchemy.exc import DBAPIError
from test_booking import booking_clinic as booking_clinic  # noqa: PLC0414
from test_patients import patient_backend as patient_backend  # noqa: PLC0414
from test_request_boundary import boundary_data as boundary_data  # noqa: PLC0414
from test_w3_admission import (
    admit_body,
    admit_request,
    today,
    waiting_case,
)
from test_w3_admission_races import _admission, _assert_delta, _case_row
from test_w3_checkin import _new_patient
from test_w3_checkin_races import _ordered_race
from test_w3_intake import _created, _queue, intake_body
from test_w3_intake import intake_clinic as intake_clinic  # noqa: PLC0414
from test_w3_intake_races import _intake
from test_w3_visits import effects as _effects


def _routine(clinic):
    patient = _new_patient(clinic, clinic.owner, "Boundary Routine")
    return _created(
        clinic, clinic.receptionist, "routine", intake_body(clinic, patient, "routine")
    )


def _assert_receipt(state, key, actor, operation, projection, revision):
    (receipt,) = [
        row for row in state["idempotency_receipts"] if row["idempotency_key"] == key
    ]
    assert (
        receipt["operation"],
        receipt["actor_id"],
        receipt["staff_id"],
        receipt["result_projection"],
        receipt["result_revision"],
    ) == (operation, actor.staff_id, actor.staff_id, projection, revision)


def _assert_arrival(state, actor, key, outcome):
    saved, revision, created = outcome
    assert (revision, created, saved["status"], saved["position"]) == (
        0,
        True,
        "Waiting",
        1,
    )
    appointment = _case_row(state, "appointments", saved["id"])
    entry = _case_row(state, "queue_entries", saved["queue_entry_id"])
    assert (
        appointment["status"],
        appointment["revision"],
        appointment["classification"],
    ) == ("Waiting", 0, saved["classification"])
    assert (
        entry["disposition"],
        entry["routine_rank"],
        entry["lane"],
        entry["arrival_sequence"],
    ) == ("ACTIVE", None, saved["lane"], saved["arrival_sequence"])
    (history,) = [
        row
        for row in state["appointment_history"]
        if row["appointment_id"] == saved["id"]
    ]
    assert (
        history["action"],
        history["new_status"],
        history["appointment_revision"],
        history["actor_staff_id"],
    ) == ("CREATED", "Waiting", 0, actor.staff_id)
    evidence = {
        "appointment_id": saved["id"],
        **{
            field: saved[field]
            for field in (
                "queue_entry_id",
                "doctor_staff_id",
                "service_date",
                "lane",
                "classification",
                "arrival_sequence",
            )
        },
    }
    operation = f"appointments.intake_{saved['classification'].lower()}"
    (audit,) = [
        row for row in state["sensitive_audit"] if row["correlation_id"] == saved["id"]
    ]
    assert (audit["action"], audit["actor_staff_id"], audit["after_state"]) == (
        operation,
        actor.staff_id,
        {**evidence, "status": "Waiting", "revision": 0},
    )
    (event,) = [
        row for row in state["outbox_events"] if row["aggregate_id"] == saved["id"]
    ]
    assert (event["event_type"], event["payload"]) == ("APPOINTMENT_INTAKE", evidence)
    _assert_receipt(state, key, actor, operation, saved, 0)


def _assert_admission(state, actor, key, outcome, body):
    projection, revision, replayed = outcome
    target = body["appointment_id"]
    assert (revision, replayed, projection["appointment_id"], projection["status"]) == (
        body["expected_appointment_revision"] + 1,
        False,
        target,
        "In Examination",
    )
    appointment = _case_row(state, "appointments", target)
    (entry,) = [
        row for row in state["queue_entries"] if row["appointment_id"] == target
    ]
    (visit,) = [row for row in state["visits"] if row["appointment_id"] == target]
    assert (
        appointment["status"],
        appointment["revision"],
        appointment["service_date"],
    ) == ("In Examination", revision, body["date"])
    assert (
        entry["disposition"],
        entry["ordinary_head_admitted"],
        entry["closure_history_revision"],
        entry["closure_queue_revision"],
        entry["revision"],
    ) == (
        "CLOSED",
        True,
        revision,
        body["expected_queue_day_revision"],
        body["expected_queue_entry_revision"] + 1,
    )
    assert (
        visit["id"],
        visit["patient_id"],
        visit["service_date"],
        visit["admission_history_revision"],
    ) == (projection["visit_id"], body["patient_id"], body["date"], revision)
    (history,) = [
        row
        for row in state["appointment_history"]
        if row["appointment_id"] == target and row["action"] == "ADMITTED"
    ]
    assert (
        history["old_status"],
        history["new_status"],
        history["actor_staff_id"],
        history["appointment_revision"],
    ) == ("Waiting", "In Examination", actor.staff_id, revision)
    evidence = {
        **projection,
        "head_appointment_id": target,
        "non_head_reason": None,
        "physical_presence_confirmed": True,
        "actor_staff_id": actor.staff_id,
        "actor_role": actor.role,
    }
    (audit,) = [
        row
        for row in state["sensitive_audit"]
        if row["correlation_id"] == target and row["action"] == "queue.admit"
    ]
    (event,) = [
        row
        for row in state["outbox_events"]
        if row["aggregate_id"] == target and row["event_type"] == "APPOINTMENT_ADMITTED"
    ]
    assert audit["after_state"] == event["payload"] == evidence
    _assert_receipt(state, key, actor, "queue.admit", projection, revision)


def _assert_capacity_denial(clinic, waiting):
    before = _effects(clinic)
    body = admit_body(clinic, waiting, physical_presence_confirmed=True)
    with (
        pytest.raises(ContractError) as denied,
        _admission(clinic, clinic.owner, body, str(uuid4())) as (_, execute),
    ):
        execute()
    assert denied.value.status_code == 409
    # Distinguish the capacity guard from the separate additional-case approval guard.
    cause = denied.value.__cause__
    assert isinstance(cause, DBAPIError)
    assert cause.orig.sqlstate == "23514"
    assert cause.orig.diag.message_primary == "Examination capacity exhausted"
    assert _effects(clinic) == before


@pytest.mark.parametrize(
    "kind,arrival_first",
    [
        pytest.param("urgent", True, id="urgent-arrival-first"),
        pytest.param("urgent", False, id="urgent-admission-first"),
        pytest.param("emergency", True, id="emergency-arrival-first"),
        pytest.param("emergency", False, id="emergency-admission-first"),
    ],
)
def test_arrival_and_ordinary_admission_serialize(intake_clinic, kind, arrival_first):
    clinic = intake_clinic
    head, tail = _routine(clinic), _routine(clinic)
    prior = (
        waiting_case(clinic, day=today() - timedelta(days=1))
        if kind == "emergency"
        else None
    )
    arriving_patient = _new_patient(clinic, clinic.owner, "Boundary Arrival")
    arrival_body = intake_body(clinic, arriving_patient, kind)
    admission_body = admit_body(
        clinic, {"appointment_id": head["id"], **head}, physical_presence_confirmed=True
    )
    initial_main = _queue(clinic, "MAIN")
    assert [row["appointment_id"] for row in initial_main["entries"]] == [
        head["id"],
        tail["id"],
    ]
    assert initial_main["examination_capacity"] == 1
    assert initial_main["active_in_examination_count"] == 0
    assert admission_body["expected_head_appointment_id"] == head["id"]
    arrival_key, admission_key = str(uuid4()), str(uuid4())
    arrival = partial(
        _intake, clinic, clinic.receptionist, kind, arrival_body, arrival_key
    )
    admission = partial(_admission, clinic, clinic.owner, admission_body, admission_key)
    before = _effects(clinic)

    def checkpoint(_observer):
        assert _effects(clinic) == before

    stale = kind == "urgent" and arrival_first
    first, second = _ordered_race(
        clinic,
        arrival if arrival_first else admission,
        admission if arrival_first else arrival,
        denial="STALE_REVISION" if stale else None,
        checkpoint=checkpoint,
    )
    arrival_outcome, admission_outcome = (
        (first, second) if arrival_first else (second, first)
    )
    arrived = arrival_outcome[0]
    after = _effects(clinic)
    _assert_arrival(after, clinic.receptionist, arrival_key, arrival_outcome)
    assert arrived["patient_id"] == arriving_patient
    assert arrived["service_date"] == head["service_date"]
    assert arrived["classification"] == kind.upper()
    assert arrived["lane"] == ("MAIN" if kind == "urgent" else "EMERGENCY")

    admitted_count = int(not stale)
    expected_changes = ("appointment_day_counters", "queue_days")
    if not stale:
        expected_changes += ("appointments", "queue_entries")
    _assert_delta(
        before,
        after,
        changed=expected_changes,
        appended={
            "appointments": 1,
            "appointment_number_assignments": 1,
            "appointment_price_snapshots": 1,
            "queue_entries": 1,
            "queue_days": int(kind == "emergency"),
            "visits": admitted_count,
            **{
                table: 1 + admitted_count
                for table in (
                    "appointment_history",
                    "sensitive_audit",
                    "outbox_events",
                    "idempotency_receipts",
                )
            },
        },
    )
    for untouched in (head, tail) if stale else (tail,):
        for table, identifier in (
            ("appointments", untouched["id"]),
            ("queue_entries", untouched["queue_entry_id"]),
        ):
            assert _case_row(after, table, identifier) == _case_row(
                before, table, identifier
            )

    main = _queue(clinic, "MAIN")
    emergency = _queue(clinic, "EMERGENCY")
    expected_main = (
        [arrived["id"], head["id"], tail["id"]]
        if stale
        else [arrived["id"], tail["id"]]
        if kind == "urgent"
        else [tail["id"]]
    )
    assert [
        (row["appointment_id"], row["position"]) for row in main["entries"]
    ] == list(zip(expected_main, range(1, len(expected_main) + 1), strict=True))
    assert [
        row["routine_rank"]
        for row in main["entries"]
        if row["classification"] == "ROUTINE"
    ] == (
        [head["routine_rank"], tail["routine_rank"]]
        if stale
        else [tail["routine_rank"]]
    )
    expected_cursor = initial_main["routine_cursor"] if stale else head["number"]
    assert main["routine_cursor"] == expected_cursor
    assert main["queue_day_revision"] == initial_main[
        "queue_day_revision"
    ] + admitted_count + int(kind == "urgent")
    assert main["arrival_counter"] == initial_main["arrival_counter"] + int(
        kind == "urgent"
    )
    assert (
        main["active_in_examination_count"]
        == emergency["active_in_examination_count"]
        == admitted_count
    )
    assert [
        (row["appointment_id"], row["position"]) for row in emergency["entries"]
    ] == ([(arrived["id"], 1)] if kind == "emergency" else [])
    assert emergency["routine_cursor"] == 0

    if stale:
        assert admission_outcome is None
        assert not any(
            row["idempotency_key"] == admission_key
            for row in after["idempotency_receipts"]
        )
        # Even with refreshed revisions the old head identity cannot be admitted.
        fresh_body = admit_body(
            clinic,
            {"appointment_id": head["id"], **head},
            physical_presence_confirmed=True,
        )
        assert fresh_body["expected_head_appointment_id"] == arrived["id"]
        status, reply = admit_request(
            clinic,
            clinic.owner,
            {**fresh_body, "expected_head_appointment_id": head["id"]},
            admission_key,
        )
        assert status == 409 and reply["error"]["code"] == "STALE_REVISION"
        assert _effects(clinic) == after
    else:
        _assert_admission(
            after, clinic.owner, admission_key, admission_outcome, admission_body
        )
        assert admission_outcome[0]["routine_cursor"] == expected_cursor
        assert (
            admission_outcome[0]["queue_day_revision"]
            == initial_main["queue_day_revision"] + 1
        )
        if kind == "emergency":
            _assert_capacity_denial(
                clinic, {"appointment_id": arrived["id"], **arrived}
            )
            _assert_capacity_denial(clinic, prior)

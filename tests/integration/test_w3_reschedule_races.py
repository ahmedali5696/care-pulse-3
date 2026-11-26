"""T053: ordered signed PostgreSQL contention and rollback for B-18."""

from contextlib import contextmanager
from datetime import timedelta
from uuid import UUID, uuid4

import pytest
from carepulse.appointments.book import book_appointment
from carepulse.appointments.booking_contracts import BookAppointment
from carepulse.appointments.reschedule import (
    RescheduleAppointment,
    reschedule_appointment,
)
from carepulse.db import transaction
from carepulse.identity.staff import change_receptionist
from carepulse.identity.staff_contracts import StaffCommand
from carepulse.visits.eligibility import extend_followup
from runtime_identity_fixture import signed_test_transaction
from sqlalchemy import text
from test_booking import booking_body, booking_request, new_patient
from test_booking import booking_clinic as booking_clinic  # noqa: PLC0414
from test_followup_decisions import _decision, _extension, _signed_visit_request
from test_followups import claim_record, followup_body
from test_followups_races import followup_clinic as followup_clinic  # noqa: PLC0414
from test_patients import patient_backend as patient_backend  # noqa: PLC0414
from test_pricing import (
    _fresh_receptionist_session,
    _manager_change,
    _manager_transaction,
    _owner_changes_manager,
)
from test_queue_capacity import _policy_capacity
from test_request_boundary import boundary_data as boundary_data  # noqa: PLC0414
from test_schedule import _activate, _config, _patch, _schedule_request
from test_slots import _slots_request
from test_w3_admission import _admit, admit_body
from test_w3_admission_controls import _capacity_body
from test_w3_admission_races import _policy
from test_w3_cancel_replace import _appointment, book_case, cancel_body, replace_body
from test_w3_cancel_replace_races import _checkpoint, _command, _observe_waiter
from test_w3_checkin_races import _archive, _ordered_race
from test_w3_intake import (
    _policy_revision,
    _today,
    intake_body,
    intake_request,
    set_intake_schedule,
)
from test_w3_intake import intake_clinic as intake_clinic  # noqa: PLC0414
from test_w3_reschedule import denied, move_body, move_request, rows
from test_w3_visit_races import _claimed_visit, _denied_holder, _visit_command
from test_w3_visits import detail, effects, terminal_body, visit_request


@contextmanager
def move_transaction(clinic, actor, target, body, key):
    with signed_test_transaction(
        clinic,
        actor,
        method="POST",
        path=f"/v1/appointments/{target}/reschedule",
        purpose="APPOINTMENT_RESCHEDULE",
        body=body,
        key=key,
    ) as (connection, authorized):
        yield (
            connection,
            lambda: reschedule_appointment(
                connection,
                authorized,
                UUID(str(target)),
                RescheduleAppointment.model_validate(body),
                key,
            ),
        )


@contextmanager
def booking_transaction(clinic, body, key):
    with signed_test_transaction(
        clinic,
        clinic.owner,
        method="POST",
        path="/v1/appointments",
        purpose="APPOINTMENT_BOOKING",
        body=body,
        key=key,
    ) as (connection, authorized):
        yield (
            connection,
            lambda: book_appointment(
                connection, authorized, BookAppointment.model_validate(body), key
            ),
        )


def _confirmed_claim(clinic):
    """Build a qualifying source and future claim through real signed commands."""
    original = book_case(clinic, waiting=True)
    patient = _appointment(clinic, original)["patient_id"]
    waiting = {
        "appointment_id": original,
        "service_date": _today().isoformat(),
        "lane": "MAIN",
    }
    admitted = _admit(
        clinic,
        clinic.owner,
        admit_body(clinic, waiting, physical_presence_confirmed=True),
    )
    visit = UUID(admitted["visit_id"])
    status, reply = visit_request(
        clinic, clinic.owner, visit, "complete", terminal_body(detail(clinic, visit))
    )
    assert status == 200, reply
    source = {
        "patient": patient,
        "visit": visit,
        "day": _today(),
        "appointment": UUID(original),
    }
    status, reply = booking_request(
        clinic,
        clinic.owner,
        body=followup_body(
            source,
            offset=1,
            expected_price_revision=_policy_revision(clinic, "PRICE"),
            expected_schedule_revision=_policy_revision(clinic, "SCHEDULE"),
            expected_followup_revision=_policy_revision(clinic, "FOLLOWUP"),
        ),
        key=uuid4().hex,
    )
    assert status == 201, reply
    target = reply["data"]["id"]
    return source, target


def _extended_claim(clinic):
    source, target = _confirmed_claim(clinic)
    frozen = claim_record(clinic, target)
    destination = frozen["approved_deadline"] + timedelta(days=1)
    command = _extension(
        source,
        target,
        deadline=destination,
        followup_revision=_policy_revision(clinic, "FOLLOWUP"),
    )
    status, reply = _signed_visit_request(
        clinic,
        clinic.receptionist,
        path=f"/v1/visits/{source['visit']}/extend-followup",
        method="POST",
        body=command.model_dump(mode="json", exclude_unset=True),
        key=uuid4().hex,
    )
    assert status == 201, reply
    return source, target, destination, reply["data"]["event_id"]


@pytest.mark.parametrize("move_first", [True, False])
@pytest.mark.parametrize("competitor", ["restriction", "extension", "release"])
def test_confirmed_extension_move_and_claim_authority_races(
    intake_clinic, move_first, competitor
):
    clinic = intake_clinic
    source, target, destination, event_id = _extended_claim(clinic)
    body = move_body(clinic, target, destination, extension_event_id=event_id)
    move_key, other_key = uuid4().hex, uuid4().hex

    def move():
        return move_transaction(clinic, clinic.receptionist, target, body, move_key)

    if competitor == "restriction":
        command = _decision(
            "OPTED_OUT",
            reason="Synthetic later Doctor restriction",
            followup_revision=_policy_revision(clinic, "FOLLOWUP"),
        )

        def compete():
            return _visit_command(
                clinic,
                clinic.owner,
                source["visit"],
                "source",
                command.model_dump(mode="json", exclude_unset=True),
                other_key,
            )

    elif competitor == "extension":
        command = _extension(
            source,
            target,
            deadline=destination + timedelta(days=1),
            followup_revision=_policy_revision(clinic, "FOLLOWUP"),
            authorization_revision=body["expected_authorization_revision"],
        )

        @contextmanager
        def compete():
            with signed_test_transaction(
                clinic,
                clinic.owner,
                method="POST",
                path=f"/v1/visits/{source['visit']}/extend-followup",
                purpose="VISIT_FOLLOWUP",
                body=command.model_dump(mode="json", exclude_unset=True),
                key=other_key,
            ) as (connection, actor):
                yield (
                    connection,
                    lambda: extend_followup(
                        connection, actor, source["visit"], command, other_key
                    ),
                )

    else:
        command = cancel_body(clinic, target)

        def compete():
            return _command(clinic, clinic.owner, UUID(target), command, other_key)

    before = effects(clinic)
    committed = []
    both_succeed = move_first and competitor == "restriction"
    _ordered_race(
        clinic,
        move if move_first else compete,
        lambda: _observe_waiter(clinic, compete if move_first else move, committed),
        denial=None if both_succeed else "CONFLICT",
        checkpoint=_checkpoint(clinic, before),
    )
    after = effects(clinic)
    if not both_succeed:
        assert committed == [after]
    assert after["appointment_price_snapshots"] == before["appointment_price_snapshots"]
    assert after["visits"] == before["visits"]
    assert after["clinical_audit"] == before["clinical_audit"]
    old_claim = rows(before, "followup_claims", target)[0]
    new_claim = rows(after, "followup_claims", target)[0]
    if not move_first and competitor == "release":
        assert new_claim["state"] == "RELEASED"
        assert new_claim["revision"] == old_claim["revision"] + 1
        assert new_claim["policy_snapshot"] == old_claim["policy_snapshot"]
    else:
        assert new_claim == old_claim
    keys = {r["idempotency_key"] for r in after["idempotency_receipts"]}
    assert (move_key in keys) is move_first
    assert (other_key in keys) is (not move_first or both_succeed)
    assert (_appointment(clinic, target)["service_date"] == destination) is move_first
    if competitor == "restriction":
        fresh = move_body(clinic, target, destination, extension_event_id=event_id)
        denied(clinic, clinic.receptionist, target, fresh, 409)


@pytest.mark.parametrize("complete_first", [True, False])
def test_claim_consumption_and_in_exam_move_in_both_orders(
    followup_clinic, complete_first
):
    clinic = followup_clinic
    _, visit = _claimed_visit(clinic)
    projection = detail(clinic, visit)
    target = projection["appointment_id"]
    body = move_body(clinic, target, _today() + timedelta(days=1))
    move_key, complete_key = uuid4().hex, uuid4().hex

    def move():
        return move_transaction(clinic, clinic.receptionist, target, body, move_key)

    def complete():
        return _visit_command(
            clinic,
            clinic.owner,
            visit,
            "complete",
            terminal_body(projection),
            complete_key,
        )

    before = effects(clinic)
    committed = []
    _ordered_race(
        clinic,
        complete if complete_first else lambda: _denied_holder(move),
        (lambda: _observe_waiter(clinic, move, committed))
        if complete_first
        else complete,
        denial="CONFLICT" if complete_first else None,
        checkpoint=_checkpoint(clinic, before),
    )
    after = effects(clinic)
    if complete_first:
        assert committed == [after]
    assert rows(after, "followup_claims", target)[0]["state"] == "CONSUMED"
    assert _appointment(clinic, target)["status"] == "Completed"
    assert after["appointment_price_snapshots"] == before["appointment_price_snapshots"]
    assert (
        after["appointment_number_assignments"]
        == before["appointment_number_assignments"]
    )
    assert after["appointment_day_counters"] == before["appointment_day_counters"]
    assert not any(
        r["idempotency_key"] == move_key for r in after["idempotency_receipts"]
    )


@pytest.mark.parametrize("move_first", [True, False])
def test_reschedule_replacement_order(intake_clinic, move_first):
    clinic = intake_clinic
    target = UUID(book_case(clinic))
    body = move_body(clinic, target, _today() + timedelta(days=1))
    replacement = replace_body(clinic, target)
    move_key, replace_key = uuid4().hex, uuid4().hex
    before = effects(clinic)
    committed = []

    def move():
        return move_transaction(clinic, clinic.owner, target, body, move_key)

    def replace():
        return _command(
            clinic,
            clinic.receptionist,
            target,
            replacement,
            replace_key,
            replacement=True,
        )

    _ordered_race(
        clinic,
        move if move_first else replace,
        lambda: _observe_waiter(clinic, replace if move_first else move, committed),
        denial="CONFLICT",
        checkpoint=_checkpoint(clinic, before),
    )
    after = effects(clinic)
    assert committed == [after]
    assert not any(
        r["idempotency_key"] == (replace_key if move_first else move_key)
        for r in after["idempotency_receipts"]
    )
    assert _appointment(clinic, target)["status"] == (
        "Booked" if move_first else "Cancelled"
    )
    assert len(after["appointments"]) == len(before["appointments"]) + (not move_first)


@pytest.mark.parametrize("move_first", [True, False])
def test_archive_same_id_move_order(intake_clinic, move_first):
    clinic = intake_clinic
    target = book_case(clinic, waiting=True)
    patient = _appointment(clinic, target)["patient_id"]
    body = move_body(clinic, target, _today() + timedelta(days=1))
    key = uuid4().hex
    before = effects(clinic)

    def move():
        return move_transaction(clinic, clinic.owner, target, body, key)

    def archive():
        return _archive(clinic, patient)

    _ordered_race(
        clinic,
        move if move_first else archive,
        archive if move_first else move,
        checkpoint=_checkpoint(clinic, before),
    )
    after = effects(clinic)
    saved = _appointment(clinic, target)
    assert (
        saved["status"] == "Booked"
        and saved["service_date"].isoformat() == body["service_date"]
    )
    assert after["appointment_price_snapshots"] == before["appointment_price_snapshots"]
    assert len(after["appointments"]) == len(before["appointments"])
    assert all(
        r["disposition"] != "ACTIVE" for r in rows(after, "queue_entries", target)
    )
    assert (
        len(after["appointment_number_assignments"])
        == len(before["appointment_number_assignments"]) + 1
    )
    assert len(after["idempotency_receipts"]) == len(before["idempotency_receipts"]) + 1
    original = next(r for r in before["appointments"] if r["id"] == target)
    (assignment,) = [
        r
        for r in rows(after, "appointment_number_assignments", target)
        if r not in before["appointment_number_assignments"]
    ]
    assert assignment["prior_assignment_id"] == original["current_assignment_id"]
    assert assignment["number"] == saved["current_number"]
    (history,) = [
        r
        for r in rows(after, "appointment_history", target)
        if r not in before["appointment_history"]
    ]
    assert (
        history["action"],
        history["old_status"],
        history["new_status"],
        history["actor_staff_id"],
        history["reason"],
    ) == ("RESCHEDULE", "Waiting", "Booked", clinic.owner.staff_id, body["reason"])
    assert (
        history["old_number"] == original["current_number"]
        and history["new_number"] == saved["current_number"]
    )
    (old_entry,) = rows(before, "queue_entries", target)
    (closed_entry,) = rows(after, "queue_entries", target)
    for field in (
        "id",
        "service_date",
        "arrival_sequence",
        "routine_rank",
        "attested_by_staff_id",
    ):
        assert old_entry[field] == closed_entry[field]
    assert closed_entry["disposition"] == "CLOSED"
    assert closed_entry["closure_history_revision"] == saved["revision"]
    for table in (
        "followup_claims",
        "followup_source_state",
        "followup_authorization_events",
        "followup_source_decision_events",
        "visits",
    ):
        assert after[table] == before[table]
    audits = [r for r in after["sensitive_audit"] if r not in before["sensitive_audit"]]
    assert sorted(r["action"] for r in audits) == [
        "APPOINTMENT_RESCHEDULED",
        "patients.archive",
    ]
    archive_audit = next(r for r in audits if r["action"] == "patients.archive")
    assert archive_audit["actor_staff_id"] == clinic.owner.staff_id
    assert archive_audit["actor_role"] == "DOCTOR_OWNER"
    assert archive_audit["reason"] == "CON-41 synthetic archive"
    assert archive_audit["after_state"]["lifecycle_revision"] == 1
    assert archive_audit["after_state"]["archived"] is True
    assert sorted(
        r["event_type"]
        for r in after["outbox_events"]
        if r not in before["outbox_events"]
    ) == ["APPOINTMENT_RESCHEDULED", "DOCTOR_RESCHEDULED_APPOINTMENT"]
    with clinic.migration.connect() as connection:
        assert connection.execute(
            text(
                "SELECT archived,lifecycle_revision FROM public.patients WHERE organization_id=:t AND id=:p"
            ),
            {"t": clinic.tenant, "p": patient},
        ).one() == (True, 1)


@pytest.mark.parametrize("move_first", [True, False])
@pytest.mark.parametrize("overlap", [False, True])
def test_new_booking_and_reschedule_count_destination_once(
    intake_clinic, move_first, overlap
):
    clinic = intake_clinic
    target = book_case(clinic)
    patient = new_patient(clinic)
    destination = _today() + timedelta(days=1)
    set_intake_schedule(
        clinic,
        mode="EXACT_TIME",
        periods=(("09:00", "10:00"),),
        duration=15,
        overlap=overlap,
    )
    if overlap:
        with transaction(clinic.migration, clinic.tenant) as connection:
            _policy_capacity(connection, clinic.tenant, capacity=2)
    status, slots = _slots_request(clinic, clinic.owner, destination)
    assert status == 200, slots
    interval = {k: slots["data"]["candidates"][0][k] for k in ("starts_at", "ends_at")}
    if overlap:
        interval["expected_capacity_revision"] = _policy_revision(clinic, "CAPACITY")
        status, reply = booking_request(
            clinic,
            clinic.owner,
            body=booking_body(
                new_patient(clinic),
                day=destination,
                expected_price_revision=_policy_revision(clinic, "PRICE"),
                expected_schedule_revision=_policy_revision(clinic, "SCHEDULE"),
                **interval,
            ),
            key=uuid4().hex,
        )
        assert status == 201, reply
    body = move_body(clinic, target, destination, **interval)
    booking = booking_body(
        patient,
        day=destination,
        expected_price_revision=_policy_revision(clinic, "PRICE"),
        expected_schedule_revision=_policy_revision(clinic, "SCHEDULE"),
        **interval,
    )
    move_key, book_key = uuid4().hex, uuid4().hex
    before = effects(clinic)
    committed = []

    def move():
        return move_transaction(clinic, clinic.owner, target, body, move_key)

    def book():
        return booking_transaction(clinic, booking, book_key)

    _ordered_race(
        clinic,
        move if move_first else book,
        lambda: _observe_waiter(clinic, book if move_first else move, committed),
        denial="CONFLICT",
        checkpoint=_checkpoint(clinic, before),
    )
    after = effects(clinic)
    assert committed == [after]
    assert len(
        [
            r
            for r in after["appointments"]
            if r["service_date"] == destination.isoformat()
        ]
    ) == (2 if overlap else 1)
    assert not any(
        r["idempotency_key"] == (book_key if move_first else move_key)
        for r in after["idempotency_receipts"]
    )


@pytest.mark.parametrize("claimed", [False, True])
def test_reschedule_rolls_back_after_deferred_checks(intake_clinic, claimed):
    clinic = intake_clinic
    if claimed:
        _, target, destination, event_id = _extended_claim(clinic)
        body = move_body(clinic, target, destination, extension_event_id=event_id)
        assert claim_record(clinic, target)["state"] == "ACTIVE"
    else:
        target = book_case(clinic, waiting=True)
        body = move_body(clinic, target, _today() + timedelta(days=1))
    before = effects(clinic)
    key = uuid4().hex
    with (
        pytest.raises(RuntimeError, match="synthetic rollback"),
        move_transaction(clinic, clinic.owner, target, body, key) as (
            connection,
            execute,
        ),
    ):
        projection, _ = execute()
        assert projection["status"] == "Booked"
        connection.execute(text("SET CONSTRAINTS ALL IMMEDIATE"))
        raise RuntimeError("synthetic rollback")
    assert effects(clinic) == before


@pytest.mark.parametrize("move_first", [True, False])
@pytest.mark.parametrize("occupied", [False, True])
def test_reschedule_and_capacity_publication_in_both_orders(
    intake_clinic, move_first, occupied
):
    clinic = intake_clinic
    target = book_case(clinic)
    destination = _today() + timedelta(days=1)
    # Set up capacity three so a valid publication to two can contend with a move.
    with transaction(clinic.migration, clinic.tenant) as connection:
        _policy_capacity(connection, clinic.tenant, capacity=3)
    set_intake_schedule(
        clinic,
        mode="EXACT_TIME",
        periods=(("09:00", "10:00"),),
        duration=15,
        overlap=True,
    )
    status, slots = _slots_request(clinic, clinic.owner, destination)
    assert status == 200, slots
    interval = {k: slots["data"]["candidates"][0][k] for k in ("starts_at", "ends_at")}
    interval["expected_capacity_revision"] = _policy_revision(clinic, "CAPACITY")
    for _ in range(2 if occupied else 0):
        status, reply = booking_request(
            clinic,
            clinic.owner,
            body=booking_body(
                new_patient(clinic),
                day=destination,
                expected_price_revision=_policy_revision(clinic, "PRICE"),
                expected_schedule_revision=_policy_revision(clinic, "SCHEDULE"),
                **interval,
            ),
            key=uuid4().hex,
        )
        assert status == 201, reply
    body = move_body(clinic, target, destination, **interval)
    capacity = _capacity_body(clinic, 2)
    move_key, policy_key = uuid4().hex, uuid4().hex
    before = effects(clinic)
    committed = []

    def move():
        return move_transaction(clinic, clinic.receptionist, target, body, move_key)

    def publish():
        return _policy(clinic, clinic.owner, capacity, policy_key)

    loses = not move_first or occupied
    _ordered_race(
        clinic,
        move if move_first else publish,
        lambda: _observe_waiter(clinic, publish if move_first else move, committed),
        denial="CONFLICT" if loses else None,
        checkpoint=_checkpoint(clinic, before),
    )
    after = effects(clinic)
    if loses:
        assert committed == [after]
    saved = _appointment(clinic, target)
    assert (saved["service_date"] == destination) is move_first
    assert saved["revision"] == body["expected_appointment_revision"] + move_first
    assert after["appointment_price_snapshots"] == before["appointment_price_snapshots"]
    assert after["followup_claims"] == before["followup_claims"]
    assert after["queue_entries"] == before["queue_entries"]
    assert (
        len(after["appointment_number_assignments"])
        == len(before["appointment_number_assignments"]) + move_first
    )
    assert _policy_revision(clinic, "CAPACITY") == capacity[
        "expected_capacity_revision"
    ] + (not move_first or not occupied)
    keys = {r["idempotency_key"] for r in after["idempotency_receipts"]}
    assert (move_key in keys) is move_first
    assert (policy_key in keys) is (not move_first or not occupied)


@pytest.mark.parametrize("doctor_first", [True, False])
def test_distinct_reschedule_commands_share_target_revision(
    intake_clinic, doctor_first
):
    clinic = intake_clinic
    target = book_case(clinic)
    doctor_body = move_body(clinic, target, _today() + timedelta(days=1))
    reception_body = move_body(clinic, target, _today() + timedelta(days=2))
    doctor_key, reception_key = uuid4().hex, uuid4().hex
    before = effects(clinic)
    committed = []

    def doctor():
        return move_transaction(clinic, clinic.owner, target, doctor_body, doctor_key)

    def reception():
        return move_transaction(
            clinic, clinic.receptionist, target, reception_body, reception_key
        )

    _ordered_race(
        clinic,
        doctor if doctor_first else reception,
        lambda: _observe_waiter(
            clinic, reception if doctor_first else doctor, committed
        ),
        denial="STALE_REVISION",
        checkpoint=_checkpoint(clinic, before),
    )
    after = effects(clinic)
    assert committed == [after]
    expected = doctor_body if doctor_first else reception_body
    assert (
        _appointment(clinic, target)["service_date"].isoformat()
        == expected["service_date"]
    )
    assert after["appointment_price_snapshots"] == before["appointment_price_snapshots"]
    assert (
        len(after["appointment_number_assignments"])
        == len(before["appointment_number_assignments"]) + 1
    )
    assert len(after["appointment_history"]) == len(before["appointment_history"]) + 1
    assert len(after["idempotency_receipts"]) == len(before["idempotency_receipts"]) + 1
    assert (
        sum(
            r["event_type"] == "DOCTOR_RESCHEDULED_APPOINTMENT"
            for r in after["outbox_events"]
            if r not in before["outbox_events"]
        )
        == doctor_first
    )


@pytest.mark.parametrize("move_first", [True, False])
def test_schedule_publication_and_move_recheck_empty_destination(
    intake_clinic, move_first
):
    clinic = intake_clinic
    target = book_case(clinic)
    destination = _today() + timedelta(days=1)
    body = move_body(clinic, target, destination)
    config = _config(
        mode="OPEN",
        working_weekdays=list(range(1, 8)),
        weekly_work_periods=[
            {"weekday": day, "starts_at": "00:00", "ends_at": "23:59"}
            for day in range(1, 8)
        ],
        holidays=[destination.isoformat()],
    )
    status, reply = _schedule_request(
        clinic,
        clinic.owner,
        method="POST",
        suffix="/preview",
        body=_patch(config, revision=body["expected_schedule_revision"]).model_dump(
            mode="json"
        ),
        key=uuid4().hex,
    )
    assert status == 200, reply
    preview = reply["data"]
    payload = {
        "preview_id": preview["preview_id"],
        "expected_schedule_revision": preview["revisions"]["schedule"],
        "expected_capacity_revision": preview["revisions"]["capacity"],
        "reason": "Synthetic reviewed schedule publication",
    }
    move_key, schedule_key = uuid4().hex, uuid4().hex

    def move():
        return move_transaction(clinic, clinic.receptionist, target, body, move_key)

    @contextmanager
    def publish():
        with signed_test_transaction(
            clinic,
            clinic.owner,
            method="POST",
            path="/v1/settings/schedule/activate",
            purpose="SETTINGS_SCHEDULE",
            body=payload,
            key=schedule_key,
        ) as (connection, actor):
            yield (
                connection,
                lambda: _activate(connection, actor, preview, schedule_key),
            )

    before = effects(clinic)
    committed = []
    _ordered_race(
        clinic,
        move if move_first else publish,
        lambda: _observe_waiter(clinic, publish if move_first else move, committed),
        denial="CONFLICT",
        checkpoint=_checkpoint(clinic, before),
    )
    after = effects(clinic)
    assert committed == [after]
    assert (_appointment(clinic, target)["service_date"] == destination) is move_first
    assert after["appointment_price_snapshots"] == before["appointment_price_snapshots"]
    assert after["queue_entries"] == before["queue_entries"]
    assert (
        len(after["appointment_number_assignments"])
        == len(before["appointment_number_assignments"]) + move_first
    )
    assert _policy_revision(clinic, "SCHEDULE") == body[
        "expected_schedule_revision"
    ] + (not move_first)
    keys = {r["idempotency_key"] for r in after["idempotency_receipts"]}
    assert (move_key in keys) is move_first
    assert (schedule_key in keys) is (not move_first)


@pytest.mark.parametrize("move_first", [True, False])
@pytest.mark.parametrize("manager_only", [True, False])
def test_urgent_reschedule_and_live_staff_withdrawal(
    intake_clinic, move_first, manager_only
):
    clinic = intake_clinic
    if manager_only:
        _owner_changes_manager(clinic, True, 0)
        acting = _fresh_receptionist_session(clinic, 1)
    else:
        acting = clinic.receptionist
    status, created = intake_request(
        clinic, acting, "urgent", intake_body(clinic, new_patient(clinic), "urgent")
    )
    assert status == 201, created
    target = created["data"]["id"]
    body = move_body(
        clinic,
        target,
        _today(),
        physical_presence_confirmed=True,
        expected_price_revision=_policy_revision(clinic, "PRICE"),
    )
    key = uuid4().hex
    before = effects(clinic)

    def move():
        return move_transaction(clinic, acting, target, body, key)

    @contextmanager
    def withdraw():
        if manager_only:
            with _manager_transaction(clinic, False, 1) as (connection, _):
                yield connection, lambda: _manager_change(connection, clinic, False, 1)
        else:
            command = StaffCommand(
                reason="Synthetic B-04 withdrawal",
                expected_revision=0,
                expected_security_revision=0,
            )
            with signed_test_transaction(
                clinic,
                clinic.owner,
                method="POST",
                path=f"/v1/staff/receptionists/{acting.staff_id}/disable",
                purpose="STAFF_MANAGEMENT",
                body=command.model_dump(mode="json"),
            ) as (connection, authorized):
                yield (
                    connection,
                    lambda: change_receptionist(
                        connection,
                        authorized,
                        UUID(acting.staff_id),
                        command,
                        "disable",
                    ),
                )

    _ordered_race(
        clinic,
        move if move_first else withdraw,
        withdraw if move_first else move,
        denial=None if move_first else "REVOKED",
        denial_status=401 if manager_only else 403,
        checkpoint=_checkpoint(clinic, before),
    )
    after = effects(clinic)
    assert after["appointment_price_snapshots"] == before["appointment_price_snapshots"]
    assert after["queue_entries"] == before["queue_entries"]
    assert (
        len(after["appointment_history"])
        == len(before["appointment_history"]) + move_first
    )
    assert (
        len(after["idempotency_receipts"])
        == len(before["idempotency_receipts"]) + move_first
    )
    attestations = [
        r
        for r in after["sensitive_audit"]
        if r not in before["sensitive_audit"]
        and (r.get("after_state") or {}).get("physical_presence_confirmed") is True
    ]
    assert len(attestations) == move_first
    if move_first:
        assert attestations[0]["actor_staff_id"] == acting.staff_id
        assert attestations[0]["after_state"]["appointment_id"] == target
    denied(clinic, acting, target, body, 401 if manager_only else 403, key=key)
    fresh_body = move_body(
        clinic,
        target,
        _today(),
        physical_presence_confirmed=True,
        expected_price_revision=_policy_revision(clinic, "PRICE"),
    )
    denied(clinic, acting, target, fresh_body, 401 if manager_only else 403)
    if manager_only:
        fresh = _fresh_receptionist_session(clinic, 2)
        status, reply = move_request(clinic, fresh, target, fresh_body)
        assert status == 200, reply

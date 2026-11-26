"""T052: signed same-ID rescheduling and durable onsite evidence."""

import json
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from time import sleep
from uuid import UUID, uuid4

import pytest
from carepulse.appointments.reschedule import (
    RescheduleAppointment,
    recover_committed_reschedule,
)
from carepulse.db import transaction
from carepulse.identity.authorization import protected_transaction
from runtime_identity_fixture import registered_test_request, signed_test_transaction
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from test_booking import booking_body, booking_request, new_patient
from test_booking import booking_clinic as booking_clinic  # noqa: PLC0414
from test_followup_decisions import _extension, _signed_visit_request
from test_followups import (
    claim_record,
    doctor_decision,
    followup_body,
    same_day_completed_source,
)
from test_patients import patient_backend as patient_backend  # noqa: PLC0414
from test_pricing import (
    _fresh_receptionist_session,
    _owner_changes_manager,
    pricing_request,
)
from test_pricing import _patch as pricing_patch
from test_queue_capacity import _policy_capacity
from test_request_boundary import boundary_data as boundary_data  # noqa: PLC0414
from test_schedule import CAIRO
from test_slots import _slots_request
from test_w3_cancel_replace import _appointment, book_case, cancel_body, change_request
from test_w3_checkin import queue_request
from test_w3_checkin_races import _archive
from test_w3_intake import (
    _policy_revision,
    _today,
    intake_body,
    intake_request,
    set_intake_schedule,
)
from test_w3_intake import intake_clinic as intake_clinic  # noqa: PLC0414
from test_w3_visits import effects


def move_body(clinic, target, destination, **changes):
    revisions = cancel_body(clinic, target)
    source_revision = revisions.pop("expected_source_revision", None)
    if source_revision is not None:
        with clinic.migration.connect() as connection:
            visit_revision = connection.scalar(
                text("""
                SELECT v.clinical_revision FROM public.followup_claims c
                JOIN public.visits v ON v.organization_id=c.organization_id AND v.id=c.source_visit_id
                WHERE c.organization_id=:tenant AND c.appointment_id=:target
            """),
                {"tenant": clinic.tenant, "target": target},
            )
        revisions.update(
            expected_source_decision_revision=source_revision,
            expected_visit_revision=visit_revision,
            expected_followup_revision=_policy_revision(clinic, "FOLLOWUP"),
        )
    revisions["reason"] = "Synthetic reasoned reschedule"
    revisions["service_date"] = destination.isoformat()
    revisions["expected_schedule_revision"] = _policy_revision(clinic, "SCHEDULE")
    return {**revisions, **changes}


def move_request(clinic, actor, target, body, *, key=None):
    return queue_request(
        clinic,
        actor,
        method="POST",
        path=f"/v1/appointments/{target}/reschedule",
        purpose="APPOINTMENT_RESCHEDULE",
        body=body,
        key=key or uuid4().hex,
    )


def denied(clinic, actor, target, body, expected, *, key=None):
    before = effects(clinic)
    status, reply = move_request(clinic, actor, target, body, key=key)
    assert status == expected, reply
    assert set(reply) == {"error"}
    assert effects(clinic) == before


def rows(state, table, target):
    return [row for row in state[table] if row.get("appointment_id") == str(target)]


def assert_move(clinic, actor, target, body, before, reply, key):
    after = effects(clinic)
    original = next(row for row in before["appointments"] if row["id"] == str(target))
    saved = next(row for row in after["appointments"] if row["id"] == str(target))
    assert len(after["appointments"]) == len(before["appointments"])
    assert reply["revision"] == saved["revision"] == original["revision"] + 1
    assert reply["data"]["appointment_id"] == str(target)
    assert saved["service_date"] == body["service_date"]
    for table in (
        "appointment_price_snapshots",
        "followup_claims",
        "followup_authorization_events",
        "visits",
    ):
        assert after[table] == before[table], table
    assert (
        saved["patient_id"],
        saved["doctor_staff_id"],
        saved["classification"],
        saved["lane"],
    ) == (
        original["patient_id"],
        original["doctor_staff_id"],
        original["classification"],
        original["lane"],
    )
    (history,) = [
        row
        for row in rows(after, "appointment_history", target)
        if row not in before["appointment_history"]
    ]
    assert history["old_service_date"] == original["service_date"]
    assert history["new_service_date"] == saved["service_date"]
    assert history["old_number"] == original["current_number"]
    assert history["new_number"] == saved["current_number"]
    assert (
        history["actor_staff_id"] == actor.staff_id
        and history["actor_role"] == actor.role
    )
    assert (
        history["reason"] == body["reason"]
        and history["replacement_appointment_id"] is None
    )
    (audit,) = [
        row for row in after["sensitive_audit"] if row not in before["sensitive_audit"]
    ]
    assert (
        audit["actor_staff_id"] == actor.staff_id and audit["actor_role"] == actor.role
    )
    assert audit["organization_id"] == str(clinic.tenant)
    assert audit["occurred_at"] == history["occurred_at"]
    assert audit["after_state"]["appointment_id"] == str(target)
    assert audit["after_state"].get("physical_presence_confirmed") is (
        True if original["classification"] == "URGENT" else None
    )
    events = [
        row for row in after["outbox_events"] if row not in before["outbox_events"]
    ]
    assert sum(
        row["event_type"] == "DOCTOR_RESCHEDULED_APPOINTMENT" for row in events
    ) == (actor.role == "DOCTOR_OWNER")
    (receipt,) = [
        row
        for row in after["idempotency_receipts"]
        if row not in before["idempotency_receipts"]
    ]
    assert (
        receipt["idempotency_key"] == key
        and receipt["result_projection"] == reply["data"]
    )
    if original["service_date"] != saved["service_date"]:
        (assignment,) = [
            row
            for row in rows(after, "appointment_number_assignments", target)
            if row not in before["appointment_number_assignments"]
        ]
        assert assignment["prior_assignment_id"] == original["current_assignment_id"]
        assert (
            assignment["number"] == saved["current_number"]
            and assignment["service_date"] == saved["service_date"]
        )
        if original["status"] == "Waiting":
            assert saved["status"] == "Booked"
            assert all(
                row["disposition"] != "ACTIVE"
                for row in rows(after, "queue_entries", target)
            )
    else:
        assert saved["current_assignment_id"] == original["current_assignment_id"]
        assert (
            after["appointment_number_assignments"]
            == before["appointment_number_assignments"]
        )
        assert after["queue_entries"] == before["queue_entries"]
    assert move_request(clinic, actor, target, body, key=key) == (200, reply)
    assert effects(clinic) == after
    return after


def test_lost_response_receipt_recovery_is_read_only(intake_clinic):
    clinic = intake_clinic
    actor = clinic.receptionist
    target = book_case(clinic)
    body = move_body(clinic, target, _today() + timedelta(days=1))
    key = uuid4().hex
    status, reply = move_request(clinic, actor, target, body, key=key)
    assert status == 200, reply
    committed = effects(clinic)
    payload = json.dumps(body, separators=(",", ":")).encode()
    identity = replace(
        actor,
        method="POST",
        path=f"/v1/appointments/{target}/reschedule",
        purpose="APPOINTMENT_RESCHEDULE",
        query="",
        body_sha256=sha256(payload).hexdigest(),
        timestamp=str(int(datetime.now(UTC).timestamp() * 1000)),
        nonce=f"synthetic_{uuid4().hex}",
        idempotency_key=key,
    )
    idle = clinic.config.policy.session_idle_minutes
    with registered_test_request(clinic, identity, payload):
        with (
            pytest.raises(RuntimeError, match="synthetic rollback"),
            protected_transaction(clinic.runtime, identity, idle),
        ):
            raise RuntimeError("synthetic rollback")
        with protected_transaction(
            clinic.runtime, identity, idle, stage="RECEIPT_RECOVERY"
        ) as (connection, authorized):
            assert connection.scalar(text("SELECT session_user")) == "carepulse_api"
            with pytest.raises(DBAPIError) as error, connection.begin_nested():
                connection.execute(text("SELECT public.f01_b18_apply()"))
            assert error.value.orig.sqlstate == "42501"
            assert recover_committed_reschedule(
                connection,
                authorized,
                UUID(target),
                RescheduleAppointment.model_validate(body),
                key,
            ) == (reply["data"], reply["revision"])
    assert effects(clinic) == committed


def test_same_day_booked_move_preserves_number_and_assignment(intake_clinic):
    clinic = intake_clinic
    target = book_case(clinic)
    body = move_body(clinic, target, _today())
    before = effects(clinic)
    key = uuid4().hex
    status, reply = move_request(clinic, clinic.receptionist, target, body, key=key)
    assert status == 200, reply
    assert_move(clinic, clinic.receptionist, target, body, before, reply, key)


def test_holiday_and_stale_schedule_reject_without_effects(intake_clinic):
    clinic = intake_clinic
    target = book_case(clinic)
    destination = _today() + timedelta(days=1)
    stale = move_body(clinic, target, destination)
    set_intake_schedule(clinic, holidays=(destination,))
    denied(clinic, clinic.owner, target, stale, 409)
    denied(clinic, clinic.owner, target, move_body(clinic, target, destination), 409)


def test_counted_patient_destination_conflict_rolls_back(intake_clinic):
    clinic = intake_clinic
    target = book_case(clinic)
    patient = _appointment(clinic, target)["patient_id"]
    destination = _today() + timedelta(days=1)
    status, reply = booking_request(
        clinic,
        clinic.owner,
        body=booking_body(
            patient,
            day=destination,
            expected_price_revision=_policy_revision(clinic, "PRICE"),
            expected_schedule_revision=_policy_revision(clinic, "SCHEDULE"),
        ),
        key=uuid4().hex,
    )
    assert status == 201, reply
    denied(clinic, clinic.owner, target, move_body(clinic, target, destination), 409)


def test_urgent_current_work_and_price_guards(intake_clinic):
    clinic = intake_clinic
    status, created = intake_request(
        clinic,
        clinic.receptionist,
        "urgent",
        intake_body(clinic, new_patient(clinic), "urgent"),
    )
    assert status == 201, created
    target = created["data"]["id"]

    def command():
        return move_body(
            clinic,
            target,
            _today(),
            physical_presence_confirmed=True,
            expected_price_revision=_policy_revision(clinic, "PRICE"),
        )

    outside = (
        (("13:00", "14:00"),)
        if datetime.now(CAIRO).hour < 12
        else (("00:00", "01:00"),)
    )
    for config in (
        {"periods": ()},
        {"periods": outside},
        {"holidays": (_today(),), "holiday_booking_enabled": True},
        {"breaks": (("00:00", "23:59"),)},
    ):
        stale = command()
        set_intake_schedule(clinic, **config)
        denied(clinic, clinic.owner, target, stale, 409)
        denied(clinic, clinic.receptionist, target, command(), 409)
    set_intake_schedule(clinic)
    stale = command()
    status, result = pricing_request(
        clinic,
        clinic.owner,
        method="PATCH",
        body=pricing_patch(
            expected_price_revision=_policy_revision(clinic, "PRICE"),
            urgent_enabled=False,
        ),
        key=uuid4().hex,
    )
    assert status == 200, result
    denied(clinic, clinic.owner, target, stale, 409)
    denied(clinic, clinic.receptionist, target, command(), 409)


def test_cancelled_appointment_cannot_be_rescheduled(intake_clinic):
    clinic = intake_clinic
    target = book_case(clinic)
    status, reply = change_request(
        clinic, clinic.owner, target, "cancel", cancel_body(clinic, target)
    )
    assert status == 200, reply
    denied(
        clinic,
        clinic.owner,
        target,
        move_body(clinic, target, _today() + timedelta(days=1)),
        409,
    )


@pytest.mark.parametrize("kind", ["routine", "urgent"])
def test_timed_waiting_routine_denies_and_urgent_replaces_own_interval(
    intake_clinic, kind
):
    clinic = intake_clinic
    # Use the real Cairo date shared by the API and PostgreSQL. If today's
    # remaining work period cannot contain both future slots, start tomorrow
    # instead; this is a calendar precondition, never a race synchronization.
    now = datetime.now(CAIRO)
    midnight = (now + timedelta(days=1)).replace(
        hour=0, minute=0, second=0, microsecond=0
    )
    remaining = (midnight - now).total_seconds()
    if remaining < 360:
        sleep(remaining + 1)
    set_intake_schedule(clinic, mode="EXACT_TIME", duration=1)
    status, slots = _slots_request(clinic, clinic.receptionist, _today())
    assert status == 200, slots
    earliest = datetime.now(UTC) + timedelta(minutes=3)
    candidates = [
        candidate
        for candidate in slots["data"]["candidates"]
        if candidate["available"]
        and datetime.fromisoformat(candidate["starts_at"]) > earliest
    ]
    assert len(candidates) >= 2, "Real Cairo day has fewer than two future slots"
    slot = candidates[0]
    interval = {name: slot[name] for name in ("starts_at", "ends_at")}
    patient = new_patient(clinic)
    status, created = intake_request(
        clinic,
        clinic.receptionist,
        kind,
        intake_body(clinic, patient, kind, **interval),
    )
    assert status == 201, created
    target = created["data"]["id"]
    body = move_body(clinic, target, _today(), **interval)
    if kind == "routine":
        body.update({name: candidates[1][name] for name in ("starts_at", "ends_at")})
        denied(clinic, clinic.owner, target, body, 409)
        return
    with _archive(clinic, UUID(str(patient))) as (_, execute):
        execute()
    body.update(
        physical_presence_confirmed=True,
        expected_price_revision=_policy_revision(clinic, "PRICE"),
    )
    before = effects(clinic)
    key = uuid4().hex
    status, reply = move_request(clinic, clinic.owner, target, body, key=key)
    assert status == 200, reply
    assert_move(clinic, clinic.owner, target, body, before, reply, key)


@pytest.mark.parametrize("waiting", [False, True])
def test_routine_cross_day_preserves_identity_and_frozen_evidence(
    intake_clinic, waiting
):
    clinic = intake_clinic
    target = book_case(clinic, waiting=waiting)
    if waiting:
        denied(clinic, clinic.owner, target, move_body(clinic, target, _today()), 409)
    with _archive(clinic, UUID(str(_appointment(clinic, target)["patient_id"]))) as (
        _,
        execute,
    ):
        execute()
    body = move_body(clinic, target, _today() + timedelta(days=1))
    before = effects(clinic)
    key = uuid4().hex
    status, reply = move_request(clinic, clinic.owner, target, body, key=key)
    assert status == 200, reply
    assert_move(clinic, clinic.owner, target, body, before, reply, key)
    denied(
        clinic,
        clinic.owner,
        target,
        {**body, "service_date": (_today() + timedelta(days=2)).isoformat()},
        409,
        key=key,
    )


@pytest.mark.parametrize("role", ["owner", "receptionist", "manager"])
def test_urgent_two_fresh_actor_attestations_and_replay(intake_clinic, role):
    clinic = intake_clinic
    patient = new_patient(clinic)
    status, created = intake_request(
        clinic, clinic.receptionist, "urgent", intake_body(clinic, patient, "urgent")
    )
    assert status == 201, created
    target = created["data"]["id"]
    with _archive(clinic, UUID(str(patient))) as (_, execute):
        execute()
    actor = getattr(clinic, role, None)
    if role == "manager":
        _owner_changes_manager(clinic, True, 0)
        actor = _fresh_receptionist_session(clinic, 1)
    for _ in range(2):
        body = move_body(
            clinic,
            target,
            _today(),
            expected_price_revision=_policy_revision(clinic, "PRICE"),
        )
        denied(clinic, actor, target, body, 409)
        denied(
            clinic, actor, target, {**body, "physical_presence_confirmed": False}, 422
        )
        denied(
            clinic,
            actor,
            target,
            {
                **body,
                "physical_presence_confirmed": True,
                "actor_staff_id": clinic.owner.staff_id,
            },
            422,
        )
        body["physical_presence_confirmed"] = True
        denied(
            clinic,
            actor,
            target,
            {**body, "service_date": (_today() + timedelta(days=1)).isoformat()},
            409,
        )
        before = effects(clinic)
        key = uuid4().hex
        status, reply = move_request(clinic, actor, target, body, key=key)
        assert status == 200, reply
        assert_move(clinic, actor, target, body, before, reply, key)


@pytest.mark.parametrize("overlap", [False, True])
def test_signed_destination_scope_and_third_party_occupancy(intake_clinic, overlap):
    clinic = intake_clinic
    target = book_case(clinic)
    other = book_case(clinic)
    day = _today() + timedelta(days=1)
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
    status, available = _slots_request(clinic, clinic.owner, day)
    assert status == 200, available
    slot = available["data"]["candidates"][0]
    interval = {name: slot[name] for name in ("starts_at", "ends_at")}
    if overlap:
        interval["expected_capacity_revision"] = _policy_revision(clinic, "CAPACITY")
    body = move_body(clinic, target, day, **interval)
    with signed_test_transaction(
        clinic,
        clinic.owner,
        method="POST",
        path=f"/v1/appointments/{target}/reschedule",
        purpose="APPOINTMENT_RESCHEDULE",
        body=body,
        key=uuid4().hex,
    ) as (connection, _):
        assert connection.scalar(text("SELECT count(*) FROM public.appointments")) == 0
        assert (
            connection.execute(
                text("SELECT * FROM public.f01_counted_appointment_intervals(:t,:d)"),
                {"t": clinic.tenant, "d": day},
            ).all()
            == []
        )
        for tenant, destination in (
            (clinic.tenant, day + timedelta(days=1)),
            (uuid4(), day),
        ):
            with pytest.raises(DBAPIError) as error, connection.begin_nested():
                connection.execute(
                    text(
                        "SELECT * FROM public.f01_counted_appointment_intervals(:t,:d)"
                    ),
                    {"t": tenant, "d": destination},
                )
            assert error.value.orig.sqlstate == "42501"
    # A nonexistent signed target grants no destination projection, even when
    # the tenant, actor, date and purpose otherwise match.
    with signed_test_transaction(
        clinic,
        clinic.owner,
        method="POST",
        path=f"/v1/appointments/{uuid4()}/reschedule",
        purpose="APPOINTMENT_RESCHEDULE",
        body=body,
        key=uuid4().hex,
    ) as (connection, _):
        with pytest.raises(DBAPIError) as error, connection.begin_nested():
            connection.execute(
                text("SELECT * FROM public.f01_counted_appointment_intervals(:t,:d)"),
                {"t": clinic.tenant, "d": day},
            )
        assert error.value.orig.sqlstate == "42501"
    with signed_test_transaction(
        clinic,
        clinic.owner,
        method="POST",
        path=f"/v1/appointments/{target}/cancel",
        purpose="APPOINTMENT_CANCEL",
        body=cancel_body(clinic, target),
        key=uuid4().hex,
    ) as (connection, _):
        with pytest.raises(DBAPIError) as error, connection.begin_nested():
            connection.execute(
                text("SELECT * FROM public.f01_counted_appointment_intervals(:t,:d)"),
                {"t": clinic.tenant, "d": day},
            )
        assert error.value.orig.sqlstate == "42501"
    status, reply = move_request(clinic, clinic.owner, target, body)
    assert status == 200, reply
    if overlap:
        occupied = booking_body(
            new_patient(clinic),
            day=day,
            expected_price_revision=_policy_revision(clinic, "PRICE"),
            expected_schedule_revision=_policy_revision(clinic, "SCHEDULE"),
            **interval,
        )
        status, reply = booking_request(
            clinic, clinic.owner, body=occupied, key=uuid4().hex
        )
        assert status == 201, reply
    same = move_body(clinic, target, day, **interval)
    status, reply = move_request(clinic, clinic.owner, target, same)
    assert status == 200, reply
    denied(clinic, clinic.owner, other, move_body(clinic, other, day, **interval), 409)

    before = effects(clinic)
    with (
        pytest.raises(DBAPIError) as error,
        signed_test_transaction(
            clinic,
            clinic.owner,
            method="POST",
            path=f"/v1/appointments/{other}/reschedule",
            purpose="APPOINTMENT_RESCHEDULE",
            body=move_body(clinic, other, day, **interval),
            key=uuid4().hex,
        ) as (connection, _),
    ):
        connection.execute(text("SELECT public.f01_b18_prepare()"))
        connection.execute(text("SELECT public.f01_b18_apply()"))
    assert error.value.orig.sqlstate == "23514"
    assert effects(clinic) == before


def test_confirmed_extension_preserves_frozen_claim_and_doctor_precedence(
    intake_clinic,
):
    clinic = intake_clinic
    source = same_day_completed_source(clinic)
    status, booked = booking_request(
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
    assert status == 201, booked
    target = booked["data"]["id"]
    frozen = claim_record(clinic, target)
    destination = frozen["approved_deadline"] + timedelta(days=1)
    denied(
        clinic, clinic.receptionist, target, move_body(clinic, target, destination), 409
    )
    command = _extension(
        source,
        target,
        deadline=destination,
        followup_revision=_policy_revision(clinic, "FOLLOWUP"),
    )
    status, extension = _signed_visit_request(
        clinic,
        clinic.receptionist,
        path=f"/v1/visits/{source['visit']}/extend-followup",
        method="POST",
        body=command.model_dump(mode="json", exclude_unset=True),
        key=uuid4().hex,
    )
    assert status == 201, extension
    with clinic.migration.connect() as connection:
        extension_id = connection.scalar(
            text("""
            SELECT e.id FROM public.followup_authorization_events e
            WHERE e.organization_id=:tenant AND e.appointment_id=:target AND e.kind='EXTENSION'
        """),
            {"tenant": clinic.tenant, "target": target},
        )
    body = move_body(clinic, target, destination, extension_event_id=str(extension_id))
    before = effects(clinic)
    key = uuid4().hex
    status, reply = move_request(clinic, clinic.receptionist, target, body, key=key)
    assert status == 200, reply
    assert_move(clinic, clinic.receptionist, target, body, before, reply, key)
    assert claim_record(clinic, target) == frozen
    doctor_decision(
        clinic,
        source["visit"],
        {"kind": "OPTED_OUT", "reason": "Synthetic Doctor restriction"},
    )
    denied(
        clinic,
        clinic.receptionist,
        target,
        move_body(clinic, target, destination, extension_event_id=str(extension_id)),
        409,
    )


def test_new_date_authority_is_not_a_confirmed_extension(intake_clinic):
    clinic = intake_clinic
    source = same_day_completed_source(clinic)
    booking = followup_body(
        source,
        offset=8,
        expected_price_revision=_policy_revision(clinic, "PRICE"),
        expected_schedule_revision=_policy_revision(clinic, "SCHEDULE"),
        expected_followup_revision=_policy_revision(clinic, "FOLLOWUP"),
    )
    status, reply = booking_request(
        clinic,
        clinic.receptionist,
        body={
            **booking,
            "new_booking_date_exception": {
                "selected_service_date": booking["service_date"]
            },
        },
        key=uuid4().hex,
    )
    assert status == 201, reply
    target = reply["data"]["id"]
    frozen = claim_record(clinic, target)
    event_id = frozen["policy_snapshot"]["new_date_event_id"]
    day = source["day"] + timedelta(days=8)
    denied(
        clinic,
        clinic.receptionist,
        target,
        move_body(clinic, target, day, extension_event_id=event_id),
        409,
    )
    denied(clinic, clinic.receptionist, target, move_body(clinic, target, day), 409)
    assert claim_record(clinic, target) == frozen

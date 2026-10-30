"""B-09 transaction ordering with independent PostgreSQL connections."""

import json
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier, Event
from time import monotonic
from uuid import UUID, uuid4

import pytest
from carepulse.appointments.book import book_appointment
from carepulse.appointments.booking_contracts import BookAppointment
from carepulse.db import transaction
from carepulse.identity.contracts import ContractError
from carepulse.policies.schedule_service import load_current_schedule
from carepulse.policies.slots import candidates_for_date
from carepulse.policies.versions import append_version
from sqlalchemy import text
from test_booking import booking_body, booking_effects, new_patient
from test_booking import booking_clinic as booking_clinic  # noqa: PLC0414
from test_patients import patient_backend as patient_backend  # noqa: PLC0414
from test_request_boundary import boundary_data as boundary_data  # noqa: PLC0414
from test_schedule import SERVICE_DAY, _activate, _actor, _config, _publish, _stage


def _attempt(clinic, patient, key=None, **changes):
    command = BookAppointment.model_validate(booking_body(patient, **changes))
    actor = _actor(clinic.tenant, UUID(clinic.owner.staff_id))
    with transaction(clinic.runtime, clinic.tenant) as connection:
        return book_appointment(connection, actor, command, key or str(uuid4()))


def _concurrent(*actions):
    barrier = Barrier(len(actions))

    def run(action):
        barrier.wait(timeout=15)
        try:
            return action()
        except ContractError as denied:
            return denied

    with ThreadPoolExecutor(max_workers=len(actions)) as pool:
        futures = [pool.submit(run, action) for action in actions]
        return [future.result(timeout=35) for future in futures]


def _wait_for_postgres_lock(observer, waiter_pid, holder_pid):
    deadline = monotonic() + 15
    while monotonic() < deadline:
        observer.execute(text("SELECT pg_stat_clear_snapshot()"))
        waiting = observer.scalar(
            text("""
            SELECT count(*) FROM pg_stat_activity
            WHERE pid = :waiter AND wait_event_type = 'Lock'
              AND :holder = ANY(pg_blocking_pids(pid))
        """),
            {"waiter": waiter_pid, "holder": holder_pid},
        )
        if waiting:
            return
    pytest.fail("Expected competing PostgreSQL connection to wait on a row lock")


def test_same_patient_and_same_key_races_have_one_committed_appointment(booking_clinic):
    clinic = booking_clinic
    patient = new_patient(clinic)
    key = str(uuid4())
    outcomes = _concurrent(
        lambda: _attempt(clinic, patient, key),
        lambda: _attempt(clinic, patient, key),
    )
    assert all(isinstance(outcome, dict) for outcome in outcomes), outcomes
    assert outcomes[0] == outcomes[1]
    assert booking_effects(clinic) == (1, 1, 1, 1, 1, 1, 1, 1)


def test_distinct_keys_same_patient_day_have_one_winner_with_no_number_gap(
    booking_clinic,
):
    clinic = booking_clinic
    patient = new_patient(clinic)
    outcomes = _concurrent(
        lambda: _attempt(clinic, patient), lambda: _attempt(clinic, patient)
    )
    winners = [outcome for outcome in outcomes if isinstance(outcome, dict)]
    denied = [outcome for outcome in outcomes if isinstance(outcome, ContractError)]
    assert len(winners) == len(denied) == 1
    assert denied[0].status_code == 409
    assert winners[0]["number"] == 1
    assert booking_effects(clinic) == (1, 1, 1, 1, 1, 1, 1, 1)


def test_distinct_patients_race_for_unique_sequential_day_numbers(booking_clinic):
    clinic = booking_clinic
    patients = [new_patient(clinic) for _ in range(2)]
    outcomes = _concurrent(
        *[lambda patient=patient: _attempt(clinic, patient) for patient in patients]
    )
    assert all(isinstance(outcome, dict) for outcome in outcomes), outcomes
    assert {outcome["number"] for outcome in outcomes} == {1, 2}
    assert {outcome["patient_id"] for outcome in outcomes} == set(map(str, patients))
    assert booking_effects(clinic) == (2, 1, 2, 2, 2, 2, 2, 2)
    with transaction(clinic.runtime, clinic.tenant) as connection:
        assert (
            connection.scalar(
                text("SELECT last_number FROM public.appointment_day_counters")
            )
            == 2
        )
        assert set(
            connection.scalars(
                text("SELECT number FROM public.appointment_number_assignments")
            )
        ) == {1, 2}


@pytest.mark.parametrize("archive_first", [True, False])
def test_archive_and_booking_serialize_for_both_commit_orders(
    booking_clinic,
    archive_first,
):
    clinic = booking_clinic
    patient = new_patient(clinic)
    ready = Event()
    waiter = []

    def compete():
        with transaction(clinic.runtime, clinic.tenant) as connection:
            waiter.append(connection.scalar(text("SELECT pg_backend_pid()")))
            ready.set()
            if archive_first:
                return book_appointment(
                    connection,
                    _actor(clinic.tenant, UUID(clinic.owner.staff_id)),
                    BookAppointment.model_validate(booking_body(patient)),
                    str(uuid4()),
                )
            connection.execute(
                text("""
                UPDATE public.patients SET archived=true,
                    lifecycle_revision=lifecycle_revision+1 WHERE id=:id
            """),
                {"id": patient},
            )

    with ThreadPoolExecutor(max_workers=1) as pool:
        with transaction(clinic.runtime, clinic.tenant) as holder:
            holder_pid = holder.scalar(text("SELECT pg_backend_pid()"))
            if archive_first:
                holder.execute(
                    text("""
                    UPDATE public.patients SET archived=true,
                        lifecycle_revision=lifecycle_revision+1 WHERE id=:id
                """),
                    {"id": patient},
                )
            else:
                saved = book_appointment(
                    holder,
                    _actor(clinic.tenant, UUID(clinic.owner.staff_id)),
                    BookAppointment.model_validate(booking_body(patient)),
                    str(uuid4()),
                )
            future = pool.submit(compete)
            assert ready.wait(timeout=15)
            with transaction(clinic.runtime, clinic.tenant) as observer:
                _wait_for_postgres_lock(observer, waiter[0], holder_pid)
        if archive_first:
            with pytest.raises(ContractError) as denied:
                future.result(timeout=30)
            assert denied.value.status_code == 409
            assert booking_effects(clinic) == (0, 0, 0, 0, 0, 0, 0, 0)
        else:
            future.result(timeout=30)
            assert saved["number"] == 1
            assert booking_effects(clinic) == (1, 1, 1, 1, 1, 1, 1, 1)
    with transaction(clinic.runtime, clinic.tenant) as connection:
        assert (
            connection.scalar(
                text("SELECT archived FROM public.patients WHERE id=:id"),
                {"id": patient},
            )
            is True
        )


def _reprice(connection, clinic):
    raw = connection.scalar(
        text("""
        SELECT v.payload::text FROM public.clinic_policy_heads h
        JOIN public.clinic_policy_versions v
          ON (h.organization_id,h.kind,h.current_version)
           = (v.organization_id,v.kind,v.version)
        WHERE h.kind='PRICE'
    """)
    )
    payload = json.loads(raw)
    payload["standard_new_exam_egp"] = "150.00"
    append_version(connection, clinic.tenant, "PRICE", payload, 2)


@pytest.mark.parametrize("price_first", [True, False])
def test_price_policy_and_booking_freeze_only_confirmed_revision(
    booking_clinic,
    price_first,
):
    clinic = booking_clinic
    patient = new_patient(clinic)
    ready = Event()
    waiter = []

    def compete():
        with transaction(clinic.runtime, clinic.tenant) as connection:
            waiter.append(connection.scalar(text("SELECT pg_backend_pid()")))
            ready.set()
            if price_first:
                return book_appointment(
                    connection,
                    _actor(clinic.tenant, UUID(clinic.owner.staff_id)),
                    BookAppointment.model_validate(booking_body(patient)),
                    str(uuid4()),
                )
            _reprice(connection, clinic)

    with ThreadPoolExecutor(max_workers=1) as pool:
        with transaction(clinic.runtime, clinic.tenant) as holder:
            holder_pid = holder.scalar(text("SELECT pg_backend_pid()"))
            if price_first:
                _reprice(holder, clinic)
            else:
                booked = book_appointment(
                    holder,
                    _actor(clinic.tenant, UUID(clinic.owner.staff_id)),
                    BookAppointment.model_validate(booking_body(patient)),
                    str(uuid4()),
                )
            future = pool.submit(compete)
            assert ready.wait(timeout=15)
            with transaction(clinic.runtime, clinic.tenant) as observer:
                _wait_for_postgres_lock(observer, waiter[0], holder_pid)
        if price_first:
            with pytest.raises(ContractError) as denied:
                future.result(timeout=30)
            assert denied.value.status_code == 409
            assert booking_effects(clinic) == (0, 0, 0, 0, 0, 0, 0, 0)
        else:
            future.result(timeout=30)
            assert booked["net_egp"] == "100.00"
            assert booking_effects(clinic) == (1, 1, 1, 1, 1, 1, 1, 1)


@pytest.mark.parametrize("activation_first", [True, False])
def test_schedule_activation_and_booking_serialize_on_first_date(
    booking_clinic,
    activation_first,
):
    clinic = booking_clinic
    patient = new_patient(clinic)
    actor = _actor(clinic.tenant, UUID(clinic.owner.staff_id))
    with transaction(clinic.runtime, clinic.tenant) as connection:
        preview = _stage(
            connection, actor, _config(mode="OPEN", working_weekdays=[]), revision=2
        )
    ready = Event()
    waiter = []

    def compete():
        with transaction(clinic.runtime, clinic.tenant) as connection:
            waiter.append(connection.scalar(text("SELECT pg_backend_pid()")))
            ready.set()
            if activation_first:
                return book_appointment(
                    connection,
                    actor,
                    BookAppointment.model_validate(booking_body(patient)),
                    str(uuid4()),
                )
            return _activate(connection, actor, preview)

    with ThreadPoolExecutor(max_workers=1) as pool:
        with transaction(clinic.runtime, clinic.tenant) as holder:
            holder_pid = holder.scalar(text("SELECT pg_backend_pid()"))
            if activation_first:
                _activate(holder, actor, preview)
            else:
                booked = book_appointment(
                    holder,
                    actor,
                    BookAppointment.model_validate(booking_body(patient)),
                    str(uuid4()),
                )
            future = pool.submit(compete)
            assert ready.wait(timeout=15)
            with transaction(clinic.runtime, clinic.tenant) as observer:
                _wait_for_postgres_lock(observer, waiter[0], holder_pid)
        with pytest.raises(ContractError) as denied:
            future.result(timeout=30)
        assert denied.value.status_code == 409
    assert booking_effects(clinic) == (
        (0, 0, 0, 0, 0, 0, 0, 0) if activation_first else (1, 1, 1, 1, 1, 1, 1, 1)
    )
    if not activation_first:
        assert booked["number"] == 1


def test_two_timed_confirmations_for_same_candidate_cannot_overbook(booking_clinic):
    clinic = booking_clinic
    with transaction(clinic.runtime, clinic.tenant) as connection:
        _publish(
            connection,
            _actor(clinic.tenant, UUID(clinic.owner.staff_id)),
            _config(mode="EXACT_TIME"),
            revision=2,
        )
        slot = candidates_for_date(
            load_current_schedule(connection, clinic.tenant), SERVICE_DAY
        )[0]
    patients = [new_patient(clinic) for _ in range(2)]
    endpoints = {
        "expected_schedule_revision": 3,
        "starts_at": slot.starts_at.isoformat(),
        "ends_at": slot.ends_at.isoformat(),
    }
    outcomes = _concurrent(
        *[
            lambda patient=patient: _attempt(clinic, patient, **endpoints)
            for patient in patients
        ]
    )
    winners = [outcome for outcome in outcomes if isinstance(outcome, dict)]
    denied = [outcome for outcome in outcomes if isinstance(outcome, ContractError)]
    assert len(winners) == len(denied) == 1
    assert denied[0].status_code == 409
    assert booking_effects(clinic) == (1, 1, 1, 1, 1, 1, 1, 1)

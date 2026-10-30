"""B-08 serialized timed bookings on the real disposable PostgreSQL schema."""

from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, time
from threading import Barrier
from uuid import uuid4
from zoneinfo import ZoneInfo

import pytest
from carepulse.appointments.persistence import allocate_day_number
from carepulse.db import transaction
from carepulse.identity.contracts import ContractError
from carepulse.patients.lifecycle import lock_patient_for_new_appointment
from carepulse.policies.intervals import Interval
from carepulse.policies.schedule_service import load_current_schedule
from carepulse.policies.slots import candidates_for_date
from carepulse.policies.slots_service import list_slots, validate_timed_booking
from carepulse.policies.versions import (
    append_version,
    lock_policy_heads,
    lock_schedule_barrier,
)
from sqlalchemy import text
from test_appointment_persistence import (
    _patient,
    _raw_appointment,
    _raw_snapshot,
)
from test_appointment_persistence import appointment_storage as appointment_storage  # noqa: PLC0414
from test_schedule import (
    SERVICE_DAY,
    _activate,
    _actor,
    _config,
    _publish,
    _stage,
)

CAIRO = ZoneInfo("Africa/Cairo")


def _publish_slots(runtime, tenant, doctor, *, overlap=False, config=None):
    owner = _actor(tenant, doctor)
    if overlap:
        with transaction(runtime, tenant) as connection:
            append_version(
                connection, tenant, "CAPACITY", {"capacity": 2, "paused": False}, 1
            )
    with transaction(runtime, tenant) as connection:
        proposal = config or _config(allow_overlap=overlap)
        if overlap:
            preview = _stage(
                connection,
                owner,
                proposal,
                confirmation={"capacity": 2, "capacity_revision": 2},
            )
            _activate(connection, owner, preview)
        else:
            _publish(connection, owner, proposal)


def _confirmed_insert(
    connection, tenant, patient, doctor, slot, number, status="Booked"
):
    assignment = uuid4()
    appointment = _raw_appointment(
        connection,
        tenant,
        patient,
        doctor,
        service_date=SERVICE_DAY,
        booking_mode="EXACT_TIME",
        status=status,
        starts_at=slot.starts_at,
        ends_at=slot.ends_at,
        current_assignment_id=assignment,
        current_number=number,
    )
    connection.execute(
        text("""
            INSERT INTO public.appointment_number_assignments
                (id, organization_id, appointment_id, service_date, number)
            VALUES (:assignment, :tenant, :appointment, :day, :number)
        """),
        {
            "assignment": assignment,
            "tenant": tenant,
            "appointment": appointment,
            "day": SERVICE_DAY,
            "number": number,
        },
    )
    _raw_snapshot(connection, tenant, appointment)
    connection.execute(
        text("""
            INSERT INTO public.appointment_history
                (id, organization_id, appointment_id, appointment_revision,
                 action, old_status, new_status, old_service_date, new_service_date,
                 old_number, new_number, actor_staff_id, actor_role,
                 price_snapshot_appointment_id)
            VALUES (:id, :tenant, :appointment, 0, 'CREATED', NULL, :status,
                    NULL, :day, NULL, :number, :doctor, 'DOCTOR_OWNER', :appointment)
        """),
        {
            "id": uuid4(),
            "tenant": tenant,
            "appointment": appointment,
            "day": SERVICE_DAY,
            "number": number,
            "doctor": doctor,
            "status": status,
        },
    )


def _compete(runtime, tenant, doctor, patients, bounds, *, revision=None):
    ready = Barrier(len(patients))

    def attempt(patient, desired):
        try:
            with transaction(runtime, tenant) as connection:
                lock_patient_for_new_appointment(connection, tenant, patient)
                lock_schedule_barrier(connection, tenant, shared=True)
                if revision is not None:
                    lock_policy_heads(connection, tenant, {"CAPACITY": "SHARE"})
                ready.wait(timeout=15)
                number = allocate_day_number(connection, tenant, SERVICE_DAY)
                validate_timed_booking(
                    connection,
                    tenant,
                    SERVICE_DAY,
                    desired.starts_at,
                    desired.ends_at,
                    datetime.now(UTC),
                    revision,
                )
                _confirmed_insert(connection, tenant, patient, doctor, desired, number)
            return True
        except ContractError as denied:
            assert denied.status_code == 409
            return False

    with ThreadPoolExecutor(max_workers=len(patients)) as executor:
        futures = [
            executor.submit(attempt, patient, bounds[index])
            for index, patient in enumerate(patients)
        ]
        return [future.result(timeout=30) for future in futures]


@pytest.mark.parametrize("adjacent,expected", [(False, 1), (True, 2)])
def test_day_lock_serializes_intersections_and_allows_adjacent_intervals(
    appointment_storage, adjacent, expected
):
    runtime, _, (tenant, _), (doctor, _) = appointment_storage
    _publish_slots(runtime, tenant, doctor)
    with transaction(runtime, tenant) as connection:
        patients = [_patient(connection, tenant) for _ in range(2)]
        slots = candidates_for_date(
            load_current_schedule(connection, tenant), SERVICE_DAY
        )
    attempts = _compete(
        runtime,
        tenant,
        doctor,
        patients,
        [slots[0], slots[1] if adjacent else slots[0]],
    )
    assert attempts.count(True) == expected
    with transaction(runtime, tenant) as connection:
        assert (
            connection.scalar(text("SELECT count(*) FROM public.appointments"))
            == expected
        )
        assert (
            connection.scalar(
                text(
                    "SELECT last_number FROM public.appointment_day_counters WHERE service_date=:day"
                ),
                {"day": SERVICE_DAY},
            )
            == expected
        )


def test_capacity_two_rejects_third_concurrent_interval(appointment_storage):
    runtime, _, (tenant, _), (doctor, _) = appointment_storage
    _publish_slots(runtime, tenant, doctor, overlap=True)
    with transaction(runtime, tenant) as connection:
        patients = [_patient(connection, tenant) for _ in range(3)]
        slot = candidates_for_date(
            load_current_schedule(connection, tenant), SERVICE_DAY
        )[0]
    attempts = _compete(runtime, tenant, doctor, patients, [slot] * 3, revision=2)
    assert attempts.count(True) == 2
    with transaction(runtime, tenant) as connection:
        assert connection.scalar(text("SELECT count(*) FROM public.appointments")) == 2


@pytest.mark.parametrize("overlap", [False, True])
def test_cairo_postgres_session_normalizes_confirmed_intervals_for_both_paths(
    appointment_storage, overlap
):
    runtime, _, (tenant, _), (doctor, _) = appointment_storage
    _publish_slots(runtime, tenant, doctor, overlap=overlap)
    with transaction(runtime, tenant) as connection:
        slots = candidates_for_date(
            load_current_schedule(connection, tenant), SERVICE_DAY
        )
        for _ in range(2 if overlap else 1):
            patient = _patient(connection, tenant)
            number = allocate_day_number(connection, tenant, SERVICE_DAY)
            _confirmed_insert(connection, tenant, patient, doctor, slots[0], number)

    with transaction(runtime, tenant) as connection:
        connection.execute(text("SET LOCAL TIME ZONE 'Africa/Cairo'"))
        assert connection.scalar(text("SHOW TIME ZONE")) == "Africa/Cairo"
        stored = connection.execute(
            text("SELECT starts_at, ends_at FROM public.appointments LIMIT 1")
        ).one()
        cairo_start = slots[0].starts_at.astimezone(CAIRO)
        cairo_end = slots[0].ends_at.astimezone(CAIRO)
        assert cairo_start.date() == cairo_end.date() == SERVICE_DAY
        assert stored.starts_at.utcoffset() == cairo_start.utcoffset()
        assert stored.ends_at.utcoffset() == cairo_end.utcoffset()
        assert stored.starts_at.astimezone(UTC) == slots[0].starts_at
        assert stored.ends_at.astimezone(UTC) == slots[0].ends_at

        projection = list_slots(connection, _actor(tenant, doctor), SERVICE_DAY)
        assert projection["candidates"][0]["available"] is False
        assert projection["candidates"][1]["available"] is True
        with pytest.raises(ContractError) as denied:
            validate_timed_booking(
                connection,
                tenant,
                SERVICE_DAY,
                slots[0].starts_at,
                slots[0].ends_at,
                datetime.now(UTC),
                2 if overlap else None,
            )
        assert denied.value.status_code == 409
        validate_timed_booking(
            connection,
            tenant,
            SERVICE_DAY,
            slots[1].starts_at,
            slots[1].ends_at,
            datetime.now(UTC),
            2 if overlap else None,
        )


@pytest.mark.parametrize(
    "status,counts",
    [
        ("Booked", True),
        ("Waiting", True),
        ("In Examination", True),
        ("Completed", True),
        ("Cancelled", False),
        ("No-show", False),
    ],
)
def test_only_four_counted_statuses_reserve_timed_capacity(
    appointment_storage, status, counts
):
    runtime, _, (tenant, _), (doctor, _) = appointment_storage
    _publish_slots(runtime, tenant, doctor)
    with transaction(runtime, tenant) as connection:
        patient = _patient(connection, tenant)
        slot = candidates_for_date(
            load_current_schedule(connection, tenant), SERVICE_DAY
        )[0]
        number = allocate_day_number(connection, tenant, SERVICE_DAY)
        _confirmed_insert(connection, tenant, patient, doctor, slot, number, status)
    with transaction(runtime, tenant) as connection:
        if counts:
            with pytest.raises(ContractError) as denied:
                validate_timed_booking(
                    connection,
                    tenant,
                    SERVICE_DAY,
                    slot.starts_at,
                    slot.ends_at,
                    datetime.now(UTC),
                    None,
                )
            assert denied.value.status_code == 409
        else:
            validate_timed_booking(
                connection,
                tenant,
                SERVICE_DAY,
                slot.starts_at,
                slot.ends_at,
                datetime.now(UTC),
                None,
            )


def test_peak_two_accepts_candidate_intersecting_two_nonoverlapping_reservations(
    appointment_storage,
):
    runtime, _, (tenant, _), (doctor, _) = appointment_storage
    _publish_slots(runtime, tenant, doctor, overlap=True)
    with transaction(runtime, tenant) as connection:
        slot = candidates_for_date(
            load_current_schedule(connection, tenant), SERVICE_DAY
        )[0]
        patients = [_patient(connection, tenant) for _ in range(2)]
        midway = slot.starts_at + (slot.ends_at - slot.starts_at) / 2
        intervals = (Interval(slot.starts_at, midway), Interval(midway, slot.ends_at))
        for patient, interval in zip(patients, intervals):
            number = allocate_day_number(connection, tenant, SERVICE_DAY)
            _confirmed_insert(
                connection,
                tenant,
                patient,
                doctor,
                interval,
                number,
            )
    with transaction(runtime, tenant) as connection:
        validate_timed_booking(
            connection,
            tenant,
            SERVICE_DAY,
            slot.starts_at,
            slot.ends_at,
            datetime.now(UTC),
            2,
        )


def test_stale_capacity_revision_rolls_back_number_allocation(appointment_storage):
    runtime, _, (tenant, _), (doctor, _) = appointment_storage
    _publish_slots(runtime, tenant, doctor, overlap=True)
    with transaction(runtime, tenant) as connection:
        slot = candidates_for_date(
            load_current_schedule(connection, tenant), SERVICE_DAY
        )[0]
    with (
        pytest.raises(ContractError) as stale,
        transaction(runtime, tenant) as connection,
    ):
        lock_schedule_barrier(connection, tenant, shared=True)
        lock_policy_heads(connection, tenant, {"CAPACITY": "SHARE"})
        allocate_day_number(connection, tenant, SERVICE_DAY)
        validate_timed_booking(
            connection,
            tenant,
            SERVICE_DAY,
            slot.starts_at,
            slot.ends_at,
            datetime.now(UTC),
            1,
        )
    assert stale.value.status_code == 409 and stale.value.code == "STALE_REVISION"
    with transaction(runtime, tenant) as connection:
        assert (
            connection.scalar(
                text("SELECT count(*) FROM public.appointment_day_counters")
            )
            == 0
        )


@pytest.mark.parametrize("override,valid", [(False, False), (True, True)])
def test_holiday_needs_explicit_override_period_for_timed_booking(
    appointment_storage, override, valid
):
    runtime, _, (tenant, _), (doctor, _) = appointment_storage
    proposal = _config(
        working_weekdays=[],
        weekly_work_periods=[],
        holiday_booking_enabled=True,
        holidays=[SERVICE_DAY.isoformat()],
        override_work_periods=[
            {
                "service_date": SERVICE_DAY.isoformat(),
                "starts_at": "09:00",
                "ends_at": "10:00",
            }
        ]
        if override
        else [],
    )
    _publish_slots(runtime, tenant, doctor, config=proposal)
    first = datetime.combine(SERVICE_DAY, time(9), CAIRO).astimezone(UTC)
    last = datetime.combine(SERVICE_DAY, time(9, 30), CAIRO).astimezone(UTC)
    with transaction(runtime, tenant) as connection:
        slots = candidates_for_date(
            load_current_schedule(connection, tenant), SERVICE_DAY
        )
        assert bool(slots) is valid
        if not valid:
            with pytest.raises(ContractError) as invalid:
                validate_timed_booking(
                    connection,
                    tenant,
                    SERVICE_DAY,
                    first,
                    last,
                    datetime.now(UTC),
                    None,
                )
            assert invalid.value.status_code == 409
        else:
            assert (slots[0].starts_at, slots[0].ends_at) == (first, last)
            validate_timed_booking(
                connection, tenant, SERVICE_DAY, first, last, datetime.now(UTC), None
            )

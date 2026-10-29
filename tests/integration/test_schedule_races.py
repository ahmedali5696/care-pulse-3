"""B-07 publication barrier races against real PostgreSQL booking transactions."""

from concurrent.futures import ThreadPoolExecutor
from threading import Event

import pytest
from carepulse.appointments.persistence import persist_appointment
from carepulse.db import transaction
from carepulse.identity.contracts import ContractError
from carepulse.policies.schedule import date_rules
from carepulse.policies.schedule_service import load_current_schedule
from carepulse.policies.versions import lock_schedule_barrier
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from test_appointment_persistence import _draft, _patient, _query
from test_appointment_persistence import appointment_storage as appointment_storage  # noqa: PLC0414
from test_schedule import SERVICE_DAY, _activate, _actor, _config, _publish, _stage


def _setup(runtime, tenant, owner):
    with transaction(runtime, tenant) as connection:
        _publish(connection, owner, _config(mode="OPEN"))
        patient = _patient(connection, tenant)
        closing = _config(mode="OPEN", working_weekdays=[])
        preview = _stage(connection, owner, closing, revision=2)
        assert preview["affected"] == []
        assert (
            connection.scalar(
                text("""
            SELECT count(*) FROM public.appointments WHERE service_date = :day
        """),
                {"day": SERVICE_DAY},
            )
            == 0
        )
    return patient, preview


def _read_final(runtime, tenant):
    with transaction(runtime, tenant) as connection:
        return (
            connection.scalar(
                text("""
                SELECT revision FROM public.clinic_policy_heads WHERE kind = 'SCHEDULE'
            """)
            ),
            date_rules(load_current_schedule(connection, tenant), SERVICE_DAY).eligible,
            connection.execute(
                text("""
                SELECT id, status, booking_mode, starts_at, ends_at
                FROM public.appointments WHERE service_date = :day
            """),
                {"day": SERVICE_DAY},
            ).all(),
            connection.scalar(
                text("""
                SELECT count(*) FROM public.clinic_policy_versions WHERE kind = 'SCHEDULE'
            """)
            ),
        )


def test_booking_first_on_date_absent_at_preview_stales_activation(appointment_storage):
    runtime, _, (tenant, _), (doctor, _) = appointment_storage
    owner = _actor(tenant, doctor)
    patient, preview = _setup(runtime, tenant, owner)
    booking_persisted = Event()
    release_booking = Event()

    def book():
        with transaction(runtime, tenant) as connection:
            lock_schedule_barrier(connection, tenant, shared=True)
            assert date_rules(
                load_current_schedule(connection, tenant), SERVICE_DAY
            ).eligible
            booked = persist_appointment(
                connection,
                _draft(patient, doctor, service_date=SERVICE_DAY),
                _query(tenant, doctor),
            )
            booking_persisted.set()
            assert release_booking.wait(timeout=15)
        return booked

    with ThreadPoolExecutor(max_workers=1) as pool:
        booking_future = pool.submit(book)
        try:
            assert booking_persisted.wait(timeout=15)
            with (
                pytest.raises(DBAPIError) as blocked,
                transaction(runtime, tenant) as connection,
            ):
                connection.execute(text("SET LOCAL lock_timeout = '1s'"))
                _activate(connection, owner, preview)
            assert blocked.value.orig.sqlstate == "55P03"
        finally:
            release_booking.set()
        booked = booking_future.result(timeout=30)
    with (
        pytest.raises(ContractError) as denied,
        transaction(runtime, tenant) as connection,
    ):
        _activate(connection, owner, preview)
    assert denied.value.status_code == 409 and denied.value.code == "STALE_REVISION"
    revision, eligible, appointments, versions = _read_final(runtime, tenant)
    assert revision == versions == 2 and eligible is True
    assert len(appointments) == 1
    assert str(appointments[0].id) == booked["id"]
    assert (appointments[0].status, appointments[0].booking_mode) == ("Booked", "OPEN")
    assert appointments[0].starts_at is appointments[0].ends_at is None


def test_activation_first_closes_absent_date_before_stale_booking_can_commit(
    appointment_storage,
):
    runtime, _, (tenant, _), (doctor, _) = appointment_storage
    owner = _actor(tenant, doctor)
    patient, preview = _setup(runtime, tenant, owner)
    activation_published = Event()
    booking_blocked = Event()

    def publish():
        with transaction(runtime, tenant) as connection:
            published = _activate(connection, owner, preview)
            activation_published.set()
            assert booking_blocked.wait(timeout=15)
        return published

    def book():
        assert activation_published.wait(timeout=15)
        try:
            with (
                pytest.raises(DBAPIError) as blocked,
                transaction(runtime, tenant) as connection,
            ):
                connection.execute(text("SET LOCAL lock_timeout = '1s'"))
                lock_schedule_barrier(connection, tenant, shared=True)
            assert blocked.value.orig.sqlstate == "55P03"
        finally:
            booking_blocked.set()
        with (
            pytest.raises(ContractError) as denied,
            transaction(runtime, tenant) as connection,
        ):
            lock_schedule_barrier(connection, tenant, shared=True)
            if not date_rules(
                load_current_schedule(connection, tenant), SERVICE_DAY
            ).eligible:
                raise ContractError(409)
            persist_appointment(
                connection,
                _draft(patient, doctor, service_date=SERVICE_DAY),
                _query(tenant, doctor),
            )
        return denied.value

    with ThreadPoolExecutor(max_workers=2) as pool:
        activated_future = pool.submit(publish)
        booked_future = pool.submit(book)
        published = activated_future.result(timeout=30)
        denied = booked_future.result(timeout=30)
    assert published["schedule_revision"] == 3
    assert denied.status_code == 409
    revision, eligible, appointments, versions = _read_final(runtime, tenant)
    assert revision == versions == 3 and eligible is False
    assert appointments == []
    with transaction(runtime, tenant) as connection:
        assert (
            connection.scalar(
                text("""
            SELECT count(*) FROM public.appointment_price_snapshots
        """)
            )
            == 0
        )
        assert (
            connection.scalar(
                text("""
            SELECT count(*) FROM public.appointment_history
        """)
            )
            == 0
        )

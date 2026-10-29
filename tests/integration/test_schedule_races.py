"""B-07 publication barrier races against real PostgreSQL booking transactions."""

from concurrent.futures import ThreadPoolExecutor
from threading import Event
from time import monotonic

import pytest
from carepulse.appointments.persistence import persist_appointment
from carepulse.db import transaction
from carepulse.identity.contracts import ContractError
from carepulse.policies.schedule import date_rules
from carepulse.policies.schedule_service import load_current_schedule
from carepulse.policies.versions import lock_schedule_barrier
from sqlalchemy import event, text
from sqlalchemy.exc import DBAPIError
from test_appointment_persistence import _draft, _patient, _query
from test_appointment_persistence import appointment_storage as appointment_storage  # noqa: PLC0414
from test_schedule import SERVICE_DAY, _activate, _actor, _config, _publish, _stage


def _blocked_query(observer, waiter_pid, holder_pid):
    deadline = monotonic() + 15
    while monotonic() < deadline:
        observer.execute(text("SELECT pg_stat_clear_snapshot()"))
        query = observer.scalar(
            text("""
                SELECT query FROM pg_stat_activity
                WHERE pid = :waiter AND wait_event_type = 'Lock'
                  AND :holder = ANY(pg_blocking_pids(pid))
            """),
            {"waiter": waiter_pid, "holder": holder_pid},
        )
        if query:
            return " ".join(query.split())
    pytest.fail("Expected PostgreSQL lock waiter was not observed")


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

    observed_sql = []

    def record_sql(_connection, _cursor, statement, _parameters, _context, _many):
        observed_sql.append(" ".join(statement.split()))

    with ThreadPoolExecutor(max_workers=1) as pool:
        booking_future = pool.submit(book)
        try:
            assert booking_persisted.wait(timeout=15)
            with (
                pytest.raises(DBAPIError) as blocked,
                transaction(runtime, tenant) as connection,
            ):
                connection.execute(text("SET LOCAL lock_timeout = '1s'"))
                event.listen(connection, "before_cursor_execute", record_sql)
                try:
                    _activate(connection, owner, preview)
                finally:
                    event.remove(connection, "before_cursor_execute", record_sql)
            assert blocked.value.orig.sqlstate == "55P03"
            assert any(
                sql.startswith("SELECT 1 FROM public.clinic_policy_heads ")
                and " FOR UPDATE" in sql
                for sql in observed_sql
            )
            assert not any("FROM public.appointments" in sql for sql in observed_sql)
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
    activation_started = Event()
    activation_pid = []

    def publish():
        with transaction(runtime, tenant) as connection:
            activation_pid.append(connection.scalar(text("SELECT pg_backend_pid()")))
            activation_started.set()
            return _activate(connection, owner, preview)

    with ThreadPoolExecutor(max_workers=1) as pool:
        with transaction(runtime, tenant) as capacity_gate:
            capacity_pid = capacity_gate.scalar(text("SELECT pg_backend_pid()"))
            assert (
                capacity_gate.scalar(
                    text("""
                    SELECT 1 FROM public.clinic_policy_heads
                    WHERE organization_id = :tenant AND kind = 'CAPACITY' FOR UPDATE
                """),
                    {"tenant": tenant},
                )
                == 1
            )
            activated_future = pool.submit(publish)
            assert activation_started.wait(timeout=15)
            with transaction(runtime, tenant) as observer:
                blocked_sql = _blocked_query(observer, activation_pid[0], capacity_pid)
            assert blocked_sql.startswith("SELECT 1 FROM public.clinic_policy_heads ")
            assert " FOR SHARE" in blocked_sql
            with (
                pytest.raises(DBAPIError) as blocked,
                transaction(runtime, tenant) as connection,
            ):
                connection.execute(text("SET LOCAL lock_timeout = '1s'"))
                lock_schedule_barrier(connection, tenant, shared=True)
            assert blocked.value.orig.sqlstate == "55P03"
        published = activated_future.result(timeout=30)
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
    assert published["schedule_revision"] == 3
    assert denied.value.status_code == 409
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

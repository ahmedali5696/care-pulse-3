"""B-07 publication barrier races against real PostgreSQL booking transactions."""

from concurrent.futures import ThreadPoolExecutor
from threading import Barrier, Event
from time import monotonic
from uuid import UUID, uuid4

import pytest
from carepulse.appointments.persistence import persist_appointment
from carepulse.db import transaction
from carepulse.identity.contracts import ContractError
from carepulse.identity.receipts import ReceiptConflict
from carepulse.main import app
from carepulse.policies.schedule import date_rules
from carepulse.policies.schedule_service import load_current_schedule
from carepulse.policies.versions import lock_schedule_barrier
from postgres_locks import wait_for_postgres_lock
from sqlalchemy import event, text
from sqlalchemy.exc import DBAPIError
from test_appointment_persistence import _draft, _patient, _query
from test_appointment_persistence import appointment_storage as appointment_storage  # noqa: PLC0414
from test_patients import patient_backend as patient_backend  # noqa: PLC0414
from test_request_boundary import boundary_data as boundary_data  # noqa: PLC0414
from test_schedule import (
    SERVICE_DAY,
    _activate,
    _actor,
    _config,
    _patch,
    _publish,
    _schedule_request,
    _stage,
)


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


@pytest.mark.parametrize("method,suffix", [("PATCH", ""), ("POST", "/preview")])
def test_signed_identical_concurrent_schedule_stage_reuses_one_preview(
    patient_backend, method, suffix
):
    clinic = patient_backend
    app.state.service_config, app.state.db_engine = clinic.config, clinic.runtime
    body = _patch(_config()).model_dump(mode="json")
    key = str(uuid4())
    barrier = Barrier(2)

    def send():
        barrier.wait(timeout=15)
        return _schedule_request(
            clinic, clinic.owner, method=method, suffix=suffix, body=body, key=key
        )

    with ThreadPoolExecutor(max_workers=2) as pool:
        first, second = [
            future.result(timeout=35)
            for future in (pool.submit(send), pool.submit(send))
        ]
    assert first == second
    assert first[0] == 200
    assert first[1]["data"]["revisions"]["active_schedule"] == 1
    assert first[1]["data"]["revisions"]["proposed_schedule"] == 2
    with transaction(clinic.runtime, clinic.tenant) as connection:
        assert (
            connection.scalar(
                text("SELECT count(*) FROM public.clinic_policy_previews WHERE id=:id"),
                {"id": first[1]["data"]["preview_id"]},
            )
            == 1
        )
        assert (
            connection.scalar(
                text(
                    "SELECT count(*) FROM public.idempotency_receipts "
                    "WHERE operation=:operation AND idempotency_key=:key"
                ),
                {
                    "operation": "settings.schedule.stage"
                    if method == "PATCH"
                    else "settings.schedule.preview",
                    "key": key,
                },
            )
            == 1
        )


def test_signed_identical_activation_waits_for_original_and_replays_receipt(
    patient_backend,
):
    clinic = patient_backend
    app.state.service_config, app.state.db_engine = clinic.config, clinic.runtime
    status, preview = _schedule_request(
        clinic,
        clinic.owner,
        method="POST",
        suffix="/preview",
        body=_patch(_config()).model_dump(mode="json"),
        key=str(uuid4()),
    )
    assert status == 200, preview
    body = {
        "preview_id": preview["data"]["preview_id"],
        "expected_schedule_revision": preview["data"]["revisions"]["schedule"],
        "expected_capacity_revision": preview["data"]["revisions"]["capacity"],
        "reason": "Synthetic reviewed schedule publication",
    }
    key = str(uuid4())
    receipt_inserted = Event()
    release_original = Event()
    signed_started = Event()
    original_pid = []
    waiting_pid = []

    def hold_original_receipt(
        connection, _cursor, statement, _parameters, _context, _many
    ):
        if (
            "INSERT INTO public.idempotency_receipts" in statement
            and not receipt_inserted.is_set()
        ):
            original_pid.append(
                connection.connection.driver_connection.info.backend_pid
            )
            receipt_inserted.set()
            assert release_original.wait(timeout=30)

    def observe_signed_wait(
        connection, _cursor, statement, _parameters, _context, _many
    ):
        if receipt_inserted.is_set() and "public.lock_active_organization" in statement:
            waiting_pid.append(connection.connection.driver_connection.info.backend_pid)
            signed_started.set()

    def send_activation():
        return _schedule_request(
            clinic,
            clinic.owner,
            method="POST",
            suffix="/activate",
            body=body,
            key=key,
        )

    event.listen(clinic.runtime, "after_cursor_execute", hold_original_receipt)
    event.listen(clinic.runtime, "before_cursor_execute", observe_signed_wait)
    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            original_future = pool.submit(send_activation)
            try:
                assert receipt_inserted.wait(timeout=15)
                replay_future = pool.submit(send_activation)
                assert signed_started.wait(timeout=15)
                with transaction(clinic.runtime, clinic.tenant) as observer:
                    wait_for_postgres_lock(observer, waiting_pid[0], original_pid[0])
            finally:
                release_original.set()
            original = original_future.result(timeout=30)
            replay = replay_future.result(timeout=30)
    finally:
        release_original.set()
        event.remove(clinic.runtime, "before_cursor_execute", observe_signed_wait)
        event.remove(clinic.runtime, "after_cursor_execute", hold_original_receipt)

    assert original == replay
    assert original[0] == 200 and original[1]["data"]["schedule_revision"] == 2
    assert original[1]["revision"] == 2
    with transaction(clinic.runtime, clinic.tenant) as connection:
        assert connection.execute(
            text(
                "SELECT current_version, revision FROM public.clinic_policy_heads "
                "WHERE organization_id=:tenant AND kind='SCHEDULE'"
            ),
            {"tenant": clinic.tenant},
        ).one() == (2, 2)
        assert (
            connection.scalar(
                text(
                    "SELECT count(*) FROM public.clinic_policy_versions "
                    "WHERE organization_id=:tenant AND kind='SCHEDULE'"
                ),
                {"tenant": clinic.tenant},
            )
            == 2
        )
        assert (
            connection.scalar(
                text(
                    "SELECT count(*) FROM public.sensitive_audit "
                    "WHERE organization_id=:tenant AND action='settings.schedule.activate'"
                ),
                {"tenant": clinic.tenant},
            )
            == 1
        )
        assert (
            connection.scalar(
                text(
                    "SELECT count(*) FROM public.idempotency_receipts "
                    "WHERE organization_id=:tenant AND operation='settings.schedule.activate' "
                    "AND idempotency_key=:key"
                ),
                {"tenant": clinic.tenant, "key": key},
            )
            == 1
        )


def test_direct_identical_activation_rechecks_receipt_after_schedule_lock(
    patient_backend,
):
    clinic = patient_backend
    actor = _actor(clinic.tenant, UUID(clinic.owner.staff_id))
    with transaction(clinic.runtime, clinic.tenant) as connection:
        preview = _stage(connection, actor, _config())
    key = str(uuid4())
    started = Event()
    waiting_pid = []

    def retry():
        with transaction(clinic.runtime, clinic.tenant) as connection:
            waiting_pid.append(connection.scalar(text("SELECT pg_backend_pid()")))
            started.set()
            return _activate(connection, actor, preview, key=key)

    with ThreadPoolExecutor(max_workers=1) as pool:
        with transaction(clinic.runtime, clinic.tenant) as holder:
            original = _activate(holder, actor, preview, key=key)
            holder_pid = holder.scalar(text("SELECT pg_backend_pid()"))
            future = pool.submit(retry)
            assert started.wait(timeout=15)
            with transaction(clinic.runtime, clinic.tenant) as observer:
                blocked_sql = _blocked_query(observer, waiting_pid[0], holder_pid)
            assert blocked_sql.startswith("SELECT 1 FROM public.clinic_policy_heads ")
            assert " FOR UPDATE" in blocked_sql
        replay = future.result(timeout=30)
    assert replay == original
    assert original["schedule_revision"] == 2
    with transaction(clinic.runtime, clinic.tenant) as connection:
        assert (
            connection.scalar(
                text(
                    "SELECT count(*) FROM public.clinic_policy_versions "
                    "WHERE organization_id=:tenant AND kind='SCHEDULE'"
                ),
                {"tenant": clinic.tenant},
            )
            == 2
        )
        assert (
            connection.scalar(
                text(
                    "SELECT count(*) FROM public.idempotency_receipts "
                    "WHERE organization_id=:tenant AND operation='settings.schedule.activate' "
                    "AND idempotency_key=:key"
                ),
                {"tenant": clinic.tenant, "key": key},
            )
            == 1
        )


@pytest.mark.parametrize(
    "method,suffix,operation",
    [
        ("PATCH", "", "settings.schedule.stage"),
        ("POST", "/preview", "settings.schedule.preview"),
    ],
)
def test_direct_schedule_stage_preview_receipt_insert_collision_recovers_signed(
    patient_backend, method, suffix, operation
):
    clinic = patient_backend
    app.state.service_config, app.state.db_engine = clinic.config, clinic.runtime
    actor = _actor(clinic.tenant, UUID(clinic.owner.staff_id))
    config = _config()
    body = _patch(config).model_dump(mode="json")
    key = str(uuid4())
    inserted = Event()
    release_winner = Event()
    loser_insert_started = Event()
    winner_pid = []
    loser_pid = []

    def hold_winner_insert(
        connection, _cursor, statement, _parameters, _context, _many
    ):
        if (
            "INSERT INTO public.idempotency_receipts" in statement
            and not inserted.is_set()
        ):
            winner_pid.append(connection.connection.driver_connection.info.backend_pid)
            inserted.set()
            assert release_winner.wait(timeout=30)

    def observe_loser_insert(
        connection, _cursor, statement, _parameters, _context, _many
    ):
        if "INSERT INTO public.idempotency_receipts" in statement and inserted.is_set():
            pid = connection.connection.driver_connection.info.backend_pid
            if pid != winner_pid[0]:
                loser_pid.append(pid)
                loser_insert_started.set()

    def stage():
        try:
            with transaction(clinic.runtime, clinic.tenant) as connection:
                projection = _stage(
                    connection, actor, config, key=key, operation=operation
                )
            return "written", projection
        except ReceiptConflict:
            # The competing transaction has rolled back; replay via live signed authority.
            status, result = _schedule_request(
                clinic,
                clinic.owner,
                method=method,
                suffix=suffix,
                body=body,
                key=key,
            )
            assert status == 200, result
            return "replayed", result["data"]

    event.listen(clinic.runtime, "after_cursor_execute", hold_winner_insert)
    event.listen(clinic.runtime, "before_cursor_execute", observe_loser_insert)
    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            winner_future = pool.submit(stage)
            try:
                assert inserted.wait(timeout=15)
                loser_future = pool.submit(stage)
                assert loser_insert_started.wait(timeout=15)
                with transaction(clinic.runtime, clinic.tenant) as observer:
                    wait_for_postgres_lock(observer, loser_pid[0], winner_pid[0])
            finally:
                release_winner.set()
            winner = winner_future.result(timeout=30)
            loser = loser_future.result(timeout=30)
    finally:
        release_winner.set()
        event.remove(clinic.runtime, "before_cursor_execute", observe_loser_insert)
        event.remove(clinic.runtime, "after_cursor_execute", hold_winner_insert)

    assert winner[0] == "written" and loser[0] == "replayed"
    assert loser[1] == winner[1]
    with transaction(clinic.runtime, clinic.tenant) as connection:
        assert (
            connection.scalar(
                text(
                    "SELECT count(*) FROM public.clinic_policy_previews "
                    "WHERE organization_id=:tenant AND id=:id"
                ),
                {"tenant": clinic.tenant, "id": winner[1]["preview_id"]},
            )
            == 1
        )
        assert (
            connection.scalar(
                text(
                    "SELECT count(*) FROM public.clinic_policy_previews "
                    "WHERE organization_id=:tenant"
                ),
                {"tenant": clinic.tenant},
            )
            == 1
        )
        assert (
            connection.scalar(
                text(
                    "SELECT count(*) FROM public.idempotency_receipts "
                    "WHERE organization_id=:tenant AND operation=:operation "
                    "AND idempotency_key=:key"
                ),
                {"tenant": clinic.tenant, "operation": operation, "key": key},
            )
            == 1
        )


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

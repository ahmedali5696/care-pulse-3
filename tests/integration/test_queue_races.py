"""D-06 real-connection dated queue lock and Waiting-entry race simulations.

These tests use a disposable PostgreSQL database; they do not create Visits.
"""

from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from threading import Barrier, Event
from uuid import UUID, uuid4

import pytest
from carepulse.db import transaction
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from test_appointment_persistence import appointment_storage as appointment_storage  # noqa: PLC0414
from test_queue_capacity import (
    _consume,
    _doctor_context,
    _issue,
    _lock,
    _policy_capacity,
    _simulated_exam,
)
from test_queue_persistence import DAY, _appointment, _arrive


def _transition(connection, tenant, doctor, appointment, target):
    """Test-only CAS/historical evidence for one eligible Booked record."""
    appointment_id = UUID(appointment["id"])
    changed = connection.execute(
        text("""
            UPDATE public.appointments
            SET status=:target, revision=revision+1
            WHERE organization_id=:tenant AND id=:id
              AND status='Booked' AND revision=0
        """),
        {"tenant": tenant, "id": appointment_id, "target": target},
    )
    if changed.rowcount != 1:
        return False
    connection.execute(
        text("""
            INSERT INTO public.appointment_history
                (id, organization_id, appointment_id, appointment_revision,
                 action, old_status, new_status, old_service_date, new_service_date,
                 old_number, new_number, actor_staff_id, actor_role, reason,
                 price_snapshot_appointment_id)
            SELECT :history, a.organization_id, a.id, a.revision, 'STATUS',
                   'Booked', :target, a.service_date, a.service_date,
                   a.current_number, a.current_number, :doctor, 'DOCTOR_OWNER',
                   :reason, a.id
            FROM public.appointments a
            WHERE a.organization_id=:tenant AND a.id=:id
        """),
        {
            "history": uuid4(),
            "tenant": tenant,
            "id": appointment_id,
            "doctor": doctor,
            "target": target,
            "reason": "Synthetic cancellation" if target == "Cancelled" else None,
        },
    )
    if target == "Waiting":
        _arrive(connection, tenant, doctor, appointment)
    return True


def test_shared_doctor_control_blocks_alternate_lane_and_original_date(
    appointment_storage,
):
    runtime, _, (tenant, _), (doctor, _) = appointment_storage
    tomorrow = DAY + timedelta(days=1)
    with transaction(runtime, tenant) as connection:
        _arrive(connection, tenant, doctor, _appointment(connection, tenant, doctor))
        _arrive(
            connection,
            tenant,
            doctor,
            _appointment(
                connection,
                tenant,
                doctor,
                service_date=tomorrow,
                classification="EMERGENCY",
                lane="EMERGENCY",
            ),
        )

    held = Event()
    release = Event()

    def holder():
        with transaction(runtime, tenant) as connection:
            result = _lock(connection, tenant, doctor, DAY, "MAIN")
            held.set()
            assert release.wait(timeout=10), "Doctor lock holder timed out"
            return result.configured_capacity

    def alternate_lane():
        with transaction(runtime, tenant) as connection:
            connection.execute(text("SET LOCAL lock_timeout = '250ms'"))
            return _lock(connection, tenant, doctor, tomorrow, "EMERGENCY")

    with ThreadPoolExecutor(max_workers=2) as executor:
        first = executor.submit(holder)
        try:
            assert held.wait(timeout=10), "Doctor lock was not acquired"
            second = executor.submit(alternate_lane)
            result = second.exception(timeout=10)
            assert isinstance(result, DBAPIError)
            assert result.orig.sqlstate == "55P03"
        finally:
            release.set()
        assert first.result(timeout=10) == 1

    with transaction(runtime, tenant) as connection:
        assert (
            _lock(connection, tenant, doctor, tomorrow, "EMERGENCY").configured_capacity
            == 1
        )


def test_parallel_waiting_checkin_and_cancel_one_wins_without_ghost(
    appointment_storage,
):
    runtime, _, (tenant, _), (doctor, _) = appointment_storage
    with transaction(runtime, tenant) as connection:
        saved = _appointment(connection, tenant, doctor, status="Booked")
    barrier = Barrier(2)

    def change(target):
        with transaction(runtime, tenant) as connection:
            barrier.wait(timeout=10)
            return (
                target
                if _transition(connection, tenant, doctor, saved, target)
                else None
            )

    with ThreadPoolExecutor(max_workers=2) as executor:
        future_waiting = executor.submit(change, "Waiting")
        future_cancel = executor.submit(change, "Cancelled")
        outcomes = [future_waiting.result(timeout=15), future_cancel.result(timeout=15)]

    assert len([result for result in outcomes if result]) == 1
    with transaction(runtime, tenant) as connection:
        appointment_id = UUID(saved["id"])
        status = connection.scalar(
            text("SELECT status FROM public.appointments WHERE id=:id"),
            {"id": appointment_id},
        )
        assert status in ("Waiting", "Cancelled")
        assert status in outcomes
        assert connection.scalar(
            text("""
                SELECT count(*) FROM public.queue_entries
                WHERE organization_id=:tenant AND appointment_id=:id
                  AND disposition='ACTIVE'
            """),
            {"tenant": tenant, "id": appointment_id},
        ) == int(status == "Waiting")
        assert (
            connection.scalar(
                text("""
                SELECT count(*) FROM public.appointment_history
                WHERE organization_id=:tenant AND appointment_id=:id
            """),
                {"tenant": tenant, "id": appointment_id},
            )
            == 2
        )


def test_queue_day_upsert_race_does_not_duplicate_partition_or_arrival(
    appointment_storage,
):
    runtime, _, (tenant, _), (doctor, _) = appointment_storage
    with transaction(runtime, tenant) as connection:
        first = _appointment(connection, tenant, doctor, status="Booked")
        second = _appointment(connection, tenant, doctor, status="Booked")
    held = Event()
    release = Event()

    def first_checkin():
        with transaction(runtime, tenant) as connection:
            assert _transition(connection, tenant, doctor, first, "Waiting")
            held.set()
            assert release.wait(timeout=10), "QueueDay holder timed out"
        return "first"

    def competing_checkin():
        with transaction(runtime, tenant) as connection:
            connection.execute(text("SET LOCAL lock_timeout = '250ms'"))
            assert _transition(connection, tenant, doctor, second, "Waiting")

    with ThreadPoolExecutor(max_workers=2) as executor:
        holder = executor.submit(first_checkin)
        try:
            assert held.wait(timeout=10), "First QueueDay was not created"
            waiter = executor.submit(competing_checkin)
            denied = waiter.exception(timeout=10)
            assert isinstance(denied, DBAPIError)
            assert denied.orig.sqlstate == "55P03"
        finally:
            release.set()
        assert holder.result(timeout=10) == "first"

    with transaction(runtime, tenant) as connection:
        assert _transition(connection, tenant, doctor, second, "Waiting")
        assert connection.execute(
            text("""
                SELECT q.arrival_counter, count(e.id),
                       min(e.arrival_sequence), max(e.arrival_sequence)
                FROM public.queue_days q
                JOIN public.queue_entries e ON e.organization_id=q.organization_id
                    AND e.queue_day_id=q.id
                WHERE q.organization_id=:tenant AND q.service_date=:day
                    AND q.lane='MAIN'
                GROUP BY q.arrival_counter
            """),
            {"tenant": tenant, "day": DAY},
        ).one() == (2, 2, 1, 2)


def test_cross_date_cross_lane_provisional_admission_serializes_then_rolls_back(
    appointment_storage,
):
    runtime, _, (tenant, _), (doctor, _) = appointment_storage
    tomorrow = DAY + timedelta(days=1)
    with transaction(runtime, tenant) as connection:
        main = _appointment(connection, tenant, doctor)
        _arrive(connection, tenant, doctor, main)
        emergency = _appointment(
            connection,
            tenant,
            doctor,
            service_date=tomorrow,
            classification="EMERGENCY",
            lane="EMERGENCY",
        )
        _arrive(connection, tenant, doctor, emergency)

    held = Event()
    release = Event()

    def provisional_main():
        with (
            pytest.raises(RuntimeError, match="rollback simulation"),
            transaction(runtime, tenant) as connection,
        ):
            control = _lock(connection, tenant, doctor, DAY, "MAIN")
            assert control.configured_capacity == 1
            assert control.active_examinations == 0
            _simulated_exam(connection, tenant, doctor, main)
            assert _lock(connection, tenant, doctor).active_examinations == 1
            held.set()
            assert release.wait(timeout=10), "Admission lock holder timed out"
            raise RuntimeError("rollback simulation")
        return "rolled back"

    def competing_emergency():
        with transaction(runtime, tenant) as connection:
            connection.execute(text("SET LOCAL lock_timeout = '250ms'"))
            return _lock(connection, tenant, doctor, tomorrow, "EMERGENCY")

    with ThreadPoolExecutor(max_workers=2) as executor:
        holder = executor.submit(provisional_main)
        try:
            assert held.wait(timeout=10), "Provisional admission did not lock"
            competitor = executor.submit(competing_emergency)
            denied = competitor.exception(timeout=10)
            assert isinstance(denied, DBAPIError)
            assert denied.orig.sqlstate == "55P03"
        finally:
            release.set()
        assert holder.result(timeout=10) == "rolled back"

    # The other dated lane must re-read capacity after the first rollback.
    # Its own positive state/history SQL is also deliberately never committed.
    with (
        pytest.raises(RuntimeError, match="rollback simulation"),
        transaction(runtime, tenant) as connection,
    ):
        control = _lock(connection, tenant, doctor, tomorrow, "EMERGENCY")
        assert control.active_examinations == 0
        assert control.configured_capacity == 1
        _simulated_exam(connection, tenant, doctor, emergency)
        assert (
            _lock(connection, tenant, doctor, tomorrow, "EMERGENCY").active_examinations
            == 1
        )
        raise RuntimeError("rollback simulation")

    with transaction(runtime, tenant) as connection:
        assert connection.execute(
            text("""
                SELECT id, status FROM public.appointments
                WHERE organization_id=:tenant AND id IN (:main, :emergency)
                ORDER BY id
            """),
            {
                "tenant": tenant,
                "main": UUID(main["id"]),
                "emergency": UUID(emergency["id"]),
            },
        ).all() == sorted(
            [(UUID(main["id"]), "Waiting"), (UUID(emergency["id"]), "Waiting")]
        )
        assert connection.execute(
            text("""
                SELECT disposition, count(*) FROM public.queue_entries
                WHERE organization_id=:tenant GROUP BY disposition
            """),
            {"tenant": tenant},
        ).all() == [("ACTIVE", 2)]
        assert (
            connection.scalar(
                text("""
                SELECT count(*) FROM public.appointment_history
                WHERE organization_id=:tenant
                  AND old_status='Waiting' AND new_status='In Examination'
            """),
                {"tenant": tenant},
            )
            == 0
        )


def test_provisional_confirmation_is_one_use_and_invisible_until_rollback(
    appointment_storage,
):
    runtime, _, (tenant, _), (doctor, _) = appointment_storage
    with transaction(runtime, tenant) as connection:
        first = _appointment(connection, tenant, doctor)
        _arrive(connection, tenant, doctor, first)
        target = _appointment(connection, tenant, doctor)
        _arrive(connection, tenant, doctor, target)
        _policy_capacity(connection, tenant)
    held = Event()
    release = Event()
    provisional = {}

    def first_consumer():
        with (
            pytest.raises(RuntimeError, match="rollback simulation"),
            transaction(runtime, tenant) as connection,
        ):
            _doctor_context(connection, doctor)
            control = _lock(connection, tenant, doctor)
            assert control.active_examinations == 0
            _simulated_exam(connection, tenant, doctor, first)
            approval = _issue(connection, tenant, doctor, target, "MAIN", DAY)
            _simulated_exam(connection, tenant, doctor, target)
            assert _consume(connection, tenant, approval, target) == 1
            assert _consume(connection, tenant, approval, target) == 0
            assert _lock(connection, tenant, doctor).active_examinations == 2
            provisional["approval"] = approval
            held.set()
            assert release.wait(timeout=10), "Confirmation holder timed out"
            raise RuntimeError("rollback simulation")
        return "rolled back"

    def second_consumer():
        # An uncommitted Doctor approval must not become usable by another
        # connection; a competing admission first needs the SAME Doctor lock.
        with transaction(runtime, tenant) as connection:
            assert (
                connection.scalar(
                    text("""
                    SELECT state FROM public.doctor_admission_confirmations
                    WHERE organization_id=:tenant AND id=:approval
                """),
                    {"tenant": tenant, "approval": provisional["approval"]},
                )
                is None
            )
            connection.execute(text("SET LOCAL lock_timeout = '250ms'"))
            return _lock(connection, tenant, doctor)

    with ThreadPoolExecutor(max_workers=2) as executor:
        first_consumer_future = executor.submit(first_consumer)
        try:
            assert held.wait(timeout=10), "Confirmation was not provisionally consumed"
            second_consumer_future = executor.submit(second_consumer)
            denied = second_consumer_future.exception(timeout=10)
            assert isinstance(denied, DBAPIError)
            assert denied.orig.sqlstate == "55P03"
        finally:
            release.set()
        assert first_consumer_future.result(timeout=10) == "rolled back"

    with transaction(runtime, tenant) as connection:
        _doctor_context(connection, doctor)
        assert _lock(connection, tenant, doctor).active_examinations == 0
        # An approval that existed only in the other rolled-back transaction
        # cannot be consumed by this competing connection on retry.
        assert _consume(connection, tenant, provisional["approval"], target) == 0
        assert (
            connection.scalar(
                text("""
                SELECT count(*) FROM public.doctor_admission_confirmations
                WHERE organization_id=:tenant
            """),
                {"tenant": tenant},
            )
            == 0
        )
        assert (
            connection.scalar(
                text("""
                SELECT count(*) FROM public.appointments
                WHERE organization_id=:tenant AND status='In Examination'
            """),
                {"tenant": tenant},
            )
            == 0
        )
        assert (
            connection.scalar(
                text("""
                SELECT count(*) FROM public.queue_entries
                WHERE organization_id=:tenant AND disposition='ACTIVE'
            """),
                {"tenant": tenant},
            )
            == 2
        )
        assert (
            connection.scalar(
                text("""
                SELECT count(*) FROM public.appointment_history
                WHERE organization_id=:tenant
                  AND old_status='Waiting' AND new_status='In Examination'
            """),
                {"tenant": tenant},
            )
            == 0
        )

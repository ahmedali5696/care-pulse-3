"""D-11 real PostgreSQL schedule-barrier races against direct SQL attendance."""

from concurrent.futures import ThreadPoolExecutor
from datetime import date
from threading import Event
from time import monotonic
from uuid import uuid4

import pytest
from carepulse.db import transaction
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from test_appointment_persistence import appointment_storage as appointment_storage  # noqa: PLC0414
from test_day_review_persistence import _as_actor, _review
from test_queue_persistence import _appointment, _arrive
from test_queue_races import _transition

DAY = date(2026, 10, 27)


def _observe_waiter(runtime, tenant, waiter, holder):
    with transaction(runtime, tenant) as observer:
        deadline = monotonic() + 15
        while monotonic() < deadline:
            observer.execute(text("SELECT pg_stat_clear_snapshot()"))
            blocked = observer.scalar(
                text("""
                    SELECT query FROM pg_stat_activity
                    WHERE pid=:waiter AND wait_event_type='Lock'
                      AND :holder = ANY(pg_blocking_pids(pid))
                """),
                {"waiter": waiter, "holder": holder},
            )
            if blocked:
                return " ".join(blocked.split())
    pytest.fail("D-11 race never observed a real PostgreSQL lock waiter")


def _exercise_race(runtime, tenant, doctor, writer, first):
    """Leave the first transaction open while observing the second blocked in SQL."""
    started = Event()
    waiter_pid = []

    def contender():
        with transaction(runtime, tenant) as connection:
            waiter_pid.append(connection.scalar(text("SELECT pg_backend_pid()")))
            started.set()
            if first == "writer":
                _as_actor(connection, doctor)
                return _review(connection, tenant, DAY, 0, "FINAL_CLOSED", "Race close")
            return writer(connection)

    with ThreadPoolExecutor(max_workers=1) as executor:
        with transaction(runtime, tenant) as holder:
            holder_pid = holder.scalar(text("SELECT pg_backend_pid()"))
            if first == "writer":
                writer(holder)
            else:
                _as_actor(holder, doctor)
                _review(holder, tenant, DAY, 0, "FINAL_CLOSED", "Race close")
                # Finalization has already checked a clean day while holding FOR UPDATE.
                assert (
                    holder.scalar(
                        text("""
                        SELECT count(*) FROM public.appointments
                        WHERE organization_id=:tenant AND service_date=:day
                          AND status='Waiting'
                    """),
                        {"tenant": tenant, "day": DAY},
                    )
                    == 0
                )
            pending = executor.submit(contender)
            assert started.wait(timeout=10), "Competing transaction did not start"
            blocked_sql = _observe_waiter(runtime, tenant, waiter_pid[0], holder_pid)
            if first == "writer":
                assert "record_day_review" in blocked_sql
            else:
                assert any(
                    statement in blocked_sql
                    for statement in (
                        "UPDATE public.appointments",
                        "INSERT INTO public.queue_days",
                    )
                )
        if first == "writer":
            return pending
        assert pending.result(timeout=20)
        return None


@pytest.mark.parametrize("first", ["writer", "close"])
@pytest.mark.parametrize("lane", ["MAIN", "EMERGENCY"])
def test_direct_waiting_transition_serializes_with_final_close(
    appointment_storage,
    lane,
    first,
):
    runtime, _, (tenant, _), (doctor, _) = appointment_storage
    with transaction(runtime, tenant) as connection:
        appointment = _appointment(
            connection,
            tenant,
            doctor,
            status="Booked",
            service_date=DAY,
            lane=lane,
            classification="ROUTINE" if lane == "MAIN" else "EMERGENCY",
        )

    def direct_waiting(connection):
        assert _transition(connection, tenant, doctor, appointment, "Waiting")
        return True

    pending = _exercise_race(runtime, tenant, doctor, direct_waiting, first)
    if first == "writer":
        with pytest.raises(DBAPIError) as denied:
            pending.result(timeout=20)
        assert denied.value.orig.sqlstate == "23514"

    with transaction(runtime, tenant) as connection:
        assert (
            connection.scalar(
                text("""
                SELECT status FROM public.appointments
                WHERE organization_id=:tenant AND id=CAST(:appointment AS uuid)
            """),
                {"tenant": tenant, "appointment": appointment["id"]},
            )
            == "Waiting"
        )
        assert (
            connection.scalar(
                text("""
                SELECT count(*) FROM public.queue_entries
                WHERE organization_id=:tenant AND appointment_id=CAST(:appointment AS uuid)
                  AND disposition='ACTIVE' AND lane=:lane
            """),
                {"tenant": tenant, "appointment": appointment["id"], "lane": lane},
            )
            == 1
        )
        assert connection.scalar(
            text("""
                SELECT count(*) FROM public.day_reviews
                WHERE organization_id=:tenant AND service_date=:day
                  AND disposition='FINAL_CLOSED'
            """),
            {"tenant": tenant, "day": DAY},
        ) == int(first == "close")


@pytest.mark.parametrize("first", ["writer", "close"])
@pytest.mark.parametrize("lane", ["MAIN", "EMERGENCY"])
def test_first_queue_day_insert_waits_on_final_close_barrier_in_both_orders(
    appointment_storage,
    lane,
    first,
):
    runtime, _, (tenant, _), (doctor, _) = appointment_storage

    def first_day(connection):
        return connection.scalar(
            text("""
                INSERT INTO public.queue_days
                    (id, organization_id, doctor_staff_id, service_date, lane,
                     queue_policy_version, arrival_counter)
                SELECT :id, :tenant, :doctor, :day, :lane, v.version, 0
                FROM public.clinic_policy_versions v
                JOIN public.clinic_policy_heads h
                  ON h.organization_id=v.organization_id AND h.kind=v.kind
                 AND v.version<=h.current_version
                WHERE v.organization_id=:tenant AND v.kind='QUEUE'
                  AND NOT v.pending_effective
                  AND (v.version=1 OR v.effective_on<=:day)
                ORDER BY v.version DESC LIMIT 1
                RETURNING id
            """),
            {
                "id": uuid4(),
                "tenant": tenant,
                "doctor": doctor,
                "day": DAY,
                "lane": lane,
            },
        )

    pending = _exercise_race(runtime, tenant, doctor, first_day, first)
    if first == "writer":
        assert pending.result(timeout=20).revision == 1
    with transaction(runtime, tenant) as connection:
        assert connection.execute(
            text("""
                SELECT lane, arrival_counter FROM public.queue_days
                WHERE organization_id=:tenant AND service_date=:day
            """),
            {"tenant": tenant, "day": DAY},
        ).all() == [(lane, 0)]
        assert (
            connection.scalar(
                text("""
                SELECT count(*) FROM public.day_reviews
                WHERE organization_id=:tenant AND service_date=:day
                  AND disposition='FINAL_CLOSED'
            """),
                {"tenant": tenant, "day": DAY},
            )
            == 1
        )


@pytest.mark.parametrize("first", ["entry", "close"])
@pytest.mark.parametrize("lane", ["MAIN", "EMERGENCY"])
def test_direct_active_queue_entry_update_uses_same_barrier(
    appointment_storage,
    lane,
    first,
):
    runtime, _, (tenant, _), (doctor, _) = appointment_storage
    with transaction(runtime, tenant) as connection:
        appointment = _appointment(
            connection,
            tenant,
            doctor,
            service_date=DAY,
            classification="ROUTINE" if lane == "MAIN" else "EMERGENCY",
            lane=lane,
        )
        day_id, entry_id, _ = _arrive(connection, tenant, doctor, appointment)

    def update_entry(connection):
        connection.execute(
            text("""
                UPDATE public.queue_entries SET revision=revision+1
                WHERE organization_id=:tenant AND id=:entry
                  AND disposition='ACTIVE'
            """),
            {"tenant": tenant, "entry": entry_id},
        )
        connection.execute(
            text("""
                UPDATE public.queue_days SET revision=revision+1
                WHERE organization_id=:tenant AND id=:day_id
            """),
            {"tenant": tenant, "day_id": day_id},
        )
        return True

    started = Event()
    waiter_pid = []

    def contender():
        with transaction(runtime, tenant) as connection:
            waiter_pid.append(connection.scalar(text("SELECT pg_backend_pid()")))
            started.set()
            if first == "entry":
                _as_actor(connection, doctor)
                return _review(connection, tenant, DAY, 0, "FINAL_CLOSED", "Entry race")
            return update_entry(connection)

    with ThreadPoolExecutor(max_workers=1) as executor:
        with transaction(runtime, tenant) as holder:
            holder_pid = holder.scalar(text("SELECT pg_backend_pid()"))
            if first == "entry":
                update_entry(holder)
            else:
                # A preexisting valid active entry makes final close invalid.
                # Hold the exact exclusive lock phase before its attempted check.
                holder.execute(
                    text("""
                        SELECT 1 FROM public.clinic_policy_heads
                        WHERE organization_id=:tenant AND kind='SCHEDULE' FOR UPDATE
                    """),
                    {"tenant": tenant},
                ).one()
            pending = executor.submit(contender)
            assert started.wait(timeout=10)
            sql = _observe_waiter(runtime, tenant, waiter_pid[0], holder_pid)
            assert (
                "record_day_review" in sql
                if first == "entry"
                else "UPDATE public.queue_entries" in sql
            )
            if first == "close":
                _as_actor(holder, doctor)
                with pytest.raises(DBAPIError) as denied, holder.begin_nested():
                    _review(holder, tenant, DAY, 0, "FINAL_CLOSED", "Unresolved")
                assert denied.value.orig.sqlstate == "23514"
        if first == "entry":
            with pytest.raises(DBAPIError) as denied:
                pending.result(timeout=20)
            assert denied.value.orig.sqlstate == "23514"
        else:
            assert pending.result(timeout=20)

    with transaction(runtime, tenant) as connection:
        assert (
            connection.scalar(
                text("""
                SELECT revision FROM public.queue_entries
                WHERE organization_id=:tenant AND id=:entry
            """),
                {"tenant": tenant, "entry": entry_id},
            )
            == 2
        )
        assert (
            connection.scalar(
                text("""
                SELECT count(*) FROM public.day_reviews
                WHERE organization_id=:tenant AND service_date=:day
            """),
                {"tenant": tenant, "day": DAY},
            )
            == 0
        )

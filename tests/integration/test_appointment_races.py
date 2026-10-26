"""D-05 genuine PostgreSQL concurrency, rollback and archive boundaries."""

from concurrent.futures import ThreadPoolExecutor
from datetime import date
from threading import Barrier
from uuid import UUID

import pytest
from carepulse.appointments.persistence import persist_appointment
from carepulse.db import transaction
from carepulse.identity.contracts import ContractError
from carepulse.patients.lifecycle import (
    lock_patient_for_existing_appointment,
    lock_patient_for_new_appointment,
)
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from test_appointment_persistence import _draft, _patient, _query
from test_appointment_persistence import appointment_storage as appointment_storage  # noqa: PLC0414 - pytest fixture export


def _race(*operations):
    barrier = Barrier(len(operations))

    def attempt(operation):
        barrier.wait(timeout=10)
        return operation()

    with ThreadPoolExecutor(max_workers=len(operations)) as executor:
        futures = [executor.submit(attempt, operation) for operation in operations]
        return [future.exception() or future.result() for future in futures]


def test_parallel_same_patient_day_creates_exactly_one_counted_appointment(
    appointment_storage,
):
    runtime, _, (tenant, _), (doctor, _) = appointment_storage
    with transaction(runtime, tenant) as connection:
        patient = _patient(connection, tenant)

    def create():
        with transaction(runtime, tenant) as connection:
            return persist_appointment(
                connection, _draft(patient, doctor), _query(tenant, doctor)
            )

    outcomes = _race(create, create)
    winners = [outcome for outcome in outcomes if isinstance(outcome, dict)]
    losers = [outcome for outcome in outcomes if isinstance(outcome, DBAPIError)]
    assert len(winners) == len(losers) == 1
    assert losers[0].orig.sqlstate == "23505"
    with transaction(runtime, tenant) as connection:
        assert (
            connection.scalar(
                text("SELECT count(*) FROM public.appointments WHERE patient_id = :id"),
                {"id": patient},
            )
            == 1
        )
        for table in (
            "appointment_number_assignments",
            "appointment_history",
            "appointment_price_snapshots",
        ):
            assert connection.scalar(text(f"SELECT count(*) FROM public.{table}")) == 1
        assert (
            connection.scalar(
                text("SELECT last_number FROM public.appointment_day_counters")
            )
            == 1
        )


def test_concurrent_archive_and_new_id_creation_serialize_on_patient_row(
    appointment_storage,
):
    runtime, _, (tenant, _), (doctor, _) = appointment_storage
    with transaction(runtime, tenant) as connection:
        patient = _patient(connection, tenant)
        existing = persist_appointment(
            connection, _draft(patient, doctor), _query(tenant, doctor)
        )

    def archive():
        with transaction(runtime, tenant) as connection:
            connection.execute(
                text("""
                    UPDATE public.patients
                    SET archived = true, lifecycle_revision = lifecycle_revision + 1
                    WHERE id = :patient
                """),
                {"patient": patient},
            )
        return "archived"

    def new_id():
        with transaction(runtime, tenant) as connection:
            return persist_appointment(
                connection,
                _draft(patient, doctor, service_date=date(2026, 10, 28)),
                _query(tenant, doctor),
            )

    outcomes = _race(archive, new_id)
    assert "archived" in outcomes
    attempted = next(outcome for outcome in outcomes if outcome != "archived")
    assert isinstance(attempted, (dict, ContractError))
    if isinstance(attempted, ContractError):
        assert attempted.status_code == 409
    with transaction(runtime, tenant) as connection:
        assert connection.execute(
            text(
                "SELECT archived, lifecycle_revision FROM public.patients WHERE id = :id"
            ),
            {"id": patient},
        ).one() == (True, 1)
        appointment_count = 2 if isinstance(attempted, dict) else 1
        for table in (
            "appointments",
            "appointment_history",
            "appointment_number_assignments",
            "appointment_price_snapshots",
        ):
            assert (
                connection.scalar(text(f"SELECT count(*) FROM public.{table}"))
                == appointment_count
            )
        assert (
            connection.scalar(
                text(
                    "SELECT count(*) FROM public.idempotency_receipts WHERE operation='appointments.persist'"
                )
            )
            == appointment_count
        )
        assert (
            connection.scalar(
                text(
                    "SELECT count(*) FROM public.outbox_events WHERE event_type='APPOINTMENT_PERSISTED'"
                )
            )
            == appointment_count
        )
        assert (
            connection.scalar(
                text("SELECT count(*) FROM public.appointments WHERE id = :id"),
                {"id": existing["id"]},
            )
            == 1
        )


def test_parallel_main_and_emergency_serialize_unique_clinic_day_numbering(
    appointment_storage,
):
    runtime, _, (tenant, _), (doctor, _) = appointment_storage
    with transaction(runtime, tenant) as connection:
        main_patient = _patient(connection, tenant)
        emergency_patient = _patient(connection, tenant)

    def create(patient, classification, lane):
        with transaction(runtime, tenant) as connection:
            return persist_appointment(
                connection,
                _draft(patient, doctor, classification=classification, lane=lane),
                _query(tenant, doctor),
            )

    outcomes = _race(
        lambda: create(main_patient, "ROUTINE", "MAIN"),
        lambda: create(emergency_patient, "EMERGENCY", "EMERGENCY"),
    )
    assert all(isinstance(outcome, dict) for outcome in outcomes), outcomes
    assert {outcome["number"] for outcome in outcomes} == {1, 2}
    with transaction(runtime, tenant) as connection:
        assignments = connection.execute(
            text("""
                SELECT a.classification, a.lane, n.number
                FROM public.appointments a JOIN public.appointment_number_assignments n
                  ON (a.organization_id, a.id) = (n.organization_id, n.appointment_id)
                ORDER BY n.number
            """)
        ).all()
        assert len(assignments) == 2
        assert {row[0:2] for row in assignments} == {
            ("ROUTINE", "MAIN"),
            ("EMERGENCY", "EMERGENCY"),
        }
        assert (
            connection.scalar(
                text("SELECT last_number FROM public.appointment_day_counters")
            )
            == 2
        )


def test_failure_after_assignment_history_audit_outbox_and_receipt_rolls_back(
    appointment_storage,
):
    runtime, _, (tenant, _), (doctor, _) = appointment_storage
    with transaction(runtime, tenant) as connection:
        patient = _patient(connection, tenant)
    query = _query(tenant, doctor)
    with (
        pytest.raises(RuntimeError, match="synthetic domain abort"),
        transaction(runtime, tenant) as connection,
    ):
        created = persist_appointment(connection, _draft(patient, doctor), query)
        assert (
            connection.scalar(
                text(
                    "SELECT count(*) FROM public.appointment_history WHERE appointment_id = :id"
                ),
                {"id": created["id"]},
            )
            == 1
        )
        assert (
            connection.scalar(
                text(
                    "SELECT count(*) FROM public.outbox_events WHERE aggregate_id = :id"
                ),
                {"id": created["id"]},
            )
            == 1
        )
        raise RuntimeError("synthetic domain abort")

    with transaction(runtime, tenant) as connection:
        for table in (
            "appointments",
            "appointment_day_counters",
            "appointment_number_assignments",
            "appointment_history",
            "appointment_price_snapshots",
        ):
            assert connection.scalar(text(f"SELECT count(*) FROM public.{table}")) == 0
        assert (
            connection.scalar(
                text("""
                SELECT count(*) FROM public.idempotency_receipts
                WHERE idempotency_key = :key
            """),
                {"key": query.idempotency_key},
            )
            == 0
        )
        assert (
            connection.scalar(
                text(
                    "SELECT count(*) FROM public.outbox_events WHERE aggregate_id = :id"
                ),
                {"id": created["id"]},
            )
            == 0
        )
        assert (
            connection.scalar(
                text(
                    "SELECT count(*) FROM public.sensitive_audit WHERE correlation_id = :id"
                ),
                {"id": created["id"]},
            )
            == 0
        )
        replacement = persist_appointment(
            connection, _draft(patient, doctor), _query(tenant, doctor)
        )
        assert replacement["number"] == 1


def test_archived_patient_blocks_new_id_but_retains_existing_confirmed_id(
    appointment_storage,
):
    runtime, _, (tenant, _), (doctor, _) = appointment_storage
    with transaction(runtime, tenant) as connection:
        patient = _patient(connection, tenant)
        existing = persist_appointment(
            connection, _draft(patient, doctor), _query(tenant, doctor)
        )
        connection.execute(
            text("""
                UPDATE public.patients
                SET archived = true, lifecycle_revision = lifecycle_revision + 1
                WHERE id = :patient
            """),
            {"patient": patient},
        )
    with transaction(runtime, tenant) as connection:
        verified_id = connection.scalar(
            text("""
                SELECT id FROM public.appointments
                WHERE id = :id AND patient_id = :patient
            """),
            {"id": existing["id"], "patient": patient},
        )
        assert verified_id == UUID(existing["id"])
        assert lock_patient_for_existing_appointment(
            connection, tenant, patient
        ).archived
        with pytest.raises(ContractError) as denied:
            lock_patient_for_new_appointment(connection, tenant, patient)
        assert denied.value.status_code == 409
        with pytest.raises(ContractError) as denied_new:
            persist_appointment(
                connection, _draft(patient, doctor), _query(tenant, doctor)
            )
        assert denied_new.value.status_code == 409
        assert (
            connection.scalar(
                text(
                    "SELECT count(*) FROM public.appointment_history WHERE appointment_id = :id"
                ),
                {"id": verified_id},
            )
            == 1
        )
        assert (
            connection.scalar(
                text("SELECT last_number FROM public.appointment_day_counters")
            )
            == 1
        )

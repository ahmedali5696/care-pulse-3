"""D-03 real PostgreSQL tenant, demographic, archive and lifecycle proof."""

from concurrent.futures import ThreadPoolExecutor
from datetime import date
from threading import Event
from time import monotonic, sleep
from uuid import uuid4

import pytest
from carepulse.db import transaction
from carepulse.identity.contracts import ContractError
from carepulse.patients.lifecycle import (
    lock_patient_for_existing_appointment,
    lock_patient_for_new_appointment,
    lock_patient_lifecycle,
)
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError


@pytest.fixture
def patient_storage(role_engine, tenant_factory):
    _, create_tenant = tenant_factory
    tenant_a = create_tenant("Synthetic D-03 A")
    tenant_b = create_tenant("Synthetic D-03 B")
    runtime = role_engine("carepulse_runtime")
    migration = role_engine("carepulse_migration")
    try:
        yield runtime, migration, tenant_a, tenant_b
    finally:
        runtime.dispose()
        migration.dispose()


def _create(connection, tenant, *, phone=None, name=None, dob=None):
    patient_id = uuid4()
    connection.execute(
        text("""
            INSERT INTO public.patients
                (id, organization_id, name_parts, date_of_birth,
                 phone_raw, phone_normalized)
            VALUES (:id, :tenant, :name, :dob, :phone, :normalized)
        """),
        {
            "id": patient_id,
            "tenant": tenant,
            "name": name or ["Synthetic", "Family", "Patient"],
            "dob": dob or date(2000, 2, 29),
            "phone": phone,
            "normalized": "+201012345678" if phone else None,
        },
    )
    return patient_id


def test_patient_rls_clinic_scope_and_privilege_denials(patient_storage, role_engine):
    runtime, _, tenant_a, tenant_b = patient_storage
    with transaction(runtime, tenant_a) as connection:
        a = _create(connection, tenant_a, phone="01012345678")
    with transaction(runtime, tenant_b) as connection:
        b = _create(connection, tenant_b, phone="01012345678")
    assert a != b

    with transaction(runtime, tenant_a) as connection:
        assert connection.scalars(
            text("SELECT id FROM public.patients ORDER BY id")
        ).all() == [a]
        assert (
            connection.execute(
                text(
                    "UPDATE public.patients SET phone_raw = 'forbidden' WHERE id = :id"
                ),
                {"id": b},
            ).rowcount
            == 0
        )
        with pytest.raises(DBAPIError) as denied, connection.begin_nested():
            _create(connection, tenant_b)
        assert denied.value.orig.sqlstate == "42501"

    with transaction(runtime) as connection:
        assert connection.scalars(text("SELECT id FROM public.patients")).all() == []
        with pytest.raises(DBAPIError) as denied, connection.begin_nested():
            _create(connection, tenant_a)
        assert denied.value.orig.sqlstate == "42501"

    operator = role_engine("carepulse_operator")
    try:
        with pytest.raises(DBAPIError) as denied, operator.connect() as connection:
            connection.execute(text("SELECT name_parts FROM public.patients"))
        assert denied.value.orig.sqlstate == "42501"
    finally:
        operator.dispose()


@pytest.mark.parametrize(
    "field, value",
    [
        ("name_parts", ["Two", "Parts"]),
        ("name_parts", ["Valid", " ", "Name"]),
        ("name_parts", ["Valid", None, "Name"]),
        ("name_parts", ["One", "Two", "Three", "Four", "Five"]),
        ("date_of_birth", date(2999, 1, 1)),
        ("phone_normalized", "invalid-e164"),
    ],
)
def test_patient_database_rejects_invalid_demographics(patient_storage, field, value):
    runtime, _, tenant, _ = patient_storage
    with transaction(runtime, tenant) as connection:
        record = {
            "id": uuid4(),
            "tenant": tenant,
            "name": ["Synthetic", "Valid", "Patient"],
            "dob": date(2000, 1, 1),
            "normalized": None,
        }
        record[
            {
                "name_parts": "name",
                "date_of_birth": "dob",
                "phone_normalized": "normalized",
            }[field]
        ] = value
        with pytest.raises(DBAPIError) as denied, connection.begin_nested():
            connection.execute(
                text("""
                    INSERT INTO public.patients (
                        id, organization_id, name_parts, date_of_birth, phone_normalized)
                    VALUES (:id, :tenant, :name, :dob, :normalized)
                """),
                record,
            )
        assert denied.value.orig.sqlstate == "23514"


@pytest.mark.parametrize("age", ["NaN", "Infinity", "-1"])
def test_nonfinite_and_negative_declared_age_denied(patient_storage, age):
    runtime, _, tenant, _ = patient_storage
    with transaction(runtime, tenant) as connection:
        with pytest.raises(DBAPIError) as denied, connection.begin_nested():
            connection.execute(
                text("""
                    INSERT INTO public.patients (
                        id, organization_id, name_parts, declared_age, age_reference_date)
                    VALUES (:id, :tenant, ARRAY['Synthetic','Age','Patient'],
                            CAST(:age AS numeric), DATE '2026-10-26')
                """),
                {"id": uuid4(), "tenant": tenant, "age": age},
            )
        assert denied.value.orig.sqlstate == "23514"


def test_family_duplicates_and_independent_edits(patient_storage):
    runtime, _, tenant, _ = patient_storage
    with transaction(runtime, tenant) as connection:
        first = _create(connection, tenant, phone="01012345678")
        second = _create(connection, tenant, phone="01012345678")
        assert first != second
        assert (
            connection.scalar(
                text(
                    "SELECT count(*) FROM public.patients WHERE phone_normalized = :phone"
                ),
                {"phone": "+201012345678"},
            )
            == 2
        )
        connection.execute(
            text("""
                UPDATE public.patients SET phone_raw = '01099999999',
                  phone_normalized = '+201099999999',
                  demographic_revision = demographic_revision + 1
                WHERE id = :id
            """),
            {"id": first},
        )
        records = dict(
            connection.execute(
                text("SELECT id, phone_normalized FROM public.patients")
            ).all()
        )
        assert records[first] == "+201099999999"
        assert records[second] == "+201012345678"


def test_optional_unparseable_phone_and_declared_age_are_persisted(patient_storage):
    runtime, _, tenant, _ = patient_storage
    with transaction(runtime, tenant) as connection:
        patient_id = uuid4()
        connection.execute(
            text("""
                INSERT INTO public.patients
                    (id, organization_id, name_parts, phone_raw, declared_age,
                     age_reference_date)
                VALUES (:id, :tenant, ARRAY['Three','Part','Name'], :raw,
                        29.5, DATE '2026-10-26')
            """),
            {"id": patient_id, "tenant": tenant, "raw": "family desk extension"},
        )
        record = connection.execute(
            text("""
                SELECT name_parts, phone_raw, phone_normalized, date_of_birth,
                       declared_age, age_reference_date
                FROM public.patients WHERE id = :id
            """),
            {"id": patient_id},
        ).one()
        assert record.name_parts == ["Three", "Part", "Name"]
        assert record.phone_raw == "family desk extension"
        assert record.phone_normalized is None
        assert record.date_of_birth is None
        assert str(record.declared_age) == "29.5"
        assert record.age_reference_date == date(2026, 10, 26)

        with pytest.raises(DBAPIError) as invalid, connection.begin_nested():
            connection.execute(
                text("""
                    UPDATE public.patients
                    SET date_of_birth = DATE '2000-01-01',
                        demographic_revision = demographic_revision + 1
                    WHERE id = :id
                """),
                {"id": patient_id},
            )
        assert invalid.value.orig.sqlstate == "23514"


def test_archive_revision_immutability_and_existing_id_continuation(patient_storage):
    runtime, migration, tenant, _ = patient_storage
    with transaction(runtime, tenant) as connection:
        patient_id = _create(connection, tenant)
        lifecycle = lock_patient_for_new_appointment(connection, tenant, patient_id)
        assert (lifecycle.archived, lifecycle.revision) == (False, 0)
        connection.execute(
            text("""
                UPDATE public.patients
                SET archived = true, lifecycle_revision = lifecycle_revision + 1
                WHERE id = :id
            """),
            {"id": patient_id},
        )
        with pytest.raises(ContractError) as blocked:
            lock_patient_for_new_appointment(connection, tenant, patient_id)
        assert blocked.value.status_code == 409
        continued = lock_patient_for_existing_appointment(
            connection, tenant, patient_id
        )
        assert (continued.archived, continued.revision) == (True, 1)
        assert (
            connection.scalar(
                text("SELECT archived_at FROM public.patients WHERE id = :id"),
                {"id": patient_id},
            )
            is not None
        )
        with pytest.raises(DBAPIError) as denied, connection.begin_nested():
            connection.execute(
                text(
                    "UPDATE public.patients SET name_parts = ARRAY['New','Valid','Name'] WHERE id = :id"
                ),
                {"id": patient_id},
            )
        assert denied.value.orig.sqlstate == "23514"
        connection.execute(
            text("""
                UPDATE public.patients
                SET name_parts = ARRAY['New','Valid','Name'],
                    demographic_revision = demographic_revision + 1
                WHERE id = :id
            """),
            {"id": patient_id},
        )
        assert lock_patient_lifecycle(connection, tenant, patient_id).archived

    with migration.begin() as connection:
        with pytest.raises(DBAPIError) as denied, connection.begin_nested():
            connection.execute(
                text("DELETE FROM public.patients WHERE id = :id"),
                {"id": patient_id},
            )
        assert denied.value.orig.sqlstate == "23514"


def test_concurrent_archive_fences_new_id_guard(patient_storage):
    runtime, migration, tenant, _ = patient_storage
    with transaction(runtime, tenant) as connection:
        patient_id = _create(connection, tenant)

    started = Event()
    waiter_pid = []

    def concurrent_new_id_guard():
        with transaction(runtime, tenant) as connection:
            connection.execute(text("SET LOCAL lock_timeout = '5s'"))
            waiter_pid.append(connection.scalar(text("SELECT pg_backend_pid()")))
            started.set()
            return lock_patient_for_new_appointment(connection, tenant, patient_id)

    with ThreadPoolExecutor(max_workers=1) as pool:
        with transaction(runtime, tenant) as archiver:
            lock_patient_lifecycle(archiver, tenant, patient_id)
            waiting = pool.submit(concurrent_new_id_guard)
            assert started.wait(timeout=4)
            with migration.connect() as observer:
                deadline = monotonic() + 4
                while monotonic() < deadline:
                    observer.execute(text("SELECT pg_stat_clear_snapshot()"))
                    blocked = observer.scalar(
                        text("SELECT cardinality(pg_blocking_pids(:pid))"),
                        {"pid": waiter_pid[0]},
                    )
                    if blocked:
                        break
                    sleep(0.02)
                assert blocked, "No real PostgreSQL patient-row lock waiter observed"
            archiver.execute(
                text("""
                    UPDATE public.patients
                    SET archived = true, lifecycle_revision = lifecycle_revision + 1
                    WHERE id = :id
                """),
                {"id": patient_id},
            )
        with pytest.raises(ContractError) as blocked_guard:
            waiting.result(timeout=8)
        assert blocked_guard.value.status_code == 409

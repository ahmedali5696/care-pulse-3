"""D-03 real PostgreSQL tenant, demographic, archive and lifecycle proof."""

from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from dataclasses import replace
from datetime import date
from threading import Event
from time import monotonic, sleep
from uuid import uuid4

import pytest
from carepulse.identity.contracts import ContractError
from carepulse.patients.lifecycle import (
    lock_patient_for_existing_appointment,
    lock_patient_for_new_appointment,
    lock_patient_lifecycle,
)
from runtime_identity_fixture import signed_test_transaction
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from test_appointment_persistence import _activate_clinic
from test_request_boundary import _signed
from test_request_boundary import boundary_data as boundary_data  # noqa: PLC0414


@pytest.fixture
def patient_storage(boundary_data):
    clinic = boundary_data
    owner, _, _ = _signed(clinic)
    with clinic.migration.begin() as connection:
        operator = connection.scalar(
            text(
                "SELECT created_by_operator_id FROM public.organizations WHERE id=:id"
            ),
            {"id": clinic.other},
        )
        other_staff = _activate_clinic(connection, clinic.other, operator)
        native_user = connection.scalar(
            text("SELECT auth_user_id FROM public.staff_accounts WHERE id=:id"),
            {"id": other_staff},
        )
        session = f"synthetic-d03-{uuid4()}"
        connection.execute(
            text("""
                INSERT INTO public."session"
                    (id, "userId", token, "expiresAt", "createdAt", "updatedAt",
                     "securityRevision", "absoluteDeadline", "lastActivity")
                VALUES (:id, :user, :token, now()+interval '1 hour', now(), now(),
                        0, now()+interval '1 hour', now())
            """),
            {"id": session, "user": native_user, "token": f"synthetic-{uuid4()}"},
        )
    clinic.actors = {
        clinic.tenant: owner,
        clinic.other: replace(
            owner,
            organization_id=str(clinic.other),
            staff_id=str(other_staff),
            session_id=session,
        ),
    }
    return clinic, clinic.migration, clinic.tenant, clinic.other


@contextmanager
def _tx(
    clinic, tenant, *, method="POST", patient_id=None, archive=False, booking=False
):
    path = "/v1/appointments" if booking else "/v1/patients"
    if patient_id is not None and not booking:
        path += f"/{patient_id}"
    if archive:
        path += "/archive"
    with signed_test_transaction(
        clinic,
        clinic.actors[tenant],
        method=method,
        path=path,
        purpose="APPOINTMENT_BOOKING" if booking else "PATIENT_REGISTRY",
        body={"patient_id": str(patient_id)}
        if booking
        else {}
        if method != "GET"
        else None,
        key=str(uuid4()) if method != "GET" else "",
    ) as (connection, _):
        yield connection


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
    clinic, migration, tenant_a, tenant_b = patient_storage
    with _tx(clinic, tenant_a) as connection:
        a = _create(connection, tenant_a, phone="01012345678")
    with _tx(clinic, tenant_b) as connection:
        b = _create(connection, tenant_b, phone="01012345678")
    assert a != b

    with _tx(clinic, tenant_a, method="GET") as connection:
        assert connection.scalars(
            text("SELECT id FROM public.patients ORDER BY id")
        ).all() == [a]
        assert connection.scalars(
            text("""
                SELECT id FROM public.patients
                WHERE name_parts = ARRAY['Synthetic','Family','Patient']
                ORDER BY id LIMIT 10
            """)
        ).all() == [a]
    with _tx(clinic, tenant_a) as connection:
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

    with clinic.runtime.begin() as connection:
        assert connection.scalars(text("SELECT id FROM public.patients")).all() == []
        with pytest.raises(DBAPIError) as denied, connection.begin_nested():
            _create(connection, tenant_a)
        assert denied.value.orig.sqlstate == "42501"

    with migration.begin() as connection:
        with pytest.raises(DBAPIError) as invalid_fk, connection.begin_nested():
            _create(connection, uuid4())
        assert invalid_fk.value.orig.sqlstate == "23503"

    operator = role_engine("carepulse_operator")
    try:
        with pytest.raises(DBAPIError) as denied, operator.connect() as connection:
            connection.execute(text("SELECT name_parts FROM public.patients"))
        assert denied.value.orig.sqlstate == "42501"
    finally:
        operator.dispose()


@pytest.mark.parametrize(
    ("archived", "demographic_revision", "lifecycle_revision"),
    [(True, 0, 0), (False, 1, 0), (False, 0, 1)],
)
def test_patient_insert_cannot_forge_initial_archive_or_revisions(
    patient_storage, archived, demographic_revision, lifecycle_revision
):
    clinic, migration, tenant, _ = patient_storage
    # Preserve the original schema denial, and verify the stronger API scope guard.
    for manager, sqlstate in (
        (_tx(clinic, tenant), "42501"),
        (migration.begin(), "23514"),
    ):
        with (
            manager as connection,
            pytest.raises(DBAPIError) as denied,
            connection.begin_nested(),
        ):
            connection.execute(
                text("""
                    INSERT INTO public.patients (
                        id, organization_id, name_parts, date_of_birth,
                        archived, archived_at, demographic_revision, lifecycle_revision)
                    VALUES (:id, :tenant, ARRAY['Synthetic','Initial','State'],
                            DATE '2000-01-01', :archived,
                            CASE WHEN :archived THEN clock_timestamp() ELSE NULL END,
                            :demographic_revision, :lifecycle_revision)
                """),
                {
                    "id": uuid4(),
                    "tenant": tenant,
                    "archived": archived,
                    "demographic_revision": demographic_revision,
                    "lifecycle_revision": lifecycle_revision,
                },
            )
        assert denied.value.orig.sqlstate == sqlstate


@pytest.mark.parametrize(
    "field, value",
    [
        ("name_parts", ["Two", "Parts"]),
        ("name_parts", ["Valid", " ", "Name"]),
        ("name_parts", ["Valid", "\t", "Name"]),
        ("name_parts", ["Valid", None, "Name"]),
        ("name_parts", ["One", "Two", "Three", "Four", "Five"]),
        ("date_of_birth", date(2999, 1, 1)),
        ("phone_normalized", "invalid-e164"),
    ],
)
def test_patient_database_rejects_invalid_demographics(patient_storage, field, value):
    clinic, _, tenant, _ = patient_storage
    with _tx(clinic, tenant) as connection:
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


def test_patient_database_rejects_nonstandard_array_subscripts(patient_storage):
    clinic, _, tenant, _ = patient_storage
    with _tx(clinic, tenant) as connection:
        with pytest.raises(DBAPIError) as denied, connection.begin_nested():
            connection.execute(
                text("""
                    INSERT INTO public.patients
                        (id, organization_id, name_parts, date_of_birth)
                    VALUES (:id, :tenant,
                            '[5:7]={"","",""}'::text[], DATE '2000-01-01')
                """),
                {"id": uuid4(), "tenant": tenant},
            )
        assert denied.value.orig.sqlstate == "23514"


@pytest.mark.parametrize("age", ["NaN", "Infinity", "-1"])
def test_nonfinite_and_negative_declared_age_denied(patient_storage, age):
    clinic, _, tenant, _ = patient_storage
    with _tx(clinic, tenant) as connection:
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
    clinic, _, tenant, _ = patient_storage
    with _tx(clinic, tenant) as connection:
        first = _create(connection, tenant, phone="01012345678")
    with _tx(clinic, tenant) as connection:
        second = _create(connection, tenant, phone="01012345678")
    assert first != second
    with _tx(clinic, tenant, method="GET") as connection:
        assert (
            connection.scalar(
                text(
                    "SELECT count(*) FROM public.patients WHERE phone_normalized = :phone"
                ),
                {"phone": "+201012345678"},
            )
            == 2
        )
    with _tx(clinic, tenant, method="PATCH", patient_id=first) as connection:
        connection.execute(
            text("""
                UPDATE public.patients SET phone_raw = '01099999999',
                  phone_normalized = '+201099999999',
                  demographic_revision = demographic_revision + 1
                WHERE id = :id
            """),
            {"id": first},
        )
    with _tx(clinic, tenant, method="GET") as connection:
        records = dict(
            connection.execute(
                text("SELECT id, phone_normalized FROM public.patients")
            ).all()
        )
        assert records[first] == "+201099999999"
        assert records[second] == "+201012345678"


def test_optional_unparseable_phone_and_declared_age_are_persisted(patient_storage):
    clinic, _, tenant, _ = patient_storage
    with _tx(clinic, tenant) as connection:
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

    with _tx(clinic, tenant, method="PATCH", patient_id=patient_id) as connection:
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


def test_preliminary_intake_fields_are_independent_and_identity_immutable(
    patient_storage,
):
    clinic, migration, tenant, other_tenant = patient_storage
    intake_fields = (
        "preliminary_medical_history",
        "chronic_illnesses",
        "drug_allergies",
        "food_allergies",
        "prior_examination_notes",
        "preliminary_test_information",
    )
    with _tx(clinic, tenant) as connection:
        patient_id = _create(connection, tenant)
    with _tx(clinic, tenant, method="PATCH", patient_id=patient_id) as connection:
        connection.execute(
            text("""
                UPDATE public.patients SET
                    preliminary_medical_history = 'intake-history',
                    chronic_illnesses = 'intake-chronic',
                    drug_allergies = 'intake-drug',
                    food_allergies = 'intake-food',
                    prior_examination_notes = 'intake-notes',
                    preliminary_test_information = 'intake-tests',
                    demographic_revision = demographic_revision + 1
                WHERE organization_id = :tenant AND id = :id
            """),
            {"tenant": tenant, "id": patient_id},
        )
        row = connection.execute(
            text(
                f"SELECT {', '.join(intake_fields)} FROM public.patients WHERE id = :id"
            ),
            {"id": patient_id},
        ).one()
        assert row == tuple(
            f"intake-{suffix}"
            for suffix in ("history", "chronic", "drug", "food", "notes", "tests")
        )

    with migration.begin() as connection:
        for change, params in (
            ("id = :changed_id", {"changed_id": uuid4()}),
            ("organization_id = :changed_tenant", {"changed_tenant": other_tenant}),
        ):
            with pytest.raises(DBAPIError) as denied, connection.begin_nested():
                connection.execute(
                    text(f"UPDATE public.patients SET {change} WHERE id = :id"),
                    {"id": patient_id, **params},
                )
            assert denied.value.orig.sqlstate == "23514"


def test_archive_revision_immutability_and_existing_id_continuation(patient_storage):
    clinic, migration, tenant, _ = patient_storage
    with _tx(clinic, tenant) as connection:
        patient_id = _create(connection, tenant)
    with _tx(clinic, tenant, patient_id=patient_id, booking=True) as connection:
        lifecycle = lock_patient_for_new_appointment(connection, tenant, patient_id)
        assert (lifecycle.archived, lifecycle.revision) == (False, 0)
    with _tx(clinic, tenant, patient_id=patient_id, archive=True) as connection:
        connection.execute(
            text("""
                UPDATE public.patients
                SET archived = true, lifecycle_revision = lifecycle_revision + 1
                WHERE id = :id
            """),
            {"id": patient_id},
        )
    with _tx(clinic, tenant, patient_id=patient_id, booking=True) as connection:
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
    with _tx(clinic, tenant, method="PATCH", patient_id=patient_id) as connection:
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


def test_archive_preserves_existing_record_reference(patient_storage):
    clinic, migration, tenant, _ = patient_storage
    with _tx(clinic, tenant) as connection:
        patient_id = _create(connection, tenant)
    with migration.begin() as marker:
        marker.execute(
            text("""
                CREATE TABLE public.synthetic_d03_record (
                    organization_id uuid NOT NULL,
                    patient_id uuid NOT NULL,
                    FOREIGN KEY (organization_id, patient_id)
                        REFERENCES public.patients(organization_id, id)
                )
            """)
        )
        marker.execute(
            text("""
                INSERT INTO public.synthetic_d03_record VALUES (:tenant, :patient)
            """),
            {"tenant": tenant, "patient": patient_id},
        )
    try:
        with _tx(clinic, tenant, patient_id=patient_id, archive=True) as connection:
            connection.execute(
                text("""
                    UPDATE public.patients
                    SET archived = true, lifecycle_revision = lifecycle_revision + 1
                    WHERE organization_id = :tenant AND id = :patient
                """),
                {"tenant": tenant, "patient": patient_id},
            )
        with _tx(clinic, tenant, patient_id=patient_id, booking=True) as connection:
            assert lock_patient_for_existing_appointment(
                connection, tenant, patient_id
            ).archived
        with migration.connect() as marker:
            assert (
                marker.scalar(
                    text("""
                SELECT count(*) FROM public.synthetic_d03_record
                WHERE organization_id = :tenant AND patient_id = :patient
            """),
                    {"tenant": tenant, "patient": patient_id},
                )
                == 1
            )
    finally:
        with migration.begin() as marker:
            marker.execute(text("DROP TABLE public.synthetic_d03_record"))


@pytest.mark.parametrize("operation", ["new_id", "demographic_edit"])
def test_concurrent_archive_serializes_new_id_guard_or_demographic_edit(
    patient_storage, operation
):
    clinic, migration, tenant, _ = patient_storage
    with _tx(clinic, tenant) as connection:
        patient_id = _create(connection, tenant)

    started = Event()
    waiter_pid = []

    def concurrent_new_id_guard():
        with _tx(
            clinic,
            tenant,
            patient_id=patient_id,
            method="POST" if operation == "new_id" else "PATCH",
            booking=operation == "new_id",
        ) as connection:
            connection.execute(text("SET LOCAL lock_timeout = '5s'"))
            waiter_pid.append(connection.scalar(text("SELECT pg_backend_pid()")))
            started.set()
            if operation == "new_id":
                return lock_patient_for_new_appointment(connection, tenant, patient_id)
            lifecycle = lock_patient_lifecycle(connection, tenant, patient_id)
            connection.execute(
                text("""
                    UPDATE public.patients
                    SET phone_raw = 'new contact',
                        demographic_revision = demographic_revision + 1
                    WHERE organization_id = :tenant AND id = :id
                """),
                {"tenant": tenant, "id": patient_id},
            )
            return lifecycle

    with ThreadPoolExecutor(max_workers=1) as pool:
        # Isolate patient-row contention from the F-01 organization lock.
        # The competing operation still executes as the authenticated API.
        with migration.begin() as archiver:
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
        if operation == "new_id":
            with pytest.raises(ContractError) as blocked_guard:
                waiting.result(timeout=8)
            assert blocked_guard.value.status_code == 409
        else:
            updated = waiting.result(timeout=8)
            assert (updated.archived, updated.revision) == (True, 1)
        with _tx(clinic, tenant, method="GET", patient_id=patient_id) as connection:
            row = connection.execute(
                text("""
                    SELECT archived, lifecycle_revision, demographic_revision, phone_raw
                    FROM public.patients WHERE organization_id = :tenant AND id = :id
                """),
                {"tenant": tenant, "id": patient_id},
            ).one()
            assert row.archived and row.lifecycle_revision == 1
            assert row.demographic_revision == (
                1 if operation == "demographic_edit" else 0
            )
            assert row.phone_raw == (
                "new contact" if operation == "demographic_edit" else None
            )

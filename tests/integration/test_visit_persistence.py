"""D-07 Visit admission provenance, tenant isolation and database privileges."""

from uuid import UUID, uuid4

import pytest
from carepulse.appointments.persistence import persist_appointment
from carepulse.db import transaction
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from test_appointment_persistence import (
    _admit_saved,
    _draft,
    _patient,
    _query,
    _seed_waiting_entry,
)
from test_appointment_persistence import appointment_storage as appointment_storage  # noqa: PLC0414


def _receptionist(migration, tenant):
    staff, user = uuid4(), f"synthetic-d07-{uuid4()}"
    email = f"{uuid4()}@example.invalid"
    with migration.begin() as connection:
        connection.execute(
            text("""
                INSERT INTO public."user"
                    (id, name, email, "emailVerified", "createdAt", "updatedAt")
                VALUES (:user, 'Synthetic D-07 receptionist', :email, true, now(), now())
            """),
            {"user": user, "email": email},
        )
        connection.execute(
            text("""
                INSERT INTO public.staff_accounts
                    (id, organization_id, auth_user_id, verified_email,
                     role, enabled, verified)
                VALUES (:staff, :tenant, :user, :email, 'RECEPTIONIST', true, true)
            """),
            {"staff": staff, "tenant": tenant, "user": user, "email": email},
        )
    return staff


def _seed(
    connection,
    tenant,
    doctor,
    *,
    actor=None,
    role="DOCTOR_OWNER",
    complete=False,
    insert_visit=True,
):
    saved = persist_appointment(
        connection, _draft(_patient(connection, tenant), doctor), _query(tenant, doctor)
    )
    _admit_saved(
        connection,
        tenant,
        doctor,
        saved,
        complete=complete,
        actor=actor,
        role=role,
        insert_visit=insert_visit,
    )
    return UUID(saved["id"])


def _visit(connection, appointment):
    return connection.execute(
        text("""
            SELECT id, patient_id, doctor_staff_id, service_date, admitted_at,
                   admission_history_revision, clinical_revision, diagnosis,
                   followup_decision
            FROM public.visits WHERE appointment_id=:appointment
        """),
        {"appointment": appointment},
    ).one()


@pytest.mark.parametrize("role", ["DOCTOR_OWNER", "RECEPTIONIST"])
def test_admission_shell_is_tied_to_real_history_and_live_actor(
    appointment_storage, role
):
    runtime, migration, (tenant, _), (doctor, _) = appointment_storage
    actor = doctor if role == "DOCTOR_OWNER" else _receptionist(migration, tenant)
    with transaction(runtime, tenant) as connection:
        appointment = _seed(connection, tenant, doctor, actor=actor, role=role)
        visit = _visit(connection, appointment)
        history = connection.execute(
            text("""
                SELECT occurred_at, actor_staff_id, actor_role
                FROM public.appointment_history
                WHERE appointment_id=:appointment AND appointment_revision=2
            """),
            {"appointment": appointment},
        ).one()
        assert visit.admission_history_revision == 2
        assert visit.admitted_at == history.occurred_at
        assert history.actor_staff_id == actor and history.actor_role == role
        assert visit.clinical_revision == 0
        assert visit.diagnosis is None and visit.followup_decision is None
        assert (
            connection.scalar(
                text(
                    "SELECT count(*) FROM public.clinical_revisions WHERE visit_id=:visit"
                ),
                {"visit": visit.id},
            )
            == 0
        )


@pytest.mark.parametrize(
    "invalid",
    [
        "clinical_content",
        "initial_revision",
        "wrong_patient",
        "wrong_day",
        "wrong_doctor",
        "wrong_admission_revision",
        "forged_actor",
    ],
)
def test_invalid_admission_shell_rejected_without_a_visit(appointment_storage, invalid):
    runtime, _, (tenant, _other), (doctor, other_doctor) = appointment_storage
    with transaction(runtime, tenant) as connection:
        appointment = _seed(connection, tenant, doctor, insert_visit=False)
        defaults = {
            "visit": uuid4(),
            "tenant": tenant,
            "appointment": appointment,
            "patient": connection.scalar(
                text("SELECT patient_id FROM public.appointments WHERE id=:id"),
                {"id": appointment},
            ),
            "doctor": doctor,
            "day": connection.scalar(
                text("SELECT service_date FROM public.appointments WHERE id=:id"),
                {"id": appointment},
            ),
            "revision": 2,
            "clinical_revision": 0,
            "diagnosis": None,
        }
        if invalid == "clinical_content":
            defaults["diagnosis"] = "Unauthorized on admission"
        elif invalid == "initial_revision":
            defaults["clinical_revision"] = 1
        elif invalid == "wrong_patient":
            defaults["patient"] = _patient(connection, tenant)
        elif invalid == "wrong_day":
            from datetime import timedelta

            defaults["day"] += timedelta(days=1)
        elif invalid == "wrong_doctor":
            defaults["doctor"] = other_doctor
        elif invalid == "wrong_admission_revision":
            defaults["revision"] = 1
        elif invalid == "forged_actor":
            connection.execute(
                text("SELECT set_config('app.staff_id', :id, true)"),
                {"id": str(other_doctor)},
            )
        with pytest.raises(DBAPIError) as denied, connection.begin_nested():
            connection.execute(
                text("""
                    INSERT INTO public.visits (
                        id, organization_id, appointment_id, patient_id,
                        doctor_staff_id, service_date, admission_history_revision,
                        admitted_at, clinical_revision, diagnosis
                    ) VALUES (:visit, :tenant, :appointment, :patient, :doctor,
                              :day, :revision, now(), :clinical_revision, :diagnosis)
                """),
                defaults,
            )
        assert denied.value.orig.sqlstate in ("42501", "23514", "23503")
        assert (
            connection.scalar(
                text("SELECT count(*) FROM public.visits WHERE appointment_id=:id"),
                {"id": appointment},
            )
            == 0
        )
        connection.execute(
            text("SELECT set_config('app.staff_id', :actor, true)"),
            {"actor": str(doctor)},
        )
        connection.execute(
            text("""
                INSERT INTO public.visits (
                    id, organization_id, appointment_id, patient_id, doctor_staff_id,
                    service_date, admission_history_revision, admitted_at
                )
                SELECT :visit, organization_id, id, patient_id, doctor_staff_id,
                       service_date, 2, now()
                FROM public.appointments WHERE id=:id
            """),
            {"visit": uuid4(), "id": appointment},
        )


@pytest.mark.parametrize("status", ["Booked", "Waiting", "No-show", "Cancelled"])
def test_pre_admission_appointment_has_no_visit(appointment_storage, status):
    runtime, _, (tenant, _), (doctor, _) = appointment_storage
    with transaction(runtime, tenant) as connection:
        saved = persist_appointment(
            connection,
            _draft(_patient(connection, tenant), doctor, status=status),
            _query(tenant, doctor),
        )
        if status == "Waiting":
            _seed_waiting_entry(connection, tenant, doctor, saved, doctor)
        connection.execute(
            text("SELECT set_config('app.staff_id', :actor, true)"),
            {"actor": str(doctor)},
        )
        with pytest.raises(DBAPIError) as denied, connection.begin_nested():
            connection.execute(
                text("""
                    INSERT INTO public.visits
                        (id, organization_id, appointment_id, patient_id,
                         doctor_staff_id, service_date, admission_history_revision,
                         admitted_at)
                    SELECT :visit, organization_id, id, patient_id, doctor_staff_id,
                           service_date, 1, now()
                    FROM public.appointments WHERE id=:id
                """),
                {"visit": uuid4(), "id": UUID(saved["id"])},
            )
        assert denied.value.orig.sqlstate == "42501"


def test_required_visit_and_retained_exceptional_cancellation(appointment_storage):
    runtime, _, (tenant, _), (doctor, _) = appointment_storage
    with (
        pytest.raises(DBAPIError) as missing,
        transaction(runtime, tenant) as connection,
    ):
        _seed(connection, tenant, doctor, insert_visit=False)
    assert missing.value.orig.sqlstate == "23514"
    with transaction(runtime, tenant) as connection:
        appointment = _seed(connection, tenant, doctor)
        before = _visit(connection, appointment)
        connection.execute(
            text(
                "UPDATE public.appointments SET status='Cancelled', revision=3 WHERE id=:id"
            ),
            {"id": appointment},
        )
        connection.execute(
            text("""
                INSERT INTO public.appointment_history (
                    id, organization_id, appointment_id, appointment_revision,
                    action, old_status, new_status, old_service_date,
                    new_service_date, old_number, new_number, actor_staff_id,
                    actor_role, reason, price_snapshot_appointment_id
                )
                SELECT :history, organization_id, id, 3, 'DOCTOR_EXCEPTION',
                       'In Examination', 'Cancelled', service_date, service_date,
                       current_number, current_number, :doctor, 'DOCTOR_OWNER',
                       'Synthetic exception', id
                FROM public.appointments WHERE id=:id
            """),
            {"history": uuid4(), "doctor": doctor, "id": appointment},
        )
    with transaction(runtime, tenant) as connection:
        assert _visit(connection, appointment) == before
        assert (
            connection.scalar(
                text("SELECT status FROM public.appointments WHERE id=:id"),
                {"id": appointment},
            )
            == "Cancelled"
        )
        with pytest.raises(DBAPIError) as denied, connection.begin_nested():
            connection.execute(
                text("DELETE FROM public.visits WHERE appointment_id=:id"),
                {"id": appointment},
            )
        assert denied.value.orig.sqlstate == "42501"


def test_duplicate_visit_and_missing_actor_cannot_fabricate_admission(
    appointment_storage,
):
    runtime, _, (tenant, _), (doctor, _) = appointment_storage
    with transaction(runtime, tenant) as connection:
        appointment = _seed(connection, tenant, doctor)
        with pytest.raises(DBAPIError) as duplicate, connection.begin_nested():
            connection.execute(
                text("""
                    INSERT INTO public.visits
                        (id, organization_id, appointment_id, patient_id,
                         doctor_staff_id, service_date, admission_history_revision,
                         admitted_at)
                    SELECT :visit, organization_id, id, patient_id, doctor_staff_id,
                           service_date, 2, now()
                    FROM public.appointments WHERE id=:appointment
                """),
                {"visit": uuid4(), "appointment": appointment},
            )
        assert duplicate.value.orig.sqlstate == "23505"
        visit = _visit(connection, appointment)
        connection.execute(text("SELECT set_config('app.staff_id', '', true)"))
        with pytest.raises(DBAPIError) as missing, connection.begin_nested():
            connection.execute(
                text(
                    "UPDATE public.visits SET diagnosis='Unattributed' WHERE id=:visit"
                ),
                {"visit": visit.id},
            )
        assert missing.value.orig.sqlstate == "42501"


@pytest.mark.parametrize("flag", ["enabled", "verified"])
def test_non_live_receptionist_cannot_insert_admission_shell(appointment_storage, flag):
    runtime, migration, (tenant, _), (doctor, _) = appointment_storage
    receptionist = _receptionist(migration, tenant)
    with transaction(runtime, tenant) as connection:
        appointment = _seed(
            connection,
            tenant,
            doctor,
            actor=receptionist,
            role="RECEPTIONIST",
            insert_visit=False,
        )
        connection.execute(
            text(
                f"UPDATE public.staff_accounts SET {flag}=false, security_revision=security_revision+1 WHERE id=:staff"
            ),
            {"staff": receptionist},
        )
        with pytest.raises(DBAPIError) as denied, connection.begin_nested():
            connection.execute(
                text("""
                    INSERT INTO public.visits
                        (id, organization_id, appointment_id, patient_id,
                         doctor_staff_id, service_date, admission_history_revision,
                         admitted_at)
                    SELECT :visit, organization_id, id, patient_id, doctor_staff_id,
                           service_date, 2, now()
                    FROM public.appointments WHERE id=:appointment
                """),
                {"visit": uuid4(), "appointment": appointment},
            )
        assert denied.value.orig.sqlstate == "42501"
        connection.execute(
            text(
                f"UPDATE public.staff_accounts SET {flag}=true, security_revision=security_revision+1 WHERE id=:staff"
            ),
            {"staff": receptionist},
        )
        connection.execute(
            text("""
                INSERT INTO public.visits
                    (id, organization_id, appointment_id, patient_id,
                     doctor_staff_id, service_date, admission_history_revision,
                     admitted_at)
                SELECT :visit, organization_id, id, patient_id, doctor_staff_id,
                       service_date, 2, now()
                FROM public.appointments WHERE id=:appointment
            """),
            {"visit": uuid4(), "appointment": appointment},
        )


def test_tenant_rls_and_effective_permissions(appointment_storage):
    runtime, migration, (tenant, other), (doctor, _) = appointment_storage
    with transaction(runtime, tenant) as connection:
        appointment = _seed(connection, tenant, doctor)
    with transaction(runtime, other) as connection:
        assert connection.scalar(text("SELECT count(*) FROM public.visits")) == 0
        assert (
            connection.scalar(text("SELECT count(*) FROM public.clinical_revisions"))
            == 0
        )
    with transaction(runtime) as connection:
        assert connection.scalar(text("SELECT count(*) FROM public.visits")) == 0
    with migration.connect() as connection:
        assert (
            connection.scalar(
                text("SELECT count(*) FROM public.visits WHERE appointment_id=:id"),
                {"id": appointment},
            )
            == 1
        )
        for name in ("visits", "clinical_revisions"):
            row = connection.execute(
                text("""
                    SELECT relrowsecurity, relforcerowsecurity
                    FROM pg_class WHERE oid=CAST(:name AS regclass)
                """),
                {"name": f"public.{name}"},
            ).one()
            assert row == (True, True)
            for privilege in ("SELECT", "INSERT", "UPDATE", "DELETE"):
                assert (
                    connection.scalar(
                        text(
                            "SELECT has_table_privilege('carepulse_operator', :relation, :privilege)"
                        ),
                        {"relation": f"public.{name}", "privilege": privilege},
                    )
                    is False
                )
                if privilege != "DELETE":
                    assert (
                        connection.scalar(
                            text(
                                "SELECT has_any_column_privilege('carepulse_operator', :relation, :privilege)"
                            ),
                            {"relation": f"public.{name}", "privilege": privilege},
                        )
                        is False
                    )
        assert (
            connection.scalar(
                text(
                    "SELECT has_table_privilege('carepulse_runtime', 'public.visits', 'DELETE')"
                )
            )
            is False
        )
        assert (
            connection.scalar(
                text(
                    "SELECT has_column_privilege('carepulse_runtime', 'public.visits', 'clinical_revision', 'UPDATE')"
                )
            )
            is False
        )
        assert (
            connection.scalar(
                text(
                    "SELECT has_table_privilege('carepulse_runtime', 'public.clinical_revisions', 'INSERT')"
                )
            )
            is False
        )
        for name, expected_definer in (
            ("guard_visit()", True),
            ("assert_visit_appointment_consistency()", True),
            ("deny_clinical_revision_mutation()", False),
        ):
            details = connection.execute(
                text("""
                    SELECT p.prosecdef, p.proconfig,
                           EXISTS (
                             SELECT 1 FROM aclexplode(COALESCE(
                               p.proacl, acldefault('f', p.proowner))) acl
                             WHERE acl.grantee = 0 AND acl.privilege_type = 'EXECUTE'
                           ),
                           has_function_privilege('carepulse_operator', p.oid, 'EXECUTE')
                    FROM pg_proc p WHERE p.oid=CAST(:routine AS regprocedure)
                """),
                {"routine": f"public.{name}"},
            ).one()
            assert details[0] is expected_definer
            assert "search_path=pg_catalog" in details[1]
            assert details[2:] == (False, False)


def test_runtime_without_tenant_context_cannot_write_visit(appointment_storage):
    runtime, migration, (tenant, _), (doctor, _) = appointment_storage
    with transaction(runtime, tenant) as connection:
        appointment = _seed(connection, tenant, doctor)
        original = _visit(connection, appointment)

    with transaction(runtime) as connection:
        connection.execute(
            text("SELECT set_config('app.staff_id', :actor, true)"),
            {"actor": str(doctor)},
        )
        assert (
            connection.execute(
                text("""
                    UPDATE public.visits SET diagnosis='No-tenant write'
                    WHERE id=:visit RETURNING id
                """),
                {"visit": original.id},
            ).all()
            == []
        )

    with migration.connect() as connection:
        assert _visit(connection, appointment) == original
        assert (
            connection.scalar(
                text(
                    "SELECT count(*) FROM public.clinical_revisions WHERE visit_id=:visit"
                ),
                {"visit": original.id},
            )
            == 0
        )


def test_visit_foundation_has_no_queue_dependencies():
    from pathlib import Path

    root = Path(__file__).resolve().parents[2]
    for name in (
        "apps/api/carepulse/visits/tables.py",
        "tests/integration/test_visit_persistence.py",
        "tests/integration/test_visit_migration.py",
    ):
        content = (root / name).read_text()
        assert "queue_" + "days" not in content and "queue_" + "entries" not in content

"""D-06 predecessor preservation, safe refusal, and queue seed permissions."""

import runpy
import shutil
from uuid import UUID, uuid4

import pytest
from alembic import command
from alembic.config import Config
from carepulse.appointments.persistence import persist_appointment
from carepulse.db import transaction
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from test_appointment_persistence import _activate_clinic, _draft, _patient, _query
from test_database import ROOT, _create_empty_database, _engine
from test_database import admin as admin  # noqa: PLC0414 - fixture export
from test_database import disposable_project as disposable_project  # noqa: PLC0414
from test_database import empty_database as empty_database  # noqa: PLC0414
from test_queue_capacity import (
    _doctor_context,
    _issue,
    _lock,
    _policy_capacity,
    _simulated_exam,
)
from test_queue_persistence import DAY, _appointment, _arrive
from test_visit_migration import _prepare

MIGRATION = ROOT / "db/migrations/versions/0025_d06_queue_foundations.py"
BEFORE = "0024_b06_followup_window"
AFTER = "0025_d06_queue_foundations"
QUEUE_TABLES = (
    "queue_days",
    "queue_entries",
    "doctor_controls",
    "doctor_admission_confirmations",
)
PRESERVED_TABLES = (
    "organizations",
    "staff_accounts",
    "patients",
    "appointments",
    "appointment_history",
    "appointment_price_snapshots",
    "appointment_number_assignments",
    "appointment_day_counters",
    "idempotency_receipts",
    "clinic_policy_versions",
    "clinic_policy_heads",
)


def _upgrade(engine, revision, scripts=None):
    config = Config(str(ROOT / "alembic.ini"))
    if scripts is not None:
        config.set_main_option("script_location", str(scripts))
    with engine.connect() as connection:
        config.attributes["connection"] = connection
        command.upgrade(config, revision)


def _predecessor(admin, database):
    assert runpy.run_path(str(MIGRATION))["down_revision"] == BEFORE
    _create_empty_database(admin, database)
    migration = _engine("carepulse_migration", database)
    _upgrade(migration, BEFORE)
    with migration.begin() as connection:
        operator, tenant = uuid4(), uuid4()
        connection.execute(
            text("""
                INSERT INTO public.operator_identities
                    (id, credential_digest, issued_by_reference, issued_at)
                VALUES (:operator, :digest, 'synthetic-d06', clock_timestamp())
            """),
            {"operator": operator, "digest": f"synthetic:{operator}"},
        )
        connection.execute(
            text("""
                INSERT INTO public.organizations
                    (id, branch, created_by_operator_id)
                VALUES (:tenant, 'Synthetic D-06 predecessor', :operator)
            """),
            {"tenant": tenant, "operator": operator},
        )
        doctor = _activate_clinic(connection, tenant, operator)
    return migration, _engine("carepulse_runtime", database), operator, tenant, doctor


def _preserved_rows(connection):
    # Complete row images catch drift in IDs, dates, history, money and receipts.
    return {
        table: connection.execute(
            text(f"SELECT row_to_json(t)::text FROM public.{table} t ORDER BY 1")
        )
        .scalars()
        .all()
        for table in PRESERVED_TABLES
    }


def _predecessor_security(connection, tables=PRESERVED_TABLES):
    # Compare effective privileges, including column grants, not just relacl.
    return connection.execute(
        text("""
            SELECT c.relname, c.relrowsecurity, c.relforcerowsecurity,
                   r.rolname, a.attname, p.privilege,
                   has_table_privilege(r.oid, c.oid, p.privilege),
                   CASE WHEN p.privilege IN ('SELECT','INSERT','UPDATE','REFERENCES')
                        THEN has_column_privilege(r.oid, c.oid, a.attname, p.privilege)
                   END
            FROM pg_class c
            JOIN pg_attribute a ON a.attrelid = c.oid
                               AND a.attnum > 0 AND NOT a.attisdropped
            CROSS JOIN pg_roles r
            CROSS JOIN (VALUES ('SELECT'), ('INSERT'), ('UPDATE'), ('DELETE'),
                               ('TRUNCATE'), ('REFERENCES'), ('TRIGGER')) p(privilege)
            WHERE c.relnamespace = 'public'::regnamespace AND c.relkind = 'r'
              AND c.relname = ANY(:tables)
              AND r.rolname IN ('carepulse_runtime', 'carepulse_operator')
            ORDER BY c.relname, r.rolname, a.attname, p.privilege
        """),
        {"tables": list(tables)},
    ).all()


def _control(connection, tenant):
    return connection.execute(
        text("""
            SELECT c.doctor_staff_id, c.revision, c.capacity_policy_kind,
                   c.capacity_policy_version, h.current_version
            FROM public.doctor_controls c
            JOIN public.clinic_policy_heads h
              ON h.organization_id = c.organization_id AND h.kind = 'CAPACITY'
            WHERE c.organization_id = :tenant
        """),
        {"tenant": tenant},
    ).all()


def test_populated_0024_upgrade_preserves_rows_grants_and_seeds_existing_and_new_clinics(
    admin, empty_database
):
    migration, runtime, operator, tenant, doctor = _predecessor(admin, empty_database)
    try:
        with transaction(runtime, tenant) as connection:
            saved = [
                persist_appointment(
                    connection,
                    _draft(_patient(connection, tenant), doctor, status=status),
                    _query(tenant, doctor),
                )
                for status in ("Booked", "No-show", "Cancelled")
            ]
            assert len({UUID(item["id"]) for item in saved}) == 3
        with migration.connect() as connection:
            assert (
                connection.scalar(text("SELECT to_regclass('public.doctor_controls')"))
                is None
            )
            before = _preserved_rows(connection)
            grants = _predecessor_security(connection)
            assert all(
                len(before[table]) == 3
                for table in (
                    "appointments",
                    "appointment_history",
                    "appointment_price_snapshots",
                    "appointment_number_assignments",
                    "idempotency_receipts",
                )
            )
            assert all(row.relrowsecurity and row.relforcerowsecurity for row in grants)

        _upgrade(migration, AFTER)
        _upgrade(migration, AFTER)
        with migration.connect() as connection:
            assert (
                connection.scalar(
                    text("SELECT version_num FROM public.alembic_version")
                )
                == AFTER
            )
            assert _preserved_rows(connection) == before
            assert _predecessor_security(connection) == grants
            assert _control(connection, tenant) == [(doctor, 1, "CAPACITY", 1, 1)]
            for table in QUEUE_TABLES:
                assert connection.execute(
                    text("""
                        SELECT relrowsecurity, relforcerowsecurity
                        FROM pg_class WHERE oid = to_regclass(:table)
                    """),
                    {"table": f"public.{table}"},
                ).one() == (True, True)

        with migration.begin() as connection:
            new_tenant = uuid4()
            connection.execute(
                text("""
                    INSERT INTO public.organizations
                        (id, branch, created_by_operator_id)
                    VALUES (:tenant, 'Synthetic D-06 new clinic', :operator)
                """),
                {"tenant": new_tenant, "operator": operator},
            )
            assert _control(connection, new_tenant) == []
            new_doctor = _activate_clinic(connection, new_tenant, operator)
            assert _control(connection, new_tenant) == [
                (new_doctor, 1, "CAPACITY", 1, 1)
            ]
    finally:
        runtime.dispose()
        migration.dispose()


def test_incompatible_waiting_without_arrival_refuses_0025_atomically(
    admin, empty_database
):
    migration, runtime, _, tenant, doctor = _predecessor(admin, empty_database)
    try:
        with transaction(runtime, tenant) as connection:
            waiting = persist_appointment(
                connection,
                _draft(_patient(connection, tenant), doctor, status="Waiting"),
                _query(tenant, doctor),
            )
        with migration.connect() as connection:
            before = _preserved_rows(connection)
            assert any(waiting["id"] in row for row in before["appointments"])
        with pytest.raises(DBAPIError) as rejected:
            _upgrade(migration, AFTER)
        assert rejected.value.orig.sqlstate == "23514"
        with migration.connect() as connection:
            assert (
                connection.scalar(
                    text("SELECT version_num FROM public.alembic_version")
                )
                == BEFORE
            )
            assert _preserved_rows(connection) == before
            for table in QUEUE_TABLES:
                assert (
                    connection.scalar(
                        text("SELECT to_regclass(:table)"),
                        {"table": f"public.{table}"},
                    )
                    is None
                )
    finally:
        runtime.dispose()
        migration.dispose()


def test_fresh_repeated_upgrade_and_failed_successor_rollback(
    admin, empty_database, tmp_path
):
    _create_empty_database(admin, empty_database)
    migration = _engine("carepulse_migration", empty_database)
    try:
        _upgrade(migration, AFTER)
        _upgrade(migration, AFTER)
        with migration.connect() as connection:
            assert (
                connection.scalar(
                    text("SELECT version_num FROM public.alembic_version")
                )
                == AFTER
            )
            assert (
                connection.scalar(text("SELECT to_regclass('public.doctor_controls')"))
                == "doctor_controls"
            )

        scripts = tmp_path / "migrations"
        shutil.copytree(ROOT / "db/migrations", scripts)
        (scripts / "versions/9999_d06_failure.py").write_text(
            'from alembic import op\nrevision = "9999_d06_failure"\n'
            f'down_revision = "{AFTER}"\n'
            "def upgrade():\n"
            '    op.execute("CREATE TABLE public.synthetic_d06_partial (id integer)")\n'
            '    op.execute("SELECT 1 / 0")\n',
            encoding="utf-8",
        )
        with pytest.raises(DBAPIError) as failed:
            _upgrade(migration, "9999_d06_failure", scripts)
        assert failed.value.orig.sqlstate == "22012"
        with migration.connect() as connection:
            assert (
                connection.scalar(
                    text("SELECT version_num FROM public.alembic_version")
                )
                == AFTER
            )
            assert (
                connection.scalar(
                    text("SELECT to_regclass('public.synthetic_d06_partial')")
                )
                is None
            )
    finally:
        migration.dispose()


def test_seed_function_trigger_and_effective_privileges(admin, empty_database):
    _create_empty_database(admin, empty_database)
    migration = _engine("carepulse_migration", empty_database)
    try:
        _upgrade(migration, AFTER)
        with migration.connect() as connection:
            owner, security_definer, path, public_execute = connection.execute(
                text("""
                    SELECT pg_get_userbyid(p.proowner), p.prosecdef, p.proconfig,
                           EXISTS (
                             SELECT 1 FROM aclexplode(
                               coalesce(p.proacl, acldefault('f', p.proowner))) acl
                             WHERE acl.grantee = 0 AND acl.privilege_type = 'EXECUTE'
                           )
                    FROM pg_proc p
                    WHERE p.oid =
                      'public.seed_doctor_control_for_staff()'::regprocedure
                """)
            ).one()
            assert (owner, security_definer, path, public_execute) == (
                "carepulse_policy_seed",
                True,
                ["search_path=pg_catalog"],
                False,
            )
            assert connection.execute(
                text("""
                    SELECT rolcanlogin, rolsuper, rolbypassrls
                    FROM pg_roles WHERE rolname = 'carepulse_policy_seed'
                """)
            ).one() == (False, False, False)
            assert connection.execute(
                text("""
                    SELECT t.tgenabled, t.tgisinternal, t.tgtype,
                           t.tgfoid =
                             'public.seed_doctor_control_for_staff()'::regprocedure
                    FROM pg_trigger t
                    WHERE t.tgrelid = 'public.staff_accounts'::regclass
                      AND t.tgname = 'staff_doctor_control_seed'
                """)
            ).one() == ("O", False, 21, True)
            for role in ("carepulse_runtime", "carepulse_auth", "carepulse_operator"):
                assert not connection.scalar(
                    text("""
                        SELECT has_function_privilege(
                          :role, 'public.seed_doctor_control_for_staff()', 'EXECUTE')
                    """),
                    {"role": role},
                )
            assert not connection.scalar(
                text(
                    "SELECT has_schema_privilege('carepulse_policy_seed', 'public', 'CREATE')"
                )
            )
            for table in ("staff_accounts", "clinic_policy_heads"):
                assert connection.scalar(
                    text(
                        "SELECT has_table_privilege('carepulse_policy_seed', :table, 'SELECT')"
                    ),
                    {"table": f"public.{table}"},
                )
            for privilege in ("SELECT", "INSERT"):
                assert connection.scalar(
                    text(
                        "SELECT has_table_privilege('carepulse_policy_seed', 'public.doctor_controls', :privilege)"
                    ),
                    {"privilege": privilege},
                )
            assert not connection.scalar(
                text(
                    "SELECT has_table_privilege('carepulse_runtime', 'public.doctor_controls', 'INSERT')"
                )
            )
            for column in ("revision", "capacity_policy_version"):
                assert connection.scalar(
                    text("""
                        SELECT has_column_privilege(
                          'carepulse_runtime', 'public.doctor_controls', :column, 'UPDATE')
                    """),
                    {"column": column},
                )
            for column in ("organization_id", "doctor_staff_id"):
                assert not connection.scalar(
                    text("""
                        SELECT has_column_privilege(
                          'carepulse_runtime', 'public.doctor_controls', :column, 'UPDATE')
                    """),
                    {"column": column},
                )
            for column in (
                "arrival_sequence",
                "attested_by_staff_id",
                "attested_at",
                "closure_history_revision",
            ):
                assert not connection.scalar(
                    text("""
                        SELECT has_column_privilege(
                          'carepulse_runtime', 'public.queue_entries', :column, 'INSERT')
                    """),
                    {"column": column},
                )
            for column in (
                "organization_id",
                "queue_day_id",
                "appointment_id",
                "routine_rank",
            ):
                assert connection.scalar(
                    text("""
                        SELECT has_column_privilege(
                          'carepulse_runtime', 'public.queue_entries', :column, 'INSERT')
                    """),
                    {"column": column},
                )
            assert not connection.scalar(
                text("""
                SELECT has_column_privilege(
                  'carepulse_runtime', 'public.queue_days',
                  'revision_transaction_id', 'UPDATE')
            """)
            )
            for table in QUEUE_TABLES:
                for privilege in (
                    "SELECT",
                    "INSERT",
                    "UPDATE",
                    "DELETE",
                    "TRUNCATE",
                    "REFERENCES",
                    "TRIGGER",
                ):
                    assert not connection.scalar(
                        text(
                            "SELECT has_table_privilege('carepulse_operator', :table, :privilege)"
                        ),
                        {"table": f"public.{table}", "privilege": privilege},
                    )
    finally:
        migration.dispose()


def test_t160_populated_0038_capacity_and_queue_survive_numeric_upgrade(
    admin, empty_database
):
    before_revision = "0038_t159_duration_numeric"
    after_revision = "0039_t160_capacity_numeric"
    assert (
        runpy.run_path(
            str(ROOT / "db/migrations/versions/0039_t160_capacity_numeric.py")
        )["down_revision"]
        == before_revision
    )
    migration, runtime, tenant, doctor = _prepare(admin, empty_database)
    tables = (*PRESERVED_TABLES, *QUEUE_TABLES)
    try:
        _upgrade(migration, before_revision)
        with transaction(runtime, tenant) as connection:
            first = _appointment(connection, tenant, doctor)
            second = _appointment(connection, tenant, doctor)
            _arrive(connection, tenant, doctor, first)
            _arrive(connection, tenant, doctor, second)
            _policy_capacity(connection, tenant, capacity=2.0)

        def saved_rows(connection):
            return {
                table: connection.execute(
                    text(
                        f"SELECT row_to_json(t)::text FROM public.{table} t ORDER BY 1"
                    )
                )
                .scalars()
                .all()
                for table in tables
            }

        with migration.connect() as connection:
            before_rows = saved_rows(connection)
            before_grants = _predecessor_security(connection, tables)
            assert len(before_rows["queue_entries"]) == 2
            assert len(before_rows["clinic_policy_versions"]) > 0
            assert before_rows["idempotency_receipts"]
            assert all(
                row.relrowsecurity and row.relforcerowsecurity for row in before_grants
            )
            assert (
                connection.scalar(
                    text("""
                SELECT payload->>'capacity' FROM public.clinic_policy_versions
                WHERE organization_id=:tenant AND kind='CAPACITY' AND version=2
            """),
                    {"tenant": tenant},
                )
                == "2.0"
            )
        with (
            pytest.raises(DBAPIError) as old_guard,
            transaction(runtime, tenant) as conn,
        ):
            _lock(conn, tenant, doctor)
        assert old_guard.value.orig.sqlstate == "22P02"

        _upgrade(migration, after_revision)
        _upgrade(migration, after_revision)
        with migration.connect() as connection:
            assert (
                connection.scalar(
                    text("SELECT version_num FROM public.alembic_version")
                )
                == after_revision
            )
            assert saved_rows(connection) == before_rows
            assert _predecessor_security(connection, tables) == before_grants
            for signature in (
                "lock_doctor_admission_capacity(uuid,uuid,date,text)",
                "assert_confirmation_issued_scope(public.doctor_admission_confirmations)",
                "assert_admission_confirmation_final()",
            ):
                definition = connection.scalar(
                    text("SELECT pg_get_functiondef(CAST(:signature AS regprocedure))"),
                    {"signature": f"public.{signature}"},
                )
                assert "::numeric)::bigint" in definition

        # D-07 requires a Visit on commit; prove approval guards on the real
        # database and roll back this synthetic examination.
        with (
            pytest.raises(RuntimeError, match="rollback T160 simulation"),
            transaction(runtime, tenant) as connection,
        ):
            _doctor_context(connection, doctor)
            assert _lock(connection, tenant, doctor).configured_capacity == 2
            _simulated_exam(connection, tenant, doctor, first)
            approval = _issue(connection, tenant, doctor, second, "MAIN", DAY)
            connection.execute(
                text("SET CONSTRAINTS admission_confirmation_final IMMEDIATE")
            )
            assert (
                connection.scalar(
                    text("""
                    SELECT state FROM public.doctor_admission_confirmations
                    WHERE organization_id=:tenant AND id=:approval
                """),
                    {"tenant": tenant, "approval": approval},
                )
                == "ISSUED"
            )
            raise RuntimeError("rollback T160 simulation")
    finally:
        runtime.dispose()
        migration.dispose()

"""D-07 upgrades preserve compatible evidence and refuse unverifiable admissions."""

import runpy
import shutil
from pathlib import Path
from uuid import uuid4

import pytest
from alembic import command
from alembic.config import Config
from carepulse.appointments.persistence import persist_appointment
from carepulse.db import transaction
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from test_appointment_persistence import _activate_clinic, _draft, _patient, _query
from test_database import _create_empty_database, _engine
from test_database import admin as admin  # noqa: PLC0414 - fixture export
from test_database import disposable_project as disposable_project  # noqa: PLC0414 - fixture export
from test_database import empty_database as empty_database  # noqa: PLC0414

ROOT = Path(__file__).resolve().parents[2]
MIGRATION = ROOT / "db/migrations/versions/0021_d07_visits.py"
BEFORE = "0020_b07_manual_slot_weekly"
AFTER = "0021_d07_visits"
PREDECESSOR_TABLES = (
    "staff_accounts",
    "patients",
    "appointments",
    "appointment_history",
    "appointment_price_snapshots",
    "appointment_number_assignments",
    "appointment_day_counters",
    "idempotency_receipts",
)


def _upgrade(engine, revision, scripts=None):
    config = Config(str(ROOT / "alembic.ini"))
    if scripts:
        config.set_main_option("script_location", str(scripts))
    with engine.connect() as connection:
        config.attributes["connection"] = connection
        command.upgrade(config, revision)


def _prepare(admin, database):
    _create_empty_database(admin, database)
    migration = _engine("carepulse_migration", database)
    _upgrade(migration, BEFORE)
    with migration.begin() as connection:
        operator, tenant = uuid4(), uuid4()
        connection.execute(
            text("""
                INSERT INTO public.operator_identities
                    (id, credential_digest, issued_by_reference, issued_at)
                VALUES (:operator, :digest, 'synthetic-d07', clock_timestamp())
            """),
            {"operator": operator, "digest": f"synthetic:{operator}"},
        )
        connection.execute(
            text("""
                INSERT INTO public.organizations (id, branch, created_by_operator_id)
                VALUES (:tenant, 'Synthetic migration clinic', :operator)
            """),
            {"tenant": tenant, "operator": operator},
        )
        doctor = _activate_clinic(connection, tenant, operator)
    runtime = _engine("carepulse_runtime", database)
    return migration, runtime, tenant, doctor


def _predecessor_security(connection):
    return connection.execute(
        text("""
            SELECT c.relname, c.relrowsecurity, c.relforcerowsecurity,
                   r.rolname, a.attname, p.privilege,
                   has_table_privilege(r.oid, c.oid, p.privilege),
                   CASE WHEN p.privilege IN ('SELECT', 'INSERT', 'UPDATE', 'REFERENCES')
                        THEN has_column_privilege(r.oid, c.oid, a.attname, p.privilege)
                   END
            FROM pg_class c
            JOIN pg_attribute a ON a.attrelid=c.oid
                               AND a.attnum > 0 AND NOT a.attisdropped
            CROSS JOIN pg_roles r
            CROSS JOIN (VALUES ('SELECT'), ('INSERT'), ('UPDATE'), ('DELETE'),
                               ('TRUNCATE'), ('REFERENCES'), ('TRIGGER')) p(privilege)
            WHERE c.relnamespace = 'public'::regnamespace AND c.relkind='r'
              AND c.relname = ANY(:tables)
              AND r.rolname IN ('carepulse_runtime', 'carepulse_operator')
            ORDER BY c.relname, r.rolname, a.attname, p.privilege
        """),
        {"tables": list(PREDECESSOR_TABLES)},
    ).all()


def _advance_without_visit(connection, saved, tenant, doctor, target):
    appointment = saved["id"]
    transitions = [("Booked", "Waiting"), ("Waiting", "In Examination")]
    if target == "Cancelled":
        transitions.append(("In Examination", "Cancelled"))
    for revision, (before, after) in enumerate(transitions, start=1):
        connection.execute(
            text("""
                UPDATE public.appointments SET status=:status, revision=:revision
                WHERE id=:appointment
            """),
            {"status": after, "revision": revision, "appointment": appointment},
        )
        connection.execute(
            text("""
                INSERT INTO public.appointment_history (
                    id, organization_id, appointment_id, appointment_revision,
                    action, old_status, new_status, old_service_date, new_service_date,
                    old_number, new_number, actor_staff_id, actor_role,
                    reason, price_snapshot_appointment_id)
                SELECT :history, organization_id, id, :revision, 'STATUS',
                       :before, :after, service_date, service_date,
                       current_number, current_number, :doctor, 'DOCTOR_OWNER',
                       'Synthetic predecessor', id
                FROM public.appointments WHERE id=:appointment
            """),
            {
                "history": uuid4(),
                "revision": revision,
                "before": before,
                "after": after,
                "doctor": doctor,
                "appointment": appointment,
            },
        )


def test_version_and_fresh_upgrade_preserve_predecessor_records(admin, empty_database):
    assert runpy.run_path(str(MIGRATION))["down_revision"] == BEFORE
    migration, runtime, tenant, doctor = _prepare(admin, empty_database)
    try:
        with transaction(runtime, tenant) as connection:
            original = [
                persist_appointment(
                    connection,
                    _draft(
                        _patient(connection, tenant),
                        doctor,
                        status=status,
                    ),
                    _query(tenant, doctor),
                )
                for status in ("Booked", "Waiting", "No-show", "Cancelled")
            ]
            before = connection.execute(
                text("""
                    SELECT a.id, a.status, a.current_number, a.service_date,
                           h.id, h.action, s.net_egp
                    FROM public.appointments a
                    JOIN public.appointment_history h
                      ON h.organization_id=a.organization_id AND h.appointment_id=a.id
                    JOIN public.appointment_price_snapshots s
                      ON s.organization_id=a.organization_id AND s.appointment_id=a.id
                    ORDER BY a.current_number
                """),
            ).all()
            assert len(before) == len(original) == 4
            assignments = connection.execute(
                text("""
                    SELECT id, appointment_id, service_date, number
                    FROM public.appointment_number_assignments
                    ORDER BY number
                """)
            ).all()
            receipts = connection.execute(
                text("""
                    SELECT id, operation, idempotency_key, request_digest,
                           result_projection
                    FROM public.idempotency_receipts
                    WHERE organization_id=:tenant ORDER BY id
                """),
                {"tenant": tenant},
            ).all()
            assert len(assignments) == len(receipts) == 4
        with migration.connect() as connection:
            predecessor_security = _predecessor_security(connection)
            assert {row.relname for row in predecessor_security} == set(
                PREDECESSOR_TABLES
            )
            assert all(
                row.relrowsecurity and row.relforcerowsecurity
                for row in predecessor_security
            )
        _upgrade(migration, AFTER)
        _upgrade(migration, AFTER)
        with migration.connect() as connection:
            assert _predecessor_security(connection) == predecessor_security
            assert (
                connection.scalar(
                    text("SELECT version_num FROM public.alembic_version")
                )
                == AFTER
            )
            assert (
                connection.execute(
                    text("""
                    SELECT a.id, a.status, a.current_number, a.service_date,
                           h.id, h.action, s.net_egp
                    FROM public.appointments a
                    JOIN public.appointment_history h
                      ON h.organization_id=a.organization_id AND h.appointment_id=a.id
                    JOIN public.appointment_price_snapshots s
                      ON s.organization_id=a.organization_id AND s.appointment_id=a.id
                    ORDER BY a.current_number
                """),
                ).all()
                == before
            )
            assert (
                connection.execute(
                    text("""
                    SELECT id, appointment_id, service_date, number
                    FROM public.appointment_number_assignments ORDER BY number
                """)
                ).all()
                == assignments
            )
            assert (
                connection.execute(
                    text("""
                    SELECT id, operation, idempotency_key, request_digest,
                           result_projection
                    FROM public.idempotency_receipts
                    WHERE organization_id=:tenant ORDER BY id
                """),
                    {"tenant": tenant},
                ).all()
                == receipts
            )
            assert connection.scalar(text("SELECT count(*) FROM public.visits")) == 0
            assert (
                connection.scalar(
                    text("SELECT count(*) FROM public.clinical_revisions")
                )
                == 0
            )
        with transaction(runtime, tenant) as connection:
            assert (
                connection.scalar(
                    text("SELECT count(*) FROM public.appointment_number_assignments")
                )
                == 4
            )
    finally:
        runtime.dispose()
        migration.dispose()


@pytest.mark.parametrize("status", ["In Examination", "Completed", "Cancelled"])
def test_existing_unbacked_admission_refuses_upgrade_atomically(
    admin, empty_database, status
):
    migration, runtime, tenant, doctor = _prepare(admin, empty_database)
    try:
        with transaction(runtime, tenant) as connection:
            original = persist_appointment(
                connection,
                _draft(
                    _patient(connection, tenant),
                    doctor,
                    status="Booked" if status == "Cancelled" else status,
                ),
                _query(tenant, doctor),
            )
            if status == "Cancelled":
                _advance_without_visit(connection, original, tenant, doctor, status)
        with pytest.raises(DBAPIError) as denied:
            _upgrade(migration, AFTER)
        assert denied.value.orig.sqlstate == "23514"
        with migration.connect() as connection:
            assert (
                connection.scalar(
                    text("SELECT version_num FROM public.alembic_version")
                )
                == BEFORE
            )
            assert (
                connection.scalar(text("SELECT to_regclass('public.visits')")) is None
            )
            assert (
                connection.scalar(
                    text("SELECT to_regclass('public.clinical_revisions')")
                )
                is None
            )
            assert (
                connection.scalar(
                    text("SELECT count(*) FROM public.appointments WHERE id=:id"),
                    {"id": original["id"]},
                )
                == 1
            )
    finally:
        runtime.dispose()
        migration.dispose()


def test_failed_later_revision_rolls_back_and_keeps_d07(
    admin, empty_database, tmp_path
):
    _create_empty_database(admin, empty_database)
    migration = _engine("carepulse_migration", empty_database)
    try:
        _upgrade(migration, AFTER)
        scripts = tmp_path / "migrations"
        shutil.copytree(ROOT / "db/migrations", scripts)
        (scripts / "versions/0022_d07_failure.py").write_text(
            'from alembic import op\nrevision = "0022_d07_failure"\n'
            'down_revision = "0021_d07_visits"\n'
            "def upgrade():\n"
            '    op.execute("CREATE TABLE public.synthetic_d07_partial (id integer)")\n'
            '    op.execute("SELECT 1 / 0")\n'
        )
        with pytest.raises(DBAPIError) as denied:
            _upgrade(migration, "head", scripts)
        assert denied.value.orig.sqlstate == "22012"
        with migration.connect() as connection:
            assert (
                connection.scalar(
                    text("SELECT version_num FROM public.alembic_version")
                )
                == AFTER
            )
            assert (
                connection.scalar(
                    text("SELECT to_regclass('public.synthetic_d07_partial')")
                )
                is None
            )
    finally:
        migration.dispose()

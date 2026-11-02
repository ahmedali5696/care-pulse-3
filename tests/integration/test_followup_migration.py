"""D-08 predecessor, refusal, and atomic Alembic upgrade verification."""

import runpy
import shutil
from pathlib import Path
from uuid import UUID

import pytest
from carepulse.appointments.persistence import persist_appointment
from carepulse.db import transaction
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from test_appointment_persistence import _draft, _patient, _query
from test_database import _create_empty_database, _engine
from test_database import admin as admin  # noqa: PLC0414
from test_database import disposable_project as disposable_project  # noqa: PLC0414
from test_database import empty_database as empty_database  # noqa: PLC0414
from test_visit_migration import _prepare, _upgrade
from test_visit_persistence import _seed

ROOT = Path(__file__).resolve().parents[2]
MIGRATION = ROOT / "db/migrations/versions/0022_d08_followup_claims.py"
BEFORE = "0021_d07_visits"
AFTER = "0022_d08_followup_claims"


def _predecessor(admin, database):
    migration, runtime, tenant, doctor = _prepare(admin, database)
    _upgrade(migration, BEFORE)
    return migration, runtime, tenant, doctor


def test_predecessor_data_and_security_survive_compatible_upgrade(
    admin, empty_database
):
    assert runpy.run_path(str(MIGRATION))["down_revision"] == BEFORE
    migration, runtime, tenant, doctor = _predecessor(admin, empty_database)
    try:
        with transaction(runtime, tenant) as connection:
            source = _seed(connection, tenant, doctor, complete=True)
            saved = persist_appointment(
                connection,
                _draft(_patient(connection, tenant), doctor),
                _query(tenant, doctor),
            )
            pending = UUID(saved["id"])
            before = connection.execute(
                text("""
                    SELECT a.id, a.status, a.service_date, a.current_number,
                           s.net_egp, v.id AS visit_id
                    FROM public.appointments a
                    JOIN public.appointment_price_snapshots s
                      ON s.organization_id=a.organization_id AND s.appointment_id=a.id
                    LEFT JOIN public.visits v
                      ON v.organization_id=a.organization_id AND v.appointment_id=a.id
                    WHERE a.id IN (:source, :pending) ORDER BY a.id
                """),
                {"source": source, "pending": pending},
            ).all()
        _upgrade(migration, AFTER)
        _upgrade(migration, AFTER)
        with transaction(runtime, tenant) as connection:
            after = connection.execute(
                text("""
                    SELECT a.id, a.status, a.service_date, a.current_number,
                           s.net_egp, v.id AS visit_id
                    FROM public.appointments a
                    JOIN public.appointment_price_snapshots s
                      ON s.organization_id=a.organization_id AND s.appointment_id=a.id
                    LEFT JOIN public.visits v
                      ON v.organization_id=a.organization_id AND v.appointment_id=a.id
                    WHERE a.id IN (:source, :pending) ORDER BY a.id
                """),
                {"source": source, "pending": pending},
            ).all()
            assert after == before
            assert (
                connection.scalar(text("SELECT count(*) FROM public.followup_claims"))
                == 0
            )
        with migration.connect() as connection:
            assert (
                connection.scalar(
                    text("SELECT version_num FROM public.alembic_version")
                )
                == AFTER
            )
            for relation in ("followup_claims", "followup_extensions"):
                assert connection.execute(
                    text("""
                        SELECT relrowsecurity, relforcerowsecurity FROM pg_class
                        WHERE oid=to_regclass(:relation)
                    """),
                    {"relation": f"public.{relation}"},
                ).one() == (True, True)
                assert not connection.scalar(
                    text(
                        "SELECT has_table_privilege('carepulse_operator', :table, 'SELECT')"
                    ),
                    {"table": f"public.{relation}"},
                )
    finally:
        runtime.dispose()
        migration.dispose()


def test_incompatible_unpaired_predecessor_claim_refuses_atomically(
    admin, empty_database
):
    migration, runtime, tenant, doctor = _predecessor(admin, empty_database)
    try:
        with transaction(runtime, tenant) as connection:
            followup = persist_appointment(
                connection,
                _draft(
                    _patient(connection, tenant),
                    doctor,
                    service_type="FOLLOW_UP",
                    price_snapshot={
                        "entitlement_provenance": {"synthetic": "unpaired"}
                    },
                ),
                _query(tenant, doctor),
            )
        with pytest.raises(DBAPIError) as refused:
            _upgrade(migration, AFTER)
        assert refused.value.orig.sqlstate == "23514"
        with migration.connect() as connection:
            assert (
                connection.scalar(
                    text("SELECT version_num FROM public.alembic_version")
                )
                == BEFORE
            )
            assert (
                connection.scalar(text("SELECT to_regclass('public.followup_claims')"))
                is None
            )
        with transaction(runtime, tenant) as connection:
            assert (
                connection.scalar(
                    text("SELECT status FROM public.appointments WHERE id=:id"),
                    {"id": UUID(followup["id"])},
                )
                == "Booked"
            )
    finally:
        runtime.dispose()
        migration.dispose()


def test_fresh_and_failed_following_migration_keep_one_head(
    admin, empty_database, tmp_path
):
    _create_empty_database(admin, empty_database)
    migration = _engine("carepulse_migration", empty_database)
    try:
        _upgrade(migration, AFTER)
        scripts = tmp_path / "migrations"
        shutil.copytree(ROOT / "db/migrations", scripts)
        (scripts / "versions/0023_d08_failure.py").write_text(
            'from alembic import op\nrevision = "0023_d08_failure"\n'
            f'down_revision = "{AFTER}"\n'
            "def upgrade():\n"
            '    op.execute("CREATE TABLE public.synthetic_d08_partial (x integer)")\n'
            '    op.execute("SELECT 1 / 0")\n',
            encoding="utf-8",
        )
        with pytest.raises(DBAPIError) as failure:
            _upgrade(migration, "head", scripts)
        assert failure.value.orig.sqlstate == "22012"
        with migration.connect() as connection:
            assert (
                connection.scalar(
                    text("SELECT version_num FROM public.alembic_version")
                )
                == AFTER
            )
            assert (
                connection.scalar(
                    text("SELECT to_regclass('public.synthetic_d08_partial')")
                )
                is None
            )
    finally:
        migration.dispose()

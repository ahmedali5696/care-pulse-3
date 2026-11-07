"""D-11 populated predecessor and transactional migration proof on PostgreSQL."""

import runpy
import shutil
from uuid import uuid4

import pytest
from alembic import command
from alembic.config import Config
from carepulse.db import transaction
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from test_appointment_persistence import (
    _activate_clinic,
    _patient,
    _persist_with_status,
)
from test_database import ROOT, _create_empty_database, _engine
from test_database import admin as admin  # noqa: PLC0414 - fixture export
from test_database import disposable_project as disposable_project  # noqa: PLC0414
from test_database import empty_database as empty_database  # noqa: PLC0414
from test_day_review_persistence import DAY, NEXT_DAY, _cancel_admitted_visit, _review

MIGRATION = ROOT / "db/migrations/versions/0027_d11_day_review.py"
BEFORE = "0026_d07_completion_attestation"
AFTER = "0027_d11_day_review"
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
    "clinic_policy_heads",
    "clinic_policy_versions",
    "queue_days",
    "queue_entries",
    "doctor_controls",
    "visits",
    "clinical_revisions",
    "clinical_audit",
    "completion_attestations",
    "followup_source_state",
    "sensitive_audit",
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
    operator, tenant = uuid4(), uuid4()
    with migration.begin() as connection:
        connection.execute(
            text("""
                INSERT INTO public.operator_identities
                    (id, credential_digest, issued_by_reference, issued_at)
                VALUES (:operator, :digest, 'synthetic-d11', clock_timestamp())
            """),
            {"operator": operator, "digest": f"synthetic:{operator}"},
        )
        connection.execute(
            text("""
                INSERT INTO public.organizations (id, branch, created_by_operator_id)
                VALUES (:tenant, 'Synthetic D-11 predecessor', :operator)
            """),
            {"tenant": tenant, "operator": operator},
        )
        doctor = _activate_clinic(connection, tenant, operator)
    return migration, _engine("carepulse_runtime", database), tenant, doctor


def _fingerprint(connection):
    rows = {
        table: sorted(
            connection.scalars(
                text(f"SELECT row_to_json(t)::text FROM public.{table} t")
            ).all()
        )
        for table in PRESERVED_TABLES
    }
    security = connection.execute(
        text("""
            SELECT c.relname, c.relrowsecurity, c.relforcerowsecurity,
                   r.rolname, a.attname, p.privilege,
                   has_table_privilege(r.oid, c.oid, p.privilege),
                   CASE WHEN p.privilege IN ('SELECT','INSERT','UPDATE','REFERENCES')
                        THEN has_column_privilege(r.oid, c.oid, a.attname, p.privilege)
                   END
            FROM pg_class c
            JOIN pg_attribute a ON a.attrelid=c.oid
                 AND a.attnum > 0 AND NOT a.attisdropped
            CROSS JOIN pg_roles r
            CROSS JOIN (VALUES ('SELECT'),('INSERT'),('UPDATE'),('DELETE'),
                               ('TRUNCATE'),('REFERENCES'),('TRIGGER')) p(privilege)
            WHERE c.relnamespace='public'::regnamespace AND c.relkind='r'
              AND c.relname=ANY(:tables)
              AND r.rolname IN ('carepulse_runtime','carepulse_operator')
            ORDER BY c.relname, r.rolname, a.attname, p.privilege
        """),
        {"tables": list(PRESERVED_TABLES)},
    ).all()
    return rows, security


def test_populated_0026_upgrade_preserves_queue_visits_history_and_grants(
    admin,
    empty_database,
):
    migration, runtime, tenant, doctor = _predecessor(admin, empty_database)
    try:
        with transaction(runtime, tenant) as connection:
            for status in (
                "Booked",
                "Waiting",
                "In Examination",
                "Completed",
                "Cancelled",
                "No-show",
            ):
                _persist_with_status(
                    connection,
                    tenant,
                    _patient(connection, tenant),
                    doctor,
                    status,
                    service_date=DAY,
                )
            cancelled = _cancel_admitted_visit(connection, tenant, doctor)
            connection.execute(
                text(
                    "SELECT set_config('app.clinical_reason', 'Predecessor correction', true)"
                )
            )
            connection.execute(
                text(
                    "SELECT set_config('app.clinical_change_kind', 'CORRECTION', true)"
                )
            )
            connection.execute(
                text("""
                    UPDATE public.visits SET diagnosis='Corrected predecessor note'
                    WHERE organization_id=:tenant AND appointment_id=:appointment
                """),
                {"tenant": tenant, "appointment": cancelled},
            )
        with migration.connect() as connection:
            assert (
                connection.scalar(text("SELECT to_regclass('public.day_reviews')"))
                is None
            )
            before = _fingerprint(connection)
            assert len(before[0]["appointments"]) == 7
            assert len(before[0]["visits"]) == 3
            assert len(before[0]["queue_entries"]) >= 4
            assert len(before[0]["clinical_revisions"]) == 1

        _upgrade(migration, AFTER)
        _upgrade(migration, AFTER)
        with migration.connect() as connection:
            assert (
                connection.scalar(
                    text("SELECT version_num FROM public.alembic_version")
                )
                == AFTER
            )
            assert _fingerprint(connection) == before
            assert (
                connection.scalar(text("SELECT count(*) FROM public.day_reviews")) == 0
            )
            assert (
                connection.scalar(
                    text("SELECT count(*) FROM public.day_review_history")
                )
                == 0
            )

        with transaction(runtime, tenant) as connection:
            connection.execute(
                text("SELECT set_config('app.staff_id', :doctor, true)"),
                {"doctor": str(doctor)},
            )
            created = _review(
                connection, tenant, NEXT_DAY, 0, "FINAL_CLOSED", "New review"
            )
            assert created.revision == 1
        with migration.connect() as connection:
            assert (
                connection.scalar(
                    text("""
                    SELECT count(*) FROM public.day_review_history
                    WHERE organization_id=:tenant AND day_review_id=:id
                """),
                    {"tenant": tenant, "id": created.day_review_id},
                )
                == 1
            )
    finally:
        runtime.dispose()
        migration.dispose()


def test_fresh_repeat_upgrade_and_failed_successor_rolls_back(
    admin,
    empty_database,
    tmp_path,
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
            for table in ("day_reviews", "day_review_history"):
                assert connection.execute(
                    text("""
                        SELECT relrowsecurity, relforcerowsecurity FROM pg_class
                        WHERE oid=to_regclass(:table)
                    """),
                    {"table": f"public.{table}"},
                ).one() == (True, True)
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
                    assert connection.scalar(
                        text(
                            "SELECT has_table_privilege('carepulse_runtime', :table, :privilege)"
                        ),
                        {"table": f"public.{table}", "privilege": privilege},
                    ) is (privilege == "SELECT")
                    assert not connection.scalar(
                        text("""
                            SELECT EXISTS (
                                SELECT 1 FROM pg_class c,
                                    LATERAL aclexplode(coalesce(
                                        c.relacl, acldefault('r', c.relowner))) acl
                                WHERE c.oid=to_regclass(:table) AND acl.grantee=0
                                  AND acl.privilege_type=:privilege
                            )
                        """),
                        {"table": f"public.{table}", "privilege": privilege},
                    )
            signature = "public.record_day_review(uuid,date,bigint,text,text)"
            assert connection.execute(
                text("""
                    SELECT p.prosecdef, p.proconfig,
                           EXISTS (
                               SELECT 1 FROM aclexplode(coalesce(
                                   p.proacl, acldefault('f', p.proowner))) acl
                               WHERE acl.grantee=0 AND acl.privilege_type='EXECUTE'
                           )
                    FROM pg_proc p WHERE p.oid=CAST(:signature AS regprocedure)
                """),
                {"signature": signature},
            ).one() == (True, ["search_path=pg_catalog"], False)
            for role, allowed in (
                ("carepulse_runtime", True),
                ("carepulse_operator", False),
            ):
                assert (
                    connection.scalar(
                        text("""
                        SELECT has_function_privilege(
                            :role, CAST(:signature AS regprocedure), 'EXECUTE')
                    """),
                        {"role": role, "signature": signature},
                    )
                    is allowed
                )

        scripts = tmp_path / "migrations"
        shutil.copytree(ROOT / "db/migrations", scripts)
        (scripts / "versions/9999_d11_failure.py").write_text(
            'from alembic import op\nrevision = "9999_d11_failure"\n'
            f'down_revision = "{AFTER}"\n'
            "def upgrade():\n"
            '    op.execute("CREATE TABLE public.synthetic_d11_partial (id integer)")\n'
            '    op.execute("SELECT 1 / 0")\n',
            encoding="utf-8",
        )
        with pytest.raises(DBAPIError) as failed:
            _upgrade(migration, "9999_d11_failure", scripts)
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
                    text("SELECT to_regclass('public.synthetic_d11_partial')")
                )
                is None
            )
            assert (
                connection.scalar(text("SELECT to_regclass('public.day_reviews')"))
                == "day_reviews"
            )
    finally:
        migration.dispose()

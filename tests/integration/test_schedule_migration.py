"""B-07 schedule shape upgrade against disposable real PostgreSQL."""

import json
import runpy
import shutil
from datetime import date, time
from uuid import uuid4

import pytest
from alembic import command
from alembic.config import Config
from carepulse.appointments.persistence import persist_appointment
from carepulse.db import transaction
from carepulse.identity.contracts import ContractError
from carepulse.policies.schedule_service import load_current_schedule
from carepulse.policies.versions import append_version
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from test_appointment_persistence import _activate_clinic, _draft, _patient, _query
from test_database import ROOT, _create_empty_database, _engine, _upgrade
from test_database import admin as admin  # noqa: PLC0414 - fixture export
from test_database import disposable_project as disposable_project  # noqa: PLC0414
from test_database import empty_database as empty_database  # noqa: PLC0414

MIGRATION = ROOT / "db/migrations/versions/0020_b07_manual_slot_weekly.py"
TABLES = (
    "clinic_policy_versions",
    "clinic_policy_heads",
    "clinic_schedule_intervals",
    "clinic_schedule_holidays",
    "clinic_policy_previews",
    "appointments",
    "appointment_history",
    "appointment_price_snapshots",
    "appointment_number_assignments",
    "appointment_day_counters",
    "idempotency_receipts",
)
SCHEDULE = {
    "booking_mode": "OPEN",
    "duration_minutes": None,
    "holiday_booking_enabled": False,
    "routine_walk_in_enabled": False,
    "allow_overlap": False,
    "no_show_cutoff": {"enabled": False, "local_time": None},
}


def _revisions():
    migration = runpy.run_path(str(MIGRATION))
    assert migration["revision"] == MIGRATION.stem
    return migration["down_revision"], migration["revision"]


def _at_revision(engine, revision):
    config = Config(str(ROOT / "alembic.ini"))
    with engine.connect() as connection:
        config.attributes["connection"] = connection
        command.upgrade(config, revision)


def _shape_checks(connection):
    return connection.execute(
        text("""
        SELECT c.conname, pg_get_constraintdef(c.oid)
        FROM pg_constraint c
        JOIN pg_attribute kind ON kind.attrelid = c.conrelid
            AND kind.attname = 'interval_kind'
        JOIN pg_attribute weekday ON weekday.attrelid = c.conrelid
            AND weekday.attname = 'weekday'
        JOIN pg_attribute service_date ON service_date.attrelid = c.conrelid
            AND service_date.attname = 'service_date'
        WHERE c.conrelid = 'public.clinic_schedule_intervals'::regclass
          AND c.contype = 'c'
          AND c.conkey @> ARRAY[kind.attnum, weekday.attnum,
                               service_date.attnum]::smallint[]
    """)
    ).all()


def _fingerprint(connection):
    rows = {
        table: sorted(
            connection.scalars(
                text(f"SELECT row_to_json(t)::text FROM public.{table} t")
            ).all()
        )
        for table in TABLES
    }
    metadata = connection.execute(
        text("""
        SELECT relname, relrowsecurity, relforcerowsecurity, relacl::text
        FROM pg_class
        WHERE relnamespace = 'public'::regnamespace AND relkind = 'r'
        ORDER BY relname
    """)
    ).all()
    grants = connection.execute(
        text("""
        SELECT r.rolname, c.relname, p.privilege,
               has_table_privilege(r.oid, c.oid, p.privilege)
        FROM pg_roles r CROSS JOIN pg_class c
        CROSS JOIN (VALUES ('SELECT'), ('INSERT'), ('UPDATE'), ('DELETE'),
                           ('TRUNCATE'), ('REFERENCES'), ('TRIGGER')) p(privilege)
        WHERE r.rolname IN ('carepulse_runtime', 'carepulse_operator')
          AND c.relnamespace = 'public'::regnamespace AND c.relkind = 'r'
        ORDER BY r.rolname, c.relname, p.privilege
    """)
    ).all()
    return rows, metadata, grants


def _period(kind, weekday=None, service_date=None, start="09:00", end="10:00"):
    return {
        "interval_kind": kind,
        "weekday": weekday,
        "service_date": service_date,
        "starts_at": time.fromisoformat(start),
        "ends_at": time.fromisoformat(end),
    }


def test_populated_predecessor_preserves_rows_security_and_grants(
    admin, empty_database
):
    predecessor, head = _revisions()
    _create_empty_database(admin, empty_database)
    migration = _engine("carepulse_migration", empty_database)
    runtime = _engine("carepulse_runtime", empty_database)
    try:
        _at_revision(migration, predecessor)
        operator = uuid4()
        legacy_payloads = (
            {**SCHEDULE, "duration_minutes": 30.0},
            SCHEDULE,
            json.dumps(SCHEDULE).replace(
                '"duration_minutes": null', '"duration_minutes": 9007199254740993.0'
            ),
        )
        tenants = tuple(uuid4() for _ in legacy_payloads)
        with migration.begin() as connection:
            connection.execute(
                text("""
                INSERT INTO public.operator_identities
                    (id, credential_digest, issued_by_reference, issued_at)
                VALUES (:id, :digest, 'synthetic-ci', clock_timestamp())
            """),
                {"id": operator, "digest": f"synthetic:{operator}"},
            )
            doctors = []
            for tenant in tenants:
                connection.execute(
                    text("""
                    INSERT INTO public.organizations
                        (id, branch, created_by_operator_id)
                    VALUES (:tenant, :branch, :operator)
                """),
                    {"tenant": tenant, "branch": str(tenant), "operator": operator},
                )
                doctors.append(_activate_clinic(connection, tenant, operator))

        for tenant, doctor, payload in zip(
            tenants, doctors, legacy_payloads, strict=True
        ):
            with transaction(runtime, tenant) as connection:
                persist_appointment(
                    connection,
                    _draft(_patient(connection, tenant), doctor),
                    _query(tenant, doctor),
                )
                append_version(
                    connection,
                    str(tenant),
                    "SCHEDULE",
                    payload,
                    1,
                    intervals=[
                        _period(
                            "WORK",
                            weekday=1,
                            start="09:00:01.123456"
                            if tenant == tenants[1]
                            else "09:00",
                            end="17:00",
                        ),
                        _period("BREAK", weekday=1, start="12:00", end="13:00"),
                        _period(
                            "OVERRIDE_WORK",
                            service_date=date(2026, 11, 3),
                            start="10:00",
                            end="16:00",
                        ),
                        _period(
                            "MANUAL_SLOT",
                            service_date=date(2026, 11, 3),
                            start="11:00",
                            end="11:30",
                        ),
                    ],
                    holidays=[date(2026, 11, 4)],
                )
                connection.execute(
                    text("""
                    INSERT INTO public.clinic_policy_previews
                        (id, organization_id, policy_kind, policy_version,
                         policy_revision, actor_staff_id, service_date,
                         selected_refs, affected_refs, record_revisions,
                         candidate_payload)
                    VALUES (:id, :tenant, 'SCHEDULE', 2, 2, :doctor,
                            DATE '2026-11-03', '[]'::jsonb, '[]'::jsonb,
                            '{}'::jsonb, CAST(:payload AS jsonb))
                """),
                    {
                        "id": uuid4(),
                        "tenant": tenant,
                        "doctor": doctor,
                        "payload": json.dumps(SCHEDULE),
                    },
                )

        with migration.connect() as connection:
            before = _fingerprint(connection)
            old_checks = _shape_checks(connection)
            assert len(old_checks) == 1
            assert old_checks[0].conname != "clinic_schedule_interval_shape"
            assert len(before[0]["appointments"]) == 3
            assert len(before[0]["clinic_policy_previews"]) == 3
            assert len(before[0]["clinic_schedule_intervals"]) == 12
        _upgrade(migration)
        _upgrade(migration)
        with migration.connect() as connection:
            assert (
                connection.scalar(
                    text("SELECT version_num FROM public.alembic_version")
                )
                == head
            )
            assert _fingerprint(connection) == before
            checks = _shape_checks(connection)
            assert len(checks) == 1
            assert checks[0].conname == "clinic_schedule_interval_shape"
            assert "MANUAL_SLOT" in checks[0][1]
            assert (
                connection.scalar(
                    text("""
                SELECT count(*) FROM pg_constraint
                WHERE conrelid = 'public.clinic_schedule_intervals'::regclass
                  AND contype = 'c' AND conkey = ARRAY[
                      (SELECT attnum FROM pg_attribute
                       WHERE attrelid = 'public.clinic_schedule_intervals'::regclass
                         AND attname = 'interval_kind')
                  ]::smallint[]
                  AND pg_get_constraintdef(oid) LIKE '%OVERRIDE_WORK%'
            """)
                )
                == 1
            )
            metadata = {row.relname: row.relforcerowsecurity for row in before[1]}
            assert all(
                metadata[table] for table in TABLES if table != "idempotency_receipts"
            )
            assert all(
                not allowed
                for role, relation, _, allowed in before[2]
                if role == "carepulse_operator" and relation in TABLES
            )

        preserved_durations = (
            (tenants[0], 30, "30.0"),
            (tenants[2], 9007199254740993, "9007199254740993.0"),
        )
        for tenant, duration, original in preserved_durations:
            with transaction(runtime, tenant) as connection:
                assert (
                    load_current_schedule(connection, tenant).duration_minutes
                    == duration
                )
                assert (
                    connection.scalar(
                        text("""
                    SELECT payload->>'duration_minutes'
                    FROM public.clinic_policy_versions
                    WHERE organization_id=:tenant AND kind='SCHEDULE' AND version=2
                """),
                        {"tenant": tenant},
                    )
                    == original
                )

        with transaction(runtime, tenants[1]) as connection:
            with pytest.raises(ContractError) as conflict:
                load_current_schedule(connection, tenants[1])
            assert conflict.value.status_code == 409
            assert conflict.value.code is None
            assert connection.scalar(
                text("""
                SELECT starts_at FROM public.clinic_schedule_intervals
                WHERE organization_id=:tenant AND policy_version=2
                  AND interval_kind='WORK'
            """),
                {"tenant": tenants[1]},
            ) == time(9, 0, 1, 123456)

        for tenant, duration, original in preserved_durations:
            with transaction(runtime, tenant) as connection:
                assert (
                    append_version(
                        connection,
                        str(tenant),
                        "SCHEDULE",
                        {**SCHEDULE, "duration_minutes": duration},
                        2,
                        intervals=[
                            _period(
                                "MANUAL_SLOT", weekday=1, start="13:00", end="13:30"
                            ),
                            _period(
                                "MANUAL_SLOT",
                                service_date=date(2026, 11, 5),
                                start="13:30",
                                end="14:00",
                            ),
                        ],
                    )
                    == 3
                )
            with transaction(runtime, tenant) as connection:
                assert (
                    load_current_schedule(connection, tenant).duration_minutes
                    == duration
                )
                assert connection.execute(
                    text("""
                    SELECT version, payload->>'duration_minutes'
                    FROM public.clinic_policy_versions
                    WHERE organization_id=:tenant AND kind='SCHEDULE' AND version IN (2, 3)
                    ORDER BY version
                """),
                    {"tenant": tenant},
                ).all() == [(2, original), (3, str(duration))]

        invalid_shapes = [
            _period("WORK", weekday=1, service_date=date(2026, 11, 5)),
            _period("BREAK", weekday=None),
            _period("OVERRIDE_WORK", weekday=1),
            _period("OVERRIDE_WORK", service_date=date(2026, 11, 5), weekday=1),
            _period("MANUAL_SLOT", weekday=0),
            _period("MANUAL_SLOT", weekday=8),
            _period("MANUAL_SLOT"),
            _period("MANUAL_SLOT", weekday=1, service_date=date(2026, 11, 5)),
            _period("MANUAL_SLOT", service_date="infinity"),
        ]
        for interval in invalid_shapes:
            with (
                pytest.raises(DBAPIError) as denied,
                transaction(runtime, tenants[0]) as connection,
            ):
                append_version(
                    connection,
                    str(tenants[0]),
                    "SCHEDULE",
                    SCHEDULE,
                    3,
                    intervals=[interval],
                )
            assert denied.value.orig.sqlstate == "23514"
            assert (
                denied.value.orig.diag.constraint_name
                == "clinic_schedule_interval_shape"
            )
        with migration.connect() as connection:
            assert (
                connection.scalar(
                    text("""
                SELECT current_version FROM public.clinic_policy_heads
                WHERE organization_id = :tenant AND kind = 'SCHEDULE'
            """),
                    {"tenant": tenants[0]},
                )
                == 3
            )
    finally:
        runtime.dispose()
        migration.dispose()


def test_fresh_install_and_repeated_upgrade(admin, empty_database):
    _, head = _revisions()
    _create_empty_database(admin, empty_database)
    migration = _engine("carepulse_migration", empty_database)
    try:
        _upgrade(migration)
        with migration.connect() as connection:
            checks = _shape_checks(connection)
            assert len(checks) == 1
            assert checks[0].conname == "clinic_schedule_interval_shape"
        _upgrade(migration)
        with migration.connect() as connection:
            assert _shape_checks(connection) == checks
            assert (
                connection.scalar(
                    text("SELECT version_num FROM public.alembic_version")
                )
                == head
            )
    finally:
        migration.dispose()


@pytest.mark.parametrize("unexpected", ["definition", "duplicate"])
def test_unexpected_prior_shape_refuses_upgrade_without_changing_head(
    admin, empty_database, unexpected
):
    predecessor, _ = _revisions()
    _create_empty_database(admin, empty_database)
    migration = _engine("carepulse_migration", empty_database)
    try:
        _at_revision(migration, predecessor)
        with migration.begin() as connection:
            if unexpected == "definition":
                old_name = _shape_checks(connection)[0].conname
                identifier = connection.dialect.identifier_preparer.quote(old_name)
                connection.exec_driver_sql(
                    f"ALTER TABLE public.clinic_schedule_intervals DROP CONSTRAINT {identifier}"
                )
            connection.exec_driver_sql("""
                ALTER TABLE public.clinic_schedule_intervals
                ADD CONSTRAINT synthetic_unexpected_shape
                CHECK (interval_kind <> 'NEVER' OR weekday IS NOT NULL
                       OR service_date IS NOT NULL)
            """)
            unsafe_checks = _shape_checks(connection)
            assert len(unsafe_checks) == (1 if unexpected == "definition" else 2)
        with pytest.raises(DBAPIError) as rejected:
            _upgrade(migration)
        assert rejected.value.orig.sqlstate == "23514"
        with migration.connect() as connection:
            assert (
                connection.scalar(
                    text("SELECT version_num FROM public.alembic_version")
                )
                == predecessor
            )
            assert _shape_checks(connection) == unsafe_checks
    finally:
        migration.dispose()


def test_failed_following_revision_rolls_back_objects_and_version(
    admin, empty_database, tmp_path
):
    _, head = _revisions()
    _create_empty_database(admin, empty_database)
    migration = _engine("carepulse_migration", empty_database)
    scripts = tmp_path / "migrations"
    shutil.copytree(ROOT / "db/migrations", scripts)
    (scripts / "versions/9999_schedule_failure.py").write_text(
        'from alembic import op\nrevision = "9999_schedule_failure"\n'
        f'down_revision = "{head}"\n'
        "def upgrade():\n"
        '    op.execute("CREATE TABLE public.synthetic_schedule_partial (id integer)")\n'
        '    op.execute("SELECT 1 / 0")\n'
    )
    try:
        _upgrade(migration)
        with pytest.raises(DBAPIError) as failed:
            _upgrade(migration, scripts)
        assert failed.value.orig.sqlstate == "22012"
        with migration.connect() as connection:
            assert (
                connection.scalar(
                    text("SELECT version_num FROM public.alembic_version")
                )
                == head
            )
            assert (
                connection.scalar(
                    text("SELECT to_regclass('public.synthetic_schedule_partial')")
                )
                is None
            )
    finally:
        migration.dispose()

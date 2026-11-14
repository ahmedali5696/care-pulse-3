"""F-04: actual PostgreSQL evidence, only in the disposable Compose CI project."""

import json
import os
import re
import shutil
from asyncio import CancelledError
from contextlib import nullcontext
from dataclasses import replace
from pathlib import Path
from uuid import uuid4

import psycopg
import pytest
from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory
from carepulse.db import create_db_engine
from carepulse.identity.authorization import protected_transaction
from psycopg import sql
from runtime_identity_fixture import registered_test_request
from sqlalchemy import URL, create_engine, text
from sqlalchemy.exc import DBAPIError, OperationalError
from test_request_boundary import _signed
from test_request_boundary import boundary_data as boundary_data  # noqa: PLC0414

ROOT = Path(__file__).resolve().parents[2]
REVISION = "0040_t173_numeric_money"


@pytest.fixture(scope="module", autouse=True)
def disposable_project():
    if os.getenv("CI") != "true":
        pytest.skip("Real PostgreSQL checks run only in disposable Compose CI")
    project = os.environ["CP_CI_PROJECT"]
    assert re.fullmatch(r"carepulse-ci-[0-9]+-[0-9]+", project)
    config = json.loads(Path(os.environ["CP_COMPOSE_CONFIG_PATH"]).read_text())
    assert config["name"] == project
    assert config["services"]["db"]["environment"]["POSTGRES_DB"] == "carepulse"
    assert not config["services"]["db"].get("ports")
    return project


def _password(role):
    filename = {
        "carepulse_bootstrap": "postgres_password",
        "carepulse_runtime": "runtime_db_password",
        "carepulse_auth": "auth_db_password",
        "carepulse_migration": "migration_password",
        "carepulse_operator": "operator_password",
    }[role]
    return Path(f"/run/secrets/{filename}").read_text().strip()


def _engine(role, database="carepulse"):
    return create_engine(
        URL.create(
            "postgresql+psycopg",
            username=role,
            password=_password(role),
            host="db",
            database=database,
        ),
        hide_parameters=True,
    )


def _upgrade(engine, script_location=None):
    config = Config(str(ROOT / "alembic.ini"))
    if script_location is not None:
        config.set_main_option("script_location", str(script_location))
    with engine.connect() as connection:
        config.attributes["connection"] = connection
        command.upgrade(config, "head")


@pytest.fixture
def admin():
    with psycopg.connect(
        host="db",
        dbname="postgres",
        user="carepulse_bootstrap",
        password=_password("carepulse_bootstrap"),
        autocommit=True,
    ) as connection:
        yield connection


@pytest.fixture
def empty_database(admin, disposable_project):
    name = disposable_project.replace("-", "_") + "_f04_" + uuid4().hex[:8]
    assert len(name) <= 63
    try:
        yield name
    finally:
        admin.execute(
            sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(
                sql.Identifier(name)
            )
        )


def _create_empty_database(admin, name):
    admin.execute(
        sql.SQL("CREATE DATABASE {} OWNER carepulse_migration").format(
            sql.Identifier(name)
        )
    )
    with psycopg.connect(
        host="db",
        dbname=name,
        user="carepulse_migration",
        password=_password("carepulse_migration"),
    ) as connection:
        connection.execute(
            sql.SQL("REVOKE ALL ON DATABASE {} FROM PUBLIC").format(
                sql.Identifier(name)
            )
        )
        connection.execute("REVOKE ALL ON SCHEMA public FROM PUBLIC")
        connection.execute(
            sql.SQL("GRANT CONNECT ON DATABASE {} TO carepulse_runtime").format(
                sql.Identifier(name)
            )
        )
        connection.execute(
            sql.SQL("GRANT CONNECT ON DATABASE {} TO carepulse_auth").format(
                sql.Identifier(name)
            )
        )
        connection.execute(
            sql.SQL("GRANT CONNECT ON DATABASE {} TO carepulse_operator").format(
                sql.Identifier(name)
            )
        )
        connection.execute("GRANT USAGE ON SCHEMA public TO carepulse_runtime")
        connection.execute("GRANT USAGE ON SCHEMA public TO carepulse_auth")


def test_organization_schema_owned_by_migration_role(role_engine):
    engine = role_engine("carepulse_api")
    expected_head = ScriptDirectory(str(ROOT / "db/migrations")).get_current_head()
    try:
        with engine.connect() as connection:
            assert connection.scalar(text("SELECT session_user")) == "carepulse_api"
            assert (
                connection.scalar(
                    text("SELECT version_num FROM public.alembic_version")
                )
                == expected_head
            )
            owned_tables = dict(
                connection.execute(
                    text(
                        "SELECT tablename, tableowner FROM pg_tables WHERE schemaname = 'public'"
                    )
                ).all()
            )
            assert {
                "alembic_version",
                "operator_identities",
                "organizations",
                "patients",
                "clinic_policy_versions",
                "clinic_policy_heads",
                "clinic_schedule_intervals",
                "clinic_schedule_holidays",
                "clinic_policy_previews",
                "appointments",
                "appointment_price_snapshots",
                "appointment_day_counters",
                "appointment_number_assignments",
                "appointment_history",
                "visits",
                "clinical_revisions",
                "followup_claims",
                "followup_extensions",
                "queue_days",
                "queue_entries",
                "doctor_controls",
                "doctor_admission_confirmations",
                "day_reviews",
                "day_review_history",
            } <= owned_tables.keys()
            assert set(owned_tables.values()) == {"carepulse_migration"}
            assert (
                connection.scalar(
                    text(
                        "SELECT count(*) FROM pg_constraint WHERE conrelid = 'public.alembic_version'::regclass AND contype = 'p'"
                    )
                )
                == 1
            )
    finally:
        engine.dispose()


def test_empty_database_forward_only_boundary_and_repeated_upgrade(
    admin, empty_database, role_engine
):
    from test_f01_grants_migration import _bootstrap

    expected_head = ScriptDirectory(str(ROOT / "db/migrations")).get_current_head()
    api_url = role_engine("carepulse_api").url.set(database=empty_database)
    for cycle in range(2):
        if cycle:
            admin.execute(
                sql.SQL("DROP DATABASE {} WITH (FORCE)").format(
                    sql.Identifier(empty_database)
                )
            )
        _bootstrap(admin, empty_database)
        admin.execute(
            sql.SQL("GRANT CONNECT ON DATABASE {} TO carepulse_api").format(
                sql.Identifier(empty_database)
            )
        )
        engine = _engine("carepulse_migration", empty_database)
        try:
            _upgrade(engine)
            _upgrade(engine)
            config = Config(str(ROOT / "alembic.ini"))
            with (
                pytest.raises(RuntimeError, match="forward-only"),
                engine.connect() as connection,
            ):
                config.attributes["connection"] = connection
                command.downgrade(config, "0001_foundation")
            with engine.connect() as connection:
                assert (
                    connection.scalar(
                        text("SELECT version_num FROM public.alembic_version")
                    )
                    == expected_head
                )
                assert (
                    connection.scalar(
                        text("SELECT to_regclass('public.recovery_native_effects')")
                    )
                    == "recovery_native_effects"
                )
            _upgrade(engine)
        finally:
            engine.dispose()
        runtime = create_engine(api_url, hide_parameters=True)
        try:
            with runtime.connect() as connection:
                assert connection.scalar(text("SELECT session_user")) == "carepulse_api"
                assert (
                    connection.scalar(
                        text("SELECT version_num FROM public.alembic_version")
                    )
                    == expected_head
                )
        finally:
            runtime.dispose()


def test_failed_isolated_migration_rolls_back_objects_and_version(
    admin, empty_database, tmp_path
):
    from test_f01_grants_migration import _bootstrap

    expected_head = ScriptDirectory(str(ROOT / "db/migrations")).get_current_head()
    _bootstrap(admin, empty_database)
    scripts = tmp_path / "migrations"
    shutil.copytree(ROOT / "db/migrations", scripts)
    (scripts / "versions/9999_f04_failure.py").write_text(
        'from alembic import op\nrevision = "9999_f04_failure"\n'
        f'down_revision = "{expected_head}"\n'
        "def upgrade():\n"
        '    op.execute("CREATE TABLE public.synthetic_partial (value integer)")\n'
        '    op.execute("SELECT 1 / 0")\n'
    )
    engine = _engine("carepulse_migration", empty_database)
    try:
        _upgrade(engine)
        with pytest.raises(DBAPIError) as failed:
            _upgrade(engine, scripts)
        assert failed.value.orig.sqlstate == "22012"
        with engine.connect() as connection:
            assert (
                connection.scalar(
                    text("SELECT to_regclass('public.synthetic_partial')")
                )
                is None
            )
            assert (
                connection.scalar(
                    text("SELECT version_num FROM public.alembic_version")
                )
                == expected_head
            )
    finally:
        engine.dispose()


SERVICE_ROLES = (
    "carepulse_api",
    "carepulse_gate",
    "carepulse_auth",
    "carepulse_operator",
    "carepulse_sender",
)


@pytest.mark.parametrize("role", ("carepulse_runtime", *SERVICE_ROLES))
def test_restricted_roles_have_no_ownership_or_privileged_membership(role, role_engine):
    retired = role == "carepulse_runtime"
    engine = role_engine("carepulse_api" if retired else role)
    try:
        with engine.connect() as connection:
            assert connection.scalar(text("SELECT session_user")) == (
                "carepulse_api" if retired else role
            )
            assert connection.execute(
                text(
                    "SELECT rolsuper, rolcreatedb, rolcreaterole, rolreplication, rolbypassrls, rolinherit, rolcanlogin "
                    "FROM pg_roles WHERE rolname = :role"
                ),
                {"role": role},
            ).one() == (False,) * 6 + (not retired,)
            assert (
                connection.scalar(
                    text(
                        "SELECT count(*) FROM pg_auth_members WHERE member = (SELECT oid FROM pg_roles WHERE rolname = :role)"
                    ),
                    {"role": role},
                )
                == 0
            )
            assert (
                connection.scalar(
                    text(
                        "SELECT count(*) FROM pg_database WHERE datdba = (SELECT oid FROM pg_roles WHERE rolname = :role)"
                    ),
                    {"role": role},
                )
                == 0
            )
            assert (
                connection.scalar(
                    text(
                        "SELECT count(*) FROM pg_namespace WHERE nspowner = (SELECT oid FROM pg_roles WHERE rolname = :role)"
                    ),
                    {"role": role},
                )
                == 0
            )
    finally:
        engine.dispose()
    if retired:
        legacy = role_engine(role)
        try:
            with pytest.raises(OperationalError):
                legacy.connect()
        finally:
            legacy.dispose()


@pytest.mark.parametrize("role", SERVICE_ROLES)
@pytest.mark.parametrize(
    "statement",
    [
        "CREATE TABLE public.synthetic_forbidden (value integer)",
        "CREATE SCHEMA synthetic_forbidden",
        "CREATE TEMP TABLE synthetic_forbidden (value integer)",
        "ALTER TABLE public.alembic_version ADD COLUMN forbidden integer",
        "UPDATE public.alembic_version SET version_num = 'forbidden'",
        "TRUNCATE public.alembic_version",
        "CREATE ROLE synthetic_forbidden",
        "ALTER ROLE CURRENT_USER SUPERUSER",
        "SET ROLE carepulse_migration",
        "SET ROLE carepulse_bootstrap",
        "SET ROLE carepulse_identity_owner",
        "SET ROLE carepulse_runtime",
    ],
)
def test_runtime_and_operator_cannot_ddl_write_version_or_escalate(
    role, statement, role_engine
):
    engine = role_engine(role)
    try:
        with engine.begin() as connection:
            assert connection.scalar(text("SELECT session_user")) == role
            with pytest.raises(DBAPIError) as denied, connection.begin_nested():
                connection.execute(text(statement))
            assert denied.value.orig.sqlstate == "42501"
    finally:
        engine.dispose()


def test_operator_has_no_table_access():
    engine = _engine("carepulse_operator")
    try:
        with pytest.raises(DBAPIError) as denied, engine.connect() as connection:
            connection.execute(text("SELECT * FROM public.alembic_version"))
        assert denied.value.orig.sqlstate == "42501"
    finally:
        engine.dispose()


@pytest.mark.parametrize("outcome", ["commit", "rollback", "sql_error", "cancellation"])
def test_reused_runtime_connection_has_no_prior_authority(outcome, boundary_data):
    config = replace(
        boundary_data.config, db_password=boundary_data.runtime.url.password
    )
    engine = create_db_engine(config)
    identity, body, _ = _signed(boundary_data)
    expected = {
        "commit": nullcontext(),
        "rollback": pytest.raises(RuntimeError, match="Synthetic rollback"),
        "sql_error": pytest.raises(DBAPIError),
        "cancellation": pytest.raises(CancelledError, match="Synthetic cancellation"),
    }[outcome]
    try:
        with (
            registered_test_request(boundary_data, identity, body),
            expected as failed,
            protected_transaction(
                engine, identity, config.policy.session_idle_minutes
            ) as (connection, actor),
        ):
            backend_pid = connection.scalar(text("SELECT pg_backend_pid()"))
            assert connection.scalar(text("SELECT session_user")) == "carepulse_api"
            assert actor.organization_id == boundary_data.tenant
            # A legacy GUC must not change the verified tenant or survive reuse.
            assert connection.scalar(
                text("SELECT set_config('app.organization_id', :tenant, true)"),
                {"tenant": str(boundary_data.other)},
            ) == str(boundary_data.other)
            assert connection.execute(
                text("SELECT organization_id, staff_id FROM public.f01_context()")
            ).one() == (str(boundary_data.tenant), str(boundary_data.staff))
            assert connection.execute(
                text("SELECT id FROM public.organizations")
            ).scalars().all() == [boundary_data.tenant]
            if outcome == "rollback":
                raise RuntimeError("Synthetic rollback")
            if outcome == "cancellation":
                raise CancelledError("Synthetic cancellation")
            if outcome == "sql_error":
                connection.execute(text("SELECT 1 / 0"))
        if outcome == "sql_error":
            assert failed.value.orig.sqlstate == "22012"
        with engine.begin() as next_connection:
            assert (
                next_connection.scalar(text("SELECT pg_backend_pid()")) == backend_pid
            )
            assert (
                next_connection.scalar(
                    text("SELECT count(*) FROM public.f01_context()")
                )
                == 0
            )
            assert next_connection.scalar(text("SELECT public.f01_org()")) is None
            assert next_connection.scalar(text("SELECT public.f01_staff()")) is None
            assert next_connection.scalar(
                text("SELECT current_setting('app.organization_id', true)")
            ) in (None, "")
            assert (
                next_connection.scalar(
                    text("SELECT count(*) FROM public.organizations")
                )
                == 0
            )
            assert next_connection.scalar(text("SELECT 1")) == 1
    finally:
        engine.dispose()

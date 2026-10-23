"""F-04: actual PostgreSQL evidence, only in the disposable Compose CI project."""

import json
import os
import re
import shutil
from asyncio import CancelledError
from contextlib import nullcontext
from pathlib import Path
from uuid import UUID, uuid4

import psycopg
import pytest
from alembic import command
from alembic.config import Config
from carepulse.config import ServiceConfig, _load_policy
from carepulse.db import create_db_engine, transaction
from psycopg import sql
from sqlalchemy import URL, create_engine, text
from sqlalchemy.exc import DBAPIError

ROOT = Path(__file__).resolve().parents[2]
REVISION = "0003_identity"


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
            sql.SQL("GRANT CONNECT ON DATABASE {} TO carepulse_operator").format(
                sql.Identifier(name)
            )
        )
        connection.execute("GRANT USAGE ON SCHEMA public TO carepulse_runtime")


def test_organization_schema_owned_by_migration_role():
    engine = _engine("carepulse_runtime")
    try:
        with engine.connect() as connection:
            assert (
                connection.scalar(
                    text("SELECT version_num FROM public.alembic_version")
                )
                == REVISION
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


def test_empty_database_reset_and_repeated_upgrade(admin, empty_database):
    for cycle in range(2):
        if cycle:
            admin.execute(
                sql.SQL("DROP DATABASE {} WITH (FORCE)").format(
                    sql.Identifier(empty_database)
                )
            )
        _create_empty_database(admin, empty_database)
        engine = _engine("carepulse_migration", empty_database)
        try:
            _upgrade(engine)
            _upgrade(engine)
            config = Config(str(ROOT / "alembic.ini"))
            with engine.connect() as connection:
                config.attributes["connection"] = connection
                command.downgrade(config, "0001_foundation")
            with engine.connect() as connection:
                assert (
                    connection.scalar(
                        text("SELECT version_num FROM public.alembic_version")
                    )
                    == "0001_foundation"
                )
                assert (
                    connection.execute(
                        text("""
                    SELECT to_regclass('public.organizations'),
                           to_regclass('public.operator_identities'),
                           to_regclass('public.runtime_active_tenant_ids'),
                           to_regclass('public."user"'),
                           to_regclass('public."session"'),
                           to_regclass('public.staff_accounts'),
                           to_regclass('public.invitations'),
                           to_regclass('public.recovery_operations'),
                           to_regclass('public.auth_recovery_completions')
                """)
                    ).one()
                    == (None,) * 9
                )
            _upgrade(engine)
        finally:
            engine.dispose()
        runtime = _engine("carepulse_runtime", empty_database)
        try:
            with runtime.connect() as connection:
                assert (
                    connection.scalar(
                        text("SELECT version_num FROM public.alembic_version")
                    )
                    == REVISION
                )
        finally:
            runtime.dispose()


def test_failed_isolated_migration_rolls_back_objects_and_version(
    admin, empty_database, tmp_path
):
    _create_empty_database(admin, empty_database)
    scripts = tmp_path / "migrations"
    shutil.copytree(ROOT / "db/migrations", scripts)
    (scripts / "versions/0004_failure.py").write_text(
        'from alembic import op\nrevision = "0004_failure"\n'
        'down_revision = "0003_identity"\n'
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
                == REVISION
            )
    finally:
        engine.dispose()


@pytest.mark.parametrize("role", ["carepulse_runtime", "carepulse_operator"])
def test_restricted_roles_have_no_ownership_or_privileged_membership(role):
    engine = _engine(role)
    try:
        with engine.connect() as connection:
            assert (
                connection.execute(
                    text(
                        "SELECT rolsuper, rolcreatedb, rolcreaterole, rolreplication, rolbypassrls, rolinherit "
                        "FROM pg_roles WHERE rolname = current_user"
                    )
                ).one()
                == (False,) * 6
            )
            assert (
                connection.scalar(
                    text(
                        "SELECT count(*) FROM pg_auth_members WHERE member = (SELECT oid FROM pg_roles WHERE rolname = current_user)"
                    )
                )
                == 0
            )
            assert (
                connection.scalar(
                    text(
                        "SELECT count(*) FROM pg_database WHERE datdba = (SELECT oid FROM pg_roles WHERE rolname = current_user)"
                    )
                )
                == 0
            )
            assert (
                connection.scalar(
                    text(
                        "SELECT count(*) FROM pg_namespace WHERE nspowner = (SELECT oid FROM pg_roles WHERE rolname = current_user)"
                    )
                )
                == 0
            )
    finally:
        engine.dispose()


@pytest.mark.parametrize("role", ["carepulse_runtime", "carepulse_operator"])
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
    ],
)
def test_runtime_and_operator_cannot_ddl_write_version_or_escalate(role, statement):
    engine = _engine(role)
    try:
        with pytest.raises(DBAPIError) as denied, engine.begin() as connection:
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
def test_reused_runtime_connection_has_no_prior_authority(outcome):
    config = ServiceConfig(
        environment="test",
        policy=_load_policy(),
        db_host="db",
        db_port=5432,
        db_name="carepulse",
        db_user="carepulse_runtime",
        db_password=_password("carepulse_runtime"),
        file_encryption_key="synthetic-unused-by-database-check",
        service_key=None,
        mail_enabled=False,
        scan_enabled=False,
        mail=None,
        scan=None,
    )
    engine = create_db_engine(config)
    organization = UUID(int=1)
    expected = {
        "commit": nullcontext(),
        "rollback": pytest.raises(RuntimeError, match="Synthetic rollback"),
        "sql_error": pytest.raises(DBAPIError),
        "cancellation": pytest.raises(CancelledError, match="Synthetic cancellation"),
    }[outcome]
    try:
        with expected as failed, transaction(engine, organization) as connection:
            backend_pid = connection.scalar(text("SELECT pg_backend_pid()"))
            assert connection.scalar(
                text("SELECT current_setting('app.organization_id', true)")
            ) == str(organization)
            if outcome == "rollback":
                raise RuntimeError("Synthetic rollback")
            if outcome == "cancellation":
                raise CancelledError("Synthetic cancellation")
            if outcome == "sql_error":
                connection.execute(text("SELECT 1 / 0"))
        if outcome == "sql_error":
            assert failed.value.orig.sqlstate == "22012"
        with transaction(engine) as next_connection:
            assert (
                next_connection.scalar(text("SELECT pg_backend_pid()")) == backend_pid
            )
            assert next_connection.scalar(
                text("SELECT current_setting('app.organization_id', true)")
            ) in (None, "")
            assert next_connection.scalar(text("SELECT 1")) == 1
    finally:
        engine.dispose()

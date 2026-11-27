"""D-01 disposable PostgreSQL fixtures; no production database probes."""

import json
import os
import re
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from pathlib import Path
from threading import Barrier
from time import perf_counter
from uuid import uuid4

import pytest
from sqlalchemy import URL, create_engine, event, text
from sqlalchemy.engine import Engine

_BOUNDARY_PROFILE = pytest.StashKey[tuple[Counter, Counter]]()


def _boundary_statement_group(statement):
    # Only fixed labels leave this process; never emit SQL or parameters.
    for table in ("patients", "appointments", "queue_days", "queue_entries"):
        if statement == f"SELECT id::text FROM public.{table}":
            return f"unfiltered-{table}"
    for function in (
        "f01_context",
        "f01_register_proof",
        "f01_bind",
        "f01_activate",
        "f01_cleanup",
    ):
        if f"public.{function}(" in statement:
            return function
    if "SELECT to_jsonb(r)" in statement:
        return "snapshot"
    return "other"


@pytest.hookimpl(wrapper=True)
def pytest_runtest_protocol(item, nextitem):
    if item.path.name != "test_w3_boundary_security.py":
        return (yield)
    counts, seconds = Counter(), Counter()
    item.stash[_BOUNDARY_PROFILE] = counts, seconds

    def start(conn, cursor, statement, parameters, context, executemany):
        context.boundary_profile = _boundary_statement_group(statement), perf_counter()

    def finish(context):
        if context is not None and hasattr(context, "boundary_profile"):
            group, started = context.boundary_profile
            counts[group] += 1
            seconds[group] += perf_counter() - started
            del context.boundary_profile

    def complete(conn, cursor, statement, parameters, context, executemany):
        finish(context)

    def failed(context):
        finish(context.execution_context)

    listeners = (
        ("before_cursor_execute", start),
        ("after_cursor_execute", complete),
        ("handle_error", failed),
    )
    for name, listener in listeners:
        event.listen(Engine, name, listener)
    try:
        return (yield)
    finally:
        for name, listener in listeners:
            event.remove(Engine, name, listener)


@pytest.hookimpl(wrapper=True)
def pytest_runtest_makereport(item, call):
    report = yield
    profile = item.stash.get(_BOUNDARY_PROFILE, None)
    if profile is not None:
        counts, seconds = profile
        groups = " ".join(
            f"{group}={counts[group]}/{seconds[group]:.6f}s" for group in sorted(counts)
        )
        detail = f"BOUNDARY_PROFILE {item.originalname} {report.when} {report.duration:.6f}s {groups}"
        report.user_properties.append(("boundary_profile", detail))
        print(detail)
        counts.clear()
        seconds.clear()
    return report


@pytest.fixture(scope="module")
def disposable_ci():
    if os.getenv("CI") != "true":
        pytest.skip("Real PostgreSQL isolation requires disposable CI")
    project = os.environ["CP_CI_PROJECT"]
    assert re.fullmatch(r"carepulse-ci-[0-9]+-[0-9]+", project)
    config = json.loads(Path(os.environ["CP_COMPOSE_CONFIG_PATH"]).read_text())
    assert config["name"] == project
    assert config["services"]["db"]["environment"]["POSTGRES_DB"] == "carepulse"
    assert not config["services"]["db"].get("ports")


@pytest.fixture
def role_engine(disposable_ci):
    def connect_as(role):
        password_file = {
            "carepulse_migration": "migration_password",
            "carepulse_runtime": "runtime_db_password",
            "carepulse_api": "api_db_password",
            "carepulse_web": "web_db_password",
            "carepulse_worker": "worker_db_password",
            "carepulse_gate": "gate_db_password",
            "carepulse_auth": "auth_db_password",
            "carepulse_operator": "operator_password",
            "carepulse_sender": "sender_password",
        }[role]
        return create_engine(
            URL.create(
                "postgresql+psycopg",
                username=role,
                password=Path(f"/run/secrets/{password_file}").read_text().strip(),
                host="db",
                database="carepulse",
            ),
            hide_parameters=True,
        )

    return connect_as


@pytest.fixture
def synthetic_clock():
    return datetime(2026, 10, 23, 12, 0, tzinfo=UTC)


@pytest.fixture
def tenant_factory(role_engine, synthetic_clock):
    operator_id = uuid4()
    migration = role_engine("carepulse_migration")
    try:
        with migration.begin() as connection:
            connection.execute(
                text("""
                    INSERT INTO public.operator_identities
                        (id, credential_digest, issued_by_reference, issued_at)
                    VALUES (:id, :digest, 'synthetic-ci', :issued_at)
                """),
                {
                    "id": operator_id,
                    "digest": f"synthetic:{operator_id}",
                    "issued_at": synthetic_clock,
                },
            )
    finally:
        migration.dispose()

    def create_tenant(branch):
        organization_id = uuid4()
        migration = role_engine("carepulse_migration")
        try:
            with migration.begin() as connection:
                connection.execute(
                    text("""
                        INSERT INTO public.organizations (id, branch, created_by_operator_id)
                        VALUES (:id, :branch, :operator_id)
                    """),
                    {
                        "id": organization_id,
                        "branch": branch,
                        "operator_id": operator_id,
                    },
                )
        finally:
            migration.dispose()
        return organization_id

    return operator_id, create_tenant


@pytest.fixture
def two_transactions(role_engine):
    def run(role, operation):
        barrier = Barrier(2)
        engine = role_engine(role)

        def attempt():
            with engine.begin() as connection:
                barrier.wait(timeout=10)
                return operation(connection)

        try:
            with ThreadPoolExecutor(max_workers=2) as executor:
                futures = [executor.submit(attempt) for _ in range(2)]
                return [future.exception() or future.result() for future in futures]
        finally:
            engine.dispose()

    return run

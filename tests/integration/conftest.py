"""D-01 disposable PostgreSQL fixtures; no production database probes."""

import json
import os
import re
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from pathlib import Path
from threading import Barrier
from uuid import uuid4

import pytest
from sqlalchemy import URL, create_engine, text


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

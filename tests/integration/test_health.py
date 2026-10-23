"""F-05 actual HTTP/DB failure and recovery checks in disposable Compose CI."""

import json
import os
import re
import time
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import urlopen

import psycopg
import pytest

BASES = {"api": "http://api:8000", "web": "http://web:3000"}


@pytest.fixture(scope="module", autouse=True)
def disposable_project():
    if os.getenv("CI") != "true":
        pytest.skip("Running-service health checks are CI-only")
    project = os.environ["CP_CI_PROJECT"]
    assert re.fullmatch(r"carepulse-ci-[0-9]+-[0-9]+", project)
    config = json.loads(Path(os.environ["CP_COMPOSE_CONFIG_PATH"]).read_text())
    assert config["name"] == project
    assert not config["services"]["db"].get("ports")


def _health(service, kind, expected):
    try:
        response = urlopen(f"{BASES[service]}/health/{kind}", timeout=6)
    except HTTPError as error:
        response = error
    with response:
        assert response.status == expected
        assert response.headers["Cache-Control"] == "no-store"
        body = json.loads(response.read(1024))
    status = "alive" if kind == "live" else "ready" if expected == 200 else "not_ready"
    if body != {"status": status}:
        pytest.fail("Health body violated the sanitized status contract", pytrace=False)


def _eventually(api_ready=200, web_ready=200):
    deadline = time.monotonic() + 30
    while True:
        try:
            for service, expected in (("api", api_ready), ("web", web_ready)):
                _health(service, "live", 200)
                _health(service, "ready", expected)
            return
        except (AssertionError, URLError, OSError):
            if time.monotonic() >= deadline:
                raise
            time.sleep(0.5)


def _admin():
    return psycopg.connect(
        host="db",
        dbname="carepulse",
        user="carepulse_bootstrap",
        password=Path("/run/secrets/postgres_password").read_text().strip(),
        autocommit=True,
    )


def test_running_service_state():
    scenario = os.environ["CP_HEALTH_SCENARIO"]
    expected = {
        "healthy": (200, 200),
        "database_down": (503, 503),
        "mail_down": (200, 503),
        "mail_disabled": (200, 200),
    }[scenario]
    _eventually(*expected)


def test_incompatible_revision_denies_then_recovers_without_schema_creation():
    with _admin() as admin:
        try:
            admin.execute(
                "UPDATE public.alembic_version SET version_num = %s",
                ("synthetic_incompatible",),
            )
            _eventually(503, 503)
        finally:
            admin.execute(
                "UPDATE public.alembic_version SET version_num = %s",
                ("0002_organizations",),
            )
        _eventually()
        assert set(
            admin.execute(
                "SELECT tablename FROM pg_tables WHERE schemaname = 'public'"
            ).fetchall()
        ) == {("alembic_version",), ("organizations",), ("operator_identities",)}


def test_blocked_database_query_times_out_while_processes_remain_live():
    with _admin() as admin, admin.transaction():
        admin.execute("LOCK TABLE public.alembic_version IN ACCESS EXCLUSIVE MODE")
        for service in BASES:
            _health(service, "live", 200)
            started = time.monotonic()
            _health(service, "ready", 503)
            assert time.monotonic() - started < 5.5
    _eventually()

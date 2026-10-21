"""F-02 CI-only Compose topology and disposable persistence checks.

The test target runs inside the private application/database networks. CI is
responsible for the distinct runner-host port probes and the web-container to
API probe; neither can be proven from this container's loopback interface.
"""

import json
import os
import re
import time
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import urlopen

import psycopg
import pytest


def _required_ci_value(name: str) -> str:
    if os.getenv("CI") != "true":
        pytest.skip("Disposable Compose integration is CI-only")
    value = os.getenv(name)
    if value:
        return value
    pytest.fail(f"CI topology probe missing {name}", pytrace=False)


def _ci_compose_config() -> dict:
    """Refuse a shared Compose project before testing writable persistence."""
    project = _required_ci_value("CP_CI_PROJECT")
    assert re.fullmatch(r"carepulse-ci-[0-9]+-[0-9]+", project)
    with Path(_required_ci_value("CP_COMPOSE_CONFIG_PATH")).open(
        encoding="utf-8"
    ) as file:
        config = json.load(file)
    assert config["name"] == project
    return config


def _network_names(service: dict) -> set[str]:
    return set(service.get("networks", ()))


def _volume_targets(service: dict) -> dict[str, str]:
    return {
        volume["target"]: volume.get("source", "")
        for volume in service.get("volumes", ())
        if volume.get("type") == "volume"
    }


def test_resolved_compose_topology() -> None:
    """Read the CI-rendered JSON config without printing its secret paths."""
    config = _ci_compose_config()

    services = config["services"]
    assert set(services) == {"web", "api", "worker", "db", "mail", "scan", "test"}
    web_ports = services["web"]["ports"]
    assert len(web_ports) == 1
    assert web_ports[0]["host_ip"] == "127.0.0.1"
    assert int(web_ports[0]["published"]) == 3000
    assert int(web_ports[0]["target"]) == 3000
    assert all(not services[name].get("ports") for name in services if name != "web")

    assert config["networks"]["database"]["internal"] is True
    assert (
        config["networks"]["application"]["driver_opts"][
            "com.docker.network.bridge.enable_ip_masquerade"
        ]
        == "false"
    )
    assert config["networks"]["signature_updates"].get("internal", False) is False
    assert _network_names(services["db"]) == {"database"}
    assert _network_names(services["web"]) == {"application", "database"}
    assert _network_names(services["api"]) == {"application", "database"}
    assert _network_names(services["worker"]) == {"database"}
    assert _network_names(services["mail"]) == {"application"}
    assert _network_names(services["test"]) == {"application", "database"}
    assert _network_names(services["scan"]) == {"application", "signature_updates"}
    assert all(
        "signature_updates" not in _network_names(service)
        for name, service in services.items()
        if name != "scan"
    )
    assert services["mail"]["profiles"] == ["mail"]
    assert services["scan"]["profiles"] == ["scan"]
    assert services["test"]["profiles"] == ["test"]
    expected_secrets = {
        "test": {"postgres_password", "active_policy"},
        "web": {
            "active_policy",
            "runtime_db_password",
            "service_key",
            "auth_secret",
        },
        "api": {
            "active_policy",
            "runtime_db_password",
            "service_key",
            "file_encryption_key",
        },
        "worker": {"active_policy", "runtime_db_password", "file_encryption_key"},
    }
    for name, allowed in expected_secrets.items():
        assert {
            secret["source"] for secret in services[name].get("secrets", ())
        } == allowed
    for name in ("web", "api", "worker"):
        assert "postgres_password" not in {
            secret["source"] for secret in services[name].get("secrets", ())
        }
        assert not any(
            "operator" in key.lower() or "migration" in key.lower()
            for key in services[name].get("environment", {})
        )

    assert _volume_targets(services["db"])["/var/lib/postgresql/data"] == "db_data"
    for name in ("api", "worker", "test"):
        assert _volume_targets(services[name])["/srv/private-files"] == "private_files"
    assert "private_files" not in _volume_targets(services["web"]).values()


def test_private_api_reachable_from_test_network() -> None:
    """The separate web-container probe is executed on the CI runner."""
    _ci_compose_config()
    for attempt in range(12):
        try:
            with urlopen("http://api:8000/__foundation_network_probe__", timeout=2):
                pytest.fail("Unexpected public API endpoint", pytrace=False)
        except HTTPError as error:
            assert error.code == 404
            return
        except (URLError, TimeoutError):
            if attempt == 11:
                pytest.fail("Private API did not become reachable", pytrace=False)
            time.sleep(0.5)


def test_named_volumes_survive_restart() -> None:
    """Run with CP_PERSIST_PHASE=seed, restart db/api, then verify."""
    _ci_compose_config()
    phase = _required_ci_value("CP_PERSIST_PHASE")
    assert phase in {"seed", "verify"}
    file_marker = Path("/srv/private-files/.carepulse-foundation-ci-marker")
    marker = "synthetic-foundation-volume-marker"
    if phase == "seed":
        file_marker.write_text(marker, encoding="utf-8")
    else:
        assert file_marker.read_text(encoding="utf-8") == marker

    # The one-off test target alone gets the synthetic CI bootstrap password
    # file. Runtime web/API/worker never receive the credential from this test.
    password = (
        Path("/run/secrets/postgres_password").read_text(encoding="utf-8").strip()
    )
    for attempt in range(12):
        try:
            with (
                psycopg.connect(
                    host="db",
                    dbname="carepulse",
                    user="carepulse_bootstrap",
                    password=password,
                    connect_timeout=3,
                ) as connection,
                connection.cursor() as cursor,
            ):
                if phase == "seed":
                    cursor.execute(
                        "CREATE TABLE foundation_ci_volume_marker (value text NOT NULL)"
                    )
                    cursor.execute(
                        "INSERT INTO foundation_ci_volume_marker (value) VALUES (%s)",
                        (marker,),
                    )
                else:
                    cursor.execute("SELECT value FROM foundation_ci_volume_marker")
                    assert cursor.fetchone() == (marker,)
                    cursor.execute("DROP TABLE foundation_ci_volume_marker")
            if phase == "verify":
                file_marker.unlink()
            return
        except psycopg.OperationalError:
            if attempt == 11:
                pytest.fail(
                    "Disposable database unreachable after restart", pytrace=False
                )
            time.sleep(0.5)

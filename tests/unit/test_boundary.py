"""ASGI boundary checks without a second HTTP client/test framework."""

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace
from uuid import UUID

import pytest
from carepulse.config import ServiceConfig, _load_policy
from carepulse.identity.boundary import ClosedServiceBoundary


@pytest.fixture
def api_config(monkeypatch):
    monkeypatch.setenv(
        "CP_POLICY_FILE",
        str(Path(__file__).parents[1] / "fixtures/foundation-policy.json"),
    )
    return ServiceConfig(
        environment="test",
        policy=_load_policy(),
        db_host="db",
        db_port=5432,
        db_name="carepulse",
        db_user="carepulse_runtime",
        db_password="synthetic-db-password",
        file_encryption_key="synthetic-file-key",
        service_key="synthetic-key",
        mail_enabled=False,
        scan_enabled=False,
        mail=None,
        scan=None,
    )


@pytest.mark.parametrize(
    ("keys", "expected"),
    [
        ([], 401),
        ([b"forged"], 401),
        ([b"synthetic-key"], 403),
        ([b"synthetic-key", b"synthetic-key"], 401),
    ],
)
def test_protected_request_never_reaches_application(keys, expected, api_config):
    async def forbidden(scope, receive, send):
        pytest.fail("Closed boundary forwarded a protected request")

    messages = []

    async def send(message):
        messages.append(message)

    async def receive():
        pytest.fail("Denied request consumed a business payload")

    scope = {
        "type": "http",
        "path": "/v1/patients",
        "method": "POST",
        "headers": [(b"x-carepulse-service-key", key) for key in keys],
        "app": SimpleNamespace(state=SimpleNamespace(service_config=api_config)),
    }
    asyncio.run(ClosedServiceBoundary(forbidden)(scope, receive, send))
    assert messages[0]["status"] == expected
    assert (b"cache-control", b"no-store") in messages[0]["headers"]
    error = json.loads(messages[1]["body"])["error"]
    assert set(error) == {"code", "message", "request_id"}
    assert error["code"] == ("FORBIDDEN" if expected == 403 else "UNAUTHENTICATED")
    assert error["message"] == (
        "Operation unavailable" if expected == 403 else "Authentication required"
    )
    assert UUID(error["request_id"]).version == 4


@pytest.mark.parametrize("path", ["/health/live", "/unknown", "/v10/patients"])
def test_unprotected_paths_keep_normal_routing(path):
    calls = []

    async def application(scope, receive, send):
        calls.append(scope["path"])

    asyncio.run(
        ClosedServiceBoundary(application)({"type": "http", "path": path}, None, None)
    )
    assert calls == [path]

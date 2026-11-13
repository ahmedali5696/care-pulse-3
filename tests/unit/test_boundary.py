"""ASGI boundary checks without a second HTTP client/test framework."""

import asyncio
import hashlib
import hmac
import json
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from urllib.parse import unquote
from uuid import UUID

import pytest
from carepulse.config import ServiceConfig, _load_policy
from carepulse.identity.boundary import ClosedServiceBoundary, _read_body
from carepulse.identity.signing import (
    CanonicalEnvelope,
    canonical_path,
    canonical_payload,
    canonical_query,
    raw_body_sha256,
)


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
        signing_key_id="v1",
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


@pytest.mark.parametrize(
    "declared", [b"1048577", b"999999999999999999999", b"-1", b"1, 2"]
)
def test_declared_oversize_or_invalid_length_does_not_consume_stream(declared):
    async def forbidden_receive():
        pytest.fail("Rejected declared length consumed body")

    from carepulse.identity.boundary import BoundaryDenied

    with pytest.raises(BoundaryDenied) as denied:
        asyncio.run(
            _read_body({"headers": [(b"content-length", declared)]}, forbidden_receive)
        )
    assert denied.value.status == 422


@pytest.mark.parametrize("size", [1_048_576, 1_048_577])
def test_streamed_body_stops_at_one_mib(size):
    seen = 0

    async def receive():
        nonlocal seen
        seen += 1
        if seen > 2:
            pytest.fail("Oversized stream was read past its first excessive chunk")
        return {
            "type": "http.request",
            "body": b"a" * (1_048_575 if seen == 1 else size - 1_048_575),
            "more_body": seen == 1,
        }

    if size == 1_048_576:
        assert len(asyncio.run(_read_body({"headers": []}, receive))) == size
    else:
        from carepulse.identity.boundary import BoundaryDenied

        with pytest.raises(BoundaryDenied) as denied:
            asyncio.run(_read_body({"headers": []}, receive))
        assert denied.value.status == 422
    assert seen == 2


@pytest.fixture(scope="module")
def signing_vectors():
    return json.loads(
        (Path(__file__).parents[1] / "fixtures/signing-vectors.json").read_text(
            encoding="utf-8"
        )
    )


@pytest.mark.parametrize("index", [0, 1])
def test_signed_golden_vectors_preserve_raw_bytes_and_every_field(
    signing_vectors, index
):
    vector = signing_vectors["vectors"][index]
    envelope = CanonicalEnvelope(*vector["canonical_fields"])
    body = vector["raw_body"].encode("utf-8")
    assert raw_body_sha256(body) == envelope.body_sha256
    assert canonical_path(vector["raw_path"]) == envelope.path
    assert canonical_query(vector["raw_query"]) == envelope.query
    assert json.loads(canonical_payload(envelope)) == vector["canonical_fields"]
    signature = hmac.new(
        signing_vectors["test_only_key"].encode(),
        canonical_payload(envelope),
        hashlib.sha256,
    ).hexdigest()
    assert signature == vector["signature"]


@pytest.mark.parametrize(
    "path",
    [
        "/v1/%2Fadmin",
        "/v1/%5Cadmin",
        "/v1/%2E%2E/patients",
        "/v1/%252Fadmin",
        "/v1/%ZZ",
        "/v1//patients",
    ],
)
def test_encoded_route_changes_and_malformed_escapes_are_denied(path):
    with pytest.raises(ValueError):
        canonical_path(path)


def _signed_request(vector, api_config, changes=None):
    envelope = CanonicalEnvelope(*vector["canonical_fields"])
    field_names = (
        "protocol-version",
        "key-id",
        "service",
        "organization-id",
        "staff-id",
        "session-id",
        "role",
        "security-revision",
        "timestamp",
        "nonce",
        "purpose",
        "idempotency-key",
    )
    fields = (
        envelope.protocol_version,
        envelope.key_id,
        envelope.service,
        envelope.organization_id,
        envelope.staff_id,
        envelope.session_id,
        envelope.role,
        envelope.security_revision,
        envelope.timestamp,
        envelope.nonce,
        envelope.purpose,
        envelope.idempotency_key,
    )
    headers = {name: field.encode("utf-8") for name, field in zip(field_names, fields)}
    headers["signature"] = vector["signature"].encode("ascii")
    headers["service-key"] = api_config.service_key.encode("ascii")
    if changes:
        headers.update(changes)
    return {
        "type": "http",
        "path": unquote(vector["raw_path"]),
        "raw_path": vector["raw_path"].encode(),
        "query_string": vector["raw_query"].encode(),
        "method": envelope.method,
        "headers": [
            (f"x-carepulse-{name}".encode(), field) for name, field in headers.items()
        ],
        "app": SimpleNamespace(
            state=SimpleNamespace(service_config=api_config, db_engine=object())
        ),
    }


def _run_signed(scope, body, routes, when_ms):
    delivered = []
    calls = []

    async def authorized(request_scope, receive, send):
        calls.append(request_scope["carepulse.identity"])
        assert (
            request_scope["query_string"]
            == request_scope["carepulse.identity"].query.encode()
        )
        assert (
            request_scope["raw_path"]
            == request_scope["carepulse.identity"].path.encode()
        )
        assert (await receive())["body"] == body
        await send({"type": "http.response.start", "status": 204, "headers": []})
        await send({"type": "http.response.body", "body": b""})

    async def receive():
        return {"type": "http.request", "body": body, "more_body": False}

    async def send(message):
        delivered.append(message)

    with (
        patch("carepulse.identity.boundary.time_ns", return_value=when_ms * 1_000_000),
        patch("carepulse.identity.boundary.accept_verified_nonce") as nonce,
    ):
        asyncio.run(
            ClosedServiceBoundary(authorized, routes=routes)(scope, receive, send)
        )
    return delivered[0]["status"], calls, nonce.call_count


@pytest.mark.parametrize("index", [0, 1])
def test_verified_request_passes_exact_raw_body_and_consumes_one_nonce(
    signing_vectors, api_config, index
):
    vector = signing_vectors["vectors"][index]
    signed = CanonicalEnvelope(*vector["canonical_fields"])
    scope = _signed_request(vector, api_config)
    body = vector["raw_body"].encode("utf-8")
    routes = {(signed.method, signed.path): signed.purpose}
    status, calls, consumed = _run_signed(scope, body, routes, int(signed.timestamp))
    assert (status, calls, consumed) == (204, [signed], 1)


def test_only_configured_signing_key_id_is_accepted(signing_vectors, api_config):
    vector = signing_vectors["vectors"][0]
    configured_id = "rollover_key-2"
    config = replace(api_config, signing_key_id=configured_id)
    envelope = replace(
        CanonicalEnvelope(*vector["canonical_fields"]), key_id=configured_id
    )
    scope = _signed_request(
        vector,
        config,
        {
            "key-id": configured_id.encode(),
            "signature": hmac.new(
                config.service_key.encode(), canonical_payload(envelope), hashlib.sha256
            )
            .hexdigest()
            .encode(),
        },
    )
    routes = {(envelope.method, envelope.path): envelope.purpose}
    assert _run_signed(scope, b"", routes, int(envelope.timestamp)) == (
        204,
        [envelope],
        1,
    )
    # Even a correctly signed request is unknown when it names a different active ID.
    other_config = replace(api_config, signing_key_id="different-active-id")
    scope["app"].state.service_config = other_config
    assert _run_signed(scope, b"", routes, int(envelope.timestamp)) == (401, [], 0)
    scope["app"].state.service_config = config
    scope["headers"] = [
        header for header in scope["headers"] if header[0] != b"x-carepulse-key-id"
    ]
    assert _run_signed(scope, b"", routes, int(envelope.timestamp)) == (401, [], 0)


def test_duplicate_signed_header_and_forwarded_cookie_are_denied(
    signing_vectors, api_config
):
    vector = signing_vectors["vectors"][0]
    signed = CanonicalEnvelope(*vector["canonical_fields"])
    routes = {(signed.method, signed.path): signed.purpose}
    for extra_header in (
        (b"x-carepulse-staff-id", signed.staff_id.encode()),
        (b"cookie", b"synthetic-session-cookie"),
    ):
        scope = _signed_request(vector, api_config)
        scope["headers"].append(extra_header)
        assert _run_signed(scope, b"", routes, int(signed.timestamp)) == (401, [], 0)


def test_signed_request_cannot_change_body_or_resolved_route(
    signing_vectors, api_config
):
    vector = signing_vectors["vectors"][0]
    signed = CanonicalEnvelope(*vector["canonical_fields"])
    routes = {(signed.method, signed.path): signed.purpose}
    assert _run_signed(
        _signed_request(vector, api_config), b"changed", routes, int(signed.timestamp)
    ) == (401, [], 0)
    altered_scope = _signed_request(vector, api_config)
    altered_scope["path"] = "/v1/other"
    assert _run_signed(altered_scope, b"", routes, int(signed.timestamp)) == (
        401,
        [],
        0,
    )


@pytest.mark.parametrize(
    "header",
    [
        "key-id",
        "service",
        "role",
        "staff-id",
        "timestamp",
        "purpose",
        "signature",
        "service-key",
    ],
)
def test_tampered_identity_headers_are_denied_without_consuming_nonce(
    signing_vectors, api_config, header
):
    vector = signing_vectors["vectors"][0]
    signed = CanonicalEnvelope(*vector["canonical_fields"])
    scope = _signed_request(vector, api_config, {header: b"tampered"})
    status, calls, consumed = _run_signed(
        scope,
        b"",
        {(signed.method, signed.path): signed.purpose},
        int(signed.timestamp),
    )
    assert status == 401
    assert calls == []
    assert consumed == 0


@pytest.mark.parametrize(
    "age_delta_ms, expected", [(9999, 204), (10000, 401), (-15000, 204), (-15001, 401)]
)
def test_signed_freshness_and_skew_boundaries(
    signing_vectors, api_config, age_delta_ms, expected
):
    vector = signing_vectors["vectors"][0]
    signed = CanonicalEnvelope(*vector["canonical_fields"])
    config = api_config.policy
    # The test fixture supplies its own policy; map exact boundaries to it.
    actual_delta = (
        (config.signed_request_freshness_seconds * 1000 + age_delta_ms - 10000)
        if age_delta_ms >= 0
        else (-config.signed_request_max_skew_seconds * 1000 + age_delta_ms + 15000)
    )
    status, _, consumed = _run_signed(
        _signed_request(vector, api_config),
        b"",
        {(signed.method, signed.path): signed.purpose},
        int(signed.timestamp) + actual_delta,
    )
    assert status == expected
    assert consumed == int(expected == 204)

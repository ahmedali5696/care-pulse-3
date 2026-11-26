"""Signed, replay-resistant private BFF transport boundary."""

from collections.abc import Callable
from hashlib import sha256
from hmac import compare_digest, new
from re import Pattern
from time import time_ns
from urllib.parse import unquote

from sqlalchemy import text
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from starlette.types import ASGIApp, Receive, Scope, Send

from carepulse.compat.admission import (
    CompatAdmissionError,
    CompatRuntimeConfig,
    acquire_invocation_permit,
    check_pool_checkout,
    is_compat_enabled,
    release_invocation_permit,
)
from carepulse.identity.contracts import ContractError, error_response
from carepulse.identity.runtime_identity import RegisteredRequest, current_request
from carepulse.identity.signing import (
    CanonicalEnvelope,
    canonical_path,
    canonical_payload,
    canonical_query,
)

_HEADER_FIELDS = (
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
    "signature",
    "service-key",
    "db-key-id",
    "db-proof",
)
_EXPECTED_SERVICE = "web"
_MAX_MUTATION_BODY_BYTES = 1_048_576


class BoundaryDenied(Exception):
    def __init__(self, status: int) -> None:
        self.status = status


def _signed_headers(scope: Scope, secret: str, signing_key_id: str) -> dict[str, str]:
    headers: dict[str, str] = {}
    for raw_name, raw_value in scope.get("headers", ()):
        name = raw_name.lower()
        if name in (b"cookie", b"authorization"):
            raise BoundaryDenied(401)
        if not name.startswith(b"x-carepulse-"):
            continue
        field = name.removeprefix(b"x-carepulse-").decode("ascii", "strict")
        if field not in _HEADER_FIELDS or field in headers:
            raise BoundaryDenied(401)
        try:
            headers[field] = raw_value.decode("utf-8", "strict")
        except UnicodeDecodeError as invalid:
            raise BoundaryDenied(401) from invalid
    if set(headers) != set(_HEADER_FIELDS):
        if (
            len(headers) == 1
            and "service-key" in headers
            and compare_digest(headers["service-key"], secret)
        ):
            raise BoundaryDenied(403)
        raise BoundaryDenied(401)
    if not compare_digest(headers["service-key"], secret):
        raise BoundaryDenied(401)
    if headers["key-id"] != signing_key_id or headers["service"] != _EXPECTED_SERVICE:
        raise BoundaryDenied(401)
    return headers


async def _read_body(scope: Scope, receive: Receive) -> bytes:
    declared = [
        value
        for name, value in scope.get("headers", ())
        if name.lower() == b"content-length"
    ]
    if declared:
        length = declared[0].lstrip(b"0") or b"0"
        if (
            len(declared) != 1
            or not declared[0].isascii()
            or not declared[0].isdigit()
            or len(length) > 7
            or (len(length) == 7 and length > b"1048576")
        ):
            raise BoundaryDenied(422)
    body = bytearray()
    while True:
        message = await receive()
        if message["type"] != "http.request":
            raise BoundaryDenied(401)
        chunk = message.get("body", b"")
        if len(body) + len(chunk) > _MAX_MUTATION_BODY_BYTES:
            raise BoundaryDenied(422)
        body.extend(chunk)
        if not message.get("more_body", False):
            return bytes(body)


def _envelope(scope: Scope, headers: dict[str, str], body: bytes) -> CanonicalEnvelope:
    method = scope["method"]
    if method != method.upper():
        raise BoundaryDenied(401)
    path = canonical_path(scope.get("raw_path", scope["path"].encode("utf-8")))
    if scope["path"] != unquote(path):
        raise BoundaryDenied(401)
    query = canonical_query(scope.get("query_string", b""))
    return CanonicalEnvelope(
        headers["protocol-version"],
        headers["key-id"],
        headers["service"],
        method,
        path,
        query,
        sha256(body).hexdigest(),
        headers["organization-id"],
        headers["staff-id"],
        headers["session-id"],
        headers["role"],
        headers["security-revision"],
        headers["timestamp"],
        headers["nonce"],
        headers["purpose"],
        headers["idempotency-key"],
    )


class ClosedServiceBoundary:
    """Only an explicitly registered route/purpose reaches a domain handler."""

    def __init__(
        self,
        app: ASGIApp,
        *,
        routes: dict[tuple[str, str], str] | None = None,
        route_patterns: tuple[
            tuple[str, Pattern[str], str | Callable[[bytes], str]], ...
        ] = (),
    ) -> None:
        self.app = app
        self.routes = routes or {}
        self.route_patterns = route_patterns

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        path = scope.get("path", "")
        if scope["type"] != "http" or not (path == "/v1" or path.startswith("/v1/")):
            await self.app(scope, receive, send)
            return

        permit = None
        permit_conn = None
        if is_compat_enabled():
            try:
                compat_cfg = CompatRuntimeConfig.from_env("api")
                assert compat_cfg is not None
                app_state = scope["app"].state
                permit, permit_conn = acquire_invocation_permit(
                    app_state.db_engine,
                    config=compat_cfg,
                    expected_login="carepulse_api",
                    invocation_class="http",
                    record_attempt=True,
                )
                with app_state.gate_engine.connect() as gate_conn:
                    check_pool_checkout(
                        gate_conn,
                        config=compat_cfg,
                        expected_login="carepulse_gate",
                        expected_epoch=permit.owner_epoch,
                    )
                scope["carepulse.compat_permit"] = permit
            except (CompatAdmissionError, SQLAlchemyError, KeyError, AttributeError):
                if permit is not None and permit_conn is not None:
                    release_invocation_permit(permit_conn, permit)
                await self._deny(503, scope, receive, send)
                return

        try:
            envelope, payload, registered = await self._authenticate(scope, receive)
        except (KeyError, TypeError, ValueError, UnicodeError):
            if permit is not None and permit_conn is not None:
                release_invocation_permit(permit_conn, permit)
            await self._deny(401, scope, receive, send)
            return
        except BoundaryDenied as denied:
            if permit is not None and permit_conn is not None:
                release_invocation_permit(permit_conn, permit)
            await self._deny(denied.status, scope, receive, send)
            return

        scope["carepulse.identity"] = envelope
        # Downstream parsers must process exactly the target HMAC authorized.
        # Reordered duplicate parameters must not reinterpret a signed request.
        scope["query_string"] = envelope.query.encode("ascii")
        scope["raw_path"] = envelope.path.encode("ascii")
        delivered = False

        async def replay_body() -> dict:
            nonlocal delivered
            if not delivered:
                delivered = True
                return {"type": "http.request", "body": payload, "more_body": False}
            return {"type": "http.disconnect"}

        token = current_request.set(registered)
        try:
            await self.app(scope, replay_body, send)
        finally:
            current_request.reset(token)
            if permit is not None and permit_conn is not None:
                # Leave PENDING until the exact transaction/receipt is proven.
                release_invocation_permit(permit_conn, permit)

    async def _authenticate(
        self, scope: Scope, receive: Receive
    ) -> tuple[CanonicalEnvelope, bytes, RegisteredRequest]:
        config = scope["app"].state.service_config
        headers = _signed_headers(scope, config.service_key, config.signing_key_id)
        payload = await _read_body(scope, receive)
        envelope = _envelope(scope, headers, payload)
        mac = new(config.service_key.encode(), canonical_payload(envelope), sha256)
        if not compare_digest(headers["signature"], mac.hexdigest()):
            raise BoundaryDenied(401)
        signed_at_ms = int(envelope.timestamp)
        age_ms = time_ns() // 1_000_000 - signed_at_ms
        if (
            age_ms >= config.policy.signed_request_freshness_seconds * 1000
            or age_ms < -config.policy.signed_request_max_skew_seconds * 1000
        ):
            raise BoundaryDenied(401)
        permitted_purpose = self.routes.get((scope["method"], envelope.path))
        if permitted_purpose is None:
            permitted_purpose = next(
                (
                    purpose
                    for method, pattern, purpose in self.route_patterns
                    if method == scope["method"] and pattern.fullmatch(envelope.path)
                ),
                None,
            )
        if callable(permitted_purpose):
            try:
                permitted_purpose = permitted_purpose(payload)
            except ContractError as invalid:
                raise BoundaryDenied(invalid.status_code) from invalid
        if permitted_purpose != envelope.purpose:
            raise BoundaryDenied(403)
        try:
            if not headers["db-key-id"] or len(headers["db-proof"]) != 64:
                raise BoundaryDenied(401)
            try:
                proof = bytes.fromhex(headers["db-proof"])
            except ValueError as malformed:
                raise BoundaryDenied(401) from malformed
            if len(proof) != 32:
                raise BoundaryDenied(401)
            gate_engine = scope["app"].state.gate_engine
            with gate_engine.begin() as gate:
                request_id = gate.scalar(
                    text(
                        "SELECT public.f01_register_proof(:payload, :key, :proof, :body)"
                    ),
                    {
                        "payload": canonical_payload(envelope),
                        "key": headers["db-key-id"],
                        "proof": proof,
                        "body": payload,
                    },
                )
                if not isinstance(request_id, int):
                    raise BoundaryDenied(401)
        except IntegrityError as replay:
            raise BoundaryDenied(401) from replay
        except SQLAlchemyError as unavailable:
            if getattr(getattr(unavailable, "orig", None), "sqlstate", None) in (
                "42501",
                "22023",
            ):
                raise BoundaryDenied(401) from unavailable
            raise BoundaryDenied(503) from unavailable
        return envelope, payload, RegisteredRequest(gate_engine, request_id, envelope)

    @staticmethod
    async def _deny(status: int, scope: Scope, receive: Receive, send: Send) -> None:
        await error_response(status)(scope, receive, send)

"""Signed, replay-resistant private BFF transport boundary."""

from datetime import UTC, datetime
from hashlib import sha256
from hmac import compare_digest, new
from re import Pattern
from time import time_ns
from urllib.parse import unquote

from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from starlette.types import ASGIApp, Receive, Scope, Send

from carepulse.identity.contracts import error_response
from carepulse.identity.signing import (
    CanonicalEnvelope,
    canonical_path,
    canonical_payload,
    canonical_query,
)
from carepulse.notifications.evidence import accept_verified_nonce

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
)
_EXPECTED_SERVICE = "web"


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


async def _read_body(receive: Receive) -> bytes:
    body = bytearray()
    while True:
        message = await receive()
        if message["type"] != "http.request":
            raise BoundaryDenied(401)
        body.extend(message.get("body", b""))
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
        route_patterns: tuple[tuple[str, Pattern[str], str], ...] = (),
    ) -> None:
        self.app = app
        self.routes = routes or {}
        self.route_patterns = route_patterns

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        path = scope.get("path", "")
        if scope["type"] != "http" or not (path == "/v1" or path.startswith("/v1/")):
            await self.app(scope, receive, send)
            return
        try:
            envelope, payload = await self._authenticate(scope, receive)
        except (KeyError, TypeError, ValueError, UnicodeError):
            await self._deny(401, scope, receive, send)
            return
        except BoundaryDenied as denied:
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

        await self.app(scope, replay_body, send)

    async def _authenticate(
        self, scope: Scope, receive: Receive
    ) -> tuple[CanonicalEnvelope, bytes]:
        config = scope["app"].state.service_config
        headers = _signed_headers(scope, config.service_key, config.signing_key_id)
        payload = await _read_body(receive)
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
        if permitted_purpose != envelope.purpose:
            raise BoundaryDenied(403)
        try:
            accept_verified_nonce(
                scope["app"].state.db_engine,
                envelope.key_id,
                envelope.nonce,
                datetime.fromtimestamp(signed_at_ms / 1000, UTC),
            )
        except IntegrityError as replay:
            raise BoundaryDenied(401) from replay
        except SQLAlchemyError as unavailable:
            raise BoundaryDenied(503) from unavailable
        return envelope, payload

    @staticmethod
    async def _deny(status: int, scope: Scope, receive: Receive, send: Send) -> None:
        await error_response(status)(scope, receive, send)

"""Canonical bytes shared by the signed BFF request and FastAPI verifier."""

import hashlib
import json
import re
from dataclasses import astuple, dataclass
from urllib.parse import quote, unquote_to_bytes

PROTOCOL_VERSION = "carepulse-request-v1"
_BAD_ESCAPE = re.compile(rb"%(?![0-9A-Fa-f]{2})")
_SECOND_ESCAPE = re.compile(r"%[0-9A-Fa-f]{2}")
_TOKEN = re.compile(r"[A-Za-z0-9_-]{1,128}\Z")
_PURPOSE = re.compile(r"[A-Za-z][A-Za-z0-9_.:-]{0,127}\Z")
_DECIMAL = re.compile(r"(?:0|[1-9][0-9]{0,18})\Z")
_DIGEST = re.compile(r"[0-9a-f]{64}\Z")
_PATH_SAFE = "/-._~!$&'()*+,;=:@"


@dataclass(frozen=True, slots=True)
class CanonicalEnvelope:
    protocol_version: str
    key_id: str
    service: str
    method: str
    path: str
    query: str
    body_sha256: str
    organization_id: str
    staff_id: str
    session_id: str
    role: str
    security_revision: str
    timestamp: str
    nonce: str
    purpose: str
    idempotency_key: str


def _source_bytes(raw_component: bytes | str) -> bytes:
    if isinstance(raw_component, bytes):
        return raw_component
    if isinstance(raw_component, str):
        try:
            return raw_component.encode("utf-8")
        except UnicodeError:
            pass
    raise ValueError("Invalid URL component")


def _decoded_component(raw_component: bytes) -> str:
    if _BAD_ESCAPE.search(raw_component):
        raise ValueError("Malformed URL percent escape")
    try:
        decoded = unquote_to_bytes(raw_component).decode("utf-8")
    except UnicodeError:
        raise ValueError("Invalid URL UTF-8") from None
    if any(
        ord(character) < 32 or 127 <= ord(character) <= 159 for character in decoded
    ):
        raise ValueError("URL contains control characters")
    return decoded


def canonical_path(raw_path: bytes | str) -> str:
    """Normalize the actual request target path; reject alternate route spellings."""
    source = _source_bytes(raw_path)
    if not source.startswith(b"/") or b"?" in source or b"#" in source:
        raise ValueError("Invalid request path")
    decoded = _decoded_component(source)
    if (
        "\\" in decoded
        or _SECOND_ESCAPE.search(decoded)
        or any(segment in (".", "..") for segment in decoded.split("/"))
        or "" in decoded.split("/")[1:-1]
    ):
        raise ValueError("Ambiguous request path")
    # A percent-encoded slash changes how intermediate routers split segments.
    if b"%2f" in source.lower():
        raise ValueError("Encoded path separator")
    return quote(decoded, safe=_PATH_SAFE)


def canonical_query(raw_query: bytes | str) -> str:
    """Keep all query pairs, including repeats, ordered by RFC3986-encoded pair."""
    source = _source_bytes(raw_query)
    if not source:
        return ""
    pairs: list[tuple[str, str]] = []
    for raw_pair in source.split(b"&"):
        if not raw_pair:
            raise ValueError("Empty query pair")
        raw_name, _, raw_value = raw_pair.partition(b"=")
        name = _decoded_component(raw_name.replace(b"+", b" "))
        value = _decoded_component(raw_value.replace(b"+", b" "))
        pairs.append((quote(name, safe="-._~"), quote(value, safe="-._~")))
    return "&".join(f"{name}={value}" for name, value in sorted(pairs))


def raw_body_sha256(body: bytes) -> str:
    """Digest exactly the bytes that will cross the private HTTP boundary."""
    return hashlib.sha256(body).hexdigest()


def _validate_envelope(envelope: CanonicalEnvelope) -> None:
    fields = astuple(envelope)
    if any(
        not isinstance(field, str)
        or len(field) > 4096
        or any(
            ord(char) < 32 or 127 <= ord(char) <= 159 or 0xD800 <= ord(char) <= 0xDFFF
            for char in field
        )
        for field in fields
    ):
        raise ValueError("Invalid envelope string")
    if (
        envelope.protocol_version != PROTOCOL_VERSION
        or not _TOKEN.fullmatch(envelope.key_id)
        or not _TOKEN.fullmatch(envelope.service)
        or not re.fullmatch(r"[A-Z]{1,16}", envelope.method)
        or not _DIGEST.fullmatch(envelope.body_sha256)
        or not _PURPOSE.fullmatch(envelope.purpose)
        or not _DECIMAL.fullmatch(envelope.timestamp)
        or (
            envelope.security_revision != ""
            and not _DECIMAL.fullmatch(envelope.security_revision)
        )
        or not 22 <= len(envelope.nonce) <= 128
        or not _TOKEN.fullmatch(envelope.nonce)
        or envelope.path != canonical_path(envelope.path)
        or envelope.query != canonical_query(envelope.query)
    ):
        raise ValueError("Noncanonical signed envelope")


def canonical_payload(envelope: CanonicalEnvelope) -> bytes:
    """Encode the exact 16 signed fields as compact UTF-8 JSON strings."""
    _validate_envelope(envelope)
    return json.dumps(
        astuple(envelope), ensure_ascii=False, separators=(",", ":")
    ).encode("utf-8")

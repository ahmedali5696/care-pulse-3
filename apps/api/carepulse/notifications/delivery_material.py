"""Encrypt bounded, context-bound delivery proofs with the issuer's public key."""

import base64
import json
import re
from collections.abc import Mapping

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa

_CONTEXT_FIELDS = (
    "job_id",
    "proof_reference",
    "purpose",
    "tenant_id",
    "committed_recipient",
    "expiry_epoch_ms",
)
_KEY_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}\Z")
_MILLISECONDS = re.compile(r"[1-9][0-9]*\Z")


def _label(context: Mapping[str, str], key_id: str) -> bytes:
    if not isinstance(key_id, str) or not _KEY_ID.fullmatch(key_id):
        raise ValueError("Invalid delivery key ID")
    if not isinstance(context, Mapping) or set(context) != set(_CONTEXT_FIELDS):
        raise ValueError("Invalid delivery context")
    fields = [context[name] for name in _CONTEXT_FIELDS]
    if any(not isinstance(field, str) or not field for field in fields):
        raise ValueError("Invalid delivery context")
    if not _MILLISECONDS.fullmatch(context["expiry_epoch_ms"]):
        raise ValueError("Invalid delivery expiry")
    try:
        return json.dumps(
            ["carepulse-delivery-v1", key_id, *fields],
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")
    except UnicodeEncodeError as exc:
        raise ValueError("Invalid delivery context") from exc


def encrypt_delivery_token(
    token: str, context: Mapping[str, str], key_id: str, public_key_pem: bytes
) -> dict[str, int | str]:
    """The returned mapping is the complete restricted delivery material payload."""
    label = _label(context, key_id)
    if not isinstance(token, str):
        raise TypeError("Invalid delivery token")
    try:
        plaintext = token.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise ValueError("Invalid delivery token") from exc
    if not 1 <= len(plaintext) <= 318:
        raise ValueError("Invalid delivery token size")
    if not isinstance(public_key_pem, bytes) or not public_key_pem.startswith(
        b"-----BEGIN PUBLIC KEY-----\n"
    ):
        raise ValueError("Invalid delivery public key")
    try:
        public_key = serialization.load_pem_public_key(public_key_pem)
    except (ValueError, TypeError) as exc:
        raise ValueError("Invalid delivery public key") from exc
    if (
        not isinstance(public_key, rsa.RSAPublicKey)
        or public_key.key_size != 3072
        or public_key.public_numbers().e != 65537
        or public_key.public_bytes(
            serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo
        )
        != public_key_pem
    ):
        raise ValueError("Invalid delivery public key")
    ciphertext = public_key.encrypt(
        plaintext,
        padding.OAEP(
            mgf=padding.MGF1(algorithm=hashes.SHA256()),
            algorithm=hashes.SHA256(),
            label=label,
        ),
    )
    return {
        "version": 1,
        "key_id": key_id,
        "ciphertext_base64url": base64.urlsafe_b64encode(ciphertext)
        .decode("ascii")
        .rstrip("="),
    }

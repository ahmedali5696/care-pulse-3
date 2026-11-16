"""Synthetic signer for disposable PostgreSQL acceptance tests."""

import json
import os
from contextlib import contextmanager
from dataclasses import replace
from datetime import UTC, datetime
from hashlib import sha256
from hmac import new
from pathlib import Path
from uuid import uuid4

from carepulse.identity.authorization import protected_transaction
from carepulse.identity.runtime_identity import RegisteredRequest, current_request
from carepulse.identity.signing import CanonicalEnvelope, canonical_payload
from sqlalchemy import text


def db_proof_headers(envelope: CanonicalEnvelope) -> list[tuple[bytes, bytes]]:
    key_id = os.environ["CP_DB_IDENTITY_KEY_ID"].encode("ascii")
    key = bytes.fromhex(Path("/run/secrets/db_identity_key").read_text().strip())
    assert len(key) == 32
    proof = new(
        key,
        b"carepulse-db-context-v1\x00" + key_id + b"\x00" + canonical_payload(envelope),
        sha256,
    ).hexdigest()
    return [
        (b"x-carepulse-db-key-id", key_id),
        (b"x-carepulse-db-proof", proof.encode("ascii")),
    ]


@contextmanager
def registered_test_request(clinic, identity, body):
    """Use the real gate and committed proof; never bypass database activation."""
    proof_headers = dict(db_proof_headers(identity))
    with clinic.gate.begin() as connection:
        request_id = connection.scalar(
            text("SELECT public.f01_register_proof(:payload, :key, :proof, :body)"),
            {
                "payload": canonical_payload(identity),
                "key": proof_headers[b"x-carepulse-db-key-id"].decode("ascii"),
                "proof": bytes.fromhex(
                    proof_headers[b"x-carepulse-db-proof"].decode("ascii")
                ),
                "body": body,
            },
        )
    assert isinstance(request_id, int)
    token = current_request.set(RegisteredRequest(clinic.gate, request_id, identity))
    try:
        yield
    finally:
        current_request.reset(token)


@contextmanager
def signed_test_transaction(
    clinic, actor, *, method, path, purpose, body=None, key="", query=""
):
    """A signed request with explicit scope for direct transaction/race probes."""
    payload = (
        json.dumps(body, separators=(",", ":")).encode() if body is not None else b""
    )
    identity = replace(
        actor,
        method=method,
        path=path,
        purpose=purpose,
        query=query,
        body_sha256=sha256(payload).hexdigest(),
        timestamp=str(int(datetime.now(UTC).timestamp() * 1000)),
        nonce=f"synthetic_{uuid4().hex}",
        idempotency_key=key,
    )
    with (
        registered_test_request(clinic, identity, payload),
        protected_transaction(
            clinic.runtime, identity, clinic.config.policy.session_idle_minutes
        ) as result,
    ):
        yield result

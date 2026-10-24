"""Credential-bound restricted operator identity, separate from staff auth."""

import hashlib
from dataclasses import dataclass
from uuid import UUID

from sqlalchemy import Engine, text

from carepulse.config import SecurityPolicy


class OperatorDenied(Exception):
    """Operator identity is missing, disabled, locked, or invalid."""


@dataclass(frozen=True, slots=True, repr=False)
class VerifiedOperator:
    id: UUID
    credential_digest: str


def authenticate_operator(
    engine: Engine, identity: UUID, secret: str, policy: SecurityPolicy
) -> VerifiedOperator:
    """The DB owns durable throttling and fixed-length digest comparison."""
    if len(secret) < 32 or not secret.isascii():
        raise OperatorDenied
    digest = hashlib.sha256(secret.encode("ascii")).hexdigest()
    with engine.begin() as connection:
        permitted = connection.scalar(
            text("""
                SELECT public.authenticate_operator(:actor, :digest, :attempts, :window)
            """),
            {
                "actor": identity,
                "digest": digest,
                "attempts": policy.login_max_attempts,
                "window": policy.login_window_minutes,
            },
        )
    if not permitted:
        raise OperatorDenied
    return VerifiedOperator(identity, digest)

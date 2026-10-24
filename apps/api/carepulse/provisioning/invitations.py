"""Atomic first-Owner invitation provisioning and proof-bound activation."""

import hashlib
import json
import re
import secrets
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

from sqlalchemy import Engine, text

from carepulse.config import ServiceConfig
from carepulse.identity.receipts import (
    OperatorReceiptQuery,
    Receipt,
    ReceiptConflict,
    insert_operator_create_receipt,
    resolve_operator_create_receipt,
)
from carepulse.provisioning.operators import VerifiedOperator
from carepulse.provisioning.service import audit_operator_action


def _email(address: str) -> str:
    normalized = address.strip().lower()
    if len(normalized) > 254 or not re.fullmatch(
        r"[^@\s]+@[^@\s]+\.[^@\s]+", normalized
    ):
        raise ValueError("Invalid intended email")
    return normalized


def _request_digest(branch: str, email: str, reason: str) -> str:
    canonical = json.dumps(
        [branch, email, reason], ensure_ascii=False, separators=(",", ":")
    ).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


@dataclass(frozen=True, slots=True)
class PreparedInvitation:
    id: UUID
    job_id: UUID
    expires: datetime
    proof_digest: str
    material: dict


@dataclass(frozen=True, slots=True)
class InvitationCommand:
    clinic: UUID
    prior: UUID | None
    recipient: str
    expected_lifecycle: int
    expected_invitation: int
    reason: str


def _prepare_invitation(
    config: ServiceConfig, clinic: UUID, email: str
) -> PreparedInvitation:
    from carepulse.notifications.delivery_material import encrypt_delivery_token

    invite = uuid4()
    expiry = datetime.now(UTC) + timedelta(
        minutes=config.policy.invitation_expiry_minutes
    )
    token = secrets.token_urlsafe(32)
    job_id = uuid4()
    context = {
        "job_id": str(job_id),
        "proof_reference": str(invite),
        "purpose": "FIRST_OWNER",
        "tenant_id": str(clinic),
        "committed_recipient": email,
        "expiry_epoch_ms": str(int(expiry.timestamp() * 1000)),
    }
    material = encrypt_delivery_token(
        token, context, config.delivery_key_id, config.delivery_public_key_pem
    )
    return PreparedInvitation(
        invite,
        job_id,
        expiry,
        hashlib.sha256(token.encode("ascii")).hexdigest(),
        material,
    )


def _owner_invite_in_transaction(
    connection,
    operator: VerifiedOperator,
    command: InvitationCommand,
    prepared: PreparedInvitation,
) -> UUID:
    connection.execute(
        text("""
            SELECT public.operator_issue_owner_invitation(
              :actor, :digest, :clinic, :invite, :email, :proof, :expiry,
              :prior, :lifecycle, :revision)
        """),
        {
            "actor": operator.id,
            "digest": operator.credential_digest,
            "clinic": command.clinic,
            "invite": prepared.id,
            "email": command.recipient,
            "proof": prepared.proof_digest,
            "expiry": prepared.expires,
            "prior": command.prior,
            "lifecycle": command.expected_lifecycle,
            "revision": command.expected_invitation,
        },
    )
    connection.execute(
        text("""
            SELECT public.issue_operator_first_owner_delivery(
              :actor, :digest, :job, :invite, :email, :template,
              :logical_key, :key_id, :ciphertext)
        """),
        {
            "actor": operator.id,
            "digest": operator.credential_digest,
            "job": prepared.job_id,
            "invite": prepared.id,
            "email": command.recipient,
            "template": "FIRST_OWNER_INVITE",
            "logical_key": str(prepared.id),
            "key_id": prepared.material["key_id"],
            "ciphertext": prepared.material["ciphertext_base64url"],
        },
    )
    audit_operator_action(
        connection,
        operator,
        command.clinic,
        "OWNER_INVITE_REISSUED" if command.prior else "OWNER_INVITE_ISSUED",
        command.reason,
        str(prepared.id),
    )
    return prepared.id


def _cached_create(connection, operator: VerifiedOperator, key: str, digest: str):
    return resolve_operator_create_receipt(
        connection,
        OperatorReceiptQuery(operator.id, key, digest),
        operator.credential_digest,
    )


def _created_projection(clinic: UUID, revision: int) -> dict:
    """The committed create receipt captures its original PROVISIONING snapshot."""
    return {
        "organization_id": str(clinic),
        "revision": revision,
        "state": "PROVISIONING",
    }


def create_clinic(
    engine: Engine,
    operator: VerifiedOperator,
    config: ServiceConfig,
    branch: str,
    owner_email: str,
    idempotency_key: str,
    reason: str,
) -> dict:
    branch = branch.strip()
    if (
        not branch
        or len(branch) > 200
        or any(ord(char) < 32 or ord(char) == 127 for char in branch)
        or not idempotency_key
        or len(idempotency_key) > 128
        or not re.fullmatch(r"[A-Za-z0-9._:-]{1,128}", idempotency_key)
    ):
        raise ValueError("Invalid clinic create metadata")
    address = _email(owner_email)
    digest = _request_digest(branch, address, reason)
    with engine.begin() as connection:
        cached = _cached_create(connection, operator, idempotency_key, digest)
    if cached is not None:
        return _created_projection(*cached)
    clinic = uuid4()
    command = InvitationCommand(clinic, None, address, 0, 0, reason)
    prepared = _prepare_invitation(config, clinic, address)
    try:
        with engine.begin() as connection:
            connection.execute(
                text(
                    "SELECT public.operator_create_clinic(:actor,:digest,:clinic,:branch)"
                ),
                {
                    "actor": operator.id,
                    "digest": operator.credential_digest,
                    "clinic": clinic,
                    "branch": branch,
                },
            )
            _owner_invite_in_transaction(connection, operator, command, prepared)
            insert_operator_create_receipt(
                connection,
                Receipt(
                    actor_id=operator.id,
                    operation="clinics.create",
                    idempotency_key=idempotency_key,
                    request_digest=digest,
                    result_projection={},
                    result_revision=0,
                    result_organization_id=clinic,
                ),
                operator.credential_digest,
            )
            audit_operator_action(
                connection, operator, clinic, "CLINIC_CREATED", reason, idempotency_key
            )
    except ReceiptConflict:
        with engine.begin() as connection:
            cached = _cached_create(connection, operator, idempotency_key, digest)
        if cached is None:
            raise
        return _created_projection(*cached)
    return _created_projection(clinic, 0)


def reissue_invitation(
    engine: Engine,
    operator: VerifiedOperator,
    config: ServiceConfig,
    clinic: UUID,
    prior: UUID | None,
    expected_lifecycle: int,
    expected_invitation: int,
    reason: str,
    owner_email: str | None = None,
) -> dict:
    if prior is not None:
        with engine.begin() as connection:
            invite = (
                connection.execute(
                    text("""
                SELECT * FROM public.operator_get_owner_invitation(
                  :actor, :digest, :clinic, :invite, :reason)
            """),
                    {
                        "actor": operator.id,
                        "digest": operator.credential_digest,
                        "clinic": clinic,
                        "invite": prior,
                        "reason": reason,
                    },
                )
                .mappings()
                .one_or_none()
            )
        if invite is None:
            raise ValueError("Invitation unavailable")
        intended_email = invite["target_email"]
        if owner_email is not None and _email(owner_email) != intended_email:
            raise ValueError("Reissue cannot change intended recipient")
    else:
        if owner_email is None or expected_invitation != 0:
            raise ValueError(
                "New issuance requires intended email and initial revision"
            )
        intended_email = _email(owner_email)
    command = InvitationCommand(
        clinic,
        prior,
        intended_email,
        expected_lifecycle,
        expected_invitation,
        reason,
    )
    prepared = _prepare_invitation(config, clinic, command.recipient)
    with engine.begin() as connection:
        created = _owner_invite_in_transaction(connection, operator, command, prepared)
    return {
        "organization_id": str(clinic),
        "invitation_id": str(created),
        "state": "ISSUED",
        "revision": 0,
    }


def revoke_invitation(
    engine: Engine,
    operator: VerifiedOperator,
    clinic: UUID,
    invitation: UUID,
    expected_lifecycle: int,
    expected_invitation: int,
    reason: str,
) -> dict:
    with engine.begin() as connection:
        revision = connection.scalar(
            text("""
                SELECT public.operator_revoke_owner_invitation(
                   :actor, :digest, :clinic, :invite, :lifecycle, :revision)
            """),
            {
                "actor": operator.id,
                "digest": operator.credential_digest,
                "clinic": clinic,
                "invite": invitation,
                "lifecycle": expected_lifecycle,
                "revision": expected_invitation,
            },
        )
        audit_operator_action(
            connection,
            operator,
            clinic,
            "OWNER_INVITE_REVOKED",
            reason,
            str(invitation),
        )
    return {
        "organization_id": str(clinic),
        "invitation_id": str(invitation),
        "state": "REVOKED",
        "revision": revision,
    }

"""Recovery-purpose coordination; native credential effects remain in Better Auth."""

from hmac import compare_digest
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import Connection, text

from carepulse.db import lock_active_organization
from carepulse.identity.contracts import ContractError
from carepulse.notifications.evidence import append_sensitive_audit


class RecoveryCommand(BaseModel):
    model_config = ConfigDict(extra="forbid")

    native_user_id: str = Field(min_length=1, max_length=255)
    proof_reference: str = Field(min_length=1, max_length=255)
    native_identifier_hash: str = Field(pattern=r"^[A-Za-z0-9_-]{43}$")
    proof_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    request_fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")
    fingerprint_key_version: str = Field(pattern=r"^[A-Za-z0-9._:-]{1,64}$")


def _owner(connection: Connection, tenant: UUID, staff_id: UUID, user_id: str):
    if not lock_active_organization(connection, tenant):
        raise ContractError(403)
    owner = (
        connection.execute(
            text("""
            SELECT s.id, s.auth_user_id, s.verified_email, s.role, s.enabled,
                   s.verified, s.revision, s.security_revision,
                   s.reset_block_operation_id, o.lifecycle_revision
              FROM public.staff_accounts s
              JOIN public.organizations o ON o.id = s.organization_id
              JOIN public."user" u ON u.id = s.auth_user_id
             WHERE s.organization_id = :tenant AND s.id = :staff
               AND s.auth_user_id = :user AND o.state = 'ACTIVE'
               AND s.role = 'DOCTOR_OWNER' AND s.enabled AND s.verified
               AND u."emailVerified" AND u.email = s.verified_email
             FOR UPDATE OF s
        """),
            {"tenant": tenant, "staff": staff_id, "user": user_id},
        )
        .mappings()
        .one_or_none()
    )
    if owner is None:
        raise ContractError(403)
    # Only the verified recovery Owner can inspect linked revocation audit evidence.
    connection.execute(
        text("SELECT set_config('app.staff_id', :staff, true)"),
        {"staff": str(staff_id)},
    )
    return owner


def _operation(connection: Connection, digest: str):
    return (
        connection.execute(
            text("""
            SELECT * FROM public.recovery_operations
             WHERE purpose = 'PASSWORD_RESET' AND proof_digest = :digest
             FOR UPDATE
        """),
            {"digest": digest},
        )
        .mappings()
        .one_or_none()
    )


def _completion(connection: Connection, operation):
    return (
        connection.execute(
            text("""
            SELECT 1 FROM public.auth_recovery_completions c
             WHERE c.id = :id AND c.proof_digest = :digest AND c.auth_user_id = :user
               AND c.request_fingerprint = :fingerprint
               AND c.reserved_security_revision = :revision
               AND c.auth_fence = :fence AND c.completed_at = :finished
               AND public.recovery_native_verified(c.id)
        """),
            {
                "id": operation["id"],
                "digest": operation["proof_digest"],
                "user": operation["auth_user_id"],
                "fingerprint": operation["request_fingerprint"],
                "revision": operation["reserved_security_revision"],
                "fence": operation["auth_fence"],
                "finished": operation["auth_done_at"],
            },
        ).scalar_one_or_none()
        is not None
    )


def _has_completion(connection: Connection, operation_id: UUID) -> bool:
    return bool(
        connection.scalar(
            text("""
        SELECT EXISTS (SELECT 1 FROM public.auth_recovery_completions WHERE id = :id)
    """),
            {"id": operation_id},
        )
    )


def _success_audited(connection: Connection, operation_id: UUID) -> bool:
    return bool(
        connection.scalar(
            text("""
        SELECT EXISTS (SELECT 1 FROM public.sensitive_audit
                        WHERE recovery_operation_id = :id
                          AND action = 'PASSWORD_RESET_SUCCEEDED')
    """),
            {"id": operation_id},
        )
    )


def _proof(
    connection: Connection, tenant: UUID, staff_id: UUID, command: RecoveryCommand
):
    proof = (
        connection.execute(
            text("""
            SELECT v."expiresAt" AS expires_at
              FROM public.verification v
              JOIN public.delivery_jobs j ON j.verification_id = v.id
              JOIN public.staff_accounts s ON s.id = j.staff_id
             WHERE v.id = :reference AND v.identifier = :identifier AND v.value = :user
               AND j.organization_id = :tenant AND j.staff_id = :staff
               AND j.purpose = 'PASSWORD_RESET' AND j.proof_reference = :digest
               AND j.recipient = s.verified_email
               AND j.proof_expires_at = v."expiresAt"
               AND j.state NOT IN ('FAILED', 'CANCELLED')
        """),
            {
                "reference": command.proof_reference,
                "identifier": command.native_identifier_hash,
                "user": command.native_user_id,
                "tenant": tenant,
                "staff": staff_id,
                "digest": command.proof_digest,
            },
        )
        .mappings()
        .one_or_none()
    )
    now = connection.scalar(text("SELECT clock_timestamp()"))
    if proof is None or not now < proof["expires_at"]:
        raise ContractError(409)
    return proof["expires_at"]


def _matching(
    operation, tenant: UUID, staff_id: UUID, command: RecoveryCommand
) -> None:
    if (
        operation["organization_id"] != tenant
        or operation["staff_id"] != staff_id
        or operation["auth_user_id"] != command.native_user_id
        or operation["proof_reference"] != command.proof_reference
        or operation["fingerprint_key_version"] != command.fingerprint_key_version
        or not compare_digest(
            operation["request_fingerprint"], command.request_fingerprint
        )
    ):
        raise ContractError(409, "IDEMPOTENCY_CONFLICT")


def _pending_operation(connection: Connection, user_id: str):
    return (
        connection.execute(
            text("""
            SELECT * FROM public.recovery_operations
             WHERE auth_user_id = :user AND closed_at IS NULL
             ORDER BY id FOR UPDATE
        """),
            {"user": user_id},
        )
        .mappings()
        .one_or_none()
    )


def _revocation_proven(connection: Connection, prior, lifecycle: int) -> bool:
    reference = prior["invalidation_reference"] or ""
    prefix = "OPERATOR_SUSPEND:"
    if not reference.startswith(prefix):
        return False
    revision = reference.removeprefix(prefix)
    if not revision.isdecimal() or int(revision) != lifecycle - 1:
        return False
    if int(revision) != prior["captured_lifecycle_revision"] + 1:
        return False
    return bool(
        connection.scalar(
            text("""
        SELECT EXISTS (
            SELECT 1 FROM public.sensitive_audit
             WHERE recovery_operation_id = :id AND organization_id = :tenant
               AND action = 'RECOVERY_AUTH_INVALIDATED' AND actor_role = 'OPERATOR'
               AND correlation_id = :reference
        )
    """),
            {
                "id": prior["id"],
                "tenant": prior["organization_id"],
                "reference": reference,
            },
        )
    )


def _abandonable(connection: Connection, prior, owner) -> bool:
    now = connection.scalar(text("SELECT clock_timestamp()"))
    if prior["state"] == "RESERVED":
        return (
            now >= prior["proof_expires_at"]
            and prior["auth_done_at"] is None
            and prior["auth_fence"] == 0
            and not _has_completion(connection, prior["id"])
        )
    if prior["state"] == "AUTH_DONE" and prior["invalidated"]:
        return (
            prior["auth_done_at"] is not None
            and prior["auth_fence"] > 0
            and _completion(connection, prior)
            and prior["reserved_security_revision"] == owner["security_revision"]
            and prior["captured_staff_revision"] == owner["revision"]
            and _revocation_proven(connection, prior, owner["lifecycle_revision"])
        )
    return False


def reserve_recovery(
    connection: Connection, tenant: UUID, staff_id: UUID, command: RecoveryCommand
) -> dict:
    owner = _owner(connection, tenant, staff_id, command.native_user_id)
    current = _operation(connection, command.proof_digest)
    if current is not None:
        _matching(current, tenant, staff_id, command)
        if current["state"] == "ABORTED" or current["invalidated"]:
            raise ContractError(409)
        if current["state"] in ("AUTH_DONE", "FINALIZED") and not _completion(
            connection, current
        ):
            raise ContractError(409)
        if current["state"] == "FINALIZED" and not _success_audited(
            connection, current["id"]
        ):
            raise ContractError(409)
        if (
            current["state"] != "FINALIZED"
            and owner["reset_block_operation_id"] != current["id"]
        ):
            raise ContractError(409)
        if (
            current["state"] == "RESERVED"
            and connection.scalar(text("SELECT clock_timestamp()"))
            >= current["proof_expires_at"]
        ):
            raise ContractError(409)
        return {
            "operation_id": str(current["id"]),
            "state": current["state"],
            "reserved_security_revision": current["reserved_security_revision"],
        }

    expires_at = _proof(connection, tenant, staff_id, command)
    prior = _pending_operation(connection, command.native_user_id)
    if prior is not None:
        if owner["reset_block_operation_id"] != prior["id"] or not _abandonable(
            connection, prior, owner
        ):
            raise ContractError(409, "RESET_IN_PROGRESS")
        abandoned = connection.scalar(
            text("SELECT public.abandon_owned_recovery(:id)"),
            {"id": prior["id"]},
        )
        if abandoned != "ABORTED":
            raise ContractError(409)
    elif owner["reset_block_operation_id"] is not None:
        raise ContractError(409)

    # Recheck B after the prior operation lock and any in-flight native transaction.
    expires_at = _proof(connection, tenant, staff_id, command)
    reserved_id = uuid4()
    revision = owner["security_revision"] + 1
    next_staff_revision = owner["revision"] + 1
    connection.execute(
        text("""
        INSERT INTO public.recovery_operations
            (id, organization_id, staff_id, auth_user_id, purpose, proof_digest,
             proof_reference, proof_expires_at, request_fingerprint,
             fingerprint_key_version, captured_staff_revision,
             captured_lifecycle_revision, reserved_security_revision)
        VALUES (:id, :tenant, :staff, :user, 'PASSWORD_RESET', :digest,
                :reference, :expires, :fingerprint, :key_version,
                :staff_revision, :lifecycle_revision, :security_revision)
    """),
        {
            "id": reserved_id,
            "tenant": tenant,
            "staff": staff_id,
            "user": command.native_user_id,
            "digest": command.proof_digest,
            "reference": command.proof_reference,
            "expires": expires_at,
            "fingerprint": command.request_fingerprint,
            "key_version": command.fingerprint_key_version,
            "staff_revision": next_staff_revision,
            "lifecycle_revision": owner["lifecycle_revision"],
            "security_revision": revision,
        },
    )
    changed = (
        connection.execute(
            text("""
        UPDATE public.staff_accounts SET security_revision = security_revision + 1,
            reset_block_operation_id = :new_id
         WHERE organization_id = :tenant AND id = :staff
           AND security_revision = :prior_revision
           AND reset_block_operation_id IS NOT DISTINCT FROM :prior_id
         RETURNING revision, security_revision
    """),
            {
                "new_id": reserved_id,
                "tenant": tenant,
                "staff": staff_id,
                "prior_revision": owner["security_revision"],
                "prior_id": prior["id"] if prior else None,
            },
        )
        .mappings()
        .one_or_none()
    )
    if (
        changed is None
        or changed["revision"] != next_staff_revision
        or changed["security_revision"] != revision
    ):
        raise ContractError(409)
    append_sensitive_audit(
        connection,
        {
            "organization_id": tenant,
            "actor_staff_id": staff_id,
            "actor_role": "SYSTEM",
            "action": "PASSWORD_RESET_RESERVED",
            "correlation_id": str(reserved_id),
            "recovery_operation_id": reserved_id,
            "after_state": {"security_revision": revision},
        },
    )
    return {
        "operation_id": str(reserved_id),
        "state": "RESERVED",
        "reserved_security_revision": revision,
    }


def finalize_recovery(
    connection: Connection, tenant: UUID, staff_id: UUID, command: RecoveryCommand
) -> dict:
    owner = _owner(connection, tenant, staff_id, command.native_user_id)
    operation = _operation(connection, command.proof_digest)
    if operation is None:
        raise ContractError(409)
    _matching(operation, tenant, staff_id, command)
    if (
        operation["state"] == "FINALIZED"
        and not operation["invalidated"]
        and _completion(connection, operation)
        and _success_audited(connection, operation["id"])
    ):
        return {"state": "FINALIZED"}
    if (
        operation["state"] != "AUTH_DONE"
        or operation["invalidated"]
        or operation["auth_done_at"] is None
        or operation["auth_fence"] < 1
        or owner["reset_block_operation_id"] != operation["id"]
        or owner["security_revision"] != operation["reserved_security_revision"]
        or owner["revision"] != operation["captured_staff_revision"]
        or owner["lifecycle_revision"] != operation["captured_lifecycle_revision"]
        or not _completion(connection, operation)
    ):
        raise ContractError(409)
    # The database owns finalization and success evidence. A caller must not
    # manufacture a successful reset by updating the state, block and audit.
    state = connection.scalar(
        text("SELECT public.finalize_verified_recovery(:id)"),
        {"id": operation["id"]},
    )
    if state != "FINALIZED":
        raise ContractError(409)
    return {"state": "FINALIZED"}

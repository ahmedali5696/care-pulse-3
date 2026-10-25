"""Owner-managed Receptionist lifecycle on B-01's locked authority boundary."""

import hashlib
import re
import secrets
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

from sqlalchemy import Connection, Engine, text
from sqlalchemy.exc import IntegrityError, SQLAlchemyError

from carepulse.config import ServiceConfig
from carepulse.db import transaction
from carepulse.identity.authorization import AuthorizedStaff
from carepulse.identity.contracts import (
    ContractError,
    normalized_request_digest,
    require_matching_revision,
)
from carepulse.identity.receipts import (
    Receipt,
    TenantReceiptQuery,
    insert_tenant_receipt,
    resolve_tenant_receipt,
)
from carepulse.identity.staff_contracts import (
    AcceptReceptionist,
    CreateReceptionist,
    SetReceptionistGrant,
    StaffCommand,
)
from carepulse.notifications.delivery_material import encrypt_delivery_token
from carepulse.notifications.evidence import append_sensitive_audit, create_delivery_job


def _owner(actor: AuthorizedStaff) -> None:
    if actor.role != "DOCTOR_OWNER":
        raise ContractError(403)


def _email(raw: str) -> str:
    address = raw.strip().lower()
    if len(address) > 254 or not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", address):
        raise ContractError(422)
    return address


def _staff(
    connection: Connection,
    actor: AuthorizedStaff,
    staff_id: UUID,
    *,
    lock: bool = False,
):
    row = (
        connection.execute(
            text(
                """
            SELECT s.id, s.organization_id, s.auth_user_id, s.verified_email,
                   s.enabled, s.verified, s.bulk_reschedule, s.revision,
                   s.security_revision,
                   (SELECT i.state FROM public.invitations i
                     WHERE i.organization_id = s.organization_id AND i.staff_id = s.id
                     ORDER BY i.issued_at DESC, i.id DESC LIMIT 1) AS invitation_state
              FROM public.staff_accounts s
             WHERE s.organization_id = :tenant AND s.id = :staff
               AND s.role = 'RECEPTIONIST'
        """
                + (" FOR UPDATE OF s" if lock else "")
            ),
            {"tenant": actor.organization_id, "staff": staff_id},
        )
        .mappings()
        .one_or_none()
    )
    if row is None:
        raise ContractError(404)
    return row


def _projection(row) -> dict:
    return {
        "id": str(row["id"]),
        "email": row["verified_email"],
        "enabled": row["enabled"],
        "verified": row["verified"],
        "bulk_reschedule": row["bulk_reschedule"],
        "revision": row["revision"],
        "security_revision": row["security_revision"],
        "invitation_state": row["invitation_state"],
    }


def _audit(
    connection: Connection,
    actor: AuthorizedStaff,
    action: str,
    reason: str,
    correlation: UUID,
    previous: dict | None,
    current: dict | None,
) -> None:
    append_sensitive_audit(
        connection,
        {
            "organization_id": actor.organization_id,
            "actor_staff_id": actor.staff_id,
            "actor_role": actor.role,
            "action": action,
            "reason": reason,
            "before_state": previous,
            "after_state": current,
            "correlation_id": str(correlation),
        },
    )


def list_receptionists(
    connection: Connection, actor: AuthorizedStaff, after: UUID | None, limit: int
) -> dict:
    _owner(actor)
    if not 1 <= limit <= 100:
        raise ContractError(422)
    rows = (
        connection.execute(
            text("""
            SELECT id FROM public.staff_accounts
             WHERE organization_id = :tenant AND role = 'RECEPTIONIST'
               AND (CAST(:after AS uuid) IS NULL OR id > CAST(:after AS uuid))
             ORDER BY id LIMIT :limit
        """),
            {"tenant": actor.organization_id, "after": after, "limit": limit + 1},
        )
        .scalars()
        .all()
    )
    selected = rows[:limit]
    return {
        "items": [
            _projection(_staff(connection, actor, staff_id)) for staff_id in selected
        ],
        "next_cursor": str(selected[-1]) if len(rows) > limit else None,
    }


def view_receptionist(
    connection: Connection, actor: AuthorizedStaff, staff_id: UUID
) -> dict:
    _owner(actor)
    return _projection(_staff(connection, actor, staff_id))


def create_receptionist(
    connection: Connection,
    actor: AuthorizedStaff,
    command: CreateReceptionist,
    key: str,
) -> tuple[dict, int]:
    _owner(actor)
    if not re.fullmatch(r"[A-Za-z0-9._:-]{1,128}", key):
        raise ContractError(422)
    digest = normalized_request_digest(command)
    cached = resolve_tenant_receipt(
        connection, TenantReceiptQuery(actor, "staff.create", key, digest)
    )
    if cached is not None:
        return cached
    address = _email(command.email)
    prepared = connection.execute(
        text("""
            SELECT u.id FROM public."user" u
             WHERE u.id = :user AND u.email = :email AND u.name = :name
               AND NOT u."emailVerified"
               AND NOT EXISTS (SELECT 1 FROM public.account a WHERE a."userId" = u.id)
               AND NOT EXISTS (SELECT 1 FROM public.staff_accounts s WHERE s.auth_user_id = u.id)
        """),
        {"user": command.prepared_user_id, "email": address, "name": command.name},
    ).scalar_one_or_none()
    if prepared is None:
        raise ContractError(409)
    staff_id = uuid4()
    try:
        connection.execute(
            text("""
            INSERT INTO public.staff_accounts
                (id, organization_id, auth_user_id, verified_email, role,
                 enabled, verified, bulk_reschedule)
            VALUES (:id, :tenant, :user, :email, 'RECEPTIONIST', true, false, false)
        """),
            {
                "id": staff_id,
                "tenant": actor.organization_id,
                "user": prepared,
                "email": address,
            },
        )
    except IntegrityError as conflict:
        if (
            getattr(conflict.orig, "sqlstate", None) == "23505"
            and getattr(getattr(conflict.orig, "diag", None), "constraint_name", None)
            == "staff_accounts_auth_user_id_key"
        ):
            raise ContractError(409) from conflict
        raise
    projection = _projection(_staff(connection, actor, staff_id))
    _audit(
        connection,
        actor,
        "RECEPTIONIST_CREATED",
        command.reason,
        staff_id,
        None,
        {"id": str(staff_id), "enabled": True, "verified": False},
    )
    insert_tenant_receipt(
        connection,
        Receipt(
            actor_id=actor.staff_id,
            staff_id=actor.staff_id,
            organization_id=actor.organization_id,
            operation="staff.create",
            idempotency_key=key,
            request_digest=digest,
            result_projection=projection,
            result_revision=0,
        ),
    )
    return projection, 0


def _lock_target(
    connection: Connection,
    actor: AuthorizedStaff,
    staff_id: UUID,
    command: StaffCommand,
):
    _owner(actor)
    row = _staff(connection, actor, staff_id, lock=True)
    require_matching_revision(command.expected_revision, row["revision"])
    require_matching_revision(
        command.expected_security_revision, row["security_revision"]
    )
    return row


def _invalidate_recovery(
    connection: Connection, actor: AuthorizedStaff, staff_id: UUID, correlation: UUID
) -> None:
    operation_ids = (
        connection.execute(
            text("""
            SELECT id FROM public.recovery_operations
             WHERE organization_id = :tenant AND staff_id = :staff
               AND closed_at IS NULL
             ORDER BY id FOR UPDATE
        """),
            {"tenant": actor.organization_id, "staff": staff_id},
        )
        .scalars()
        .all()
    )
    for operation_id in operation_ids:
        connection.execute(
            text("""
                UPDATE public.recovery_operations SET invalidated = true,
                    invalidation_reference = COALESCE(invalidation_reference, :reference)
                 WHERE id = :operation AND NOT invalidated
            """),
            {"operation": operation_id, "reference": str(correlation)},
        )


def _revoke_pending(
    connection: Connection,
    actor: AuthorizedStaff,
    staff_id: UUID,
    superseded: UUID | None = None,
) -> int:
    old_ids = (
        connection.execute(
            text("""
            SELECT id FROM public.invitations
             WHERE organization_id = :tenant AND staff_id = :staff
               AND purpose = 'RECEPTIONIST' AND state = 'ISSUED'
             ORDER BY id FOR UPDATE
        """),
            {"tenant": actor.organization_id, "staff": staff_id},
        )
        .scalars()
        .all()
    )
    for old_id in old_ids:
        connection.execute(
            text("""
                UPDATE public.invitations SET state = 'REVOKED',
                    superseded_by_id = :successor, revocation_reference = :reason
                 WHERE id = :id
            """),
            {"id": old_id, "successor": superseded, "reason": str(actor.staff_id)},
        )
    return len(old_ids)


def change_receptionist(
    connection: Connection,
    actor: AuthorizedStaff,
    staff_id: UUID,
    command: StaffCommand | SetReceptionistGrant,
    action: str,
) -> tuple[dict, int, str | None]:
    before = _lock_target(connection, actor, staff_id, command)
    if action == "permissions":
        if not isinstance(command, SetReceptionistGrant):
            raise ContractError(422)
        changed = before["bulk_reschedule"] != command.bulk_reschedule
        withdrawn = before["bulk_reschedule"] and not command.bulk_reschedule
        column = "bulk_reschedule"
        enabled = None
    elif action in ("enable", "disable"):
        enabled = action == "enable"
        changed = before["enabled"] != enabled
        withdrawn = not enabled
        column = "enabled"
    else:
        raise ContractError(403)
    if not changed:
        raise ContractError(409)
    correlation = uuid4()
    if withdrawn:
        _invalidate_recovery(connection, actor, staff_id, correlation)
    connection.execute(
        text(f"""
            UPDATE public.staff_accounts SET {column} = :new_state,
                security_revision = security_revision + 1
             WHERE id = :staff AND organization_id = :tenant
        """),
        {
            "new_state": command.bulk_reschedule
            if column == "bulk_reschedule"
            else enabled,
            "staff": staff_id,
            "tenant": actor.organization_id,
        },
    )
    if action == "disable":
        _revoke_pending(connection, actor, staff_id)
    after = _staff(connection, actor, staff_id)
    _audit(
        connection,
        actor,
        f"RECEPTIONIST_{action.upper()}",
        command.reason,
        correlation,
        {"id": str(staff_id), column: before[column]},
        {"id": str(staff_id), column: after[column]},
    )
    return (
        _projection(after),
        after["revision"],
        before["auth_user_id"] if withdrawn else None,
    )


@dataclass(frozen=True)
class PreparedStaffInvitation:
    id: UUID
    job_id: UUID
    expires_at: datetime
    proof_digest: str
    material: dict


def prepare_staff_invitation(
    config: ServiceConfig, clinic: UUID, email: str
) -> PreparedStaffInvitation:
    invitation_id, job_id = uuid4(), uuid4()
    proof = secrets.token_urlsafe(32)
    expiry = datetime.now(UTC) + timedelta(
        minutes=config.policy.invitation_expiry_minutes
    )
    material = encrypt_delivery_token(
        proof,
        {
            "job_id": str(job_id),
            "proof_reference": str(invitation_id),
            "purpose": "RECEPTIONIST",
            "tenant_id": str(clinic),
            "committed_recipient": email,
            "expiry_epoch_ms": str(int(expiry.timestamp() * 1000)),
        },
        config.delivery_key_id,
        config.delivery_public_key_pem,
    )
    return PreparedStaffInvitation(
        invitation_id,
        job_id,
        expiry,
        hashlib.sha256(proof.encode()).hexdigest(),
        material,
    )


def change_staff_invitation(
    connection: Connection,
    actor: AuthorizedStaff,
    staff_id: UUID,
    command: StaffCommand,
    action: str,
    prepared: PreparedStaffInvitation | None = None,
) -> tuple[dict, int]:
    before = _lock_target(connection, actor, staff_id, command)
    if action == "invite":
        if before["verified"] or not before["enabled"] or prepared is None:
            raise ContractError(409)
        _revoke_pending(connection, actor, staff_id, prepared.id)
        connection.execute(
            text("""
                INSERT INTO public.invitations
                    (id, organization_id, staff_id, purpose, target_email,
                     proof_digest, expires_at, issued_by_staff_id)
                VALUES (:id, :tenant, :staff, 'RECEPTIONIST', :email,
                        :proof, :expires, :actor)
            """),
            {
                "id": prepared.id,
                "tenant": actor.organization_id,
                "staff": staff_id,
                "email": before["verified_email"],
                "proof": prepared.proof_digest,
                "expires": prepared.expires_at,
                "actor": actor.staff_id,
            },
        )
        create_delivery_job(
            connection,
            {
                "id": prepared.job_id,
                "organization_id": actor.organization_id,
                "staff_id": staff_id,
                "invitation_id": prepared.id,
                "proof_reference": str(prepared.id),
                "purpose": "RECEPTIONIST",
                "recipient": before["verified_email"],
                "template": "RECEPTIONIST_INVITE",
                "logical_key": str(prepared.id),
                "proof_expires_at": prepared.expires_at,
            },
            {
                "delivery_key_id": prepared.material["key_id"],
                "ciphertext_base64url": prepared.material["ciphertext_base64url"],
            },
        )
    elif action == "revoke-invite":
        if not _revoke_pending(connection, actor, staff_id):
            raise ContractError(409)
    else:
        raise ContractError(403)
    connection.execute(
        text("""
            UPDATE public.staff_accounts SET updated_at = clock_timestamp()
             WHERE id = :staff AND organization_id = :tenant
        """),
        {"staff": staff_id, "tenant": actor.organization_id},
    )
    after = _staff(connection, actor, staff_id)
    _audit(
        connection,
        actor,
        "RECEPTIONIST_INVITE_ISSUED"
        if action == "invite"
        else "RECEPTIONIST_INVITE_REVOKED",
        command.reason,
        prepared.id if prepared else uuid4(),
        {"id": str(staff_id), "invitation_state": before["invitation_state"]},
        {"id": str(staff_id), "invitation_state": after["invitation_state"]},
    )
    return _projection(after), after["revision"]


def accept_receptionist(
    connection: Connection, clinic: UUID, command: AcceptReceptionist
) -> tuple[dict, int]:
    target_staff = connection.scalar(
        text("""
            SELECT staff_id FROM public.invitations
             WHERE id = :id AND organization_id = :tenant AND purpose = 'RECEPTIONIST'
        """),
        {"id": command.invitation_id, "tenant": clinic},
    )
    if target_staff is None:
        raise ContractError(403)
    actor = AuthorizedStaff(
        clinic,
        target_staff,
        "RECEPTIONIST",
        False,
        0,
        0,
        command.expected_lifecycle_revision,
    )
    staff = _staff(connection, actor, target_staff, lock=True)
    invitation = (
        connection.execute(
            text("""
            SELECT i.id, i.staff_id, i.target_email, i.state, i.expires_at, i.revision
              FROM public.invitations i
             WHERE i.id = :id AND i.organization_id = :tenant
               AND i.purpose = 'RECEPTIONIST'
             FOR UPDATE
        """),
            {"id": command.invitation_id, "tenant": clinic},
        )
        .mappings()
        .one_or_none()
    )
    digest = hashlib.sha256(command.proof.encode()).hexdigest()
    if (
        invitation is None
        or invitation["state"] != "ISSUED"
        or invitation["revision"] != command.expected_invitation_revision
        or connection.scalar(text("SELECT clock_timestamp()"))
        >= invitation["expires_at"]
        or connection.scalar(
            text("""
                SELECT 1 FROM public.invitations WHERE id = :id AND proof_digest = :digest
            """),
            {"id": command.invitation_id, "digest": digest},
        )
        is None
    ):
        raise ContractError(403)
    if (
        staff["verified"]
        or not staff["enabled"]
        or staff["verified_email"] != invitation["target_email"]
        or staff["auth_user_id"] != command.prepared_user_id
    ):
        raise ContractError(403)
    require_matching_revision(command.expected_staff_revision, staff["revision"])
    require_matching_revision(
        command.expected_security_revision, staff["security_revision"]
    )
    native = connection.scalar(
        text("""
        SELECT 1 FROM public."user" u JOIN public.account a ON a."userId" = u.id
         WHERE u.id = :user AND u.email = :email AND u."emailVerified"
           AND a."providerId" = 'credential' AND a.password IS NOT NULL
    """),
        {"user": command.prepared_user_id, "email": invitation["target_email"]},
    )
    if native is None:
        raise ContractError(403)
    connection.execute(
        text("""
        UPDATE public.invitations SET state = 'ACCEPTED',
            accepted_by_user_id = :user, acceptance_reference = :reference
         WHERE id = :invitation
    """),
        {
            "invitation": command.invitation_id,
            "user": command.prepared_user_id,
            "reference": str(command.invitation_id),
        },
    )
    connection.execute(
        text("""
        UPDATE public.staff_accounts SET verified = true,
            security_revision = security_revision + 1
         WHERE id = :staff AND organization_id = :tenant
    """),
        {"staff": invitation["staff_id"], "tenant": clinic},
    )
    after = _staff(connection, actor, invitation["staff_id"])
    _audit(
        connection,
        actor,
        "RECEPTIONIST_INVITE_ACCEPTED",
        "Invitation proof accepted",
        command.invitation_id,
        {"id": str(invitation["staff_id"]), "verified": False},
        {"id": str(invitation["staff_id"]), "verified": True},
    )
    return _projection(after), after["revision"]


def cleanup_native_sessions(engine: Engine, tenant: UUID, native_user_id: str) -> None:
    """Best-effort cleanup; committed security_revision is the authority barrier."""
    try:
        with transaction(engine, tenant) as connection:
            connection.execute(
                text("SELECT public.delete_staff_sessions(:tenant, :user)"),
                {"tenant": tenant, "user": native_user_id},
            )
    except SQLAlchemyError:
        pass

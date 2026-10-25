"""Exact, signed Receptionist management and invitation acceptance routes."""

from uuid import UUID

from fastapi import APIRouter, Query, Request
from sqlalchemy import text

from carepulse.db import lock_active_organization, transaction
from carepulse.identity.authorization import protected_transaction
from carepulse.identity.contracts import (
    ContractError,
    require_matching_revision,
    success_response,
)
from carepulse.identity.recovery import (
    RecoveryCommand,
    finalize_recovery,
    reserve_recovery,
)
from carepulse.identity.staff import (
    _email,
    _owner,
    _staff,
    accept_receptionist,
    change_receptionist,
    change_staff_invitation,
    cleanup_native_sessions,
    create_receptionist,
    list_receptionists,
    prepare_staff_invitation,
    view_receptionist,
)
from carepulse.identity.staff_contracts import (
    AcceptReceptionist,
    CreateReceptionist,
    SetReceptionistGrant,
    StaffCommand,
)

router = APIRouter()


def _recovery_identity(request: Request) -> tuple[UUID, UUID]:
    identity = request.scope.get("carepulse.identity")
    if (
        identity is None
        or identity.purpose != "OWNER_RECOVERY"
        or identity.role != "BOOTSTRAP"
        or identity.session_id
        or identity.security_revision != "0"
    ):
        raise ContractError(403)
    try:
        return UUID(identity.organization_id), UUID(identity.staff_id)
    except (TypeError, ValueError) as invalid:
        raise ContractError(401) from invalid


@router.post("/v1/staff/recovery/reserve")
def reserve_staff_recovery(command: RecoveryCommand, request: Request):
    tenant, staff = _recovery_identity(request)
    with transaction(request.app.state.db_engine, tenant) as connection:
        return reserve_recovery(connection, tenant, staff, command)


@router.post("/v1/staff/recovery/finalize")
def finalize_staff_recovery(command: RecoveryCommand, request: Request):
    tenant, staff = _recovery_identity(request)
    with transaction(request.app.state.db_engine, tenant) as connection:
        return finalize_recovery(connection, tenant, staff, command)


def _actor_transaction(request: Request):
    identity = request.scope.get("carepulse.identity")
    if identity is None:
        raise ContractError(403)
    return protected_transaction(
        request.app.state.db_engine,
        identity,
        request.app.state.service_config.policy.session_idle_minutes,
    )


@router.get("/v1/staff/receptionists")
def list_staff(
    request: Request,
    after: UUID | None = None,
    limit: int = Query(default=25, ge=1, le=100),
):
    with _actor_transaction(request) as (connection, actor):
        listing = list_receptionists(connection, actor, after, limit)
        return success_response(listing, actor.staff_revision)


@router.post("/v1/staff/receptionists")
def create_staff(command: CreateReceptionist, request: Request):
    with _actor_transaction(request) as (connection, actor):
        projection, revision = create_receptionist(
            connection,
            actor,
            command,
            request.scope["carepulse.identity"].idempotency_key,
        )
        return success_response(projection, revision, status_code=201)


@router.get("/v1/staff/receptionists/{staff_id}")
def view_staff(staff_id: UUID, request: Request):
    with _actor_transaction(request) as (connection, actor):
        projection = view_receptionist(connection, actor, staff_id)
        return success_response(projection, projection["revision"])


@router.post("/v1/staff/receptionists/{staff_id}/disable")
def disable_staff(staff_id: UUID, command: StaffCommand, request: Request):
    with _actor_transaction(request) as (connection, actor):
        projection, revision, native_user = change_receptionist(
            connection,
            actor,
            staff_id,
            command,
            "disable",
        )
        tenant = actor.organization_id
    if native_user:
        cleanup_native_sessions(request.app.state.db_engine, tenant, native_user)
    return success_response(projection, revision)


@router.post("/v1/staff/receptionists/{staff_id}/enable")
def enable_staff(staff_id: UUID, command: StaffCommand, request: Request):
    with _actor_transaction(request) as (connection, actor):
        projection, revision, _ = change_receptionist(
            connection,
            actor,
            staff_id,
            command,
            "enable",
        )
    return success_response(projection, revision)


@router.patch("/v1/staff/receptionists/{staff_id}/permissions")
def set_staff_permissions(
    staff_id: UUID, command: SetReceptionistGrant, request: Request
):
    with _actor_transaction(request) as (connection, actor):
        projection, revision, native_user = change_receptionist(
            connection,
            actor,
            staff_id,
            command,
            "permissions",
        )
        tenant = actor.organization_id
    if native_user:
        cleanup_native_sessions(request.app.state.db_engine, tenant, native_user)
    return success_response(projection, revision)


@router.post("/v1/staff/receptionists/{staff_id}/invite")
def invite_staff(staff_id: UUID, command: StaffCommand, request: Request):
    with _actor_transaction(request) as (connection, actor):
        _owner(actor)
        address = _email(_staff(connection, actor, staff_id)["verified_email"])
        clinic = actor.organization_id
    prepared = prepare_staff_invitation(
        request.app.state.service_config, clinic, address
    )
    with _actor_transaction(request) as (connection, actor):
        projection, revision = change_staff_invitation(
            connection,
            actor,
            staff_id,
            command,
            "invite",
            prepared,
        )
        return success_response(projection, revision)


@router.post("/v1/staff/receptionists/{staff_id}/revoke-invite")
def revoke_staff_invite(staff_id: UUID, command: StaffCommand, request: Request):
    with _actor_transaction(request) as (connection, actor):
        projection, revision = change_staff_invitation(
            connection,
            actor,
            staff_id,
            command,
            "revoke-invite",
        )
        return success_response(projection, revision)


@router.post("/v1/staff/receptionists/invite-accept")
def accept_staff_invite(command: AcceptReceptionist, request: Request):
    identity = request.scope.get("carepulse.identity")
    if (
        identity is None
        or identity.purpose != "RECEPTIONIST_ACCEPT"
        or identity.role != "BOOTSTRAP"
        or identity.staff_id
        or identity.session_id
        or identity.security_revision != "0"
    ):
        raise ContractError(403)
    try:
        clinic = UUID(identity.organization_id)
    except (ValueError, TypeError) as invalid:
        raise ContractError(401) from invalid
    with transaction(request.app.state.db_engine, clinic) as connection:
        if not lock_active_organization(connection, clinic):
            raise ContractError(403)
        lifecycle = connection.scalar(
            text("SELECT lifecycle_revision FROM public.organizations WHERE id = :id"),
            {"id": clinic},
        )
        require_matching_revision(command.expected_lifecycle_revision, lifecycle)
        projection, revision = accept_receptionist(connection, clinic, command)
    return success_response(projection, revision)

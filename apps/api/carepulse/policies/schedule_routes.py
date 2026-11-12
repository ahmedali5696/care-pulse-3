"""Authorized calendar configuration and revision-bound staged activation."""

from fastapi import APIRouter, Request

from carepulse.identity.authorization import protected_transaction
from carepulse.identity.contracts import (
    ContractError,
    normalized_request_digest,
    success_response,
)
from carepulse.identity.receipts import (
    ReceiptConflict,
    TenantReceiptQuery,
    require_committed_tenant_receipt,
)
from carepulse.policies.schedule_contracts import ScheduleActivate, SchedulePatch
from carepulse.policies.schedule_service import (
    _require_schedule_manager,
    activate,
    read_schedule,
    stage_or_preview,
)

router = APIRouter(prefix="/v1/settings/schedule")


def _transaction(request: Request):
    if request.query_params:
        raise ContractError(422)
    identity = request.scope.get("carepulse.identity")
    if identity is None or identity.purpose != "SETTINGS_SCHEDULE":
        raise ContractError(403)
    return protected_transaction(
        request.app.state.db_engine,
        identity,
        request.app.state.service_config.policy.session_idle_minutes,
    )


@router.get("")
def get_schedule(request: Request):
    with _transaction(request) as (connection, actor):
        projection = read_schedule(connection, actor)
        return success_response(projection, projection["schedule_revision"])


@router.patch("")
def patch_schedule(command: SchedulePatch, request: Request):
    return _schedule_mutation(request, command, "settings.schedule.stage")


@router.post("/preview")
def preview_schedule(command: SchedulePatch, request: Request):
    return _schedule_mutation(request, command, "settings.schedule.preview")


@router.post("/activate")
def activate_schedule(command: ScheduleActivate, request: Request):
    return _schedule_mutation(request, command, "settings.schedule.activate")


def _schedule_mutation(request: Request, command, operation: str):
    key = request.scope["carepulse.identity"].idempotency_key
    try:
        with _transaction(request) as (connection, actor):
            if operation == "settings.schedule.activate":
                projection = activate(connection, actor, command, key)
                revision = projection["schedule_revision"]
            else:
                projection = stage_or_preview(
                    connection, actor, command, key, operation
                )
                revision = projection["revisions"]["schedule"]
            return success_response(projection, revision)
    except ReceiptConflict:
        with _transaction(request) as (connection, actor):
            _require_schedule_manager(actor)
            projection, revision = require_committed_tenant_receipt(
                connection,
                TenantReceiptQuery(
                    actor, operation, key, normalized_request_digest(command)
                ),
            )
            return success_response(projection, revision)

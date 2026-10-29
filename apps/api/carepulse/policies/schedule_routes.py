"""Owner-only calendar configuration and revision-bound staged activation."""

from fastapi import APIRouter, Request

from carepulse.identity.authorization import protected_transaction
from carepulse.identity.contracts import ContractError, success_response
from carepulse.policies.schedule_contracts import ScheduleActivate, SchedulePatch
from carepulse.policies.schedule_service import (
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
    with _transaction(request) as (connection, actor):
        projection = stage_or_preview(
            connection,
            actor,
            command,
            request.scope["carepulse.identity"].idempotency_key,
            "settings.schedule.stage",
        )
        return success_response(projection, projection["revisions"]["schedule"])


@router.post("/preview")
def preview_schedule(command: SchedulePatch, request: Request):
    with _transaction(request) as (connection, actor):
        projection = stage_or_preview(
            connection,
            actor,
            command,
            request.scope["carepulse.identity"].idempotency_key,
            "settings.schedule.preview",
        )
        return success_response(projection, projection["revisions"]["schedule"])


@router.post("/activate")
def activate_schedule(command: ScheduleActivate, request: Request):
    with _transaction(request) as (connection, actor):
        projection = activate(
            connection,
            actor,
            command,
            request.scope["carepulse.identity"].idempotency_key,
        )
        return success_response(projection, projection["schedule_revision"])

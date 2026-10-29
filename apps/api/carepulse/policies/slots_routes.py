"""Tenant-scoped advisory exact-time candidates."""

from datetime import date
from re import fullmatch

from fastapi import APIRouter, Request

from carepulse.identity.authorization import protected_transaction
from carepulse.identity.contracts import ContractError, success_response
from carepulse.policies.slots_service import list_slots

router = APIRouter(prefix="/v1/slots")


@router.get("")
def get_slots(request: Request, service_date: str):
    if (
        len(request.query_params) != 1
        or len(request.query_params.getlist("service_date")) != 1
    ):
        raise ContractError(422)
    if fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", service_date) is None:
        raise ContractError(422)
    try:
        day = date.fromisoformat(service_date)
    except ValueError as invalid:
        raise ContractError(422) from invalid
    identity = request.scope.get("carepulse.identity")
    if identity is None or identity.purpose != "SLOTS":
        raise ContractError(403)
    with protected_transaction(
        request.app.state.db_engine,
        identity,
        request.app.state.service_config.policy.session_idle_minutes,
    ) as (connection, actor):
        projection = list_slots(connection, actor, day)
        return success_response(projection, projection["schedule_revision"])

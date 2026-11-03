"""Signed Doctor-only follow-up authorizations."""

from uuid import UUID

from fastapi import APIRouter, Request

from carepulse.identity.authorization import protected_transaction
from carepulse.identity.contracts import ContractError, success_response
from carepulse.visits.eligibility import extend_followup, record_followup_decision
from carepulse.visits.eligibility_contracts import ExtendFollowup, FollowupDecisionPatch

router = APIRouter(prefix="/v1/visits")


def _transaction(request: Request):
    if request.query_params:
        raise ContractError(422)
    identity = request.scope.get("carepulse.identity")
    if identity is None or identity.purpose != "VISIT_FOLLOWUP":
        raise ContractError(403)
    return protected_transaction(
        request.app.state.db_engine,
        identity,
        request.app.state.service_config.policy.session_idle_minutes,
    )


@router.patch("/{visit_id}")
def patch_followup_decision(
    visit_id: UUID, command: FollowupDecisionPatch, request: Request
):
    with _transaction(request) as (connection, actor):
        projection = record_followup_decision(
            connection,
            actor,
            visit_id,
            command,
            request.scope["carepulse.identity"].idempotency_key,
        )
        return success_response(projection, projection["revision"])


@router.post("/{visit_id}/extend-followup")
def post_followup_extension(visit_id: UUID, command: ExtendFollowup, request: Request):
    with _transaction(request) as (connection, actor):
        projection = extend_followup(
            connection,
            actor,
            visit_id,
            command,
            request.scope["carepulse.identity"].idempotency_key,
        )
        return success_response(projection, projection["revision"], status_code=201)

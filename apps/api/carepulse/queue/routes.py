"""Signed dated queue reads and B-14 Doctor controls."""

from functools import partial
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Request
from pydantic import ValidationError

from carepulse.identity.authorization import protected_transaction
from carepulse.identity.contracts import ContractError, success_response
from carepulse.policies.admission_routes import control_mutation, strict_command
from carepulse.queue.admission_contracts import (
    CapacityCommand,
    IssueConfirmationCommand,
    PauseCommand,
    RevokeConfirmationCommand,
    UsableConfirmationQuery,
)
from carepulse.queue.admission_control import (
    get_usable_confirmation,
    issue_confirmation,
    set_capacity,
    set_pause,
)
from carepulse.queue.admission_control import (
    revoke_confirmation as revoke_case_confirmation,
)
from carepulse.queue.contracts import QueueQuery
from carepulse.queue.queries import dated_queue_projection

router = APIRouter(prefix="/v1/queue")


def _transaction(
    request: Request, *, purpose: str = "QUEUE_READ", stage: str = "PRIMARY"
):
    identity = request.scope.get("carepulse.identity")
    if identity is None or identity.purpose != purpose:
        raise ContractError(403)
    return protected_transaction(
        request.app.state.db_engine,
        identity,
        request.app.state.service_config.policy.session_idle_minutes,
        stage=stage,
    )


@router.get("")
def get_queue(request: Request):
    params = request.query_params
    if len(params.multi_items()) != len(params):
        raise ContractError(422)
    try:
        query = QueueQuery.model_validate(dict(params))
    except ValidationError as invalid:
        raise ContractError(422) from invalid
    with _transaction(request) as (connection, actor):
        projection, revision = dated_queue_projection(connection, actor, query)
        return success_response(projection, revision)


async def _capacity_body(request: Request) -> CapacityCommand:
    return await strict_command(request, CapacityCommand)


async def _pause_body(request: Request) -> PauseCommand:
    return await strict_command(request, PauseCommand)


async def _issue_body(request: Request) -> IssueConfirmationCommand:
    return await strict_command(request, IssueConfirmationCommand)


async def _revoke_body(request: Request) -> RevokeConfirmationCommand:
    return await strict_command(request, RevokeConfirmationCommand)


@router.patch("/capacity")
def patch_capacity(
    request: Request, command: Annotated[CapacityCommand, Depends(_capacity_body)]
):
    return control_mutation(
        request, command, set_capacity, purpose="QUEUE_CAPACITY_UPDATE"
    )


@router.post("/pause")
def post_pause(
    request: Request, command: Annotated[PauseCommand, Depends(_pause_body)]
):
    return control_mutation(request, command, set_pause, purpose="QUEUE_PAUSE")


@router.post("/admission-confirmations")
def post_confirmation(
    request: Request, command: Annotated[IssueConfirmationCommand, Depends(_issue_body)]
):
    execute = partial(
        issue_confirmation,
        ttl_minutes=request.app.state.service_config.policy.doctor_confirmation_expiry_minutes,
    )
    return control_mutation(
        request, command, execute, purpose="QUEUE_CONFIRMATION_ISSUE"
    )


@router.get("/admission-confirmations/usable")
def read_usable_confirmation(request: Request):
    params = request.query_params
    if len(params.multi_items()) != len(params):
        raise ContractError(422)
    try:
        query = UsableConfirmationQuery.model_validate(dict(params))
    except ValidationError as invalid:
        raise ContractError(422) from invalid
    with _transaction(request, purpose="QUEUE_CONFIRMATION_USABLE") as (
        connection,
        actor,
    ):
        projection, revision = get_usable_confirmation(
            connection,
            actor,
            query,
            ttl_minutes=request.app.state.service_config.policy.doctor_confirmation_expiry_minutes,
        )
        return success_response(projection, revision)


@router.post("/admission-confirmations/{confirmation_id}/revoke")
def revoke_confirmation(
    confirmation_id: UUID,
    request: Request,
    command: Annotated[RevokeConfirmationCommand, Depends(_revoke_body)],
):
    def execute(connection, actor, command, key):
        return revoke_case_confirmation(
            connection, actor, confirmation_id, command, key
        )

    return control_mutation(
        request,
        command,
        execute,
        purpose="QUEUE_CONFIRMATION_REVOKE",
        confirmation_id=confirmation_id,
    )

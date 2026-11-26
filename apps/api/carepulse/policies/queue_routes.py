"""Signed B-13 queue settings boundary."""

from typing import Annotated

from fastapi import APIRouter, Depends, Request

from carepulse.appointments.routes import _transaction
from carepulse.identity.contracts import ContractError, success_response
from carepulse.identity.receipts import ReceiptConflict
from carepulse.policies.admission_routes import strict_command
from carepulse.policies.queue import (
    QueuePolicyCommand,
    get_queue_policy,
    recover_queue_policy,
    set_queue_policy,
)

router = APIRouter(prefix="/v1/settings/queue")


async def _policy_body(request: Request) -> QueuePolicyCommand:
    return await strict_command(request, QueuePolicyCommand)


@router.get("")
def read_queue_policy(request: Request):
    if request.query_params:
        raise ContractError(422)
    with _transaction(request, purpose="SETTINGS_QUEUE_READ") as (connection, actor):
        projection, revision = get_queue_policy(connection, actor)
        return success_response(projection, revision)


@router.patch("")
def patch_queue_policy(
    request: Request, command: Annotated[QueuePolicyCommand, Depends(_policy_body)]
):
    key = request.scope["carepulse.identity"].idempotency_key
    if not key:
        raise ContractError(422)
    try:
        with _transaction(request, purpose="SETTINGS_QUEUE_UPDATE") as (
            connection,
            actor,
        ):
            projection, revision, _ = set_queue_policy(connection, actor, command, key)
            return success_response(projection, revision)
    except ReceiptConflict:
        with _transaction(
            request, purpose="SETTINGS_QUEUE_UPDATE", stage="RECEIPT_RECOVERY"
        ) as (connection, actor):
            projection, revision = recover_queue_policy(connection, actor, command, key)
            return success_response(projection, revision)

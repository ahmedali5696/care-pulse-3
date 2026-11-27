"""Signed B-14 settings and Doctor-control request boundary."""

import json
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, ValidationError

from carepulse.appointments.routes import _transaction, _unique_json_pairs
from carepulse.identity.contracts import ContractError, success_response
from carepulse.identity.receipts import ReceiptConflict
from carepulse.queue.admission_contracts import (
    AdmissionModeCommand,
    IssueConfirmationCommand,
)
from carepulse.queue.admission_control import (
    get_admission_settings,
    recover_control,
    set_admission_mode,
)

router = APIRouter(prefix="/v1/settings/admission")


async def strict_command(request: Request, schema: type[BaseModel]):
    if request.query_params:
        raise ContractError(422)
    try:
        body = json.loads(
            await request.body(),
            object_pairs_hook=_unique_json_pairs,
            parse_constant=lambda value: (_ for _ in ()).throw(ValueError(value)),
        )
        return schema.model_validate(body)
    except (ValueError, UnicodeError, ValidationError, RecursionError) as invalid:
        raise ContractError(422) from invalid


async def _mode_body(request: Request) -> AdmissionModeCommand:
    return await strict_command(request, AdmissionModeCommand)


def control_mutation(
    request: Request,
    command: BaseModel,
    execute,
    *,
    purpose: str,
    confirmation_id: UUID | None = None,
):
    key = request.scope["carepulse.identity"].idempotency_key
    if not key:
        raise ContractError(422)
    try:
        with _transaction(request, purpose=purpose) as (connection, actor):
            projection, revision, replayed = execute(connection, actor, command, key)
            status_code = (
                201
                if isinstance(command, IssueConfirmationCommand) and not replayed
                else 200
            )
            return success_response(projection, revision, status_code=status_code)
    except ReceiptConflict:
        with _transaction(request, purpose=purpose, stage="RECEIPT_RECOVERY") as (
            connection,
            actor,
        ):
            projection, revision = recover_control(
                connection, actor, command, key, confirmation_id=confirmation_id
            )
            return success_response(projection, revision)


@router.get("")
def read_admission_settings(request: Request):
    if request.query_params:
        raise ContractError(422)
    with _transaction(request, purpose="ADMISSION_SETTINGS_READ") as (
        connection,
        actor,
    ):
        projection, revision = get_admission_settings(connection, actor)
        return success_response(projection, revision)


@router.patch("")
def patch_admission_settings(
    request: Request, command: Annotated[AdmissionModeCommand, Depends(_mode_body)]
):
    return control_mutation(
        request, command, set_admission_mode, purpose="ADMISSION_MODE_UPDATE"
    )

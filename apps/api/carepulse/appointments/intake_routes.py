"""Distinct signed immediate-intake operations."""

import json
from typing import Annotated

from fastapi import APIRouter, Depends, Request
from pydantic import ValidationError

from carepulse.appointments.intake import intake_appointment, recover_committed_intake
from carepulse.appointments.intake_contracts import (
    EmergencyIntake,
    RoutineIntake,
    UrgentIntake,
)
from carepulse.appointments.routes import _transaction, _unique_json_pairs
from carepulse.identity.contracts import ContractError, success_response
from carepulse.identity.receipts import ReceiptConflict

router = APIRouter(prefix="/v1/intake")
_OPERATIONS = {
    "/v1/intake/routine": ("INTAKE_ROUTINE", RoutineIntake),
    "/v1/intake/emergency": ("INTAKE_EMERGENCY", EmergencyIntake),
    "/v1/intake/urgent": ("INTAKE_URGENT", UrgentIntake),
}


async def _intake_body(
    request: Request,
) -> RoutineIntake | EmergencyIntake | UrgentIntake:
    _, schema = _OPERATIONS[request.url.path]
    try:
        body = json.loads(
            await request.body(),
            object_pairs_hook=_unique_json_pairs,
            parse_constant=lambda value: (_ for _ in ()).throw(ValueError(value)),
        )
        return schema.model_validate(body)
    except (ValueError, UnicodeError, ValidationError, RecursionError) as invalid:
        raise ContractError(422) from invalid


@router.post("/routine")
@router.post("/emergency")
@router.post("/urgent")
def post_intake(
    request: Request,
    command: Annotated[
        RoutineIntake | EmergencyIntake | UrgentIntake, Depends(_intake_body)
    ],
):
    purpose, _ = _OPERATIONS[request.url.path]
    key = request.scope["carepulse.identity"].idempotency_key
    if not key:
        raise ContractError(422)
    try:
        with _transaction(request, purpose=purpose) as (connection, actor):
            projection, revision, created = intake_appointment(
                connection, actor, command, key
            )
            return success_response(
                projection, revision, status_code=201 if created else 200
            )
    except ReceiptConflict:
        with _transaction(request, purpose=purpose, stage="RECEIPT_RECOVERY") as (
            connection,
            actor,
        ):
            projection, revision = recover_committed_intake(
                connection, actor, command, key
            )
            return success_response(projection, revision)

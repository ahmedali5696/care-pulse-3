"""Purpose-bound signed source decisions and claim extensions."""

import json
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Request
from pydantic import ValidationError

from carepulse.identity.authorization import protected_transaction
from carepulse.identity.contracts import ContractError, success_response
from carepulse.identity.receipts import ReceiptConflict
from carepulse.visits.eligibility import (
    extend_followup,
    record_followup_decision,
    recover_followup_receipt,
)
from carepulse.visits.eligibility_contracts import (
    ExtendFollowup,
    ExtendFollowupV2,
    FollowupDecisionPatch,
    FollowupDecisionPatchV2,
)

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


def _unique_json_pairs(pairs):
    fields = {}
    for key, value in pairs:
        if key in fields:
            raise ValueError("Duplicate request key")
        fields[key] = value
    return fields


async def _strict_body(request: Request) -> dict:
    try:
        body = json.loads(
            await request.body(),
            object_pairs_hook=_unique_json_pairs,
            parse_constant=lambda value: (_ for _ in ()).throw(ValueError(value)),
        )
        if not isinstance(body, dict):
            raise TypeError("Expected an object")
        return body
    except (TypeError, ValueError, UnicodeError, RecursionError) as invalid:
        raise ContractError(422) from invalid


async def _decision_body(
    request: Request,
) -> FollowupDecisionPatch | FollowupDecisionPatchV2:
    body = await _strict_body(request)
    v2_fields = {"expected_source_decision_revision", "expected_followup_revision"}
    contract = (
        FollowupDecisionPatchV2 if body.keys() & v2_fields else FollowupDecisionPatch
    )
    try:
        return contract.model_validate(body)
    except ValidationError as invalid:
        raise ContractError(422) from invalid


async def _extension_body(request: Request) -> ExtendFollowup | ExtendFollowupV2:
    body = await _strict_body(request)
    v2_fields = {
        "expected_source_decision_revision",
        "expected_followup_revision",
        "expected_authorization_revision",
        "expected_appointment_revision",
    }
    contract = ExtendFollowupV2 if body.keys() & v2_fields else ExtendFollowup
    try:
        return contract.model_validate(body)
    except ValidationError as invalid:
        raise ContractError(422) from invalid


@router.patch("/{visit_id}")
def patch_followup_decision(
    visit_id: UUID,
    command: Annotated[
        FollowupDecisionPatch | FollowupDecisionPatchV2, Depends(_decision_body)
    ],
    request: Request,
):
    key = request.scope["carepulse.identity"].idempotency_key
    try:
        with _transaction(request) as (connection, actor):
            projection, revision = record_followup_decision(
                connection, actor, visit_id, command, key
            )
            return success_response(projection, revision)
    except ReceiptConflict:
        with _transaction(request) as (connection, actor):
            projection, revision = recover_followup_receipt(
                connection, actor, visit_id, command, key, "visits.followup_decision"
            )
            return success_response(projection, revision)


@router.post("/{visit_id}/extend-followup")
def post_followup_extension(
    visit_id: UUID,
    command: Annotated[ExtendFollowup | ExtendFollowupV2, Depends(_extension_body)],
    request: Request,
):
    key = request.scope["carepulse.identity"].idempotency_key
    try:
        with _transaction(request) as (connection, actor):
            projection, revision = extend_followup(
                connection, actor, visit_id, command, key
            )
            return success_response(projection, revision, status_code=201)
    except ReceiptConflict:
        with _transaction(request) as (connection, actor):
            projection, revision = recover_followup_receipt(
                connection, actor, visit_id, command, key, "visits.extend_followup"
            )
            return success_response(projection, revision, status_code=201)

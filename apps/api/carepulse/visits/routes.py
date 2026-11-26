"""Purpose-bound Visit operations and existing source decisions/extensions."""

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Request
from pydantic import ValidationError

from carepulse.identity.authorization import protected_transaction
from carepulse.identity.contracts import ContractError, success_response
from carepulse.identity.receipts import ReceiptConflict
from carepulse.visits.clinical import patch_clinical, recover_clinical
from carepulse.visits.clinical_contracts import (
    ClinicalPatch,
    CompleteVisit,
    ExceptionalCancel,
    VisitPatch,
    parse_visit_patch,
    strict_body,
    visit_patch_purpose,
)
from carepulse.visits.complete import (
    cancel_exceptional,
    complete_visit,
    recover_terminal,
)
from carepulse.visits.eligibility import (
    extend_followup,
    record_followup_decision,
    recover_followup_receipt,
)
from carepulse.visits.eligibility_contracts import (
    ExtendFollowup,
    ExtendFollowupV2,
)
from carepulse.visits.queries import visit_detail

router = APIRouter(prefix="/v1/visits")


def _transaction(request: Request, *, purpose: str, stage: str = "PRIMARY"):
    if request.query_params:
        raise ContractError(422)
    identity = request.scope.get("carepulse.identity")
    if identity is None or identity.purpose != purpose:
        raise ContractError(403)
    if (
        purpose in {"VISIT_CLINICAL", "VISIT_CANCEL_EXCEPTIONAL"}
        and identity.role != "DOCTOR_OWNER"
    ):
        raise ContractError(403)
    return protected_transaction(
        request.app.state.db_engine,
        identity,
        request.app.state.service_config.policy.session_idle_minutes,
        stage=stage,
    )


async def _decision_body(request: Request) -> VisitPatch:
    return parse_visit_patch(await request.body())


async def _extension_body(request: Request) -> ExtendFollowup | ExtendFollowupV2:
    body = strict_body(await request.body())
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


@router.get("/{visit_id}")
def get_visit(visit_id: UUID, request: Request):
    with _transaction(request, purpose="VISIT_READ") as (connection, actor):
        projection, revision = visit_detail(connection, actor, visit_id)
        return success_response(projection, revision)


def _clinical_mutation(
    request: Request,
    visit_id: UUID,
    command: ClinicalPatch | CompleteVisit | ExceptionalCancel,
    purpose: str,
):
    identity = request.scope.get("carepulse.identity")
    if identity is None or identity.purpose != purpose:
        raise ContractError(403)
    key = identity.idempotency_key
    if not key:
        raise ContractError(422)
    execute, recover = {
        "VISIT_CLINICAL": (patch_clinical, recover_clinical),
        "VISIT_COMPLETE": (complete_visit, recover_terminal),
        "VISIT_CANCEL_EXCEPTIONAL": (cancel_exceptional, recover_terminal),
    }[purpose]
    try:
        with _transaction(request, purpose=purpose) as (connection, actor):
            projection, revision, _ = execute(connection, actor, visit_id, command, key)
            return success_response(projection, revision)
    except ReceiptConflict:
        with _transaction(request, purpose=purpose, stage="RECEIPT_RECOVERY") as (
            connection,
            actor,
        ):
            projection, revision = recover(connection, actor, visit_id, command, key)
            return success_response(projection, revision)


@router.patch("/{visit_id}")
def patch_followup_decision(
    visit_id: UUID,
    command: Annotated[VisitPatch, Depends(_decision_body)],
    request: Request,
):
    purpose = visit_patch_purpose(command)
    identity = request.scope.get("carepulse.identity")
    if identity is None or identity.purpose != purpose:
        raise ContractError(403)
    if isinstance(command, ClinicalPatch):
        return _clinical_mutation(request, visit_id, command, purpose)
    key = identity.idempotency_key
    try:
        with _transaction(request, purpose=purpose) as (connection, actor):
            projection, revision = record_followup_decision(
                connection, actor, visit_id, command, key
            )
            return success_response(projection, revision)
    except ReceiptConflict:
        with _transaction(request, purpose=purpose, stage="RECEIPT_RECOVERY") as (
            connection,
            actor,
        ):
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
        with _transaction(request, purpose="VISIT_FOLLOWUP") as (connection, actor):
            projection, revision = extend_followup(
                connection, actor, visit_id, command, key
            )
            return success_response(projection, revision, status_code=201)
    except ReceiptConflict:
        with _transaction(
            request, purpose="VISIT_FOLLOWUP", stage="RECEIPT_RECOVERY"
        ) as (connection, actor):
            projection, revision = recover_followup_receipt(
                connection, actor, visit_id, command, key, "visits.extend_followup"
            )
            return success_response(projection, revision, status_code=201)


async def _terminal_body(
    request: Request, schema: type[CompleteVisit] | type[ExceptionalCancel]
):
    try:
        return schema.model_validate(strict_body(await request.body()))
    except ValidationError as invalid:
        raise ContractError(422) from invalid


async def _complete_body(request: Request) -> CompleteVisit:
    return await _terminal_body(request, CompleteVisit)


async def _cancel_body(request: Request) -> ExceptionalCancel:
    return await _terminal_body(request, ExceptionalCancel)


@router.post("/{visit_id}/complete")
def post_complete(
    visit_id: UUID,
    request: Request,
    command: Annotated[CompleteVisit, Depends(_complete_body)],
):
    return _clinical_mutation(request, visit_id, command, "VISIT_COMPLETE")


@router.post("/{visit_id}/cancel-exceptional")
def post_exceptional_cancel(
    visit_id: UUID,
    request: Request,
    command: Annotated[ExceptionalCancel, Depends(_cancel_body)],
):
    return _clinical_mutation(request, visit_id, command, "VISIT_CANCEL_EXCEPTIONAL")

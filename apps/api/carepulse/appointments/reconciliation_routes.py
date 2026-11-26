"""Closed staff-only original-day review routes."""

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Request
from pydantic import TypeAdapter, ValidationError

from carepulse.appointments.booking_contracts import CanonicalServiceDate
from carepulse.appointments.day_review import CloseCommand, ReviewCommand, record_review
from carepulse.appointments.reconciliation import (
    ManualNoShow,
    ReconciliationQuery,
    manual_no_show,
    reconciliation,
)
from carepulse.appointments.routes import _transaction
from carepulse.identity.contracts import ContractError, success_response
from carepulse.identity.receipts import ReceiptConflict
from carepulse.policies.admission_routes import strict_command

router = APIRouter(prefix="/v1")


@router.get("/reconciliation")
def get_reconciliation(request: Request):
    params = request.query_params
    if len(params.multi_items()) != len(params):
        raise ContractError(422)
    try:
        query = ReconciliationQuery.model_validate(dict(params))
    except ValidationError as invalid:
        raise ContractError(422) from invalid
    with _transaction(request, purpose="RECONCILIATION_READ") as (connection, actor):
        return success_response(*reconciliation(connection, actor, query))


async def _absence_body(request: Request) -> ManualNoShow:
    return await strict_command(request, ManualNoShow)


async def _review_body(request: Request) -> ReviewCommand:
    return await strict_command(request, ReviewCommand)


async def _close_body(request: Request) -> CloseCommand:
    return await strict_command(request, CloseCommand)


def _mutate(request: Request, purpose: str, execute):
    if not request.scope["carepulse.identity"].idempotency_key:
        raise ContractError(422)
    try:
        with _transaction(request, purpose=purpose) as (connection, actor):
            return success_response(*execute(connection, actor))
    except ReceiptConflict:
        with _transaction(request, purpose=purpose, stage="RECEIPT_RECOVERY") as (
            connection,
            actor,
        ):
            return success_response(*execute(connection, actor))


@router.post("/appointments/{appointment_id}/manual-no-show")
def post_manual_no_show(
    appointment_id: UUID,
    request: Request,
    command: Annotated[ManualNoShow, Depends(_absence_body)],
):
    return _mutate(
        request,
        "APPOINTMENT_MANUAL_NO_SHOW",
        lambda connection, actor: manual_no_show(
            connection, actor, appointment_id, command
        ),
    )


def _record(request: Request, raw_date: str, command: ReviewCommand):
    try:
        service_date = TypeAdapter(CanonicalServiceDate).validate_python(raw_date)
    except ValidationError as invalid:
        raise ContractError(422) from invalid
    purpose = "DAY_CLOSE" if isinstance(command, CloseCommand) else "DAY_REVIEW"
    return _mutate(
        request,
        purpose,
        lambda connection, actor: record_review(connection, service_date, command),
    )


@router.post("/days/{raw_date}/review")
def post_review(
    raw_date: str,
    request: Request,
    command: Annotated[ReviewCommand, Depends(_review_body)],
):
    return _record(request, raw_date, command)


@router.post("/days/{raw_date}/close")
def post_close(
    raw_date: str,
    request: Request,
    command: Annotated[CloseCommand, Depends(_close_body)],
):
    return _record(request, raw_date, command)

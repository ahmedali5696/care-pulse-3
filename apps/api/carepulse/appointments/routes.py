"""Signed booking, attendance and individual appointment operations."""

import json
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Request
from pydantic import ValidationError

from carepulse.appointments.book import _committed_booking, book_appointment
from carepulse.appointments.booking_contracts import (
    AppointmentQuery,
    BookAppointment,
    FollowupPreviewQuery,
)
from carepulse.appointments.cancel import (
    cancel_appointment,
    recover_cancel_replace,
    replace_urgent,
)
from carepulse.appointments.cancel_contracts import CancelAppointment, ReplaceUrgent
from carepulse.appointments.queries import (
    appointment_detail,
    confirmed_followup_control,
    followup_qualification,
    list_appointments,
    source_decision_control,
)
from carepulse.identity.authorization import protected_transaction
from carepulse.identity.contracts import (
    ContractError,
    normalized_request_digest,
    success_response,
)
from carepulse.identity.receipts import (
    ReceiptConflict,
    TenantReceiptQuery,
    lookup_tenant_receipt,
)
from carepulse.queue.checkin import check_in_appointment, recover_committed_checkin
from carepulse.queue.contracts import CheckInAppointment

router = APIRouter(prefix="/v1/appointments")


def _transaction(
    request: Request, *, purpose: str = "APPOINTMENT_BOOKING", stage: str = "PRIMARY"
):
    if request.method != "GET" and request.query_params:
        raise ContractError(422)
    identity = request.scope.get("carepulse.identity")
    if identity is None or identity.purpose != purpose:
        raise ContractError(403)
    return protected_transaction(
        request.app.state.db_engine,
        identity,
        request.app.state.service_config.policy.session_idle_minutes,
        stage=stage,
    )


def _unique_json_pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate request key")
        result[key] = value
    return result


async def _booking_body(request: Request) -> BookAppointment:
    try:
        body = json.loads(
            await request.body(),
            object_pairs_hook=_unique_json_pairs,
            parse_constant=lambda value: (_ for _ in ()).throw(ValueError(value)),
        )
        return BookAppointment.model_validate(body)
    except (ValueError, UnicodeError, ValidationError, RecursionError) as invalid:
        raise ContractError(422) from invalid


@router.post("")
def create_appointment(
    request: Request, command: Annotated[BookAppointment, Depends(_booking_body)]
):
    key = request.scope["carepulse.identity"].idempotency_key
    try:
        with _transaction(request) as (connection, actor):
            projection = book_appointment(connection, actor, command, key)
            stored = lookup_tenant_receipt(
                connection,
                actor.organization_id,
                actor.staff_id,
                "appointments.book",
                key,
            )
            if stored is None:
                raise ContractError(409)
            return success_response(
                projection, stored["result_revision"], status_code=201
            )
    except ReceiptConflict:
        # The losing write has rolled back; check live grants and resource visibility anew.
        with _transaction(request, stage="RECEIPT_RECOVERY") as (connection, actor):
            query = TenantReceiptQuery(
                actor, "appointments.book", key, normalized_request_digest(command)
            )
            cached = _committed_booking(connection, query, command.patient_id)
            if cached is None:
                raise ContractError(409, "IDEMPOTENCY_CONFLICT")
            return success_response(cached[0], cached[1], status_code=201)


@router.get("")
def get_appointments(request: Request):
    params = request.query_params
    if len(params.multi_items()) != len(params):
        raise ContractError(422)
    try:
        query = AppointmentQuery.model_validate(dict(params))
    except ValidationError as invalid:
        raise ContractError(422) from invalid
    with _transaction(request) as (connection, actor):
        return success_response(
            list_appointments(
                connection,
                actor,
                query,
                request.app.state.service_config.service_key,
            ),
            actor.staff_revision,
        )


@router.get("/{appointment_id}")
def get_appointment(appointment_id: UUID, request: Request):
    params = request.query_params
    if len(params.multi_items()) != len(params):
        raise ContractError(422)
    try:
        preview = FollowupPreviewQuery.model_validate(dict(params)) if params else None
    except ValidationError as invalid:
        raise ContractError(422) from invalid
    with _transaction(request) as (connection, actor):
        projection = appointment_detail(connection, actor, appointment_id)
        if projection["service_type"] == "FOLLOW_UP":
            projection["followup_control"] = confirmed_followup_control(
                connection, actor, appointment_id
            )
        if preview is not None:
            projection["followup_qualification"] = followup_qualification(
                connection, actor, appointment_id, preview.intended_service_date
            )
            control = projection["followup_qualification"].get("decision_control")
        else:
            control = source_decision_control(connection, actor, projection)
        if control is not None:
            projection["decision_control"] = control
        return success_response(projection, projection["revision"])


async def _checkin_body(request: Request) -> CheckInAppointment:
    try:
        body = json.loads(
            await request.body(),
            object_pairs_hook=_unique_json_pairs,
            parse_constant=lambda value: (_ for _ in ()).throw(ValueError(value)),
        )
        return CheckInAppointment.model_validate(body)
    except (ValueError, UnicodeError, ValidationError, RecursionError) as invalid:
        raise ContractError(422) from invalid


@router.post("/{appointment_id}/check-in")
def post_appointment_checkin(
    appointment_id: UUID,
    request: Request,
    command: Annotated[CheckInAppointment, Depends(_checkin_body)],
):
    key = request.scope["carepulse.identity"].idempotency_key
    if not key:
        raise ContractError(422)
    try:
        with _transaction(request, purpose="QUEUE_CHECK_IN") as (connection, actor):
            projection, revision = check_in_appointment(
                connection, actor, appointment_id, command, key
            )
            return success_response(projection, revision)
    except ReceiptConflict:
        with _transaction(
            request, purpose="QUEUE_CHECK_IN", stage="RECEIPT_RECOVERY"
        ) as (connection, actor):
            projection, revision = recover_committed_checkin(
                connection, actor, appointment_id, command, key
            )
            return success_response(projection, revision)


async def _change_body(
    request: Request, schema: type[CancelAppointment] | type[ReplaceUrgent]
):
    try:
        body = json.loads(
            await request.body(),
            object_pairs_hook=_unique_json_pairs,
            parse_constant=lambda value: (_ for _ in ()).throw(ValueError(value)),
        )
        return schema.model_validate(body)
    except (ValueError, UnicodeError, ValidationError, RecursionError) as invalid:
        raise ContractError(422) from invalid


async def _cancel_body(request: Request) -> CancelAppointment:
    return await _change_body(request, CancelAppointment)


async def _replace_body(request: Request) -> ReplaceUrgent:
    return await _change_body(request, ReplaceUrgent)


def _change_appointment(
    request: Request, appointment_id: UUID, command: CancelAppointment | ReplaceUrgent
):
    replacing = isinstance(command, ReplaceUrgent)
    purpose = "APPOINTMENT_REPLACE_URGENT" if replacing else "APPOINTMENT_CANCEL"
    execute = replace_urgent if replacing else cancel_appointment
    identity = request.scope.get("carepulse.identity")
    if identity is None or identity.purpose != purpose:
        raise ContractError(403)
    if replacing and identity.role != "RECEPTIONIST":
        raise ContractError(403)
    key = identity.idempotency_key
    if not key:
        raise ContractError(422)
    try:
        with _transaction(request, purpose=purpose) as (connection, actor):
            projection, revision, created = execute(
                connection, actor, appointment_id, command, key
            )
            return success_response(
                projection, revision, status_code=201 if replacing and created else 200
            )
    except ReceiptConflict:
        with _transaction(request, purpose=purpose, stage="RECEIPT_RECOVERY") as (
            connection,
            actor,
        ):
            projection, revision = recover_cancel_replace(
                connection, actor, appointment_id, command, key
            )
            return success_response(projection, revision)


@router.post("/{appointment_id}/cancel")
def post_appointment_cancel(
    appointment_id: UUID,
    request: Request,
    command: Annotated[CancelAppointment, Depends(_cancel_body)],
):
    return _change_appointment(request, appointment_id, command)


@router.post("/{appointment_id}/replace-urgent")
def post_appointment_replace_urgent(
    appointment_id: UUID,
    request: Request,
    command: Annotated[ReplaceUrgent, Depends(_replace_body)],
):
    return _change_appointment(request, appointment_id, command)

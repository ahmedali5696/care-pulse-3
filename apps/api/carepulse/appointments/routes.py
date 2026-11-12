"""B-09 signed booking and read-only operational appointment surfaces."""

import json
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Request
from pydantic import ValidationError

from carepulse.appointments.book import book_appointment
from carepulse.appointments.booking_contracts import (
    AppointmentQuery,
    BookAppointment,
    FollowupPreviewQuery,
)
from carepulse.appointments.queries import (
    appointment_detail,
    confirmed_followup_control,
    followup_qualification,
    list_appointments,
)
from carepulse.identity.authorization import protected_transaction
from carepulse.identity.contracts import ContractError, success_response
from carepulse.identity.receipts import lookup_tenant_receipt

router = APIRouter(prefix="/v1/appointments")


def _transaction(request: Request):
    if request.method != "GET" and request.query_params:
        raise ContractError(422)
    identity = request.scope.get("carepulse.identity")
    if identity is None or identity.purpose != "APPOINTMENT_BOOKING":
        raise ContractError(403)
    return protected_transaction(
        request.app.state.db_engine,
        identity,
        request.app.state.service_config.policy.session_idle_minutes,
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
    with _transaction(request) as (connection, actor):
        projection = book_appointment(
            connection,
            actor,
            command,
            request.scope["carepulse.identity"].idempotency_key,
        )
        stored = lookup_tenant_receipt(
            connection,
            actor.organization_id,
            actor.staff_id,
            "appointments.book",
            request.scope["carepulse.identity"].idempotency_key,
        )
        if stored is None:
            raise ContractError(409)
        return success_response(projection, stored["result_revision"], status_code=201)


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
        return success_response(projection, projection["revision"])

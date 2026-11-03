"""B-09 signed booking and read-only operational appointment surfaces."""

from uuid import UUID

from fastapi import APIRouter, Request
from pydantic import ValidationError

from carepulse.appointments.book import book_appointment
from carepulse.appointments.booking_contracts import (
    AppointmentQuery,
    BookAppointment,
    FollowupPreviewQuery,
)
from carepulse.appointments.queries import (
    appointment_detail,
    followup_qualification,
    list_appointments,
)
from carepulse.identity.authorization import protected_transaction
from carepulse.identity.contracts import ContractError, success_response

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


@router.post("")
def create_appointment(command: BookAppointment, request: Request):
    with _transaction(request) as (connection, actor):
        projection = book_appointment(
            connection,
            actor,
            command,
            request.scope["carepulse.identity"].idempotency_key,
        )
        return success_response(projection, projection["revision"], status_code=201)


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
        if preview is not None:
            projection["followup_qualification"] = followup_qualification(
                connection, actor, appointment_id, preview.intended_service_date
            )
        return success_response(projection, projection["revision"])

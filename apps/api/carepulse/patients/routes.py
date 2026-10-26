"""Only the approved B-05 signed private Patient surfaces."""

from uuid import UUID

from fastapi import APIRouter, Request
from pydantic import ValidationError

from carepulse.identity.authorization import protected_transaction
from carepulse.identity.contracts import ContractError, success_response
from carepulse.patients.contracts import (
    ArchivePatient,
    CreatePatient,
    SearchPatients,
    UpdatePatient,
)
from carepulse.patients.queries import (
    patient_projection,
    search_patients,
    visible_patient,
)
from carepulse.patients.service import (
    PatientTarget,
    archive_patient,
    register_patient,
    unarchive_patient,
    update_patient,
)

router = APIRouter(prefix="/v1/patients")


def _transaction(request: Request):
    if request.method != "GET" and request.query_params:
        raise ContractError(422)
    identity = request.scope.get("carepulse.identity")
    if identity is None or identity.purpose != "PATIENT_REGISTRY":
        raise ContractError(403)
    return protected_transaction(
        request.app.state.db_engine,
        identity,
        request.app.state.service_config.policy.session_idle_minutes,
    )


@router.get("")
def list_patients(request: Request):
    parameters = request.query_params
    if len(parameters.multi_items()) != len(parameters):
        raise ContractError(422)
    try:
        query = SearchPatients.model_validate(dict(parameters))
    except ValidationError as invalid:
        raise ContractError(422) from invalid
    with _transaction(request) as (connection, actor):
        listing = search_patients(
            connection, actor, query, request.app.state.service_config.service_key
        )
        return success_response(listing, actor.staff_revision)


@router.post("")
def create_patient(command: CreatePatient, request: Request):
    with _transaction(request) as (connection, actor):
        projection = register_patient(
            connection,
            actor,
            command,
            request.scope["carepulse.identity"].idempotency_key,
        )
        return success_response(
            projection, projection["demographic_revision"], status_code=201
        )


@router.get("/{patient_id}")
def read_patient(patient_id: UUID, request: Request):
    if request.query_params:
        raise ContractError(422)
    with _transaction(request) as (connection, actor):
        projection = patient_projection(visible_patient(connection, actor, patient_id))
        return success_response(projection, projection["demographic_revision"])


@router.patch("/{patient_id}")
def patch_patient(patient_id: UUID, command: UpdatePatient, request: Request):
    with _transaction(request) as (connection, actor):
        projection = update_patient(
            connection, PatientTarget(actor, patient_id), command
        )
        return success_response(projection, projection["demographic_revision"])


@router.post("/{patient_id}/archive")
def archive(patient_id: UUID, command: ArchivePatient, request: Request):
    with _transaction(request) as (connection, actor):
        projection = archive_patient(
            connection, PatientTarget(actor, patient_id), command
        )
        return success_response(projection, projection["lifecycle_revision"])


@router.post("/{patient_id}/unarchive")
def unarchive(patient_id: UUID, command: ArchivePatient, request: Request):
    with _transaction(request) as (connection, actor):
        projection = unarchive_patient(
            connection, PatientTarget(actor, patient_id), command
        )
        return success_response(projection, projection["lifecycle_revision"])

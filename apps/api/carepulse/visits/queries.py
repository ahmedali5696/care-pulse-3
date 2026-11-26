"""Role-scoped Visit reads; clinical narrative is a Doctor-only projection."""

from uuid import UUID

from sqlalchemy import Connection

from carepulse.identity.authorization import AuthorizedStaff
from carepulse.identity.contracts import ContractError
from carepulse.queue.admission_control import _call, _signed_identity
from carepulse.visits.clinical import (
    CLINICAL_FIELDS,
    MUTATION_FIELDS,
    _visit_projection,
)
from carepulse.visits.complete import _ATTESTATION_FIELDS

_OPERATIONAL_FIELDS = MUTATION_FIELDS | {
    "patient_id",
    "doctor_staff_id",
    "service_date",
    "admitted_at",
    "completion_attestation",
    "claim_revision",
    "source_decision_revision",
    "followup_revision",
    "authorization_revision",
    "source_visit_id",
}


def visit_detail(connection: Connection, actor: AuthorizedStaff, visit_id: UUID):
    if actor.role not in ("DOCTOR_OWNER", "RECEPTIONIST"):
        raise ContractError(403)
    _signed_identity("VISIT_READ")
    fields = _OPERATIONAL_FIELDS
    if actor.role == "DOCTOR_OWNER":
        fields |= CLINICAL_FIELDS
    envelope = _call(connection, "SELECT public.f01_b16_read()")
    projection, revision = _visit_projection(
        envelope, visit_id, fields, "visit_revision"
    )
    attestation = projection["completion_attestation"]
    if attestation is not None and (
        not isinstance(attestation, dict) or attestation.keys() != _ATTESTATION_FIELDS
    ):
        raise ContractError(503)
    if actor.role == "DOCTOR_OWNER" and any(
        projection[name] is not None and not isinstance(projection[name], str)
        for name in CLINICAL_FIELDS
    ):
        raise ContractError(503)
    return projection, revision

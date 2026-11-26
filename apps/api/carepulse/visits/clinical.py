"""Doctor-authored Visit changes under the distinct signed clinical scope."""

from uuid import UUID

from sqlalchemy import Connection

from carepulse.identity.authorization import AuthorizedStaff
from carepulse.identity.contracts import ContractError
from carepulse.queue.admission_control import _call, _signed_identity
from carepulse.visits.clinical_contracts import ClinicalPatch

CLINICAL_FIELDS = frozenset(
    (
        "examination",
        "findings",
        "diagnosis",
        "medical_notes",
        "treatment",
        "tests",
        "recommendation",
    )
)
MUTATION_FIELDS = frozenset(
    ("visit_id", "appointment_id", "status", "visit_revision", "appointment_revision")
)


def _visit_projection(envelope, visit_id: UUID, fields: frozenset, revision_field: str):
    if not isinstance(envelope, dict):
        raise ContractError(503)
    projection = envelope.get("projection")
    revision = envelope.get("revision")
    if (
        not isinstance(projection, dict)
        or projection.keys() != fields
        or projection["visit_id"] != str(visit_id)
        or projection["status"] not in ("In Examination", "Completed", "Cancelled")
        or not isinstance(projection["appointment_id"], str)
        or any(
            type(projection[token]) is not int or projection[token] < 0
            for token in ("appointment_revision", "visit_revision")
        )
        or type(revision) is not int
        or revision < 0
        or type(projection[revision_field]) is not int
        or projection[revision_field] != revision
    ):
        raise ContractError(503)
    return projection, revision


def _clinical_result(envelope, visit_id: UUID, command: ClinicalPatch):
    projection, revision = _visit_projection(
        envelope, visit_id, MUTATION_FIELDS, "visit_revision"
    )
    if revision != command.expected_visit_revision + 1:
        raise ContractError(503)
    return projection, revision


def _require_clinical_actor(actor: AuthorizedStaff, key: str):
    if actor.role != "DOCTOR_OWNER":
        raise ContractError(403)
    _signed_identity("VISIT_CLINICAL", key)


def patch_clinical(
    connection: Connection,
    actor: AuthorizedStaff,
    visit_id: UUID,
    command: ClinicalPatch,
    key: str,
):
    _require_clinical_actor(actor, key)
    envelope = _call(connection, "SELECT public.f01_b16_mutate()")
    projection, revision = _clinical_result(envelope, visit_id, command)
    if type(envelope.get("replayed")) is not bool:
        raise ContractError(503)
    return projection, revision, envelope["replayed"]


def recover_clinical(
    connection: Connection,
    actor: AuthorizedStaff,
    visit_id: UUID,
    command: ClinicalPatch,
    key: str,
):
    _require_clinical_actor(actor, key)
    envelope = _call(connection, "SELECT public.f01_b16_replay()")
    if envelope is None:
        raise ContractError(409, "IDEMPOTENCY_CONFLICT")
    return _clinical_result(envelope, visit_id, command)

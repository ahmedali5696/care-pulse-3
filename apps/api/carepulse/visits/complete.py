"""Signed terminal Visit commands and safe receipt recovery."""

from uuid import UUID

from sqlalchemy import Connection

from carepulse.identity.authorization import AuthorizedStaff
from carepulse.identity.contracts import ContractError
from carepulse.queue.admission_control import _call, _signed_identity
from carepulse.visits.clinical import MUTATION_FIELDS, _visit_projection
from carepulse.visits.clinical_contracts import CompleteVisit, ExceptionalCancel

_ATTESTATION_FIELDS = {
    "actor_staff_id",
    "actor_role",
    "completed_at",
    "examination_occurred",
}
_TERMINAL_FIELDS = MUTATION_FIELDS | {"claim_revision"}


def _terminal_purpose(actor: AuthorizedStaff, command, key: str) -> str:
    if isinstance(command, CompleteVisit) and actor.role in (
        "DOCTOR_OWNER",
        "RECEPTIONIST",
    ):
        purpose = "VISIT_COMPLETE"
    elif isinstance(command, ExceptionalCancel) and actor.role == "DOCTOR_OWNER":
        purpose = "VISIT_CANCEL_EXCEPTIONAL"
    else:
        raise ContractError(403)
    _signed_identity(purpose, key)
    return purpose


def _terminal_projection(envelope, actor: AuthorizedStaff, visit_id: UUID, command):
    completing = isinstance(command, CompleteVisit)
    fields = (
        _TERMINAL_FIELDS | {"completion_attestation"}
        if completing
        else _TERMINAL_FIELDS
    )
    projection, revision = _visit_projection(
        envelope, visit_id, fields, "appointment_revision"
    )
    if (
        projection["status"] != ("Completed" if completing else "Cancelled")
        or projection["visit_revision"] != command.expected_visit_revision
        or revision != command.expected_appointment_revision + 1
        or projection["claim_revision"]
        != (
            None
            if command.expected_claim_revision is None
            else command.expected_claim_revision + 1
        )
    ):
        raise ContractError(503)
    if completing:
        attestation = projection["completion_attestation"]
        if (
            not isinstance(attestation, dict)
            or attestation.keys() != _ATTESTATION_FIELDS
            or attestation["actor_staff_id"] != str(actor.staff_id)
            or attestation["actor_role"] != actor.role
            or attestation["examination_occurred"] is not True
            or not isinstance(attestation["completed_at"], str)
        ):
            raise ContractError(503)
    return projection, revision


def _terminal_mutation(connection, actor, visit_id, command, key):
    _terminal_purpose(actor, command, key)
    envelope = _call(connection, "SELECT public.f01_b16_mutate()")
    projection, revision = _terminal_projection(envelope, actor, visit_id, command)
    if type(envelope.get("replayed")) is not bool:
        raise ContractError(503)
    return projection, revision, envelope["replayed"]


def complete_visit(
    connection: Connection,
    actor: AuthorizedStaff,
    visit_id: UUID,
    command: CompleteVisit,
    key: str,
):
    return _terminal_mutation(connection, actor, visit_id, command, key)


def cancel_exceptional(
    connection: Connection,
    actor: AuthorizedStaff,
    visit_id: UUID,
    command: ExceptionalCancel,
    key: str,
):
    return _terminal_mutation(connection, actor, visit_id, command, key)


def recover_terminal(
    connection: Connection,
    actor: AuthorizedStaff,
    visit_id: UUID,
    command: CompleteVisit | ExceptionalCancel,
    key: str,
):
    _terminal_purpose(actor, command, key)
    envelope = _call(connection, "SELECT public.f01_b16_replay()")
    if envelope is None:
        raise ContractError(409, "IDEMPOTENCY_CONFLICT")
    return _terminal_projection(envelope, actor, visit_id, command)

"""B-15 command boundary for the signed, atomic PostgreSQL admission writer."""

from sqlalchemy import Connection

from carepulse.identity.authorization import AuthorizedStaff
from carepulse.identity.contracts import ContractError
from carepulse.queue.admission_contracts import AdmitCommand
from carepulse.queue.admission_control import _call, _signed_identity
from carepulse.visits.create import admitted_visit_projection


def _require_admission_actor(actor: AuthorizedStaff, key: str) -> None:
    if actor.role not in ("DOCTOR_OWNER", "RECEPTIONIST") or not key:
        raise ContractError(403)
    _signed_identity("QUEUE_ADMIT", key)


def _projection(envelope: dict | None, command: AdmitCommand):
    visit = admitted_visit_projection(envelope)
    if (
        visit.appointment_id != command.appointment_id
        or visit.patient_id != command.patient_id
        or visit.service_date != command.date
        or visit.lane != command.lane
        or visit.confirmation_id != command.confirmation_id
    ):
        raise ContractError(503)
    return envelope["projection"], visit.appointment_revision


def admit_patient(
    connection: Connection, actor: AuthorizedStaff, command: AdmitCommand, key: str
):
    _require_admission_actor(actor, key)
    envelope = _call(connection, "SELECT public.f01_b15_admit()")
    projection, revision = _projection(envelope, command)
    if type(envelope.get("replayed")) is not bool:
        raise ContractError(503)
    return projection, revision, envelope["replayed"]


def recover_admission(
    connection: Connection, actor: AuthorizedStaff, command: AdmitCommand, key: str
):
    _require_admission_actor(actor, key)
    envelope = _call(connection, "SELECT public.f01_b15_replay()")
    if envelope is None:
        raise ContractError(409, "IDEMPOTENCY_CONFLICT")
    return _projection(envelope, command)

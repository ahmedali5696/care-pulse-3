"""Signed ordinary cancellation and atomic new-ID Urgent replacement."""

import json
from uuid import UUID

from sqlalchemy import Connection, text

from carepulse.appointments.cancel_contracts import CancelAppointment, ReplaceUrgent
from carepulse.appointments.intake import (
    create_replacement_urgent,
    prepare_replacement_urgent,
)
from carepulse.identity.authorization import AuthorizedStaff
from carepulse.identity.contracts import ContractError
from carepulse.queue.admission_control import _call, _signed_identity

_CANCEL_FIELDS = {
    "appointment_id",
    "status",
    "appointment_revision",
    "claim_revision",
    "queue_day_revision",
    "replacement_appointment_id",
}
_REPLACEMENT_FIELDS = {
    "new_appointment_revision",
    "new_status",
    "new_number",
    "new_queue_entry_id",
    "new_queue_day_id",
    "new_queue_day_revision",
    "new_base_egp",
    "new_net_egp",
}


def _authorize(actor, command, key):
    if isinstance(command, ReplaceUrgent):
        if actor.role != "RECEPTIONIST":
            raise ContractError(403)
        _signed_identity("APPOINTMENT_REPLACE_URGENT", key)
    elif actor.role in ("DOCTOR_OWNER", "RECEPTIONIST"):
        _signed_identity("APPOINTMENT_CANCEL", key)
    else:
        raise ContractError(403)


def _projection(envelope, appointment_id, command):
    if not isinstance(envelope, dict) or type(envelope.get("replayed")) is not bool:
        raise ContractError(503)
    projection, revision = envelope.get("projection"), envelope.get("revision")
    replacing = isinstance(command, ReplaceUrgent)
    fields = _CANCEL_FIELDS | _REPLACEMENT_FIELDS if replacing else _CANCEL_FIELDS
    if (
        not isinstance(projection, dict)
        or projection.keys() != fields
        or any(isinstance(value, (dict, list)) for value in projection.values())
        or type(revision) is not int
        or revision != command.expected_appointment_revision + 1
        or projection["appointment_revision"] != revision
        or projection["appointment_id"] != str(appointment_id)
        or projection["status"] != "Cancelled"
        or projection["claim_revision"]
        != (
            None
            if command.expected_claim_revision is None
            else command.expected_claim_revision + 1
        )
    ):
        raise ContractError(503)
    if replacing:
        _validate_replacement(projection, appointment_id)
    elif projection["replacement_appointment_id"] is not None:
        raise ContractError(503)
    return projection, revision, not envelope["replayed"]


def _validate_replacement(projection, appointment_id):
    try:
        replacement = UUID(projection["replacement_appointment_id"])
        UUID(projection["new_queue_entry_id"])
        UUID(projection["new_queue_day_id"])
    except (ValueError, TypeError, AttributeError) as invalid:
        raise ContractError(503) from invalid
    if (
        replacement == appointment_id
        or projection["new_status"] != "Waiting"
        or projection["new_appointment_revision"] != 0
        or type(projection["new_number"]) is not int
        or projection["new_number"] < 1
    ):
        raise ContractError(503)


def _cancel(connection, replacement_id):
    return _call(
        connection,
        "SELECT public.f01_b17_cancel(CAST(:replacement AS uuid))",
        {"replacement": replacement_id},
    )


def _finish(connection, projection):
    return _call(
        connection,
        "SELECT public.f01_b17_finish(CAST(:projection AS jsonb))",
        {"projection": json.dumps(projection)},
    )


def cancel_appointment(
    connection: Connection,
    actor: AuthorizedStaff,
    appointment_id: UUID,
    command: CancelAppointment,
    key: str,
):
    _authorize(actor, command, key)
    envelope = _call(connection, "SELECT public.f01_b17_replay()")
    if envelope is None:
        envelope = _finish(connection, _cancel(connection, None))
    return _projection(envelope, appointment_id, command)


def _discover_target(connection, actor, appointment_id):
    target = connection.execute(
        text("""
        SELECT patient_id, doctor_staff_id, service_date, status, classification, lane
        FROM public.appointments WHERE organization_id=:tenant AND id=:appointment
    """),
        {"tenant": actor.organization_id, "appointment": appointment_id},
    ).one_or_none()
    if target is None:
        raise ContractError(404)
    return target


def replace_urgent(
    connection: Connection,
    actor: AuthorizedStaff,
    appointment_id: UUID,
    command: ReplaceUrgent,
    key: str,
):
    _authorize(actor, command, key)
    envelope = _call(connection, "SELECT public.f01_b17_replay()")
    if envelope is None:
        target = _discover_target(connection, actor, appointment_id)
        arrival = prepare_replacement_urgent(connection, actor, command, target)
        old = _cancel(connection, arrival.appointment_id)
        new = create_replacement_urgent(connection, actor, arrival)
        envelope = _finish(connection, {**old, **new})
    return _projection(envelope, appointment_id, command)


def recover_cancel_replace(
    connection: Connection,
    actor: AuthorizedStaff,
    appointment_id: UUID,
    command: CancelAppointment | ReplaceUrgent,
    key: str,
):
    _authorize(actor, command, key)
    envelope = _call(connection, "SELECT public.f01_b17_replay()")
    if envelope is None:
        raise ContractError(409, "IDEMPOTENCY_CONFLICT")
    projection, revision, _ = _projection(envelope, appointment_id, command)
    return projection, revision

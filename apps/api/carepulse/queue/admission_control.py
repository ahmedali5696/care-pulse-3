"""B-14 signed policy controls and single-case confirmation authority.

The narrow database functions rederive the typed command from its verified
proof, take ordered locks and commit all domain and evidence writes together.
"""

from typing import Any
from urllib.parse import parse_qsl
from uuid import UUID

from sqlalchemy import Connection, text
from sqlalchemy.exc import DBAPIError

from carepulse.identity.authorization import (
    AuthorizedStaff,
    can_manage_operational_settings,
)
from carepulse.identity.contracts import ContractError
from carepulse.identity.receipts import ReceiptConflict
from carepulse.identity.runtime_identity import current_request
from carepulse.identity.signing import CanonicalEnvelope
from carepulse.queue.admission_contracts import (
    AdmissionModeCommand,
    CapacityCommand,
    IssueConfirmationCommand,
    PauseCommand,
    RevokeConfirmationCommand,
    UsableConfirmationQuery,
)

_PURPOSES = {
    AdmissionModeCommand: "ADMISSION_MODE_UPDATE",
    CapacityCommand: "QUEUE_CAPACITY_UPDATE",
    PauseCommand: "QUEUE_PAUSE",
    IssueConfirmationCommand: "QUEUE_CONFIRMATION_ISSUE",
    RevokeConfirmationCommand: "QUEUE_CONFIRMATION_REVOKE",
}
_SQL_DENIALS = {"42501": 403, "P0002": 404, "23514": 409, "22023": 422}
_BOUND_REVISIONS = {
    "appointment_revision",
    "queue_day_revision",
    "control_revision",
    "capacity_revision",
    "admission_revision",
}
_MODE_FIELDS = {"mode", "admission_revision"}
_CONTROL_FIELDS = {"capacity", "paused", "capacity_revision", "control_revision"}
_ISSUE_FIELDS = {"id", "state", "issued_at", "expires_at"} | _BOUND_REVISIONS
_REVOKE_FIELDS = {"id", "state", "revoked_at", "control_revision"}
_USABLE_FIELDS = {"id", "expires_at"} | _BOUND_REVISIONS
_PROJECTION_CONTRACTS = {
    "ADMISSION_SETTINGS_READ": (_MODE_FIELDS, "admission_revision"),
    "ADMISSION_MODE_UPDATE": (_MODE_FIELDS, "admission_revision"),
    "QUEUE_CAPACITY_UPDATE": (_CONTROL_FIELDS, "control_revision"),
    "QUEUE_PAUSE": (_CONTROL_FIELDS, "control_revision"),
    "QUEUE_CONFIRMATION_ISSUE": (_ISSUE_FIELDS, "appointment_revision"),
    "QUEUE_CONFIRMATION_REVOKE": (_REVOKE_FIELDS, "control_revision"),
    "QUEUE_CONFIRMATION_USABLE": (_USABLE_FIELDS, "appointment_revision"),
}

type MutationCommand = (
    AdmissionModeCommand
    | CapacityCommand
    | PauseCommand
    | IssueConfirmationCommand
    | RevokeConfirmationCommand
)
type Projection = tuple[dict[str, Any], int]
type MutationResult = tuple[dict[str, Any], int, bool]


def _require_settings_actor(actor: AuthorizedStaff) -> None:
    if not can_manage_operational_settings(actor):
        raise ContractError(403)


def _require_doctor(actor: AuthorizedStaff) -> None:
    if actor.role != "DOCTOR_OWNER":
        raise ContractError(403)


def _require_ttl(ttl_minutes: int) -> None:
    if type(ttl_minutes) is not int or not 0 < ttl_minutes <= 2147483647:
        raise ContractError(503)


def _signed_identity(purpose: str, key: str | None = None) -> CanonicalEnvelope:
    request = current_request.get()
    if request is None or request.identity.purpose != purpose:
        raise ContractError(403)
    if key is not None and (not key or request.identity.idempotency_key != key):
        raise ContractError(403)
    return request.identity


def _mutation_identity(
    actor: AuthorizedStaff, command: MutationCommand, key: str
) -> CanonicalEnvelope:
    if type(command) is AdmissionModeCommand:
        _require_settings_actor(actor)
    else:
        _require_doctor(actor)
    purpose = _PURPOSES.get(type(command))
    if purpose is None:
        raise ContractError(422)
    return _signed_identity(purpose, key)


def _call(connection: Connection, statement: str, parameters: dict | None = None):
    try:
        return connection.scalar(text(statement), parameters or {})
    except DBAPIError as denied:
        sqlstate = getattr(denied.orig, "sqlstate", None)
        if sqlstate == "23505":
            raise ReceiptConflict("Idempotency key already committed") from denied
        if sqlstate == "40001":
            raise ContractError(409, "STALE_REVISION") from denied
        if sqlstate == "CP001":
            raise ContractError(409, "IDEMPOTENCY_CONFLICT") from denied
        if sqlstate in _SQL_DENIALS:
            raise ContractError(_SQL_DENIALS[sqlstate]) from denied
        raise


def _projection(envelope: dict | None, purpose: str) -> Projection:
    if not isinstance(envelope, dict):
        raise ContractError(503)
    projection = envelope.get("projection")
    revision = envelope.get("revision")
    fields, revision_field = _PROJECTION_CONTRACTS[purpose]
    if (
        not isinstance(projection, dict)
        or projection.keys() != fields
        or type(revision) is not int
        or revision < 0
        or projection[revision_field] != revision
    ):
        raise ContractError(503)
    return projection, revision


def _mutate(
    connection: Connection, purpose: str, ttl_minutes: int | None = None
) -> MutationResult:
    envelope = _call(
        connection,
        "SELECT public.f01_b14_mutate(CAST(:ttl_minutes AS integer))",
        {"ttl_minutes": ttl_minutes},
    )
    projection, revision = _projection(envelope, purpose)
    replayed = envelope.get("replayed")
    if type(replayed) is not bool:
        raise ContractError(503)
    return projection, revision, replayed


def get_admission_settings(
    connection: Connection, actor: AuthorizedStaff
) -> Projection:
    _require_settings_actor(actor)
    identity = _signed_identity("ADMISSION_SETTINGS_READ")
    return _projection(
        _call(connection, "SELECT public.f01_b14_settings()"), identity.purpose
    )


def set_admission_mode(
    connection: Connection,
    actor: AuthorizedStaff,
    command: AdmissionModeCommand,
    key: str,
) -> MutationResult:
    identity = _mutation_identity(actor, command, key)
    return _mutate(connection, identity.purpose)


def set_capacity(
    connection: Connection, actor: AuthorizedStaff, command: CapacityCommand, key: str
) -> MutationResult:
    identity = _mutation_identity(actor, command, key)
    return _mutate(connection, identity.purpose)


def set_pause(
    connection: Connection, actor: AuthorizedStaff, command: PauseCommand, key: str
) -> MutationResult:
    identity = _mutation_identity(actor, command, key)
    return _mutate(connection, identity.purpose)


def issue_confirmation(
    connection: Connection,
    actor: AuthorizedStaff,
    command: IssueConfirmationCommand,
    key: str,
    *,
    ttl_minutes: int,
) -> MutationResult:
    identity = _mutation_identity(actor, command, key)
    _require_ttl(ttl_minutes)
    return _mutate(connection, identity.purpose, ttl_minutes)


def _require_revoke_target(
    identity: CanonicalEnvelope, confirmation_id: UUID | None
) -> None:
    if (
        identity.path.lower()
        != f"/v1/queue/admission-confirmations/{confirmation_id}/revoke"
    ):
        raise ContractError(404)


def revoke_confirmation(
    connection: Connection,
    actor: AuthorizedStaff,
    confirmation_id: UUID,
    command: RevokeConfirmationCommand,
    key: str,
) -> MutationResult:
    identity = _mutation_identity(actor, command, key)
    _require_revoke_target(identity, confirmation_id)
    return _mutate(connection, identity.purpose)


def recover_control(
    connection: Connection,
    actor: AuthorizedStaff,
    command: MutationCommand,
    key: str,
    *,
    confirmation_id: UUID | None = None,
) -> Projection:
    identity = _mutation_identity(actor, command, key)
    if isinstance(command, RevokeConfirmationCommand):
        _require_revoke_target(identity, confirmation_id)
    envelope = _call(connection, "SELECT public.f01_b14_replay()")
    if envelope is None:
        raise ContractError(409, "IDEMPOTENCY_CONFLICT")
    return _projection(envelope, identity.purpose)


def get_usable_confirmation(
    connection: Connection,
    actor: AuthorizedStaff,
    query: UsableConfirmationQuery,
    *,
    ttl_minutes: int,
) -> Projection:
    _require_ttl(ttl_minutes)
    identity = _signed_identity("QUEUE_CONFIRMATION_USABLE")
    expected = [
        ("appointment_id", str(query.appointment_id)),
        ("date", query.date.isoformat()),
        ("lane", query.lane),
    ]
    signed = [
        (name, field.lower() if name == "appointment_id" else field)
        for name, field in parse_qsl(identity.query, keep_blank_values=True)
    ]
    if actor.role not in ("DOCTOR_OWNER", "RECEPTIONIST") or signed != expected:
        raise ContractError(404)
    envelope = _call(connection, "SELECT public.f01_b14_usable()")
    if envelope is None:
        raise ContractError(404)
    return _projection(envelope, identity.purpose)

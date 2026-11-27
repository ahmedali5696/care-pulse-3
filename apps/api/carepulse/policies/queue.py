"""B-13 signed queue-policy publication and settings projection."""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, StrictInt
from sqlalchemy import Connection

from carepulse.identity.authorization import (
    AuthorizedStaff,
    can_manage_operational_settings,
)
from carepulse.identity.contracts import ContractError
from carepulse.queue.admission_contracts import Revision
from carepulse.queue.admission_control import _call, _signed_identity


class QueuePolicyCommand(BaseModel):
    model_config = ConfigDict(extra="forbid")

    routine_order: Literal["BOOKED_NUMBER", "ARRIVAL_FIFO"]
    late_depth: StrictInt = Field(ge=0, le=2147483647)
    expected_policy_revision: Revision


_POLICY_FIELDS = {
    "routine_order",
    "late_depth",
    "policy_version",
    "policy_revision",
    "effective_on",
    "pending_effective",
}


def _require_settings_actor(actor: AuthorizedStaff) -> None:
    if not can_manage_operational_settings(actor):
        raise ContractError(403)


def _projection(envelope: dict | None):
    if not isinstance(envelope, dict):
        raise ContractError(503)
    projection, revision = envelope.get("projection"), envelope.get("revision")
    if (
        not isinstance(projection, dict)
        or projection.keys() != _POLICY_FIELDS
        or type(revision) is not int
        or revision < 1
        or projection["policy_revision"] != revision
    ):
        raise ContractError(503)
    return projection, revision


def _published_projection(envelope: dict | None, command: QueuePolicyCommand):
    projection, revision = _projection(envelope)
    if (
        projection["routine_order"] != command.routine_order
        or projection["late_depth"] != command.late_depth
        or revision != command.expected_policy_revision + 1
    ):
        raise ContractError(503)
    return projection, revision


def get_queue_policy(connection: Connection, actor: AuthorizedStaff):
    _require_settings_actor(actor)
    _signed_identity("SETTINGS_QUEUE_READ")
    return _projection(_call(connection, "SELECT public.f01_b13_settings()"))


def set_queue_policy(
    connection: Connection,
    actor: AuthorizedStaff,
    command: QueuePolicyCommand,
    key: str,
):
    _require_settings_actor(actor)
    _signed_identity("SETTINGS_QUEUE_UPDATE", key)
    envelope = _call(connection, "SELECT public.f01_b13_publish()")
    projection, revision = _published_projection(envelope, command)
    if type(envelope.get("replayed")) is not bool:
        raise ContractError(503)
    return projection, revision, envelope["replayed"]


def recover_queue_policy(
    connection: Connection,
    actor: AuthorizedStaff,
    command: QueuePolicyCommand,
    key: str,
):
    _require_settings_actor(actor)
    _signed_identity("SETTINGS_QUEUE_UPDATE", key)
    envelope = _call(connection, "SELECT public.f01_b13_replay()")
    if envelope is None:
        raise ContractError(409, "IDEMPOTENCY_CONFLICT")
    return _published_projection(envelope, command)

"""B-13 signed original-day Routine placement command boundary."""

from datetime import date as DateValue
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, StrictStr, field_validator
from sqlalchemy import Connection

from carepulse.identity.authorization import AuthorizedStaff
from carepulse.identity.contracts import ContractError
from carepulse.queue.admission_contracts import Revision
from carepulse.queue.admission_control import _call, _signed_identity


class PlacementEntry(BaseModel):
    model_config = ConfigDict(extra="forbid")

    queue_entry_id: UUID
    expected_queue_entry_revision: Revision


class PlacementCommand(BaseModel):
    model_config = ConfigDict(extra="forbid")

    date: DateValue
    entries: list[PlacementEntry] = Field(min_length=1)
    expected_queue_day_revision: Revision
    reason: StrictStr

    @field_validator("date", mode="before")
    @classmethod
    def canonical_date(cls, day):
        if type(day) is DateValue:
            return day
        if not isinstance(day, str) or DateValue.fromisoformat(day).isoformat() != day:
            raise ValueError("Canonical service date required")
        return DateValue.fromisoformat(day)

    @field_validator("entries")
    @classmethod
    def unique_entries(cls, entries: list[PlacementEntry]) -> list[PlacementEntry]:
        if len({entry.queue_entry_id for entry in entries}) != len(entries):
            raise ValueError("Duplicate queue entry")
        return entries

    @field_validator("reason")
    @classmethod
    def nonblank_reason(cls, reason: str) -> str:
        if not reason.strip() or any(
            ord(letter) < 32 or 0xD800 <= ord(letter) <= 0xDFFF for letter in reason
        ):
            raise ValueError("Reason required")
        return reason.strip()


def _require_placement_actor(actor: AuthorizedStaff, key: str) -> None:
    if actor.role != "RECEPTIONIST":
        raise ContractError(403)
    _signed_identity("QUEUE_PLACEMENT", key)


def _projection(envelope: dict | None, command: PlacementCommand):
    if not isinstance(envelope, dict):
        raise ContractError(503)
    projection, revision = envelope.get("projection"), envelope.get("revision")
    if (
        not isinstance(projection, dict)
        or projection.keys() != {"date", "queue_day_revision", "entries"}
        or type(revision) is not int
        or revision != command.expected_queue_day_revision + 1
        or projection["queue_day_revision"] != revision
        or projection["date"] != command.date.isoformat()
        or not _matching_entries(projection["entries"], command.entries)
    ):
        raise ContractError(503)
    return projection, revision


def _matching_entries(projected, requested: list[PlacementEntry]) -> bool:
    if not isinstance(projected, list) or len(projected) != len(requested):
        return False
    for rank, (saved, entry) in enumerate(zip(projected, requested, strict=True), 1):
        if (
            not isinstance(saved, dict)
            or saved.keys()
            != {"queue_entry_id", "routine_rank", "queue_entry_revision"}
            or saved["queue_entry_id"] != str(entry.queue_entry_id)
            or saved["routine_rank"] != rank
            or type(saved["queue_entry_revision"]) is not int
            or saved["queue_entry_revision"] < entry.expected_queue_entry_revision
        ):
            return False
    return True


def place_routine(
    connection: Connection, actor: AuthorizedStaff, command: PlacementCommand, key: str
):
    _require_placement_actor(actor, key)
    envelope = _call(connection, "SELECT public.f01_b13_place()")
    projection, revision = _projection(envelope, command)
    if type(envelope.get("replayed")) is not bool:
        raise ContractError(503)
    return projection, revision, envelope["replayed"]


def recover_placement(
    connection: Connection, actor: AuthorizedStaff, command: PlacementCommand, key: str
):
    _require_placement_actor(actor, key)
    envelope = _call(connection, "SELECT public.f01_b13_replay()")
    if envelope is None:
        raise ContractError(409, "IDEMPOTENCY_CONFLICT")
    return _projection(envelope, command)

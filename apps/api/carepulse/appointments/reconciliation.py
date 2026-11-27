"""Signed original-day reconciliation and verified manual absence."""

from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, StrictStr, field_validator
from sqlalchemy import Connection

from carepulse.appointments.booking_contracts import CanonicalServiceDate, Revision
from carepulse.identity.authorization import AuthorizedStaff
from carepulse.identity.contracts import ContractError
from carepulse.queue.admission_control import _call, _signed_identity
from carepulse.visits.eligibility_contracts import _reason


class ReconciliationQuery(BaseModel):
    model_config = ConfigDict(extra="forbid")

    date: CanonicalServiceDate
    lane: Literal["MAIN", "EMERGENCY"] | None = None
    limit: int = Field(default=50, ge=1, le=100)
    offset: int = Field(default=0, ge=0, le=2147483647)


class ManualNoShow(BaseModel):
    model_config = ConfigDict(extra="forbid")

    verified_absence: Literal[True]
    reason: StrictStr
    expected_appointment_revision: Revision
    expected_claim_revision: Revision | None = None

    @field_validator("verified_absence", mode="before")
    @classmethod
    def positive_absence(cls, absence: object) -> object:
        if absence is not True:
            raise ValueError("Verified absence must be JSON true")
        return absence

    @field_validator("reason")
    @classmethod
    def valid_reason(cls, reason: str) -> str:
        return _reason(reason)


def reconciliation(
    connection: Connection, actor: AuthorizedStaff, query: ReconciliationQuery
):
    _signed_identity("RECONCILIATION_READ")
    projection = _call(connection, "SELECT public.f01_b20_reconciliation()")
    if (
        not isinstance(projection, dict)
        or projection.get("service_date") != query.date.isoformat()
    ):
        raise ContractError(503)
    return projection, projection["review_revision"]


def manual_no_show(
    connection: Connection,
    actor: AuthorizedStaff,
    appointment_id: UUID,
    command: ManualNoShow,
):
    _signed_identity("APPOINTMENT_MANUAL_NO_SHOW")
    projection = _call(connection, "SELECT public.f01_b20_staff_mutation()")
    if (
        not isinstance(projection, dict)
        or projection.get("appointment_id") != str(appointment_id)
        or projection.get("status") != "No-show"
        or projection.get("appointment_revision")
        != command.expected_appointment_revision + 1
    ):
        raise ContractError(503)
    return projection, projection["appointment_revision"]

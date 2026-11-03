"""Doctor-only follow-up decision and confirmed-claim extension inputs."""

from typing import Annotated, Literal
from uuid import UUID

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StrictStr,
    field_validator,
    model_validator,
)

from carepulse.appointments.booking_contracts import CanonicalServiceDate

Revision = Annotated[int, Field(strict=True, ge=0)]


def _reason(value: str) -> str:
    if not value.strip() or any(ord(character) < 32 for character in value):
        raise ValueError("A nonblank clinical reason is required")
    return value.strip()


class FollowupDecision(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: Literal["DEFAULT", "OPTED_OUT", "DEADLINE"]
    deadline_date: CanonicalServiceDate | None = None
    reason: StrictStr | None = None

    @model_validator(mode="after")
    def valid_decision(self):
        if self.kind == "DEFAULT":
            if self.deadline_date is not None:
                raise ValueError("Default decision has no override deadline")
        elif (
            self.reason is None
            or self.kind == "DEADLINE"
            and self.deadline_date is None
        ):
            raise ValueError("An exception requires its reason and deadline")
        elif self.kind == "OPTED_OUT" and self.deadline_date is not None:
            raise ValueError("Opt-out has no deadline")
        return self

    @field_validator("reason")
    @classmethod
    def valid_reason(cls, reason: str | None) -> str | None:
        return _reason(reason) if reason is not None else None


class FollowupDecisionPatch(BaseModel):
    model_config = ConfigDict(extra="forbid")

    followup_decision: FollowupDecision
    expected_visit_revision: Revision


class ExtendFollowup(BaseModel):
    model_config = ConfigDict(extra="forbid")

    appointment_id: UUID
    extended_deadline: CanonicalServiceDate
    reason: StrictStr
    expected_visit_revision: Revision
    expected_claim_revision: Revision

    @field_validator("reason")
    @classmethod
    def valid_reason(cls, reason: str) -> str:
        return _reason(reason)

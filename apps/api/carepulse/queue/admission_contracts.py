"""Strict B-14 admission policy and case-confirmation inputs."""

from datetime import date as DateValue
from typing import Annotated, Literal
from uuid import UUID

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StrictBool,
    StrictInt,
    StrictStr,
    field_validator,
)

Revision = Annotated[StrictInt, Field(ge=1, le=9223372036854775807)]
AppointmentRevision = Annotated[StrictInt, Field(ge=0, le=9223372036854775807)]


class AdmissionModeCommand(BaseModel):
    model_config = ConfigDict(extra="forbid")

    mode: Literal["BOTH", "DOCTOR", "RECEPTIONIST"]
    expected_admission_revision: Revision


class CapacityCommand(BaseModel):
    model_config = ConfigDict(extra="forbid")

    capacity: StrictInt = Field(ge=1, le=2147483647)
    expected_capacity_revision: Revision
    expected_control_revision: Revision


class PauseCommand(BaseModel):
    model_config = ConfigDict(extra="forbid")

    paused: StrictBool
    expected_control_revision: Revision


class UsableConfirmationQuery(BaseModel):
    model_config = ConfigDict(extra="forbid")

    appointment_id: UUID
    date: DateValue
    lane: Literal["MAIN", "EMERGENCY"]

    @field_validator("date", mode="before")
    @classmethod
    def canonical_date(cls, value):
        if type(value) is DateValue:
            return value
        if (
            not isinstance(value, str)
            or DateValue.fromisoformat(value).isoformat() != value
        ):
            raise ValueError("Canonical service date required")
        return DateValue.fromisoformat(value)


class IssueConfirmationCommand(UsableConfirmationQuery):
    patient_id: UUID
    expected_appointment_revision: AppointmentRevision
    expected_queue_day_revision: Revision
    expected_control_revision: Revision
    expected_capacity_revision: Revision
    expected_admission_revision: Revision


class RevokeConfirmationCommand(BaseModel):
    model_config = ConfigDict(extra="forbid")

    expected_control_revision: Revision
    revoke_reason: StrictStr

    @field_validator("revoke_reason")
    @classmethod
    def required_reason(cls, value: str) -> str:
        if not value.strip() or any(ord(letter) < 32 for letter in value):
            raise ValueError("Reason required")
        return value.strip()


class AdmitCommand(IssueConfirmationCommand):
    expected_queue_entry_revision: Revision
    expected_head_appointment_id: UUID
    non_head_reason: StrictStr | None = None
    confirmation_id: UUID | None = None
    physical_presence_confirmed: Literal[True] | None = None

    @field_validator("non_head_reason")
    @classmethod
    def nonblank_reason(cls, reason: str | None) -> str | None:
        if reason is not None and (
            not reason.strip()
            or any(
                ord(letter) < 32 or 0xD800 <= ord(letter) <= 0xDFFF for letter in reason
            )
        ):
            raise ValueError("Reason required")
        return reason.strip() if reason is not None else None

    @field_validator("physical_presence_confirmed", mode="before")
    @classmethod
    def explicit_presence(cls, assertion):
        if assertion is not True:
            raise ValueError("Positive physical presence required")
        return assertion

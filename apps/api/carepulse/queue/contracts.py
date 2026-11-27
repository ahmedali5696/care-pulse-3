"""Strict B-12 check-in and dated queue query DTOs."""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, StrictBool, StrictInt

from carepulse.appointments.booking_contracts import CanonicalServiceDate


class CheckInAppointment(BaseModel):
    model_config = ConfigDict(extra="forbid")

    physical_presence_confirmed: StrictBool
    expected_appointment_revision: StrictInt = Field(ge=0)
    expected_queue_day_revision: StrictInt | None = Field(default=None, ge=0)


class QueueQuery(BaseModel):
    model_config = ConfigDict(extra="forbid")

    date: CanonicalServiceDate
    lane: Literal["MAIN", "EMERGENCY"]
    limit: int = Field(default=50, ge=1, le=100)
    offset: int = Field(default=0, ge=0, le=9223372036854775807)

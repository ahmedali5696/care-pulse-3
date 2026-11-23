"""Strict B-12 check-in and dated queue query DTOs."""

from datetime import date as DateValue
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, StrictBool, StrictInt


class CheckInAppointment(BaseModel):
    model_config = ConfigDict(extra="forbid")

    physical_presence_confirmed: StrictBool
    expected_appointment_revision: StrictInt = Field(ge=0)
    expected_queue_day_revision: StrictInt | None = Field(default=None, ge=0)


class QueueQuery(BaseModel):
    model_config = ConfigDict(extra="forbid")

    date: DateValue
    lane: Literal["MAIN", "EMERGENCY"]
    limit: int = Field(default=50, ge=1, le=100)
    offset: int = Field(default=0, ge=0)

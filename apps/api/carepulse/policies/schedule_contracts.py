"""Strict full-replacement calendar preview and activation commands."""

from datetime import date, time
from typing import Annotated, Literal
from uuid import UUID

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StrictBool,
    StrictStr,
    field_validator,
)

Revision = Annotated[int, Field(strict=True, ge=0)]


class StrictScheduleModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class LocalSlotInput(StrictScheduleModel):
    starts_at: StrictStr
    ends_at: StrictStr

    @field_validator("starts_at", "ends_at")
    @classmethod
    def valid_time(cls, value: str) -> str:
        if len(value) != 5 or value[2] != ":":
            raise ValueError("Expected HH:MM")
        try:
            parsed = time.fromisoformat(value)
        except ValueError as invalid:
            raise ValueError("Expected HH:MM") from invalid
        if parsed.strftime("%H:%M") != value:
            raise ValueError("Expected HH:MM")
        return value


class WeekdaySlotInput(LocalSlotInput):
    weekday: Annotated[int, Field(strict=True, ge=1, le=7)]


class DatedSlotInput(LocalSlotInput):
    service_date: StrictStr

    @field_validator("service_date")
    @classmethod
    def valid_date(cls, value: str) -> str:
        try:
            if date.fromisoformat(value).isoformat() != value:
                raise ValueError("Expected YYYY-MM-DD")
        except ValueError as invalid:
            raise ValueError("Expected YYYY-MM-DD") from invalid
        return value


class CutoffInput(StrictScheduleModel):
    enabled: StrictBool
    local_time: StrictStr | None

    @field_validator("local_time")
    @classmethod
    def valid_cutoff(cls, value: str | None) -> str | None:
        if value is not None:
            LocalSlotInput.valid_time(value)
        return value


class ScheduleConfigInput(StrictScheduleModel):
    booking_mode: Literal["OPEN", "EXACT_TIME"]
    duration_minutes: Annotated[int, Field(strict=True, gt=0)] | None
    holiday_booking_enabled: StrictBool
    allow_overlap: StrictBool
    no_show_cutoff: CutoffInput
    working_weekdays: list[Annotated[int, Field(strict=True, ge=1, le=7)]]
    weekly_work_periods: list[WeekdaySlotInput]
    weekly_breaks: list[WeekdaySlotInput]
    weekly_manual_slots: list[WeekdaySlotInput]
    open_override_dates: list[StrictStr]
    override_work_periods: list[DatedSlotInput]
    manual_slots_by_date: dict[StrictStr, list[LocalSlotInput]]
    holidays: list[StrictStr]

    @field_validator("open_override_dates", "holidays")
    @classmethod
    def valid_dates(cls, values: list[str]) -> list[str]:
        for value in values:
            DatedSlotInput.valid_date(value)
        return values

    @field_validator("manual_slots_by_date")
    @classmethod
    def valid_manual_dates(cls, values: dict[str, list[LocalSlotInput]]):
        for key in values:
            DatedSlotInput.valid_date(key)
        return values


class OverlapConfirmation(StrictScheduleModel):
    capacity: Annotated[int, Field(strict=True, ge=1)]
    capacity_revision: Revision


class SchedulePatch(StrictScheduleModel):
    expected_schedule_revision: Revision
    reason: StrictStr
    config: ScheduleConfigInput
    overlap_capacity_confirmation: OverlapConfirmation | None

    @field_validator("reason")
    @classmethod
    def required_reason(cls, value: str) -> str:
        if not value.strip() or any(ord(letter) < 32 for letter in value):
            raise ValueError("Reason required")
        return value.strip()


SchedulePreviewRequest = SchedulePatch


class ScheduleActivate(StrictScheduleModel):
    preview_id: UUID
    expected_schedule_revision: Revision
    expected_capacity_revision: Revision | None
    reason: StrictStr

    @field_validator("reason")
    @classmethod
    def required_reason(cls, value: str) -> str:
        return SchedulePatch.required_reason(value)

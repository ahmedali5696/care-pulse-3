"""Strict operational booking inputs; all appointment authority is server-derived."""

from datetime import date
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

from carepulse.policies.pricing import parse_money
from carepulse.policies.pricing_contracts import DiscountInput

Revision = Annotated[int, Field(strict=True, ge=0)]


class BookAppointment(BaseModel):
    model_config = ConfigDict(extra="forbid")

    patient_id: UUID
    service_date: date
    requested_service_type: Literal["NEW_EXAMINATION", "FOLLOW_UP"]
    source_visit_id: UUID | None = None
    starts_at: StrictStr | None = None
    ends_at: StrictStr | None = None
    discount: DiscountInput
    expected_base_egp: StrictStr
    expected_net_egp: StrictStr
    expected_price_revision: Revision
    expected_followup_revision: Revision | None = None
    expected_schedule_revision: Revision
    expected_capacity_revision: Revision | None = None
    expected_source_revision: Revision | None = None

    @field_validator("expected_base_egp", "expected_net_egp")
    @classmethod
    def valid_expected_money(cls, amount: str) -> str:
        parse_money(amount)
        return amount

    @model_validator(mode="after")
    def booking_intent(self):
        if self.requested_service_type != "NEW_EXAMINATION":
            raise ValueError("Follow-up booking requires B-10 qualification")
        if (
            self.source_visit_id is not None
            or self.expected_source_revision is not None
        ):
            raise ValueError("New Examination cannot claim a source Visit")
        if (self.starts_at is None) != (self.ends_at is None):
            raise ValueError("Both timed endpoints are required together")
        return self


class AppointmentQuery(BaseModel):
    model_config = ConfigDict(extra="forbid")

    patient_id: UUID | None = None
    service_date: date | None = None
    status: (
        Literal[
            "Booked", "Waiting", "In Examination", "Completed", "Cancelled", "No-show"
        ]
        | None
    ) = None
    lane: Literal["MAIN", "EMERGENCY"] | None = None
    limit: Annotated[int, Field(ge=1, le=100)] = 25
    cursor: StrictStr | None = None

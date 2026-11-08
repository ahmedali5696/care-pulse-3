"""Strict operational booking inputs; all appointment authority is server-derived."""

from datetime import date
from re import fullmatch
from typing import Annotated, Literal
from uuid import UUID

from pydantic import (
    BaseModel,
    BeforeValidator,
    ConfigDict,
    Field,
    StrictStr,
    field_validator,
    model_validator,
)

from carepulse.policies.pricing import parse_money
from carepulse.policies.pricing_contracts import DiscountInput

Revision = Annotated[int, Field(strict=True, ge=0)]


def _canonical_service_date(value: object) -> str:
    if (
        not isinstance(value, str)
        or fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", value) is None
    ):
        raise ValueError("Expected YYYY-MM-DD")
    return value


CanonicalServiceDate = Annotated[date, BeforeValidator(_canonical_service_date)]


class FallbackAcknowledgment(BaseModel):
    model_config = ConfigDict(extra="forbid")

    service_type: Literal["NEW_EXAMINATION"]
    discount_reaffirmed: Literal[True]


class NewBookingDateException(BaseModel):
    model_config = ConfigDict(extra="forbid")

    selected_service_date: CanonicalServiceDate
    reason: StrictStr | None = None

    @field_validator("reason")
    @classmethod
    def valid_reason(cls, value: str | None) -> str | None:
        if value is not None and (
            not value.strip() or any(ord(character) < 32 for character in value)
        ):
            raise ValueError("A supplied exception reason must be nonblank")
        return value.strip() if value is not None else None


class BookAppointment(BaseModel):
    model_config = ConfigDict(extra="forbid")

    patient_id: UUID
    service_date: CanonicalServiceDate
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
    expected_source_decision_revision: Revision | None = None
    expected_visit_revision: Revision | None = None
    new_booking_date_exception: NewBookingDateException | None = None
    fallback_acknowledgment: FallbackAcknowledgment | None = None

    @field_validator("expected_base_egp", "expected_net_egp")
    @classmethod
    def valid_expected_money(cls, amount: str) -> str:
        parse_money(amount)
        return amount

    @model_validator(mode="after")
    def booking_intent(self):
        old = self.expected_source_revision is not None
        old_field = "expected_source_revision" in self.model_fields_set
        new = (
            self.expected_source_decision_revision is not None
            or self.expected_visit_revision is not None
            or "new_booking_date_exception" in self.model_fields_set
        )
        if self.requested_service_type == "NEW_EXAMINATION" and (
            self.source_visit_id is not None
            or old
            or new
            or self.fallback_acknowledgment is not None
        ):
            raise ValueError("New Examination cannot claim a source Visit")
        if self.requested_service_type == "FOLLOW_UP":
            if self.source_visit_id is None and (old or new):
                raise ValueError("Source tokens require a referenced Visit")
            if self.source_visit_id is not None and (
                old == new
                or old_field
                and new
                or old
                and (
                    self.expected_visit_revision is not None
                    or self.expected_source_decision_revision is not None
                )
                or new
                and (
                    self.expected_visit_revision is None
                    or self.expected_source_decision_revision is None
                )
            ):
                raise ValueError(
                    "Exactly one complete source revision family is required"
                )
            if self.expected_followup_revision is None:
                raise ValueError("Follow-up policy revision is required")
            if "new_booking_date_exception" in self.model_fields_set and (
                self.new_booking_date_exception is None
                or self.new_booking_date_exception.selected_service_date
                != self.service_date
                or self.fallback_acknowledgment is not None
                or not new
            ):
                raise ValueError("NEW_DATE requires a matching v2 preferential booking")
        if (self.starts_at is None) != (self.ends_at is None):
            raise ValueError("Both timed endpoints are required together")
        return self

    @property
    def source_family(self) -> Literal["common", "v1", "v2"]:
        if self.source_visit_id is None:
            return "common"
        return "v1" if self.expected_source_revision is not None else "v2"


class AppointmentQuery(BaseModel):
    model_config = ConfigDict(extra="forbid")

    patient_id: UUID | None = None
    service_date: CanonicalServiceDate | None = None
    status: (
        Literal[
            "Booked", "Waiting", "In Examination", "Completed", "Cancelled", "No-show"
        ]
        | None
    ) = None
    lane: Literal["MAIN", "EMERGENCY"] | None = None
    limit: Annotated[int, Field(ge=1, le=100)] = 25
    cursor: StrictStr | None = None


class FollowupPreviewQuery(BaseModel):
    model_config = ConfigDict(extra="forbid")
    intended_service_date: CanonicalServiceDate

"""Strict, target-scoped B-17 cancellation and replacement inputs."""

from typing import Literal

from pydantic import BaseModel, ConfigDict, StrictStr, field_validator, model_validator

from carepulse.appointments.booking_contracts import Revision
from carepulse.appointments.intake_contracts import _Presence
from carepulse.policies.pricing import parse_money
from carepulse.policies.pricing_contracts import DiscountInput
from carepulse.visits.eligibility_contracts import _reason


class CancelAppointment(BaseModel):
    model_config = ConfigDict(extra="forbid")

    expected_appointment_revision: Revision
    expected_queue_day_revision: Revision | None = None
    expected_claim_revision: Revision | None = None
    expected_source_revision: Revision | None = None
    expected_authorization_revision: Revision | None = None
    reason: StrictStr

    @field_validator("reason")
    @classmethod
    def valid_reason(cls, reason: str) -> str:
        return _reason(reason)


class ReplaceUrgent(CancelAppointment, _Presence):
    confirmation: Literal["REPLACE"]
    discount: DiscountInput
    expected_base_egp: StrictStr
    expected_net_egp: StrictStr
    expected_price_revision: Revision
    expected_schedule_revision: Revision
    expected_capacity_revision: Revision | None = None
    starts_at: StrictStr | None = None
    ends_at: StrictStr | None = None

    @field_validator("expected_base_egp", "expected_net_egp")
    @classmethod
    def valid_expected_money(cls, amount: str) -> str:
        parse_money(amount)
        return amount

    @model_validator(mode="after")
    def require_paired_endpoints(self):
        if self.model_fields_set & {"starts_at", "ends_at"} and (
            self.starts_at is None or self.ends_at is None
        ):
            raise ValueError("Both timed endpoints must be supplied together")
        return self

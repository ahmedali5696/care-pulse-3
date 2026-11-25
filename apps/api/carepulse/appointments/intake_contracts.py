"""Distinct, strict inputs for direct physical intake commands."""

from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, StrictStr, field_validator, model_validator

from carepulse.appointments.booking_contracts import BookAppointment, Revision
from carepulse.policies.pricing import parse_money
from carepulse.policies.pricing_contracts import DiscountInput


class _Presence(BaseModel):
    model_config = ConfigDict(extra="forbid")

    physical_presence_confirmed: Literal[True]

    @field_validator("physical_presence_confirmed", mode="before")
    @classmethod
    def require_presence(cls, value: object) -> object:
        if value is not True:
            raise ValueError("Physical presence must be JSON true")
        return value


class RoutineIntake(BookAppointment, _Presence):
    """Routine intake uses the existing ordinary booking intent and price fields."""


class EmergencyIntake(_Presence):
    patient_id: UUID
    discount: DiscountInput
    expected_base_egp: StrictStr
    expected_net_egp: StrictStr
    expected_price_revision: Revision

    @field_validator("expected_base_egp", "expected_net_egp")
    @classmethod
    def valid_expected_money(cls, amount: str) -> str:
        parse_money(amount)
        return amount


class UrgentIntake(EmergencyIntake):
    expected_schedule_revision: Revision
    expected_capacity_revision: Revision | None = None
    starts_at: StrictStr | None = None
    ends_at: StrictStr | None = None

    @model_validator(mode="after")
    def require_paired_endpoints(self):
        fields = self.model_fields_set
        if bool(fields & {"starts_at", "ends_at"}) and (
            self.starts_at is None or self.ends_at is None
        ):
            raise ValueError("Both timed endpoints must be supplied together")
        return self

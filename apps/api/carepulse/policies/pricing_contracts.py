"""Strict client inputs for B-06 settings and reason-free operational quotes."""

from typing import Annotated, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StrictBool,
    StrictStr,
    field_validator,
    model_validator,
)

from carepulse.policies.pricing import parse_money, parse_percent

Revision = Annotated[int, Field(strict=True, ge=0)]
DiscountKind = Literal["NONE", "FIXED", "PERCENT"]
ServiceType = Literal["NEW_EXAMINATION", "FOLLOW_UP"]


class PricingPatch(BaseModel):
    model_config = ConfigDict(extra="forbid")

    standard_new_exam_egp: StrictStr | None = None
    preferential_followup_egp: StrictStr | None = None
    urgent_base_egp: StrictStr | None = None
    urgent_enabled: StrictBool | None = None
    followup_last_day_inclusive: (
        Annotated[int, Field(strict=True, ge=0, le=365)] | None
    ) = None
    expected_price_revision: Revision
    expected_followup_revision: Revision | None = None
    reason: StrictStr

    @field_validator(
        "standard_new_exam_egp", "preferential_followup_egp", "urgent_base_egp"
    )
    @classmethod
    def valid_money(cls, amount: str | None) -> str | None:
        if amount is not None:
            parse_money(amount)
        return amount

    @field_validator("reason")
    @classmethod
    def valid_reason(cls, value: str) -> str:
        if not value.strip() or any(ord(c) < 32 for c in value):
            raise ValueError("A nonblank reason is required")
        return value.strip()

    @model_validator(mode="after")
    def validate_patch(self):
        fields = self.model_fields_set
        if not fields.intersection(
            {
                "standard_new_exam_egp",
                "preferential_followup_egp",
                "urgent_base_egp",
                "urgent_enabled",
                "followup_last_day_inclusive",
            }
        ):
            raise ValueError("At least one price or follow-up setting must change")
        if "followup_last_day_inclusive" in fields and (
            self.followup_last_day_inclusive is None
            or self.expected_followup_revision is None
        ):
            raise ValueError("Follow-up edit requires the expected revision")
        return self


class DiscountInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    discount_type: DiscountKind
    discount_value: StrictStr
    discount_reason: StrictStr | None = None

    @model_validator(mode="after")
    def valid_discount(self):
        (parse_percent if self.discount_type == "PERCENT" else parse_money)(
            self.discount_value
        )
        return self


class QuoteQuery(BaseModel):
    model_config = ConfigDict(extra="forbid")

    service_type: ServiceType | None = None
    discount_type: DiscountKind | None = None
    discount_value: StrictStr | None = None

    @model_validator(mode="after")
    def valid_query(self):
        fields = (self.service_type, self.discount_type, self.discount_value)
        if any(item is not None for item in fields):
            if not all(item is not None for item in fields):
                raise ValueError("Quote requires service type and discount")
            (parse_percent if self.discount_type == "PERCENT" else parse_money)(
                self.discount_value
            )
        return self

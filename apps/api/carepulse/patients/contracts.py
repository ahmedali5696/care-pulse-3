"""B-05's exhaustive demographic/intake request and response contracts."""

from datetime import date
from decimal import Decimal
from typing import Annotated
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, StrictStr, field_validator

from carepulse.patients.demographics import validate_name_parts

INTAKE_FIELDS = frozenset(
    {
        "preliminary_medical_history",
        "chronic_illnesses",
        "drug_allergies",
        "food_allergies",
        "prior_examination_notes",
        "preliminary_test_information",
    }
)
Revision = Annotated[int, Field(strict=True, ge=0)]


class PatientFields(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)

    phone_raw: StrictStr | None = None
    date_of_birth: date | None = None
    declared_age: Decimal | None = None
    age_reference_date: date | None = None
    preliminary_medical_history: StrictStr | None = None
    chronic_illnesses: StrictStr | None = None
    drug_allergies: StrictStr | None = None
    food_allergies: StrictStr | None = None
    prior_examination_notes: StrictStr | None = None
    preliminary_test_information: StrictStr | None = None

    @field_validator("phone_raw", *INTAKE_FIELDS)
    @classmethod
    def valid_text(cls, content: str | None) -> str | None:
        if content is not None and any(
            char == "\x00" or 0xD800 <= ord(char) <= 0xDFFF for char in content
        ):
            raise ValueError("Invalid text")
        return content


class CreatePatient(PatientFields):
    name_parts: list[StrictStr]

    @field_validator("name_parts")
    @classmethod
    def valid_name(cls, parts: list[str]) -> list[str]:
        validate_name_parts(parts)
        for part in parts:
            cls.valid_text(part)
        return parts


class UpdatePatient(PatientFields):
    expected_demographic_revision: Revision
    name_parts: list[StrictStr] | None = None

    @field_validator("name_parts")
    @classmethod
    def valid_name(cls, parts: list[str] | None) -> list[str]:
        validate_name_parts(parts)
        for part in parts:
            cls.valid_text(part)
        return parts


class ReadPatient(CreatePatient):
    id: UUID
    phone_normalized: str | None
    archived: bool
    demographic_revision: Revision
    lifecycle_revision: Revision


class SearchPatients(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = ""
    phone: str = ""
    archived: bool = False
    limit: int = Field(default=25, ge=1, le=100)
    cursor: str | None = Field(default=None, min_length=64, max_length=64)


class ArchivePatient(BaseModel):
    model_config = ConfigDict(extra="forbid")

    reason: str = Field(min_length=1)
    expected_lifecycle_revision: Revision

    @field_validator("reason")
    @classmethod
    def valid_reason(cls, reason: str) -> str:
        if not reason.strip() or any(ord(char) < 32 for char in reason):
            raise ValueError("A reason is required")
        return reason.strip()

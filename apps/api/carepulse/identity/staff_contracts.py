"""The fixed, owner-only Receptionist command payloads."""

from typing import Annotated
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, StringConstraints

Reason = Annotated[
    str, StringConstraints(strip_whitespace=True, min_length=1, max_length=512)
]


class StaffCommand(BaseModel):
    model_config = ConfigDict(extra="forbid")

    reason: Reason
    expected_revision: int = Field(ge=0, strict=True)
    expected_security_revision: int = Field(ge=0, strict=True)


class CreateReceptionist(BaseModel):
    model_config = ConfigDict(extra="forbid")

    email: str = Field(min_length=3, max_length=254)
    name: str = Field(min_length=1, max_length=128)
    prepared_user_id: str = Field(min_length=1, max_length=255)
    reason: Reason


class SetReceptionistGrant(StaffCommand):
    bulk_reschedule: bool = Field(strict=True)


class AcceptReceptionist(BaseModel):
    model_config = ConfigDict(extra="forbid")

    invitation_id: UUID
    proof: str = Field(min_length=43, max_length=43, pattern=r"^[A-Za-z0-9_-]{43}$")
    prepared_user_id: str = Field(min_length=1, max_length=255)
    expected_lifecycle_revision: int = Field(ge=0, strict=True)
    expected_invitation_revision: int = Field(ge=0, strict=True)
    expected_staff_revision: int = Field(ge=0, strict=True)
    expected_security_revision: int = Field(ge=0, strict=True)

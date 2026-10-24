"""Private signed first-Owner activation; no public operator API."""

import re
from uuid import UUID, uuid4

from fastapi import APIRouter, Request
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from carepulse.db import transaction
from carepulse.identity.authorization import bootstrap_owner_organization
from carepulse.identity.contracts import ContractError, success_response

router = APIRouter()


class OwnerActivation(BaseModel):
    model_config = ConfigDict(extra="forbid")

    invitation_id: UUID
    proof: str = Field(min_length=43, max_length=43)
    prepared_user_id: str = Field(min_length=1, max_length=255)
    expected_lifecycle_revision: int = Field(ge=0, strict=True)
    expected_invitation_revision: int = Field(ge=0, strict=True)


@router.post("/v1/provisioning/owner-activate")
def activate_first_owner(body: OwnerActivation, request: Request):
    envelope = request.scope.get("carepulse.identity")
    if envelope is None:
        raise ContractError(403)
    if not re.fullmatch(r"[A-Za-z0-9_-]{43}", body.proof):
        raise ContractError(422)
    clinic = bootstrap_owner_organization(envelope)
    with transaction(request.app.state.db_engine, clinic) as connection:
        try:
            activation = (
                connection.execute(
                    text("""
                  SELECT * FROM public.activate_first_owner(
                    :clinic, :invite, :proof, :user, :lifecycle, :revision, :new_staff)
                """),
                    {
                        "clinic": clinic,
                        "invite": body.invitation_id,
                        "proof": body.proof,
                        "user": body.prepared_user_id,
                        "lifecycle": body.expected_lifecycle_revision,
                        "revision": body.expected_invitation_revision,
                        "new_staff": uuid4(),
                    },
                )
                .mappings()
                .one()
            )
        except DBAPIError as failure:
            state = getattr(failure.orig, "sqlstate", "")
            if state == "42501":
                raise ContractError(403) from failure
            if state in ("40001", "23505", "23514"):
                raise ContractError(409) from failure
            raise
    return success_response(
        {
            "organization_id": str(clinic),
            "staff_id": str(activation["owner_id"]),
            "state": "ACTIVE",
        },
        int(activation["current_revision"]),
    )

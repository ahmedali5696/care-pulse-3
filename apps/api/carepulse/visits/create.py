"""Validate the operational Visit projection returned by the atomic admission writer."""

from datetime import date, datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, StrictInt, ValidationError

from carepulse.identity.contracts import ContractError


class AdmittedVisit(BaseModel):
    model_config = ConfigDict(extra="forbid")

    visit_id: UUID
    appointment_id: UUID
    patient_id: UUID
    service_date: date
    lane: Literal["MAIN", "EMERGENCY"]
    status: Literal["In Examination"]
    appointment_revision: StrictInt
    queue_day_revision: StrictInt
    queue_entry_revision: StrictInt
    control_revision: StrictInt
    capacity_revision: StrictInt
    admission_revision: StrictInt
    routine_cursor: StrictInt
    confirmation_id: UUID | None
    admitted_at: datetime


def admitted_visit_projection(envelope: dict | None) -> AdmittedVisit:
    if not isinstance(envelope, dict):
        raise ContractError(503)
    try:
        visit = AdmittedVisit.model_validate(envelope.get("projection"))
        if (
            type(envelope.get("revision")) is not int
            or envelope["revision"] != visit.appointment_revision
        ):
            raise ValueError("Invalid admission revision")
    except (ValueError, ValidationError) as invalid:
        raise ContractError(503) from invalid
    return visit

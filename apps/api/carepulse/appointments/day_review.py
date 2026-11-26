"""Review metadata changes without terminating attendance or clinical care."""

from datetime import date

from pydantic import BaseModel, ConfigDict, StrictStr, field_validator, model_validator
from sqlalchemy import Connection

from carepulse.appointments.booking_contracts import Revision
from carepulse.identity.contracts import ContractError
from carepulse.queue.admission_control import _call, _signed_identity
from carepulse.visits.eligibility_contracts import _reason


class ReviewCommand(BaseModel):
    model_config = ConfigDict(extra="forbid")

    expected_review_revision: Revision
    notes: StrictStr | None = None
    reason: StrictStr | None = None

    @field_validator("reason")
    @classmethod
    def valid_reason(cls, reason: str | None) -> str | None:
        return _reason(reason) if reason is not None else None

    @model_validator(mode="after")
    def amendment_reason(self):
        if self.expected_review_revision > 0 and self.reason is None:
            raise ValueError("Review amendments require a reason")
        return self


class CloseCommand(ReviewCommand):
    expected_main_queue_day_revision: Revision
    expected_emergency_queue_day_revision: Revision


def record_review(connection: Connection, service_date: date, command: ReviewCommand):
    closing = isinstance(command, CloseCommand)
    _signed_identity("DAY_CLOSE" if closing else "DAY_REVIEW")
    projection = _call(connection, "SELECT public.f01_b20_staff_mutation()")
    if (
        not isinstance(projection, dict)
        or projection.get("service_date") != service_date.isoformat()
        or projection.get("revision") != command.expected_review_revision + 1
        or projection.get("disposition") != ("FINAL_CLOSED" if closing else "REVIEWED")
    ):
        raise ContractError(503)
    return projection, projection["revision"]

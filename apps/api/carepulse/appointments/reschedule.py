"""Signed individual same-ID appointment changes, with frozen financial evidence."""

from datetime import UTC, datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, StrictStr, field_validator, model_validator
from sqlalchemy import Connection

from carepulse.appointments.book import _parse_interval, validate_booking_date
from carepulse.appointments.booking_contracts import CanonicalServiceDate, Revision
from carepulse.appointments.intake import validate_urgent_eligibility
from carepulse.identity.authorization import AuthorizedStaff
from carepulse.identity.contracts import ContractError, require_matching_revision
from carepulse.policies.schedule import date_rules
from carepulse.policies.schedule_service import _schedule_head, load_current_schedule
from carepulse.policies.slots_service import (
    validate_new_timed_capacity,
    validate_timed_candidate,
)
from carepulse.queue.admission_control import _call, _signed_identity
from carepulse.visits.eligibility_contracts import _reason


class RescheduleAppointment(BaseModel):
    model_config = ConfigDict(extra="forbid")

    service_date: CanonicalServiceDate
    reason: StrictStr
    expected_appointment_revision: Revision
    expected_schedule_revision: Revision
    expected_capacity_revision: Revision | None = None
    expected_price_revision: Revision | None = None
    expected_queue_day_revision: Revision | None = None
    expected_claim_revision: Revision | None = None
    expected_source_decision_revision: Revision | None = None
    expected_visit_revision: Revision | None = None
    expected_followup_revision: Revision | None = None
    expected_authorization_revision: Revision | None = None
    extension_event_id: UUID | None = None
    physical_presence_confirmed: Literal[True] | None = None
    starts_at: StrictStr | None = None
    ends_at: StrictStr | None = None

    @field_validator("reason")
    @classmethod
    def valid_reason(cls, reason: str) -> str:
        return _reason(reason)

    @field_validator("physical_presence_confirmed", mode="before")
    @classmethod
    def positive_presence(cls, value: object) -> object:
        if value is not True:
            raise ValueError("Physical presence must be JSON true")
        return value

    @model_validator(mode="after")
    def paired_endpoints(self):
        if self.model_fields_set & {"starts_at", "ends_at"} and (
            self.starts_at is None or self.ends_at is None
        ):
            raise ValueError("Both timed endpoints must be supplied together")
        return self


_PURPOSE = "APPOINTMENT_RESCHEDULE"
_FIELDS = {
    "appointment_id",
    "appointment_revision",
    "status",
    "service_date",
    "number",
    "booking_mode",
    "starts_at",
    "ends_at",
    "queue_day_revision",
    "claim_revision",
}


def _authorize(actor: AuthorizedStaff, key: str) -> None:
    if actor.role not in ("DOCTOR_OWNER", "RECEPTIONIST"):
        raise ContractError(403)
    _signed_identity(_PURPOSE, key)


def _projection(envelope, appointment_id, command):
    if not isinstance(envelope, dict):
        raise ContractError(503)
    projection, revision = envelope.get("projection"), envelope.get("revision")
    if (
        not isinstance(projection, dict)
        or projection.keys() != _FIELDS
        or type(revision) is not int
        or revision != command.expected_appointment_revision + 1
        or projection["appointment_revision"] != revision
        or projection["appointment_id"] != str(appointment_id)
        or projection["service_date"] != command.service_date.isoformat()
        or projection["status"] not in ("Booked", "Waiting")
    ):
        raise ContractError(503)
    return projection, revision


def _validate_destination(connection, actor, command, classification):
    now = datetime.now(UTC)
    tenant = actor.organization_id
    if classification == "URGENT":
        _, start, end, capacity = validate_urgent_eligibility(
            connection, actor, command, command.service_date, now
        )
    else:
        schedule = load_current_schedule(connection, tenant)
        require_matching_revision(
            command.expected_schedule_revision,
            _schedule_head(connection, tenant)["revision"],
        )
        validate_booking_date(command.service_date, now)
        if not date_rules(schedule, command.service_date).eligible:
            raise ContractError(409)
        start, end = _parse_interval(
            command, schedule.booking_mode, command.service_date
        )
        capacity = (
            None
            if start is None
            else validate_timed_candidate(
                connection,
                tenant,
                command.service_date,
                start,
                end,
                now,
                command.expected_capacity_revision,
            )
        )
    if start is not None:
        # The activated B-18 projection omits exactly the signed target ID.
        validate_new_timed_capacity(
            connection, tenant, command.service_date, start, end, capacity
        )


def reschedule_appointment(
    connection: Connection,
    actor: AuthorizedStaff,
    appointment_id: UUID,
    command: RescheduleAppointment,
    key: str,
):
    _authorize(actor, key)
    envelope = _call(connection, "SELECT public.f01_b18_replay()")
    if envelope is None:
        target = _call(connection, "SELECT public.f01_b18_prepare()")
        if not isinstance(target, dict) or target.get("appointment_id") != str(
            appointment_id
        ):
            raise ContractError(503)
        _validate_destination(connection, actor, command, target["classification"])
        envelope = _call(connection, "SELECT public.f01_b18_apply()")
    return _projection(envelope, appointment_id, command)


def recover_committed_reschedule(
    connection: Connection,
    actor: AuthorizedStaff,
    appointment_id: UUID,
    command: RescheduleAppointment,
    key: str,
):
    _authorize(actor, key)
    envelope = _call(connection, "SELECT public.f01_b18_replay()")
    if envelope is None:
        raise ContractError(409, "IDEMPOTENCY_CONFLICT")
    return _projection(envelope, appointment_id, command)

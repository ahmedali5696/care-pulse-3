"""B-10 transactional qualification of an explicitly reviewed follow-up source."""

from dataclasses import dataclass, replace
from datetime import date, datetime
from typing import Literal
from uuid import UUID

from sqlalchemy import Connection, text

from carepulse.appointments.followup_rules import (
    cairo_completion_date,
    default_deadline,
    intended_date_allowed,
    validate_exception_deadline,
)
from carepulse.identity.contracts import require_matching_revision


@dataclass(frozen=True)
class Qualification:
    outcome: Literal["PREFERENTIAL", "BUSY", "FALLBACK", "NOT_FOUND"]
    source_visit_id: UUID | None = None
    source_appointment_id: UUID | None = None
    source_revision: int | None = None
    source_doctor_id: UUID | None = None
    completion_local_date: date | None = None
    approved_deadline: date | None = None
    decision_kind: str | None = None
    decision_actor_staff_id: UUID | None = None


def _locked_source(connection: Connection, tenant: UUID, source_visit_id: UUID):
    source_appointment_id = connection.scalar(
        text("""
            SELECT appointment_id FROM public.visits
            WHERE organization_id=:tenant AND id=:source_visit
        """),
        {"tenant": tenant, "source_visit": source_visit_id},
    )
    if source_appointment_id is None:
        return None
    source_appointment = (
        connection.execute(
            text("""
            SELECT id, patient_id, doctor_staff_id, status, service_type, classification
            FROM public.appointments
            WHERE organization_id=:tenant AND id=:appointment FOR UPDATE
        """),
            {"tenant": tenant, "appointment": source_appointment_id},
        )
        .mappings()
        .one_or_none()
    )
    source_visit = (
        connection.execute(
            text("""
            SELECT id, appointment_id, patient_id, doctor_staff_id,
                   clinical_revision, followup_decision
            FROM public.visits
            WHERE organization_id=:tenant AND id=:source_visit FOR UPDATE
        """),
            {"tenant": tenant, "source_visit": source_visit_id},
        )
        .mappings()
        .one_or_none()
    )
    if (
        source_appointment is None
        or source_visit is None
        or source_visit["appointment_id"] != source_appointment["id"]
    ):
        return None
    return source_appointment, source_visit


def _approved_deadline(completion: date, decision: object) -> date | None:
    if not isinstance(decision, dict):
        return None
    if decision.get("kind") == "DEFAULT":
        return default_deadline(completion)
    if decision.get("kind") != "DEADLINE":
        return None
    stated_date = decision.get("deadline_date")
    if not isinstance(stated_date, str):
        return None
    try:
        deadline = date.fromisoformat(stated_date)
        if deadline.isoformat() != stated_date:
            return None
        return validate_exception_deadline(
            completion, deadline, decision.get("reason", "")
        )
    except (TypeError, ValueError, AttributeError):
        return None


def qualify_followup(
    connection: Connection,
    tenant: UUID,
    patient_id: UUID,
    intended_date: date,
    source_visit_id: UUID | None,
    expected_source_revision: int | None,
    now: datetime,
) -> Qualification:
    """Read under the booking transaction's patient, policy and day-counter locks."""
    if source_visit_id is None:
        return Qualification("FALLBACK")
    locked = _locked_source(connection, tenant, source_visit_id)
    if locked is None:
        return Qualification("NOT_FOUND")

    appointment, visit = locked
    if appointment["patient_id"] != patient_id or visit["patient_id"] != patient_id:
        return Qualification("NOT_FOUND")
    require_matching_revision(expected_source_revision, visit["clinical_revision"])
    claims = (
        connection.execute(
            text("""
            SELECT state FROM public.followup_claims
            WHERE organization_id=:tenant AND source_visit_id=:source_visit
            ORDER BY id FOR UPDATE
        """),
            {"tenant": tenant, "source_visit": source_visit_id},
        )
        .scalars()
        .all()
    )
    qualification = Qualification(
        "FALLBACK",
        source_visit_id=source_visit_id,
        source_appointment_id=appointment["id"],
        source_revision=visit["clinical_revision"],
        source_doctor_id=visit["doctor_staff_id"],
    )

    current_doctor_id = connection.scalar(
        text("""
            SELECT id FROM public.staff_accounts
            WHERE organization_id=:tenant AND role='DOCTOR_OWNER'
              AND enabled AND verified
        """),
        {"tenant": tenant},
    )
    if (
        appointment["doctor_staff_id"] != visit["doctor_staff_id"]
        or visit["doctor_staff_id"] != current_doctor_id
        or appointment["status"] != "Completed"
        or appointment["service_type"] != "NEW_EXAMINATION"
        or appointment["classification"] not in ("ROUTINE", "URGENT", "EMERGENCY")
    ):
        return qualification
    if "ACTIVE" in claims:
        return replace(qualification, outcome="BUSY")
    if "CONSUMED" in claims or visit["clinical_revision"] < 1:
        return qualification

    completed_at = connection.scalar(
        text("""
            SELECT occurred_at FROM public.appointment_history
            WHERE organization_id=:tenant AND appointment_id=:appointment
              AND new_status='Completed'
            ORDER BY appointment_revision DESC LIMIT 1
        """),
        {"tenant": tenant, "appointment": appointment["id"]},
    )
    if completed_at is None or completed_at > now:
        return qualification
    completion = cairo_completion_date(completed_at)
    decision = visit["followup_decision"]
    deadline = _approved_deadline(completion, decision)
    if deadline is None or not intended_date_allowed(
        completion, intended_date, deadline
    ):
        return qualification

    decision_actor = connection.scalar(
        text("""
            SELECT actor_staff_id FROM public.clinical_revisions
            WHERE organization_id=:tenant AND visit_id=:source_visit
              AND change_kind IN ('DECISION_DEFAULT', 'DECISION_EXCEPTION')
            ORDER BY revision DESC LIMIT 1
        """),
        {"tenant": tenant, "source_visit": source_visit_id},
    )
    if decision_actor != visit["doctor_staff_id"]:
        return qualification
    return replace(
        qualification,
        outcome="PREFERENTIAL",
        completion_local_date=completion,
        approved_deadline=deadline,
        decision_kind=decision["kind"],
        decision_actor_staff_id=decision_actor,
    )

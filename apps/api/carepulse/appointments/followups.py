"""B-10 protected operational qualification of a completed source examination."""

from dataclasses import dataclass, replace
from datetime import date, datetime
from typing import Literal
from uuid import UUID

from sqlalchemy import Connection, text

from carepulse.appointments.followup_rules import (
    cairo_completion_date,
    current_last_day_inclusive,
    default_deadline,
)
from carepulse.identity.contracts import ContractError, require_matching_revision
from carepulse.policies.pricing_service import current_price_state


@dataclass(frozen=True)
class Qualification:
    outcome: Literal["PREFERENTIAL", "BUSY", "FALLBACK", "NOT_FOUND"]
    source_visit_id: UUID | None = None
    source_appointment_id: UUID | None = None
    source_revision: int | None = None
    source_decision_revision: int | None = None
    visit_revision: int | None = None
    source_doctor_id: UUID | None = None
    completion_local_date: date | None = None
    approved_deadline: date | None = None
    decision_kind: str | None = None
    decision_actor_staff_id: UUID | None = None
    decision_actor_role: str | None = None


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


def _approved_deadline(
    completion: date, decision: object, last_day_inclusive: int
) -> date | None:
    if decision is None:
        return default_deadline(completion, last_day_inclusive)
    if not isinstance(decision, dict):
        return None
    if decision.get("kind") == "DEFAULT":
        return default_deadline(completion, last_day_inclusive)
    if decision.get("kind") != "DEADLINE":
        return None
    stated_date = decision.get("deadline_date")
    if not isinstance(stated_date, str):
        return None
    try:
        deadline = date.fromisoformat(stated_date)
        if deadline.isoformat() != stated_date:
            return None
        if deadline < completion:
            return None
        # The SQL proof deliberately omits historical Doctor reason text. Both
        # the persisted decision writer and the proof reader validate it at rest.
        return deadline
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
    *,
    expected_visit_revision: int | None = None,
    exception_declared: bool = False,
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
    # The source Appointment and Visit are already locked. A row-lock read of
    # the protected source state would require an unnecessary UPDATE grant.
    source_state = (
        connection.execute(
            text("""
                SELECT source_revision, provenance_state, decision,
                       decision_actor_staff_id, decision_actor_role
                FROM public.followup_source_state
                WHERE organization_id=:tenant AND visit_id=:visit
            """),
            {"tenant": tenant, "visit": source_visit_id},
        )
        .mappings()
        .one_or_none()
    )
    if source_state is None:
        proof = connection.scalar(
            text("SELECT public.b10_operational_source_proof(:tenant, :visit)"),
            {"tenant": tenant, "visit": source_visit_id},
        )
        source_state = {
            "source_revision": 0,
            "provenance_state": proof["state"],
            "decision": proof.get("decision"),
            "decision_actor_staff_id": proof.get("actor_staff_id"),
            "decision_actor_role": proof.get("actor_role"),
        }
    if expected_visit_revision is None:
        require_matching_revision(expected_source_revision, visit["clinical_revision"])
    else:
        require_matching_revision(expected_visit_revision, visit["clinical_revision"])
        require_matching_revision(
            expected_source_revision, source_state["source_revision"]
        )
    claims = (
        connection.execute(
            text("""
            SELECT state FROM public.followup_claims
            WHERE organization_id=:tenant AND source_visit_id=:source_visit
            ORDER BY id
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
        source_decision_revision=source_state["source_revision"],
        visit_revision=visit["clinical_revision"],
        source_doctor_id=visit["doctor_staff_id"],
        decision_actor_staff_id=source_state["decision_actor_staff_id"],
        decision_actor_role=source_state["decision_actor_role"],
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
    if "CONSUMED" in claims or source_state["provenance_state"] != "VERIFIED":
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
    if not connection.scalar(
        text("""
            SELECT EXISTS (
                SELECT 1 FROM public.completion_attestations
                WHERE organization_id=:tenant AND visit_id=:visit
                  AND appointment_id=:appointment AND examination_occurred
            )
        """),
        {"tenant": tenant, "visit": source_visit_id, "appointment": appointment["id"]},
    ):
        return qualification
    completion = cairo_completion_date(completed_at)
    decision = source_state["decision"]
    try:
        last_day = current_last_day_inclusive(
            current_price_state(connection, tenant)["FOLLOWUP"]["payload"]
        )
    except (TypeError, ValueError) as invalid:
        raise ContractError(409) from invalid
    deadline = _approved_deadline(completion, decision, last_day)
    if deadline is None or intended_date < completion:
        return qualification
    if intended_date > deadline and (
        not exception_declared
        or (
            source_state["decision_actor_role"] == "DOCTOR_OWNER"
            and isinstance(decision, dict)
            and decision.get("kind") == "DEADLINE"
        )
    ):
        return qualification
    return replace(
        qualification,
        outcome="PREFERENTIAL",
        completion_local_date=completion,
        approved_deadline=deadline,
        decision_kind=decision["kind"] if isinstance(decision, dict) else "NO_DECISION",
    )

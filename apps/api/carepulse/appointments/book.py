"""One atomic Routine booking engine for New Examinations and preferential care."""

import re
from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import uuid4

from sqlalchemy import Connection, insert, text
from sqlalchemy.exc import IntegrityError

from carepulse.appointments.booking_contracts import BookAppointment
from carepulse.appointments.followup_tables import followup_claims
from carepulse.appointments.followups import qualify_followup
from carepulse.appointments.persistence import (
    AppointmentDraft,
    allocate_day_number,
    insert_appointment_records,
)
from carepulse.appointments.queries import appointment_detail
from carepulse.identity.authorization import AuthorizedStaff
from carepulse.identity.contracts import (
    ContractError,
    normalized_request_digest,
    require_matching_revision,
)
from carepulse.identity.receipts import (
    Receipt,
    TenantReceiptQuery,
    insert_tenant_receipt,
    resolve_tenant_receipt,
)
from carepulse.notifications.evidence import append_outbox, append_sensitive_audit
from carepulse.patients.lifecycle import lock_patient_for_new_appointment
from carepulse.policies.pricing import (
    parse_money,
    quote_price,
    validate_discount_reason,
)
from carepulse.policies.pricing_service import _money_output, current_price_state
from carepulse.policies.schedule import date_rules
from carepulse.policies.schedule_service import _schedule_head, load_current_schedule
from carepulse.policies.slots import parse_timed_endpoint
from carepulse.policies.slots_service import CAIRO, validate_timed_booking
from carepulse.policies.versions import StalePolicyRevision, lock_policy_heads


@dataclass(frozen=True)
class BookingPlan:
    draft: AppointmentDraft
    claim: dict | None = None


def _doctor_id(connection: Connection, tenant):
    doctor = connection.scalar(
        text("""
            SELECT id FROM public.staff_accounts
            WHERE organization_id=:tenant AND role='DOCTOR_OWNER'
              AND enabled AND verified
        """),
        {"tenant": tenant},
    )
    if doctor is None:
        raise ContractError(409)
    return doctor


def _snapshot(
    connection: Connection,
    actor: AuthorizedStaff,
    command: BookAppointment,
    service_type: str,
    provenance: dict | None,
):
    state = current_price_state(connection, actor.organization_id)
    require_matching_revision(
        command.expected_price_revision, state["PRICE"]["revision"]
    )
    if command.expected_followup_revision is not None:
        require_matching_revision(
            command.expected_followup_revision, state["FOLLOWUP"]["revision"]
        )
    standard_base = _money_output(state["PRICE"]["payload"]["standard_new_exam_egp"])
    base = _money_output(
        state["PRICE"]["payload"][
            "preferential_followup_egp"
            if service_type == "FOLLOW_UP"
            else "standard_new_exam_egp"
        ]
    )
    if base is None or standard_base is None:
        raise ContractError(409)
    discount = command.discount
    try:
        validate_discount_reason(discount.discount_type, discount.discount_reason)
        quote = quote_price(
            base,
            standard_base,
            service_type,
            "ROUTINE",
            discount.discount_type,
            discount.discount_value,
        )
    except (ValueError, ArithmeticError) as invalid:
        raise ContractError(422) from invalid
    if quote.base_egp != parse_money(
        command.expected_base_egp
    ) or quote.net_egp != parse_money(command.expected_net_egp):
        raise ContractError(409, "STALE_REVISION")
    return {
        "service_type": service_type,
        "classification": "ROUTINE",
        "base_egp": quote.base_egp,
        "discount_type": quote.discount_type,
        "discount_value": quote.discount_value,
        "discount_reason": discount.discount_reason,
        "is_free": quote.is_free,
        "net_egp": quote.net_egp,
        "pricing_version": state["PRICE"]["version"],
        "creation_standard_base_egp": parse_money(standard_base),
        "entitlement_provenance": provenance,
    }


def _timed_interval(connection: Connection, actor, command, booking_mode, now):
    if booking_mode == "OPEN":
        if command.starts_at is not None or command.ends_at is not None:
            raise ContractError(422)
        return None, None
    if command.starts_at is None or command.ends_at is None:
        raise ContractError(422)
    try:
        start = parse_timed_endpoint(command.starts_at, command.service_date)
        end = parse_timed_endpoint(command.ends_at, command.service_date)
    except ValueError as invalid:
        raise ContractError(422) from invalid
    validate_timed_booking(
        connection,
        actor.organization_id,
        command.service_date,
        start,
        end,
        now,
        command.expected_capacity_revision,
    )
    return start, end


def validate_booking_date(service_date, now):
    if service_date < now.astimezone(CAIRO).date():
        raise ContractError(409)


def _booking_plan(connection: Connection, actor, command, now):
    schedule = load_current_schedule(connection, actor.organization_id)
    require_matching_revision(
        command.expected_schedule_revision,
        _schedule_head(connection, actor.organization_id)["revision"],
    )
    validate_booking_date(command.service_date, now)
    if not date_rules(schedule, command.service_date).eligible:
        raise ContractError(409)
    start, end = _timed_interval(connection, actor, command, schedule.booking_mode, now)
    doctor_id = _doctor_id(connection, actor.organization_id)
    claim = None
    service_type = "NEW_EXAMINATION"
    if command.requested_service_type == "FOLLOW_UP":
        qualification = qualify_followup(
            connection,
            actor.organization_id,
            command.patient_id,
            command.service_date,
            command.source_visit_id,
            command.expected_source_revision,
            now,
        )
        if qualification.outcome == "NOT_FOUND":
            raise ContractError(404)
        if qualification.outcome == "BUSY":
            raise ContractError(409)
        if qualification.outcome == "PREFERENTIAL":
            if command.fallback_acknowledgment is not None:
                raise ContractError(409, "STALE_REVISION")
            service_type = "FOLLOW_UP"
            state = current_price_state(connection, actor.organization_id)
            claim = {
                "source_visit_id": qualification.source_visit_id,
                "source_appointment_id": qualification.source_appointment_id,
                "completion_local_date": qualification.completion_local_date,
                "approved_deadline": qualification.approved_deadline,
                "policy_snapshot": {
                    "source_visit_id": str(qualification.source_visit_id),
                    "source_revision": qualification.source_revision,
                    "doctor_staff_id": str(doctor_id),
                    "actor_staff_id": str(actor.staff_id),
                    "authorized": True,
                    "policy_revision": state["FOLLOWUP"]["version"],
                },
            }
        elif command.fallback_acknowledgment is None:
            raise ContractError(409)
    return BookingPlan(
        AppointmentDraft(
            patient_id=command.patient_id,
            doctor_staff_id=doctor_id,
            service_date=command.service_date,
            status="Booked",
            service_type=service_type,
            classification="ROUTINE",
            lane="MAIN",
            booking_mode=schedule.booking_mode,
            starts_at=start,
            ends_at=end,
            price_snapshot=_snapshot(
                connection,
                actor,
                command,
                service_type,
                claim["policy_snapshot"] if claim else None,
            ),
        ),
        claim,
    )


def book_appointment(
    connection: Connection,
    actor: AuthorizedStaff,
    command: BookAppointment,
    idempotency_key: str,
) -> dict:
    """Caller owns protected_transaction; all mutations commit or roll back together."""
    if actor.role not in ("DOCTOR_OWNER", "RECEPTIONIST"):
        raise ContractError(403)
    if not re.fullmatch(r"[A-Za-z0-9._:-]{1,128}", idempotency_key):
        raise ContractError(422)
    receipt = TenantReceiptQuery(
        actor,
        "appointments.book",
        idempotency_key,
        normalized_request_digest(command),
    )
    cached = resolve_tenant_receipt(connection, receipt)
    if cached is not None:
        return cached[0]
    lock_patient_for_new_appointment(
        connection, actor.organization_id, command.patient_id
    )
    # A concurrent same-patient request may have committed while the row was locked.
    cached = resolve_tenant_receipt(connection, receipt)
    if cached is not None:
        return cached[0]
    try:
        lock_policy_heads(
            connection,
            actor.organization_id,
            {kind: "SHARE" for kind in ("SCHEDULE", "CAPACITY", "FOLLOWUP", "PRICE")},
        )
    except StalePolicyRevision as invalid:
        raise ContractError(409, "STALE_REVISION") from invalid
    number = allocate_day_number(
        connection, actor.organization_id, command.service_date
    )
    plan = _booking_plan(connection, actor, command, datetime.now(UTC))
    try:
        appointment_id, _ = insert_appointment_records(
            connection, actor, plan.draft, number
        )
    except IntegrityError as invalid:
        if invalid.orig.sqlstate == "23505":
            raise ContractError(409) from invalid
        raise
    if plan.claim is not None:
        connection.execute(
            insert(followup_claims).values(
                id=uuid4(),
                organization_id=actor.organization_id,
                appointment_id=appointment_id,
                patient_id=command.patient_id,
                doctor_staff_id=plan.draft.doctor_staff_id,
                **plan.claim,
                state="ACTIVE",
            )
        )
    projection = appointment_detail(connection, actor, appointment_id)
    append_sensitive_audit(
        connection,
        {
            "organization_id": actor.organization_id,
            "actor_staff_id": actor.staff_id,
            "actor_role": actor.role,
            "action": "appointments.book",
            "correlation_id": str(appointment_id),
            "after_state": {"appointment_id": str(appointment_id), "revision": 0},
        },
    )
    append_outbox(
        connection,
        {
            "organization_id": actor.organization_id,
            "event_type": "APPOINTMENT_BOOKED",
            "aggregate_type": "APPOINTMENT",
            "aggregate_id": str(appointment_id),
            "payload": {"appointment_id": str(appointment_id)},
        },
    )
    insert_tenant_receipt(
        connection,
        Receipt(
            actor_id=actor.staff_id,
            operation=receipt.operation,
            idempotency_key=receipt.idempotency_key,
            request_digest=receipt.request_digest,
            result_projection=projection,
            result_revision=0,
            organization_id=actor.organization_id,
            staff_id=actor.staff_id,
        ),
    )
    return projection

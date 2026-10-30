"""Internal D-05 atomic fixture persistence, not an operational booking route."""

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date, datetime
from uuid import UUID, uuid4

from sqlalchemy import Connection, insert, text

from carepulse.appointments.snapshots import validate_price_snapshot
from carepulse.appointments.tables import (
    appointment_history,
    appointment_number_assignments,
    appointment_price_snapshots,
    appointments,
)
from carepulse.identity.receipts import (
    Receipt,
    TenantReceiptQuery,
    insert_tenant_receipt,
    resolve_tenant_receipt,
)
from carepulse.notifications.evidence import append_outbox, append_sensitive_audit
from carepulse.patients.lifecycle import lock_patient_for_new_appointment


@dataclass(frozen=True)
class AppointmentDraft:
    patient_id: UUID
    doctor_staff_id: UUID
    service_date: date
    status: str
    service_type: str
    classification: str
    lane: str
    booking_mode: str
    price_snapshot: Mapping[str, object]
    starts_at: datetime | None = None
    ends_at: datetime | None = None


def _allocate_number(connection: Connection, clinic: UUID, service_date: date) -> int:
    # The unique clinic/day counter row is the serialization point across both lanes.
    return connection.scalar(
        text("""
            INSERT INTO public.appointment_day_counters
                (organization_id, service_date, last_number)
            VALUES (:clinic, :day, 1)
            ON CONFLICT (organization_id, service_date)
            DO UPDATE SET last_number = public.appointment_day_counters.last_number + 1
            RETURNING last_number
        """),
        {"clinic": clinic, "day": service_date},
    )


def allocate_day_number(
    connection: Connection, organization_id: UUID, service_date: date
) -> int:
    return _allocate_number(connection, organization_id, service_date)


def insert_appointment_records(
    connection: Connection, actor, draft: AppointmentDraft, number: int
) -> tuple[UUID, dict]:
    """Insert the numbered appointment, immutable price and initial history only."""
    frozen = validate_price_snapshot(draft.price_snapshot)
    if (frozen["service_type"], frozen["classification"]) != (
        draft.service_type,
        draft.classification,
    ):
        raise ValueError("Snapshot does not describe the appointment")
    appointment_id, assignment_id = uuid4(), uuid4()
    connection.execute(
        insert(appointments).values(
            id=appointment_id,
            organization_id=actor.organization_id,
            patient_id=draft.patient_id,
            doctor_staff_id=draft.doctor_staff_id,
            service_date=draft.service_date,
            status=draft.status,
            service_type=draft.service_type,
            classification=draft.classification,
            lane=draft.lane,
            booking_mode=draft.booking_mode,
            starts_at=draft.starts_at,
            ends_at=draft.ends_at,
            current_assignment_id=assignment_id,
            current_number=number,
        )
    )
    connection.execute(
        insert(appointment_number_assignments).values(
            id=assignment_id,
            organization_id=actor.organization_id,
            appointment_id=appointment_id,
            service_date=draft.service_date,
            number=number,
        )
    )
    connection.execute(
        insert(appointment_price_snapshots).values(
            organization_id=actor.organization_id,
            appointment_id=appointment_id,
            **frozen,
        )
    )
    connection.execute(
        insert(appointment_history).values(
            id=uuid4(),
            organization_id=actor.organization_id,
            appointment_id=appointment_id,
            appointment_revision=0,
            action="CREATED",
            old_status=None,
            new_status=draft.status,
            old_service_date=None,
            new_service_date=draft.service_date,
            old_number=None,
            new_number=number,
            actor_staff_id=actor.staff_id,
            actor_role=actor.role,
            price_snapshot_appointment_id=appointment_id,
        )
    )
    return appointment_id, frozen


def persist_appointment(
    connection: Connection, draft: AppointmentDraft, query: TenantReceiptQuery
) -> dict:
    """Caller owns the live-authorized transaction and tenant SET LOCAL context.

    A database failure rolls back the counter, appointment, immutable ledger,
    audit, outbox and existing D-09 receipt in the *same* transaction.
    """
    if query.operation != "appointments.persist":
        raise ValueError("Invalid appointment fixture operation")
    actor = query.actor
    frozen = validate_price_snapshot(draft.price_snapshot)
    if (frozen["service_type"], frozen["classification"]) != (
        draft.service_type,
        draft.classification,
    ):
        raise ValueError("Snapshot does not describe the appointment")
    retained = resolve_tenant_receipt(connection, query)
    if retained is not None:
        return retained[0]

    lock_patient_for_new_appointment(
        connection, actor.organization_id, draft.patient_id
    )
    number = _allocate_number(connection, actor.organization_id, draft.service_date)
    appointment_id, _ = insert_appointment_records(connection, actor, draft, number)
    projection = {
        "id": str(appointment_id),
        "service_date": draft.service_date.isoformat(),
        "status": draft.status,
        "number": number,
        "net_egp": format(frozen["net_egp"], ".2f"),
        "revision": 0,
    }
    append_sensitive_audit(
        connection,
        {
            "organization_id": actor.organization_id,
            "actor_staff_id": actor.staff_id,
            "actor_role": actor.role,
            "action": "appointments.persist",
            "correlation_id": str(appointment_id),
            "after_state": {"appointment_id": str(appointment_id), "revision": 0},
        },
    )
    append_outbox(
        connection,
        {
            "organization_id": actor.organization_id,
            "event_type": "APPOINTMENT_PERSISTED",
            "aggregate_type": "APPOINTMENT",
            "aggregate_id": str(appointment_id),
            "payload": {"appointment_id": str(appointment_id)},
        },
    )
    insert_tenant_receipt(
        connection,
        Receipt(
            actor_id=actor.staff_id,
            operation=query.operation,
            idempotency_key=query.idempotency_key,
            request_digest=query.request_digest,
            result_projection=projection,
            result_revision=0,
            organization_id=actor.organization_id,
            staff_id=actor.staff_id,
        ),
    )
    return projection

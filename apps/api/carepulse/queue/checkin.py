"""B-12 serialized dated QueueDay creation, Routine positioning, and physical check-in."""

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import Connection, text
from sqlalchemy.exc import IntegrityError

from carepulse.identity.authorization import AuthorizedStaff
from carepulse.identity.contracts import (
    ContractError,
    normalized_command_digest,
    require_matching_revision,
)
from carepulse.identity.receipts import (
    Receipt,
    ReceiptConflict,
    TenantReceiptQuery,
    insert_tenant_receipt,
    require_safe_committed_tenant_receipt,
    resolve_safe_tenant_receipt,
)
from carepulse.notifications.evidence import append_outbox, append_sensitive_audit
from carepulse.patients.lifecycle import lock_patient_for_existing_appointment
from carepulse.policies.versions import StalePolicyRevision, lock_policy_heads

CHECK_IN_OPERATION = "appointments.check_in"

_CHECK_IN_RECEIPT_FIELDS = frozenset(
    {
        "appointment_id",
        "queue_entry_id",
        "queue_day_id",
        "patient_id",
        "doctor_staff_id",
        "service_date",
        "lane",
        "classification",
        "status",
        "booking_number",
        "arrival_sequence",
        "routine_rank",
        "position",
        "attested_by_staff_id",
        "attested_at",
        "appointment_revision",
        "queue_day_revision",
        "queue_entry_revision",
        "queue_policy_version",
        "routine_cursor",
    }
)


@dataclass(frozen=True, slots=True)
class RoutineWaitingEntry:
    id: UUID
    routine_rank: int
    current_number: int


@dataclass(frozen=True, slots=True)
class QueueDayState:
    id: UUID
    doctor_staff_id: UUID
    service_date: date
    lane: str
    revision: int
    queue_policy_version: int
    arrival_counter: int
    routine_cursor: int
    routine_order: str
    late_depth: int


def compute_routine_insert_index(
    existing: Sequence[RoutineWaitingEntry],
    *,
    routine_order: str,
    late_depth: int,
    routine_cursor: int,
    appointment_number: int,
) -> int:
    """Compute the 0-based insertion index for a Routine arrival among active Waiting entries.

    - ARRIVAL_FIFO: always appends at the end.
    - BOOKED_NUMBER:
      - Late iff routine_cursor > 0 and appointment_number <= routine_cursor.
      - Late arrival goes after the first min(late_depth, len(existing)) Routine entries.
      - On-time arrival goes before the first existing entry with current_number > appointment_number.
    """
    if routine_order == "ARRIVAL_FIFO":
        return len(existing)
    if routine_order != "BOOKED_NUMBER":
        raise ValueError("Unsupported routine ordering mode")
    if late_depth < 0 or routine_cursor < 0 or appointment_number < 1:
        raise ValueError("Invalid queue positioning inputs")
    is_late = routine_cursor > 0 and appointment_number <= routine_cursor
    if is_late:
        return min(late_depth, len(existing))
    for index, entry in enumerate(existing):
        if entry.current_number > appointment_number:
            return index
    return len(existing)


def _effective_queue_policy(
    connection: Connection, organization_id: UUID, service_date: date
) -> tuple[int, str, int]:
    """Resolve the latest committed QUEUE policy effective on or before service_date."""
    row = connection.execute(
        text("""
            SELECT v.version, v.payload
            FROM public.clinic_policy_versions v
            JOIN public.clinic_policy_heads h
              ON h.organization_id = v.organization_id
             AND h.kind = v.kind
             AND v.version <= h.current_version
            WHERE v.organization_id = :tenant
              AND v.kind = 'QUEUE'
              AND NOT v.pending_effective
              AND (
                  v.version = 1
                  OR (v.effective_on IS NOT NULL AND v.effective_on <= :service_date)
              )
            ORDER BY v.version DESC
            LIMIT 1
        """),
        {"tenant": organization_id, "service_date": service_date},
    ).one_or_none()
    if row is None:
        raise ContractError(503)
    return (
        int(row.version),
        str(row.payload["routine_order"]),
        int(row.payload["late_depth"]),
    )


def _policy_payload_for_version(
    connection: Connection, organization_id: UUID, version: int
) -> tuple[str, int]:
    row = connection.execute(
        text("""
            SELECT payload
            FROM public.clinic_policy_versions
            WHERE organization_id = :tenant
              AND kind = 'QUEUE'
              AND version = :version
        """),
        {"tenant": organization_id, "version": version},
    ).one_or_none()
    if row is None:
        raise ContractError(503)
    return str(row.payload["routine_order"]), int(row.payload["late_depth"])


def lock_or_create_queue_day_for_arrival(
    connection: Connection,
    *,
    organization_id: UUID,
    doctor_staff_id: UUID,
    service_date: date,
    lane: str,
    expected_queue_day_revision: int | None,
) -> QueueDayState:
    """Lock the dated lane QueueDay and advance arrival_counter by 1 (or create at 1).

    Caller must already hold SCHEDULE FOR SHARE and QUEUE FOR SHARE policy locks
    so first-day creation serializes with QUEUE-policy publication and close barriers.
    """
    existing = connection.execute(
        text("""
            SELECT id, doctor_staff_id, service_date, lane, revision,
                   queue_policy_version, arrival_counter, routine_cursor
            FROM public.queue_days
            WHERE organization_id = :tenant
              AND service_date = :service_date
              AND lane = :lane
            FOR UPDATE
        """),
        {"tenant": organization_id, "service_date": service_date, "lane": lane},
    ).one_or_none()

    if existing is None:
        if expected_queue_day_revision is not None and expected_queue_day_revision != 0:
            raise ContractError(409, "STALE_REVISION")
        policy_version, routine_order, late_depth = _effective_queue_policy(
            connection, organization_id, service_date
        )
        created = connection.execute(
            text("""
                INSERT INTO public.queue_days
                    (id, organization_id, doctor_staff_id, service_date, lane,
                     revision, queue_policy_kind, queue_policy_version,
                     arrival_counter, routine_cursor)
                VALUES (:id, :tenant, :doctor, :service_date, :lane,
                        1, 'QUEUE', :policy_version, 1, 0)
                ON CONFLICT (organization_id, service_date, lane) DO NOTHING
                RETURNING id, doctor_staff_id, service_date, lane, revision,
                          queue_policy_version, arrival_counter, routine_cursor
            """),
            {
                "id": uuid4(),
                "tenant": organization_id,
                "doctor": doctor_staff_id,
                "service_date": service_date,
                "lane": lane,
                "policy_version": policy_version,
            },
        ).one_or_none()
        if created is not None:
            return QueueDayState(
                id=created.id,
                doctor_staff_id=created.doctor_staff_id,
                service_date=created.service_date,
                lane=created.lane,
                revision=int(created.revision),
                queue_policy_version=int(created.queue_policy_version),
                arrival_counter=int(created.arrival_counter),
                routine_cursor=int(created.routine_cursor),
                routine_order=routine_order,
                late_depth=late_depth,
            )
        # Concurrent transaction created the QueueDay first; lock and continue.
        existing = connection.execute(
            text("""
                SELECT id, doctor_staff_id, service_date, lane, revision,
                       queue_policy_version, arrival_counter, routine_cursor
                FROM public.queue_days
                WHERE organization_id = :tenant
                  AND service_date = :service_date
                  AND lane = :lane
                FOR UPDATE
            """),
            {"tenant": organization_id, "service_date": service_date, "lane": lane},
        ).one()

    if expected_queue_day_revision is not None:
        require_matching_revision(expected_queue_day_revision, int(existing.revision))

    updated = connection.execute(
        text("""
            UPDATE public.queue_days
            SET revision = revision + 1,
                arrival_counter = arrival_counter + 1
            WHERE organization_id = :tenant AND id = :id
            RETURNING id, doctor_staff_id, service_date, lane, revision,
                      queue_policy_version, arrival_counter, routine_cursor
        """),
        {"tenant": organization_id, "id": existing.id},
    ).one()
    routine_order, late_depth = _policy_payload_for_version(
        connection, organization_id, int(updated.queue_policy_version)
    )
    return QueueDayState(
        id=updated.id,
        doctor_staff_id=updated.doctor_staff_id,
        service_date=updated.service_date,
        lane=updated.lane,
        revision=int(updated.revision),
        queue_policy_version=int(updated.queue_policy_version),
        arrival_counter=int(updated.arrival_counter),
        routine_cursor=int(updated.routine_cursor),
        routine_order=routine_order,
        late_depth=late_depth,
    )


def _allocate_routine_rank(
    connection: Connection,
    *,
    organization_id: UUID,
    queue_day: QueueDayState,
    appointment_number: int,
) -> int:
    """Compute and reserve the unique active Routine rank under the locked QueueDay."""
    rows = connection.execute(
        text("""
            SELECT e.id, e.routine_rank, a.current_number
            FROM public.queue_entries e
            JOIN public.appointments a
              ON a.organization_id = e.organization_id
             AND a.id = e.appointment_id
            WHERE e.organization_id = :tenant
              AND e.queue_day_id = :queue_day_id
              AND e.disposition = 'ACTIVE'
              AND e.routine_rank IS NOT NULL
            ORDER BY e.routine_rank ASC
            FOR UPDATE OF e
        """),
        {"tenant": organization_id, "queue_day_id": queue_day.id},
    ).all()
    existing = [
        RoutineWaitingEntry(
            id=row.id,
            routine_rank=int(row.routine_rank),
            current_number=int(row.current_number),
        )
        for row in rows
    ]
    insert_index = compute_routine_insert_index(
        existing,
        routine_order=queue_day.routine_order,
        late_depth=queue_day.late_depth,
        routine_cursor=queue_day.routine_cursor,
        appointment_number=appointment_number,
    )
    if insert_index == len(existing):
        return (existing[-1].routine_rank + 1) if existing else 1

    target_rank = existing[insert_index].routine_rank
    # Shift trailing active entries upward in descending rank order to avoid
    # violating queue_entries_unique_active_routine_rank.
    for entry in reversed(existing[insert_index:]):
        connection.execute(
            text("""
                UPDATE public.queue_entries
                SET routine_rank = routine_rank + 1,
                    revision = revision + 1
                WHERE organization_id = :tenant AND id = :entry_id
            """),
            {"tenant": organization_id, "entry_id": entry.id},
        )
    return target_rank


def insert_arrival_entry(
    connection: Connection,
    *,
    actor: AuthorizedStaff,
    appointment_id: UUID,
    patient_id: UUID,
    doctor_staff_id: UUID,
    service_date: date,
    lane: str,
    queue_day: QueueDayState,
    routine_rank: int | None,
):
    """Insert an arrival using the existing database-derived attendance evidence."""
    entry_id = uuid4()
    try:
        inserted_entry = connection.execute(
            text("""
                INSERT INTO public.queue_entries
                    (id, organization_id, queue_day_id, appointment_id,
                     patient_id, doctor_staff_id, service_date, lane,
                     routine_rank, disposition)
                VALUES (:id, :tenant, :queue_day_id, :appointment_id,
                        :patient_id, :doctor_staff_id, :service_date, :lane,
                        :routine_rank, 'ACTIVE')
                RETURNING arrival_sequence, attested_by_staff_id, attested_at, revision
            """),
            {
                "id": entry_id,
                "tenant": actor.organization_id,
                "queue_day_id": queue_day.id,
                "appointment_id": appointment_id,
                "patient_id": patient_id,
                "doctor_staff_id": doctor_staff_id,
                "service_date": service_date,
                "lane": lane,
                "routine_rank": routine_rank,
            },
        ).one()
    except IntegrityError as conflict:
        raise ContractError(409) from conflict
    return entry_id, inserted_entry


def _live_position(
    connection: Connection,
    *,
    organization_id: UUID,
    queue_day_id: UUID,
    lane: str,
    classification: str,
    arrival_sequence: int,
    routine_rank: int | None,
) -> int:
    if lane == "EMERGENCY" or classification == "URGENT":
        return int(
            connection.scalar(
                text("""
                    SELECT count(*)
                    FROM public.queue_entries e
                    JOIN public.appointments a
                      ON a.organization_id = e.organization_id
                     AND a.id = e.appointment_id
                    WHERE e.organization_id = :tenant
                      AND e.queue_day_id = :queue_day_id
                      AND e.disposition = 'ACTIVE'
                      AND a.status = 'Waiting'
                      AND (:lane = 'EMERGENCY' OR a.classification = 'URGENT')
                      AND e.arrival_sequence <= :arrival_sequence
                """),
                {
                    "tenant": organization_id,
                    "queue_day_id": queue_day_id,
                    "lane": lane,
                    "arrival_sequence": arrival_sequence,
                },
            )
        )
    urgent_ahead = int(
        connection.scalar(
            text("""
                SELECT count(*)
                FROM public.queue_entries e
                JOIN public.appointments a
                  ON a.organization_id = e.organization_id
                 AND a.id = e.appointment_id
                WHERE e.organization_id = :tenant
                  AND e.queue_day_id = :queue_day_id
                  AND e.disposition = 'ACTIVE'
                  AND a.status = 'Waiting'
                  AND a.classification = 'URGENT'
            """),
            {"tenant": organization_id, "queue_day_id": queue_day_id},
        )
    )
    routine_pos = int(
        connection.scalar(
            text("""
                SELECT count(*)
                FROM public.queue_entries e
                JOIN public.appointments a
                  ON a.organization_id = e.organization_id
                 AND a.id = e.appointment_id
                WHERE e.organization_id = :tenant
                  AND e.queue_day_id = :queue_day_id
                  AND e.disposition = 'ACTIVE'
                  AND a.status = 'Waiting'
                  AND e.routine_rank IS NOT NULL
                  AND e.routine_rank <= :routine_rank
            """),
            {
                "tenant": organization_id,
                "queue_day_id": queue_day_id,
                "routine_rank": routine_rank,
            },
        )
    )
    return urgent_ahead + routine_pos


def _check_appointment_visible(
    connection: Connection, organization_id: UUID, appointment_id: UUID
) -> None:
    visible = connection.scalar(
        text("""
            SELECT 1 FROM public.appointments
            WHERE organization_id = :tenant AND id = :appointment_id
        """),
        {"tenant": organization_id, "appointment_id": appointment_id},
    )
    if visible is None:
        raise ContractError(404)


def _valid_checkin_projection(
    appointment_id: UUID, projection: dict[str, Any], revision: int
) -> bool:
    return (
        set(projection) == _CHECK_IN_RECEIPT_FIELDS
        and projection.get("appointment_id") == str(appointment_id)
        and projection.get("status") == "Waiting"
        and projection.get("appointment_revision") == revision
    )


def recover_committed_checkin(
    connection: Connection,
    actor: AuthorizedStaff,
    appointment_id: UUID,
    command,
    idempotency_key: str,
) -> tuple[dict[str, Any], int]:
    """Safe target-bound receipt recovery after a rolled-back competing check-in."""
    digest = normalized_command_digest(
        CHECK_IN_OPERATION, command, target={"appointment_id": appointment_id}
    )
    query = TenantReceiptQuery(actor, CHECK_IN_OPERATION, idempotency_key, digest)
    return require_safe_committed_tenant_receipt(
        connection,
        query,
        check_target_visible=lambda: _check_appointment_visible(
            connection, actor.organization_id, appointment_id
        ),
        validate_projection=lambda proj, rev: _valid_checkin_projection(
            appointment_id, proj, rev
        ),
    )


def check_in_appointment(
    connection: Connection,
    actor: AuthorizedStaff,
    appointment_id: UUID,
    command,
    idempotency_key: str,
) -> tuple[dict[str, Any], int]:
    """Transition a pre-existing Booked appointment to Waiting with physical evidence."""
    if actor.role not in ("DOCTOR_OWNER", "RECEPTIONIST"):
        raise ContractError(403)
    if not command.physical_presence_confirmed:
        raise ContractError(422)

    digest = normalized_command_digest(
        CHECK_IN_OPERATION, command, target={"appointment_id": appointment_id}
    )
    query = TenantReceiptQuery(actor, CHECK_IN_OPERATION, idempotency_key, digest)

    cached = resolve_safe_tenant_receipt(
        connection,
        query,
        check_target_visible=lambda: _check_appointment_visible(
            connection, actor.organization_id, appointment_id
        ),
        validate_projection=lambda proj, rev: _valid_checkin_projection(
            appointment_id, proj, rev
        ),
    )
    if cached is not None:
        return cached

    # Non-locking discovery to determine patient/day/lane before acquiring ordered locks.
    discovered = connection.execute(
        text("""
            SELECT patient_id, doctor_staff_id, service_date, lane
            FROM public.appointments
            WHERE organization_id = :tenant AND id = :appointment_id
        """),
        {"tenant": actor.organization_id, "appointment_id": appointment_id},
    ).one_or_none()
    if discovered is None:
        raise ContractError(404)

    # Ordered lock 1: Patient lifecycle (archived patient IS allowed for existing ID).
    lock_patient_for_existing_appointment(
        connection, actor.organization_id, discovered.patient_id
    )

    # Ordered lock 2: Policy heads in canonical order (SCHEDULE -> QUEUE).
    try:
        lock_policy_heads(
            connection,
            str(actor.organization_id),
            {"SCHEDULE": "SHARE", "QUEUE": "SHARE"},
        )
    except StalePolicyRevision as missing:
        raise ContractError(503) from missing

    # Ordered lock 3: Dated QueueDay for original (service_date, lane).
    queue_day = lock_or_create_queue_day_for_arrival(
        connection,
        organization_id=actor.organization_id,
        doctor_staff_id=discovered.doctor_staff_id,
        service_date=discovered.service_date,
        lane=discovered.lane,
        expected_queue_day_revision=command.expected_queue_day_revision,
    )

    # Ordered lock 4: Target Appointment row.
    appt = connection.execute(
        text("""
            SELECT id, patient_id, doctor_staff_id, service_date, status,
                   classification, lane, current_number, revision
            FROM public.appointments
            WHERE organization_id = :tenant AND id = :appointment_id
            FOR UPDATE
        """),
        {"tenant": actor.organization_id, "appointment_id": appointment_id},
    ).one_or_none()
    if appt is None:
        raise ContractError(404)

    # Re-verify cached receipt after locks in case a competing same-key winner committed.
    cached = resolve_safe_tenant_receipt(
        connection,
        query,
        check_target_visible=lambda: None,
        validate_projection=lambda proj, rev: _valid_checkin_projection(
            appointment_id, proj, rev
        ),
    )
    if cached is not None:
        # Roll back the provisional QueueDay revision/counter before recovery.
        raise ReceiptConflict("Idempotency key already committed")

    if (
        appt.patient_id != discovered.patient_id
        or appt.service_date != discovered.service_date
        or appt.lane != discovered.lane
    ):
        raise ContractError(409)
    if appt.status != "Booked":
        raise ContractError(409)
    require_matching_revision(command.expected_appointment_revision, int(appt.revision))

    new_appt_revision = int(
        connection.scalar(
            text("""
                SELECT public.f01_check_in_appointment(
                    :tenant, :appointment_id, :expected_revision
                )
            """),
            {
                "tenant": actor.organization_id,
                "appointment_id": appointment_id,
                "expected_revision": int(appt.revision),
            },
        )
    )

    routine_rank: int | None = None
    if appt.classification == "ROUTINE":
        routine_rank = _allocate_routine_rank(
            connection,
            organization_id=actor.organization_id,
            queue_day=queue_day,
            appointment_number=int(appt.current_number),
        )

    entry_id, inserted_entry = insert_arrival_entry(
        connection,
        actor=actor,
        appointment_id=appointment_id,
        patient_id=appt.patient_id,
        doctor_staff_id=appt.doctor_staff_id,
        service_date=appt.service_date,
        lane=appt.lane,
        queue_day=queue_day,
        routine_rank=routine_rank,
    )

    position = _live_position(
        connection,
        organization_id=actor.organization_id,
        queue_day_id=queue_day.id,
        lane=appt.lane,
        classification=appt.classification,
        arrival_sequence=int(inserted_entry.arrival_sequence),
        routine_rank=routine_rank,
    )

    projection: dict[str, Any] = {
        "appointment_id": str(appointment_id),
        "queue_entry_id": str(entry_id),
        "queue_day_id": str(queue_day.id),
        "patient_id": str(appt.patient_id),
        "doctor_staff_id": str(appt.doctor_staff_id),
        "service_date": appt.service_date.isoformat(),
        "lane": appt.lane,
        "classification": appt.classification,
        "status": "Waiting",
        "booking_number": int(appt.current_number),
        "arrival_sequence": int(inserted_entry.arrival_sequence),
        "routine_rank": routine_rank,
        "position": position,
        "attested_by_staff_id": str(inserted_entry.attested_by_staff_id),
        "attested_at": inserted_entry.attested_at.isoformat(),
        "appointment_revision": new_appt_revision,
        "queue_day_revision": queue_day.revision,
        "queue_entry_revision": int(inserted_entry.revision),
        "queue_policy_version": queue_day.queue_policy_version,
        "routine_cursor": queue_day.routine_cursor,
    }

    append_sensitive_audit(
        connection,
        {
            "organization_id": actor.organization_id,
            "actor_staff_id": actor.staff_id,
            "actor_role": actor.role,
            "action": CHECK_IN_OPERATION,
            "correlation_id": str(appointment_id),
            "before_state": {
                "appointment_id": str(appointment_id),
                "status": "Booked",
                "revision": int(appt.revision),
            },
            "after_state": {
                "appointment_id": str(appointment_id),
                "queue_entry_id": str(entry_id),
                "status": "Waiting",
                "revision": new_appt_revision,
                "service_date": appt.service_date.isoformat(),
                "lane": appt.lane,
                "arrival_sequence": int(inserted_entry.arrival_sequence),
            },
        },
    )
    append_outbox(
        connection,
        {
            "organization_id": actor.organization_id,
            "event_type": "PATIENT_CHECKED_IN",
            "aggregate_type": "APPOINTMENT",
            "aggregate_id": str(appointment_id),
            "payload": {
                "appointment_id": str(appointment_id),
                "queue_entry_id": str(entry_id),
                "doctor_staff_id": str(appt.doctor_staff_id),
                "service_date": appt.service_date.isoformat(),
                "lane": appt.lane,
                "classification": appt.classification,
                "arrival_sequence": int(inserted_entry.arrival_sequence),
            },
        },
    )
    insert_tenant_receipt(
        connection,
        Receipt(
            actor_id=actor.staff_id,
            operation=CHECK_IN_OPERATION,
            idempotency_key=idempotency_key,
            request_digest=digest,
            result_projection=projection,
            result_revision=new_appt_revision,
            organization_id=actor.organization_id,
            staff_id=actor.staff_id,
        ),
    )
    return projection, new_appt_revision

"""B-12 bounded staff-only dated queue projection."""

from typing import Any
from uuid import UUID

from sqlalchemy import Connection, text

from carepulse.identity.authorization import AuthorizedStaff
from carepulse.identity.contracts import ContractError
from carepulse.queue.contracts import QueueQuery


def _current_policy_payload(
    connection: Connection, organization_id: UUID, kind: str
) -> tuple[int, dict[str, Any]]:
    row = connection.execute(
        text("""
            SELECT v.version, v.payload
            FROM public.clinic_policy_heads h
            JOIN public.clinic_policy_versions v
              ON (v.organization_id, v.kind, v.version) =
                 (h.organization_id, h.kind, h.current_version)
            WHERE h.organization_id = :tenant AND h.kind = :kind
        """),
        {"tenant": organization_id, "kind": kind},
    ).one_or_none()
    if row is None:
        raise ContractError(503)
    return int(row.version), dict(row.payload)


def _operational_controls(
    connection: Connection, organization_id: UUID
) -> dict[str, Any]:
    """Read-only operational controls + FR-019 shared capacity & active examination count."""
    admission_version, admission_payload = _current_policy_payload(
        connection, organization_id, "ADMISSION"
    )
    control_row = connection.execute(
        text("""
            SELECT dc.revision AS control_revision,
                   dc.capacity_policy_version,
                   v.payload AS capacity_payload
            FROM public.doctor_controls dc
            JOIN public.clinic_policy_versions v
              ON v.organization_id = dc.organization_id
             AND v.kind = dc.capacity_policy_kind
             AND v.version = dc.capacity_policy_version
            WHERE dc.organization_id = :tenant
            LIMIT 1
        """),
        {"tenant": organization_id},
    ).one_or_none()
    if control_row is not None:
        capacity_version = int(control_row.capacity_policy_version)
        capacity_payload = dict(control_row.capacity_payload)
        control_revision = int(control_row.control_revision)
    else:
        capacity_version, capacity_payload = _current_policy_payload(
            connection, organization_id, "CAPACITY"
        )
        control_revision = 0

    active_in_exam = int(
        connection.scalar(
            text("""
                SELECT count(*)
                FROM public.appointments
                WHERE organization_id = :tenant
                  AND status = 'In Examination'
            """),
            {"tenant": organization_id},
        )
    )
    return {
        "paused": bool(capacity_payload["paused"]),
        "admission_mode": str(admission_payload["actors"]),
        "admission_policy_version": admission_version,
        "examination_capacity": int(capacity_payload["capacity"]),
        "capacity_policy_version": capacity_version,
        "control_revision": control_revision,
        "active_in_examination_count": active_in_exam,
    }


def dated_queue_projection(
    connection: Connection, actor: AuthorizedStaff, query: QueueQuery
) -> tuple[dict[str, Any], int]:
    """Return bounded operational dated queue view for authorized staff only."""
    if actor.role not in ("DOCTOR_OWNER", "RECEPTIONIST"):
        raise ContractError(403)

    day_row = connection.execute(
        text("""
            SELECT q.id, q.doctor_staff_id, q.service_date, q.lane, q.revision,
                   q.queue_policy_version, q.arrival_counter, q.routine_cursor,
                   v.payload AS queue_policy_payload
            FROM public.queue_days q
            JOIN public.clinic_policy_versions v
              ON v.organization_id = q.organization_id
             AND v.kind = q.queue_policy_kind
             AND v.version = q.queue_policy_version
            WHERE q.organization_id = :tenant
              AND q.service_date = :service_date
              AND q.lane = :lane
        """),
        {
            "tenant": actor.organization_id,
            "service_date": query.date,
            "lane": query.lane,
        },
    ).one_or_none()

    controls = _operational_controls(connection, actor.organization_id)

    if day_row is None:
        return (
            {
                "service_date": query.date.isoformat(),
                "lane": query.lane,
                "queue_day_id": None,
                "queue_day_revision": 0,
                "queue_policy_version": None,
                "routine_order": None,
                "late_depth": None,
                "arrival_counter": 0,
                "routine_cursor": 0,
                **controls,
                "total_waiting": 0,
                "limit": query.limit,
                "offset": query.offset,
                "entries": [],
            },
            0,
        )

    total_waiting = int(
        connection.scalar(
            text("""
                SELECT count(*)
                FROM public.queue_entries e
                JOIN public.appointments a
                  ON a.organization_id = e.organization_id
                 AND a.id = e.appointment_id
                WHERE e.organization_id = :tenant
                  AND e.queue_day_id = :queue_day_id
                  AND e.service_date = :service_date
                  AND e.lane = :lane
                  AND e.disposition = 'ACTIVE'
                  AND a.service_date = :service_date
                  AND a.status = 'Waiting'
            """),
            {
                "tenant": actor.organization_id,
                "queue_day_id": day_row.id,
                "service_date": query.date,
                "lane": query.lane,
            },
        )
    )

    rows = connection.execute(
        text("""
            WITH ordered AS (
                SELECT
                    e.id AS queue_entry_id,
                    e.appointment_id,
                    e.patient_id,
                    p.full_name AS patient_name,
                    p.archived AS patient_archived,
                    e.service_date,
                    e.lane,
                    a.classification,
                    a.service_type,
                    a.status,
                    a.current_number AS booking_number,
                    e.arrival_sequence,
                    e.routine_rank,
                    e.attested_by_staff_id,
                    e.attested_at,
                    a.revision AS appointment_revision,
                    e.revision AS queue_entry_revision,
                    ROW_NUMBER() OVER (
                        ORDER BY
                            CASE
                                WHEN e.lane = 'MAIN' AND a.classification = 'URGENT' THEN 0
                                ELSE 1
                            END ASC,
                            CASE
                                WHEN e.lane = 'MAIN' AND a.classification = 'ROUTINE' THEN e.routine_rank
                                ELSE e.arrival_sequence
                            END ASC,
                            e.id ASC
                    ) AS position
                FROM public.queue_entries e
                JOIN public.appointments a
                  ON a.organization_id = e.organization_id
                 AND a.id = e.appointment_id
                JOIN public.patients p
                  ON p.organization_id = e.organization_id
                 AND p.id = e.patient_id
                WHERE e.organization_id = :tenant
                  AND e.queue_day_id = :queue_day_id
                  AND e.service_date = :service_date
                  AND e.lane = :lane
                  AND e.disposition = 'ACTIVE'
                  AND a.service_date = :service_date
                  AND a.status = 'Waiting'
            )
            SELECT *
            FROM ordered
            ORDER BY position ASC
            LIMIT :limit OFFSET :offset
        """),
        {
            "tenant": actor.organization_id,
            "queue_day_id": day_row.id,
            "service_date": query.date,
            "lane": query.lane,
            "limit": query.limit,
            "offset": query.offset,
        },
    ).all()

    entries = [
        {
            "queue_entry_id": str(row.queue_entry_id),
            "appointment_id": str(row.appointment_id),
            "patient_id": str(row.patient_id),
            "patient_name": row.patient_name,
            "patient_archived": bool(row.patient_archived),
            "service_date": row.service_date.isoformat(),
            "lane": row.lane,
            "classification": row.classification,
            "service_type": row.service_type,
            "status": row.status,
            "booking_number": int(row.booking_number),
            "position": int(row.position),
            "arrival_sequence": int(row.arrival_sequence),
            "routine_rank": int(row.routine_rank)
            if row.routine_rank is not None
            else None,
            "attested_by_staff_id": str(row.attested_by_staff_id),
            "attested_at": row.attested_at.isoformat(),
            "appointment_revision": int(row.appointment_revision),
            "queue_entry_revision": int(row.queue_entry_revision),
        }
        for row in rows
    ]

    policy_payload = dict(day_row.queue_policy_payload)
    queue_day_revision = int(day_row.revision)
    return (
        {
            "service_date": query.date.isoformat(),
            "lane": query.lane,
            "queue_day_id": str(day_row.id),
            "queue_day_revision": queue_day_revision,
            "queue_policy_version": int(day_row.queue_policy_version),
            "routine_order": str(policy_payload["routine_order"]),
            "late_depth": int(policy_payload["late_depth"]),
            "arrival_counter": int(day_row.arrival_counter),
            "routine_cursor": int(day_row.routine_cursor),
            **controls,
            "total_waiting": total_waiting,
            "limit": query.limit,
            "offset": query.offset,
            "entries": entries,
        },
        queue_day_revision,
    )

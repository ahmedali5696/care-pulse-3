"""Atomic direct Waiting creation for the three distinct physical intake commands."""

from dataclasses import replace
from datetime import UTC, date, datetime
from uuid import UUID

from sqlalchemy import Connection, text
from sqlalchemy.exc import IntegrityError

from carepulse.appointments.book import (
    _CREATED_FIELDS,
    _RECEIPT_FIELDS,
    _booking_plan,
    _doctor_id,
    _parse_interval,
    _snapshot,
    insert_followup_appointment,
)
from carepulse.appointments.intake_contracts import (
    EmergencyIntake,
    RoutineIntake,
    UrgentIntake,
)
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
    assert_same_request,
    insert_tenant_receipt,
    lookup_tenant_receipt,
)
from carepulse.notifications.evidence import append_outbox, append_sensitive_audit
from carepulse.patients.lifecycle import lock_patient_lifecycle
from carepulse.policies.pricing_service import current_price_state
from carepulse.policies.schedule import date_rules
from carepulse.policies.schedule_service import _schedule_head, load_current_schedule
from carepulse.policies.slots_service import (
    CAIRO,
    validate_new_timed_capacity,
    validate_timed_candidate,
)
from carepulse.policies.versions import StalePolicyRevision, lock_policy_heads
from carepulse.queue.checkin import (
    _allocate_routine_rank,
    _live_position,
    insert_arrival_entry,
    lock_or_create_queue_day_for_arrival,
)

_INTAKE_TYPES = {
    RoutineIntake: ("appointments.intake_routine", "ROUTINE"),
    EmergencyIntake: ("appointments.intake_emergency", "EMERGENCY"),
    UrgentIntake: ("appointments.intake_urgent", "URGENT"),
}
_QUEUE_FIELDS = {
    "queue_entry_id",
    "queue_day_id",
    "arrival_sequence",
    "routine_rank",
    "position",
    "attested_by_staff_id",
    "attested_at",
    "queue_day_revision",
    "queue_entry_revision",
    "queue_policy_version",
    "routine_cursor",
}


def _intake_query(actor, command, idempotency_key):
    operation, classification = _INTAKE_TYPES[type(command)]
    if actor.role not in ("DOCTOR_OWNER", "RECEPTIONIST") or (
        classification == "URGENT" and actor.role != "RECEPTIONIST"
    ):
        raise ContractError(403)
    if command.physical_presence_confirmed is not True:
        raise ContractError(422)
    try:
        digest = normalized_request_digest(command)
    except UnicodeEncodeError as invalid:
        raise ContractError(422) from invalid
    return TenantReceiptQuery(actor, operation, idempotency_key, digest), classification


def _committed_intake(connection, query, command, classification):
    actor = query.actor
    stored = lookup_tenant_receipt(
        connection,
        actor.organization_id,
        actor.staff_id,
        query.operation,
        query.idempotency_key,
    )
    if stored is None:
        return None
    if (
        connection.scalar(
            text(
                "SELECT 1 FROM public.patients WHERE organization_id=:tenant AND id=:patient"
            ),
            {"tenant": actor.organization_id, "patient": command.patient_id},
        )
        is None
    ):
        raise ContractError(404)
    projection = stored["result_projection"]
    if (
        not isinstance(projection, dict)
        or set(projection) != _RECEIPT_FIELDS | _QUEUE_FIELDS
        or type(stored["result_revision"]) is not int
        or stored["result_revision"] != 0
        or type(projection.get("revision")) is not int
        or projection["revision"] != 0
        or projection["status"] != "Waiting"
        or projection["classification"] != classification
        or projection["lane"]
        != ("EMERGENCY" if classification == "EMERGENCY" else "MAIN")
        or projection["attested_by_staff_id"] != str(actor.staff_id)
        or any(
            isinstance(value, (dict, list))
            for key, value in projection.items()
            if key != "history"
        )
    ):
        raise ContractError(403)
    history = projection["history"]
    if (
        not isinstance(history, list)
        or len(history) != 1
        or not isinstance(history[0], dict)
        or set(history[0]) != _CREATED_FIELDS
        or any(isinstance(value, (dict, list)) for value in history[0].values())
        or history[0]
        != {
            "action": "CREATED",
            "old_status": None,
            "new_status": "Waiting",
            "old_service_date": None,
            "new_service_date": projection["service_date"],
            "old_number": None,
            "new_number": projection["number"],
            "appointment_revision": 0,
            "reason": None,
        }
    ):
        raise ContractError(403)
    try:
        appointment_id = UUID(projection["id"])
        entry_id = UUID(projection["queue_entry_id"])
    except (TypeError, ValueError, AttributeError) as invalid:
        raise ContractError(403) from invalid
    current = appointment_detail(connection, actor, appointment_id)
    if any(
        current[key] != projection[key]
        for key in ("patient_id", "doctor_staff_id", "service_type", "classification")
    ):
        raise ContractError(403)
    if (
        connection.scalar(
            text("""
            SELECT 1 FROM public.queue_entries
            WHERE organization_id=:tenant AND id=:entry AND appointment_id=:appointment
              AND patient_id=:patient AND attested_by_staff_id=:actor
        """),
            {
                "tenant": actor.organization_id,
                "entry": entry_id,
                "appointment": appointment_id,
                "patient": UUID(current["patient_id"]),
                "actor": actor.staff_id,
            },
        )
        is None
    ):
        raise ContractError(404)
    assert_same_request(stored["request_digest"], query.request_digest)
    if current["patient_id"] != str(command.patient_id):
        raise ContractError(403)
    return projection, 0


def recover_committed_intake(connection, actor, command, idempotency_key):
    query, classification = _intake_query(actor, command, idempotency_key)
    cached = _committed_intake(connection, query, command, classification)
    if cached is None:
        raise ContractError(404)
    return cached


def validate_urgent_eligibility(
    connection: Connection,
    actor: AuthorizedStaff,
    command,
    service_date: date,
    now: datetime,
) -> tuple[str, datetime | None, datetime | None, int | None]:
    """Current shared guards; callers own new- or existing-ID counted-capacity checks."""
    local_now = now.astimezone(CAIRO)
    if (
        command.physical_presence_confirmed is not True
        or local_now.date() != service_date
    ):
        raise ContractError(409)
    price = current_price_state(connection, actor.organization_id)["PRICE"]
    require_matching_revision(command.expected_price_revision, price["revision"])
    if price["payload"]["urgent_enabled"] is not True:
        raise ContractError(409)
    head = _schedule_head(connection, actor.organization_id)
    require_matching_revision(command.expected_schedule_revision, head["revision"])
    schedule = load_current_schedule(connection, actor.organization_id)
    rules = date_rules(schedule, service_date)
    wall_time = local_now.time()
    if (
        service_date in schedule.holidays
        or not rules.eligible
        or not any(
            period.starts_at <= wall_time < period.ends_at for period in rules.periods
        )
        or any(pause.starts_at <= wall_time < pause.ends_at for pause in rules.breaks)
    ):
        raise ContractError(409)
    start, end = _parse_interval(command, schedule.booking_mode, service_date)
    capacity = None
    if start is not None:
        capacity = validate_timed_candidate(
            connection,
            actor.organization_id,
            service_date,
            start,
            end,
            now,
            command.expected_capacity_revision,
        )
    return schedule.booking_mode, start, end, capacity


def _nonroutine_draft(
    connection, actor, command, classification, service_date, doctor_id, now
):
    mode, start, end = "OPEN", None, None
    if classification == "URGENT":
        mode, start, end, capacity = validate_urgent_eligibility(
            connection, actor, command, service_date, now
        )
        if start is not None:
            validate_new_timed_capacity(
                connection, actor.organization_id, service_date, start, end, capacity
            )
    snapshot = _snapshot(
        connection,
        actor,
        command,
        "NEW_EXAMINATION",
        None,
        classification=classification,
    )
    return AppointmentDraft(
        patient_id=command.patient_id,
        doctor_staff_id=doctor_id,
        service_date=service_date,
        status="Waiting",
        service_type="NEW_EXAMINATION",
        classification=classification,
        lane="EMERGENCY" if classification == "EMERGENCY" else "MAIN",
        booking_mode=mode,
        starts_at=start,
        ends_at=end,
        price_snapshot=snapshot,
    )


def intake_appointment(
    connection, actor, command, idempotency_key
) -> tuple[dict, int, bool]:
    """Caller owns the authorized transaction; no intermediate Booked state is created."""
    query, classification = _intake_query(actor, command, idempotency_key)
    cached = _committed_intake(connection, query, command, classification)
    if cached is not None:
        return *cached, False
    patient = lock_patient_lifecycle(
        connection, actor.organization_id, command.patient_id
    )
    cached = _committed_intake(connection, query, command, classification)
    if cached is not None:
        return *cached, False
    if patient.archived:
        raise ContractError(409)

    kinds = {"SCHEDULE", "PRICE", "QUEUE"}
    if classification != "EMERGENCY":
        kinds.add("CAPACITY")
    if classification == "ROUTINE":
        kinds.add("FOLLOWUP")
    try:
        lock_policy_heads(
            connection, actor.organization_id, {kind: "SHARE" for kind in kinds}
        )
    except StalePolicyRevision as invalid:
        raise ContractError(409, "STALE_REVISION") from invalid

    if (
        classification == "ROUTINE"
        and _schedule_head(connection, actor.organization_id)["payload"][
            "routine_walk_in_enabled"
        ]
        is not True
    ):
        raise ContractError(409)
    service_date = (
        command.service_date
        if classification == "ROUTINE"
        else datetime.now(UTC).astimezone(CAIRO).date()
    )
    doctor_id = _doctor_id(connection, actor.organization_id)
    number = allocate_day_number(connection, actor.organization_id, service_date)
    lane = "EMERGENCY" if classification == "EMERGENCY" else "MAIN"
    queue_day = lock_or_create_queue_day_for_arrival(
        connection,
        organization_id=actor.organization_id,
        doctor_staff_id=doctor_id,
        service_date=service_date,
        lane=lane,
        expected_queue_day_revision=None,
    )
    now = datetime.now(UTC)
    if classification != "ROUTINE" and service_date != now.astimezone(CAIRO).date():
        raise ContractError(409)
    plan = None
    if classification == "ROUTINE":
        plan = _booking_plan(connection, actor, command, now)
        draft = replace(plan.draft, status="Waiting")
    else:
        draft = _nonroutine_draft(
            connection, actor, command, classification, service_date, doctor_id, now
        )

    try:
        if plan is not None and plan.claim is not None:
            appointment_id = insert_followup_appointment(
                connection, actor, command, plan, number
            )
        else:
            appointment_id, _ = insert_appointment_records(
                connection, actor, draft, number
            )
    except IntegrityError as conflict:
        if conflict.orig.sqlstate == "23505":
            raise ContractError(409) from conflict
        raise

    routine_rank = None
    if classification == "ROUTINE":
        routine_rank = _allocate_routine_rank(
            connection,
            organization_id=actor.organization_id,
            queue_day=queue_day,
            appointment_number=number,
        )
    entry_id, entry = insert_arrival_entry(
        connection,
        actor=actor,
        appointment_id=appointment_id,
        patient_id=command.patient_id,
        doctor_staff_id=doctor_id,
        service_date=service_date,
        lane=lane,
        queue_day=queue_day,
        routine_rank=routine_rank,
    )
    position = _live_position(
        connection,
        organization_id=actor.organization_id,
        queue_day_id=queue_day.id,
        lane=lane,
        classification=classification,
        arrival_sequence=int(entry.arrival_sequence),
        routine_rank=routine_rank,
    )
    projection = appointment_detail(connection, actor, appointment_id)
    if projection["status"] != "Waiting":
        raise ContractError(409)
    projection.update(
        queue_entry_id=str(entry_id),
        queue_day_id=str(queue_day.id),
        arrival_sequence=int(entry.arrival_sequence),
        routine_rank=routine_rank,
        position=position,
        attested_by_staff_id=str(entry.attested_by_staff_id),
        attested_at=entry.attested_at.isoformat(),
        queue_day_revision=queue_day.revision,
        queue_entry_revision=int(entry.revision),
        queue_policy_version=queue_day.queue_policy_version,
        routine_cursor=queue_day.routine_cursor,
    )
    evidence = {
        "appointment_id": str(appointment_id),
        "queue_entry_id": str(entry_id),
        "doctor_staff_id": str(doctor_id),
        "service_date": service_date.isoformat(),
        "lane": lane,
        "classification": classification,
        "arrival_sequence": int(entry.arrival_sequence),
    }
    append_sensitive_audit(
        connection,
        {
            "organization_id": actor.organization_id,
            "actor_staff_id": actor.staff_id,
            "actor_role": actor.role,
            "action": query.operation,
            "correlation_id": str(appointment_id),
            "after_state": {**evidence, "status": "Waiting", "revision": 0},
        },
    )
    append_outbox(
        connection,
        {
            "organization_id": actor.organization_id,
            "event_type": "APPOINTMENT_INTAKE",
            "aggregate_type": "APPOINTMENT",
            "aggregate_id": str(appointment_id),
            "payload": evidence,
        },
    )
    insert_tenant_receipt(
        connection,
        Receipt(
            actor_id=actor.staff_id,
            operation=query.operation,
            idempotency_key=idempotency_key,
            request_digest=query.request_digest,
            result_projection=projection,
            result_revision=0,
            organization_id=actor.organization_id,
            staff_id=actor.staff_id,
        ),
    )
    return projection, 0, True

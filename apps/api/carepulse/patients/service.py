"""Patient commands inside B-01's live-authorized transaction."""

import re
from dataclasses import dataclass
from uuid import UUID, uuid4

from sqlalchemy import Connection, insert, text, update

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
from carepulse.notifications.evidence import append_sensitive_audit
from carepulse.patients.contracts import ArchivePatient, CreatePatient, UpdatePatient
from carepulse.patients.demographics import normalize_phone, validate_age_evidence
from carepulse.patients.lifecycle import lock_patient_lifecycle
from carepulse.patients.queries import patient_projection, visible_patient
from carepulse.patients.tables import patients


@dataclass(frozen=True)
class PatientTarget:
    actor: AuthorizedStaff
    patient_id: UUID


def _validated_fields(connection: Connection, fields: dict) -> dict:
    today = connection.scalar(text("SELECT (now() AT TIME ZONE 'Africa/Cairo')::date"))
    try:
        validate_age_evidence(
            fields["date_of_birth"],
            fields["declared_age"],
            fields["age_reference_date"],
            today,
        )
    except ValueError as invalid:
        raise ContractError(422) from invalid
    return {**fields, "phone_normalized": normalize_phone(fields["phone_raw"])}


def _audit(
    connection: Connection, target: PatientTarget, action: str, states: dict
) -> None:
    for phase in ("before_state", "after_state"):
        if states.get(phase) is not None:
            states[phase] = {"patient_id": str(target.patient_id), **states[phase]}
    append_sensitive_audit(
        connection,
        {
            "organization_id": target.actor.organization_id,
            "actor_staff_id": target.actor.staff_id,
            "actor_role": target.actor.role,
            "action": action,
            "correlation_id": str(uuid4()),
            **states,
        },
    )


def _demographic_audit(
    connection: Connection, target: PatientTarget, previous, current: dict
) -> None:
    # Revisions and changed field names identify the edit without copying intake text.
    previous_projection = patient_projection(previous) if previous else {}
    _audit(
        connection,
        target,
        "patients.update" if previous else "patients.create",
        {
            "before_state": {
                "demographic_revision": previous["demographic_revision"],
            }
            if previous
            else None,
            "after_state": {
                "demographic_revision": current["demographic_revision"],
                "changed_fields": sorted(
                    name
                    for name in CreatePatient.model_fields
                    if previous is None or current[name] != previous_projection[name]
                ),
            },
        },
    )


def _save_create_receipt(
    connection: Connection,
    target: PatientTarget,
    query: TenantReceiptQuery,
    projection: dict,
) -> None:
    insert_tenant_receipt(
        connection,
        Receipt(
            actor_id=target.actor.staff_id,
            operation=query.operation,
            idempotency_key=query.idempotency_key,
            request_digest=query.request_digest,
            result_projection=projection,
            result_revision=0,
            organization_id=target.actor.organization_id,
            staff_id=target.actor.staff_id,
        ),
    )


def register_patient(
    connection: Connection, actor: AuthorizedStaff, command: CreatePatient, key: str
) -> dict:
    if not re.fullmatch(r"[A-Za-z0-9._:-]{1,128}", key):
        raise ContractError(422)
    query = TenantReceiptQuery(
        actor, "patients.create", key, normalized_request_digest(command)
    )
    stored = resolve_tenant_receipt(connection, query)
    if stored is not None:
        visible_patient(connection, actor, UUID(stored[0]["id"]))
        return stored[0]
    fields = _validated_fields(connection, command.model_dump())
    target = PatientTarget(actor, uuid4())
    connection.execute(
        insert(patients).values(
            id=target.patient_id, organization_id=actor.organization_id, **fields
        )
    )
    projection = patient_projection(
        visible_patient(connection, actor, target.patient_id)
    )
    _demographic_audit(connection, target, None, projection)
    _save_create_receipt(connection, target, query, projection)
    return projection


def update_patient(
    connection: Connection, target: PatientTarget, command: UpdatePatient
) -> dict:
    lock_patient_lifecycle(connection, target.actor.organization_id, target.patient_id)
    previous = visible_patient(connection, target.actor, target.patient_id)
    require_matching_revision(
        command.expected_demographic_revision, previous["demographic_revision"]
    )
    changes = command.model_dump(
        exclude_unset=True, exclude={"expected_demographic_revision"}
    )
    if not changes:
        raise ContractError(422)
    fields = {name: previous[name] for name in CreatePatient.model_fields}
    fields = _validated_fields(connection, {**fields, **changes})
    changed = sorted(name for name in fields if fields[name] != previous[name])
    if changed:
        connection.execute(
            update(patients)
            .where(
                patients.c.organization_id == target.actor.organization_id,
                patients.c.id == target.patient_id,
            )
            .values(**fields, demographic_revision=previous["demographic_revision"] + 1)
        )
    projection = patient_projection(
        visible_patient(connection, target.actor, target.patient_id)
    )
    if changed:
        _demographic_audit(connection, target, previous, projection)
    return projection


def _transition_archive(
    connection: Connection,
    target: PatientTarget,
    command: ArchivePatient,
    action: str,
) -> dict:
    if target.actor.role != "DOCTOR_OWNER":
        raise ContractError(403)
    lifecycle = lock_patient_lifecycle(
        connection, target.actor.organization_id, target.patient_id
    )
    require_matching_revision(command.expected_lifecycle_revision, lifecycle.revision)
    archived = action == "archive"
    if lifecycle.archived == archived:
        raise ContractError(409)
    connection.execute(
        update(patients)
        .where(
            patients.c.organization_id == target.actor.organization_id,
            patients.c.id == target.patient_id,
        )
        .values(archived=archived, lifecycle_revision=lifecycle.revision + 1)
    )
    _audit(
        connection,
        target,
        f"patients.{action}",
        {
            "reason": command.reason,
            "before_state": {
                "archived": lifecycle.archived,
                "lifecycle_revision": lifecycle.revision,
            },
            "after_state": {
                "archived": archived,
                "lifecycle_revision": lifecycle.revision + 1,
            },
        },
    )
    return patient_projection(
        visible_patient(connection, target.actor, target.patient_id)
    )


def archive_patient(
    connection: Connection, target: PatientTarget, command: ArchivePatient
) -> dict:
    return _transition_archive(connection, target, command, "archive")


def unarchive_patient(
    connection: Connection, target: PatientTarget, command: ArchivePatient
) -> dict:
    return _transition_archive(connection, target, command, "unarchive")

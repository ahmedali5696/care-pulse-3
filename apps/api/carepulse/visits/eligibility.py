"""Doctor follow-up authorization with serialized source and claim evidence."""

import json
import re
from dataclasses import dataclass
from hashlib import sha256
from uuid import UUID, uuid4

from sqlalchemy import Connection, text

from carepulse.appointments.followup_rules import (
    extension_must_not_shorten,
    validate_exception_deadline,
)
from carepulse.identity.authorization import AuthorizedStaff
from carepulse.identity.contracts import (
    ContractError,
    normalize_typed_body,
    require_matching_revision,
)
from carepulse.identity.receipts import (
    Receipt,
    TenantReceiptQuery,
    insert_tenant_receipt,
    resolve_tenant_receipt,
)
from carepulse.notifications.evidence import append_clinical_audit
from carepulse.patients.lifecycle import lock_patient_for_existing_appointment
from carepulse.policies.versions import StalePolicyRevision, lock_policy_heads
from carepulse.visits.eligibility_contracts import ExtendFollowup, FollowupDecisionPatch


@dataclass(frozen=True)
class _ClinicalChange:
    decision: dict
    kind: str
    reason: str | None


def _receipt_query(
    actor: AuthorizedStaff, operation: str, key: str, request_digest: str
) -> TenantReceiptQuery:
    if actor.role != "DOCTOR_OWNER":
        raise ContractError(403)
    if not re.fullmatch(r"[A-Za-z0-9._:-]{1,128}", key):
        raise ContractError(422)
    return TenantReceiptQuery(actor, operation, key, request_digest)


def _source(connection: Connection, actor: AuthorizedStaff, visit_id: UUID):
    tenant = actor.organization_id
    source = (
        connection.execute(
            text("""
            SELECT patient_id, appointment_id, doctor_staff_id FROM public.visits
            WHERE organization_id=:tenant AND id=:visit
        """),
            {"tenant": tenant, "visit": visit_id},
        )
        .mappings()
        .one_or_none()
    )
    if source is None or source["doctor_staff_id"] != actor.staff_id:
        raise ContractError(404)
    lock_patient_for_existing_appointment(connection, tenant, source["patient_id"])
    try:
        lock_policy_heads(
            connection,
            tenant,
            {
                "SCHEDULE": "SHARE",
                "CAPACITY": "SHARE",
                "FOLLOWUP": "SHARE",
                "PRICE": "SHARE",
            },
        )
    except StalePolicyRevision as invalid:
        raise ContractError(409, "STALE_REVISION") from invalid
    appointment = (
        connection.execute(
            text("""
            SELECT id, patient_id, doctor_staff_id, status, service_type, classification
            FROM public.appointments WHERE organization_id=:tenant AND id=:appointment
            FOR UPDATE
        """),
            {"tenant": tenant, "appointment": source["appointment_id"]},
        )
        .mappings()
        .one_or_none()
    )
    visit = (
        connection.execute(
            text("""
            SELECT id, patient_id, appointment_id, doctor_staff_id, clinical_revision,
                   followup_decision FROM public.visits
            WHERE organization_id=:tenant AND id=:visit FOR UPDATE
        """),
            {"tenant": tenant, "visit": visit_id},
        )
        .mappings()
        .one_or_none()
    )
    if (
        appointment is None
        or visit is None
        or visit["patient_id"] != source["patient_id"]
        or visit["doctor_staff_id"] != actor.staff_id
        or visit["appointment_id"] != appointment["id"]
        or appointment["patient_id"] != visit["patient_id"]
        or appointment["doctor_staff_id"] != actor.staff_id
    ):
        raise ContractError(404)
    claims = (
        connection.execute(
            text("""
            SELECT id, appointment_id, state, revision, approved_deadline
            FROM public.followup_claims
            WHERE organization_id=:tenant AND source_visit_id=:visit
            ORDER BY id FOR UPDATE
        """),
            {"tenant": tenant, "visit": visit_id},
        )
        .mappings()
        .all()
    )
    if (
        appointment["status"] != "Completed"
        or appointment["service_type"] != "NEW_EXAMINATION"
        or appointment["classification"] not in ("ROUTINE", "URGENT", "EMERGENCY")
    ):
        raise ContractError(409)
    completion = connection.scalar(
        text("""
            SELECT (occurred_at AT TIME ZONE 'Africa/Cairo')::date
            FROM public.appointment_history
            WHERE organization_id=:tenant AND appointment_id=:appointment
              AND new_status='Completed'
            ORDER BY appointment_revision DESC LIMIT 1
        """),
        {"tenant": tenant, "appointment": appointment["id"]},
    )
    if completion is None:
        raise ContractError(409)
    return visit, claims, completion


def _clinical_revision(
    connection: Connection,
    actor: AuthorizedStaff,
    visit_id: UUID,
    change: _ClinicalChange,
):
    connection.execute(
        text("""
            SELECT set_config('app.clinical_change_kind', :kind, true),
                   set_config('app.clinical_reason', :reason, true)
        """),
        {"kind": change.kind, "reason": change.reason or ""},
    )
    current = connection.execute(
        text("""
            UPDATE public.visits SET followup_decision=CAST(:decision AS jsonb)
            WHERE organization_id=:tenant AND id=:visit RETURNING clinical_revision
        """),
        {
            "tenant": actor.organization_id,
            "visit": visit_id,
            "decision": json.dumps(change.decision, allow_nan=False),
        },
    ).scalar_one()
    revision = connection.execute(
        text("""
            SELECT id, occurred_at FROM public.clinical_revisions
            WHERE organization_id=:tenant AND visit_id=:visit AND revision=:revision
        """),
        {"tenant": actor.organization_id, "visit": visit_id, "revision": current},
    ).one()
    audit_id = append_clinical_audit(
        connection,
        {
            "organization_id": actor.organization_id,
            "doctor_staff_id": actor.staff_id,
            "clinical_revision": current,
            "before_ref": f"{visit_id}:{current - 1}",
            "after_ref": f"{visit_id}:{current}",
            "correlation_id": str(revision.id),
        },
    )
    return current, revision.id, revision.occurred_at, audit_id


def _store_receipt(
    connection: Connection, query: TenantReceiptQuery, projection: dict, revision: int
):
    insert_tenant_receipt(
        connection,
        Receipt(
            actor_id=query.actor.staff_id,
            operation=query.operation,
            idempotency_key=query.idempotency_key,
            request_digest=query.request_digest,
            result_projection=projection,
            result_revision=revision,
            organization_id=query.actor.organization_id,
            staff_id=query.actor.staff_id,
        ),
    )


def record_followup_decision(
    connection: Connection,
    actor: AuthorizedStaff,
    visit_id: UUID,
    command: FollowupDecisionPatch,
    idempotency_key: str,
) -> dict:
    receipt = _receipt_query(
        actor,
        "visits.followup_decision",
        idempotency_key,
        sha256(visit_id.bytes + normalize_typed_body(command)).hexdigest(),
    )
    cached = resolve_tenant_receipt(connection, receipt)
    if cached is not None:
        return cached[0]
    visit, _, completion = _source(connection, actor, visit_id)
    cached = resolve_tenant_receipt(connection, receipt)
    if cached is not None:
        return cached[0]
    require_matching_revision(
        command.expected_visit_revision, visit["clinical_revision"]
    )
    decision = command.followup_decision.model_dump(mode="json", exclude_none=True)
    if decision["kind"] == "DEADLINE":
        try:
            validate_exception_deadline(
                completion, command.followup_decision.deadline_date, decision["reason"]
            )
        except ValueError as invalid:
            raise ContractError(422) from invalid
    if decision == visit["followup_decision"]:
        raise ContractError(409)
    kind = "DECISION_DEFAULT" if decision["kind"] == "DEFAULT" else "DECISION_EXCEPTION"
    revision, _, _, _ = _clinical_revision(
        connection,
        actor,
        visit_id,
        _ClinicalChange(decision, kind, decision.get("reason")),
    )
    projection = {
        "visit_id": str(visit_id),
        "followup_decision": decision,
        "revision": revision,
    }
    _store_receipt(connection, receipt, projection, revision)
    return projection


def extend_followup(
    connection: Connection,
    actor: AuthorizedStaff,
    visit_id: UUID,
    command: ExtendFollowup,
    idempotency_key: str,
) -> dict:
    receipt = _receipt_query(
        actor,
        "visits.extend_followup",
        idempotency_key,
        sha256(visit_id.bytes + normalize_typed_body(command)).hexdigest(),
    )
    cached = resolve_tenant_receipt(connection, receipt)
    if cached is not None:
        return cached[0]
    visit, claims, _ = _source(connection, actor, visit_id)
    cached = resolve_tenant_receipt(connection, receipt)
    if cached is not None:
        return cached[0]
    require_matching_revision(
        command.expected_visit_revision, visit["clinical_revision"]
    )
    claim = next(
        (row for row in claims if row["appointment_id"] == command.appointment_id), None
    )
    if claim is None or claim["state"] != "ACTIVE":
        raise ContractError(409)
    require_matching_revision(command.expected_claim_revision, claim["revision"])
    latest_deadline = connection.scalar(
        text("""
            SELECT COALESCE(max(extended_deadline), :deadline)
            FROM public.followup_extensions
            WHERE organization_id=:tenant AND claim_id=:claim
        """),
        {
            "tenant": actor.organization_id,
            "claim": claim["id"],
            "deadline": claim["approved_deadline"],
        },
    )
    try:
        extension_must_not_shorten(latest_deadline, command.extended_deadline)
    except ValueError as invalid:
        raise ContractError(422) from invalid
    decision = dict(visit["followup_decision"] or {})
    decision.update(
        {
            "extension_claim_id": str(claim["id"]),
            "extension_deadline": command.extended_deadline.isoformat(),
            "extension_evidence_id": str(uuid4()),
        }
    )
    revision, clinical_id, issued_at, audit_id = _clinical_revision(
        connection,
        actor,
        visit_id,
        _ClinicalChange(decision, "EXTENSION", command.reason),
    )
    connection.execute(
        text("""
            INSERT INTO public.followup_extensions
                (id, organization_id, source_visit_id, claim_id, appointment_id,
                 prior_deadline, extended_deadline, issuing_doctor_staff_id,
                 reason, issued_at, expected_source_revision,
                 expected_claim_revision, clinical_revision_id, clinical_audit_id)
            VALUES (:id, :tenant, :visit, :claim, :appointment, :prior, :deadline,
                    :doctor, :reason, :issued_at, :source_revision,
                    :claim_revision, :clinical_id, :audit_id)
        """),
        {
            "id": uuid4(),
            "tenant": actor.organization_id,
            "visit": visit_id,
            "claim": claim["id"],
            "appointment": claim["appointment_id"],
            "prior": latest_deadline,
            "deadline": command.extended_deadline,
            "doctor": actor.staff_id,
            "reason": command.reason,
            "issued_at": issued_at,
            "source_revision": command.expected_visit_revision,
            "claim_revision": command.expected_claim_revision,
            "clinical_id": clinical_id,
            "audit_id": audit_id,
        },
    )
    projection = {
        "visit_id": str(visit_id),
        "appointment_id": str(claim["appointment_id"]),
        "extended_deadline": command.extended_deadline.isoformat(),
        "revision": revision,
        "claim_revision": claim["revision"],
    }
    _store_receipt(connection, receipt, projection, revision)
    return projection

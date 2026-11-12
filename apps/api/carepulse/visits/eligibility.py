"""Signed operational follow-up decisions and confirmed claim extensions."""

import json
import re
from dataclasses import dataclass
from datetime import date
from hashlib import sha256
from uuid import UUID

from pydantic import ValidationError
from sqlalchemy import Connection, text
from sqlalchemy.exc import DBAPIError

from carepulse.appointments.followup_rules import extension_must_not_shorten
from carepulse.identity.authorization import AuthorizedStaff
from carepulse.identity.contracts import (
    ContractError,
    normalize_typed_body,
    require_matching_revision,
)
from carepulse.identity.receipts import (
    Receipt,
    TenantReceiptQuery,
    assert_same_request,
    insert_tenant_receipt,
    lookup_tenant_receipt,
)
from carepulse.patients.lifecycle import lock_patient_for_existing_appointment
from carepulse.policies.versions import StalePolicyRevision, lock_policy_heads
from carepulse.visits.eligibility_contracts import (
    ExtendFollowup,
    ExtendFollowupV2,
    FollowupDecision,
    FollowupDecisionPatch,
    FollowupDecisionPatchV2,
)


@dataclass(frozen=True)
class _SourceContext:
    visit_revision: int
    source_revision: int
    followup_revision: int
    policy_version: int
    completion_date: date
    decision: dict | None
    decision_actor_role: str | None
    claim: dict | None = None
    appointment_revision: int | None = None


def _receipt_query(
    actor: AuthorizedStaff, operation: str, key: str, request_digest: str
) -> TenantReceiptQuery:
    if actor.role not in ("DOCTOR_OWNER", "RECEPTIONIST"):
        raise ContractError(403)
    if not re.fullmatch(r"[A-Za-z0-9._:-]{1,128}", key):
        raise ContractError(422)
    return TenantReceiptQuery(actor, operation, key, request_digest)


def _source_reference(connection: Connection, actor: AuthorizedStaff, visit_id: UUID):
    tenant = actor.organization_id
    source = (
        connection.execute(
            text("""
            SELECT v.patient_id, v.appointment_id, v.doctor_staff_id
            FROM public.visits v JOIN public.appointments a
              ON (a.organization_id, a.id) = (v.organization_id, v.appointment_id)
            WHERE v.organization_id=:tenant AND v.id=:visit
              AND (v.patient_id, v.doctor_staff_id) =
                  (a.patient_id, a.doctor_staff_id)
        """),
            {"tenant": tenant, "visit": visit_id},
        )
        .mappings()
        .one_or_none()
    )
    if source is None or (
        actor.role == "DOCTOR_OWNER" and source["doctor_staff_id"] != actor.staff_id
    ):
        raise ContractError(404)
    return source


def _locked_source(
    connection: Connection,
    actor: AuthorizedStaff,
    visit_id: UUID,
    target_id: UUID | None = None,
) -> _SourceContext:
    tenant = actor.organization_id
    source = _source_reference(connection, actor, visit_id)
    lock_patient_for_existing_appointment(connection, tenant, source["patient_id"])
    try:
        lock_policy_heads(connection, tenant, {"FOLLOWUP": "SHARE"})
    except StalePolicyRevision as invalid:
        raise ContractError(409, "STALE_REVISION") from invalid
    claim_id = None
    if target_id is not None:
        claim_id = connection.scalar(
            text("""
                SELECT id FROM public.followup_claims
                WHERE organization_id=:tenant AND source_visit_id=:visit
                  AND appointment_id=:appointment
            """),
            {"tenant": tenant, "visit": visit_id, "appointment": target_id},
        )
        if claim_id is None:
            raise ContractError(409)
    appointments = {}
    for appointment_id in sorted({source["appointment_id"], target_id} - {None}):
        appointments[appointment_id] = (
            connection.execute(
                text("""
                    SELECT id, patient_id, doctor_staff_id, status, service_type,
                           classification, revision FROM public.appointments
                    WHERE organization_id=:tenant AND id=:appointment FOR UPDATE
                """),
                {"tenant": tenant, "appointment": appointment_id},
            )
            .mappings()
            .one_or_none()
        )
        if appointments[appointment_id] is None:
            raise ContractError(404)
    appointment = appointments[source["appointment_id"]]
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
        or visit["doctor_staff_id"] != source["doctor_staff_id"]
        or visit["appointment_id"] != appointment["id"]
        or appointment["patient_id"] != visit["patient_id"]
        or appointment["doctor_staff_id"] != visit["doctor_staff_id"]
    ):
        raise ContractError(404)
    # The source Appointment/Visit (and optional target Appointment) serialize
    # source and claim changes; these protected rows need only SELECT privileges.
    state = (
        connection.execute(
            text("""
                SELECT source_revision, provenance_state, decision, decision_actor_role
                FROM public.followup_source_state
                WHERE organization_id=:tenant AND visit_id=:visit
        """),
            {"tenant": tenant, "visit": visit_id},
        )
        .mappings()
        .one_or_none()
    )
    if (
        appointment["status"] != "Completed"
        or appointment["service_type"] != "NEW_EXAMINATION"
        or appointment["classification"] not in ("ROUTINE", "URGENT", "EMERGENCY")
    ):
        raise ContractError(409)
    completion_date = connection.scalar(
        text("""
            SELECT (completed_at AT TIME ZONE 'Africa/Cairo')::date
            FROM public.completion_attestations
            WHERE organization_id=:tenant AND visit_id=:visit
        """),
        {"tenant": tenant, "visit": visit_id},
    )
    if completion_date is None or (state and state["provenance_state"] != "VERIFIED"):
        raise ContractError(409)
    policy = connection.execute(
        text("""
            SELECT current_version, revision FROM public.clinic_policy_heads
            WHERE organization_id=:tenant AND kind='FOLLOWUP'
        """),
        {"tenant": tenant},
    ).one()
    claim = None
    if claim_id is not None:
        claim = (
            connection.execute(
                text("""
                    SELECT id, appointment_id, state, revision, approved_deadline
                    FROM public.followup_claims
                    WHERE organization_id=:tenant AND id=:claim
                """),
                {"tenant": tenant, "claim": claim_id},
            )
            .mappings()
            .one_or_none()
        )
        target = appointments[target_id]
        if (
            claim is None
            or claim["appointment_id"] != target["id"]
            or target["patient_id"] != visit["patient_id"]
            or target["doctor_staff_id"] != visit["doctor_staff_id"]
            or target["service_type"] != "FOLLOW_UP"
        ):
            raise ContractError(404)
    return _SourceContext(
        visit["clinical_revision"],
        state["source_revision"] if state else 0,
        policy.revision,
        policy.current_version,
        completion_date,
        state["decision"] if state else None,
        state["decision_actor_role"] if state else None,
        dict(claim) if claim else None,
        appointments[target_id]["revision"] if target_id else None,
    )


_DECISION_V1_FIELDS = {"visit_id", "followup_decision", "revision"}
_DECISION_V2_FIELDS = _DECISION_V1_FIELDS | {
    "source_decision_revision",
    "visit_revision",
    "followup_revision",
    "event_id",
    "actor_role",
}
_EXTENSION_V1_FIELDS = {
    "visit_id",
    "appointment_id",
    "extended_deadline",
    "revision",
    "claim_revision",
}
_EXTENSION_V2_FIELDS = _EXTENSION_V1_FIELDS | {
    "claim_id",
    "authorization_revision",
    "source_decision_revision",
    "visit_revision",
    "followup_revision",
    "appointment_revision",
    "event_id",
    "actor_role",
}


def _valid_revisions(projection: dict, fields: tuple[str, ...]) -> bool:
    return all(
        type(projection.get(field)) is int and projection[field] >= 0
        for field in fields
    )


def _replay_receipt(
    connection: Connection, query: TenantReceiptQuery, visit_id: UUID
) -> tuple[dict, int] | None:
    stored = lookup_tenant_receipt(
        connection,
        query.actor.organization_id,
        query.actor.staff_id,
        query.operation,
        query.idempotency_key,
    )
    if stored is None:
        return None
    actor = query.actor
    _source_reference(connection, actor, visit_id)
    projection = stored["result_projection"]
    revision = stored["result_revision"]
    if not isinstance(projection, dict) or type(revision) is not int or revision < 0:
        raise ContractError(403)
    v2 = "event_id" in projection
    if query.operation == "visits.followup_decision":
        permitted = _DECISION_V2_FIELDS if v2 else _DECISION_V1_FIELDS
        decision = projection.get("followup_decision")
        if (
            set(projection) != permitted
            or projection.get("visit_id") != str(visit_id)
            or not isinstance(decision, dict)
            or not {"kind"} <= set(decision) <= {"kind", "deadline_date", "reason"}
            or not _valid_revisions(
                projection,
                (
                    "revision",
                    "source_decision_revision",
                    "visit_revision",
                    "followup_revision",
                )
                if v2
                else ("revision",),
            )
        ):
            raise ContractError(403)
        try:
            if (
                FollowupDecision.model_validate(decision).model_dump(
                    mode="json", exclude_unset=True
                )
                != decision
            ):
                raise ContractError(403)
        except ValidationError as invalid:
            raise ContractError(403) from invalid
        if v2:
            event = connection.execute(
                text("""
                    SELECT actor_staff_id, actor_role, decision,
                           source_revision, clinical_revision, policy_version
                    FROM public.followup_source_decision_events
                    WHERE organization_id=:tenant AND source_visit_id=:visit
                      AND id=:event
                """),
                {
                    "tenant": actor.organization_id,
                    "visit": visit_id,
                    "event": _event_uuid(projection["event_id"]),
                },
            ).one_or_none()
            if (
                event is None
                or (event.actor_staff_id, event.actor_role)
                != (actor.staff_id, actor.role)
                or event.decision != decision
                or projection.get("revision") != event.source_revision
                or projection.get("source_decision_revision") != event.source_revision
                or projection.get("visit_revision") != event.clinical_revision
                or projection.get("followup_revision") != event.policy_version
                or revision != event.source_revision
            ):
                raise ContractError(403)
        elif actor.role != "DOCTOR_OWNER":
            raise ContractError(403)
        if "reason" in decision and actor.role == "RECEPTIONIST" and not v2:
            raise ContractError(403)
    else:
        permitted = _EXTENSION_V2_FIELDS if v2 else _EXTENSION_V1_FIELDS
        if (
            set(projection) != permitted
            or projection.get("visit_id") != str(visit_id)
            or not _valid_revisions(
                projection,
                (
                    "revision",
                    "claim_revision",
                    "authorization_revision",
                    "source_decision_revision",
                    "visit_revision",
                    "followup_revision",
                    "appointment_revision",
                )
                if v2
                else ("revision", "claim_revision"),
            )
        ):
            raise ContractError(403)
        target = _event_uuid(projection.get("appointment_id"))
        claim = connection.execute(
            text("""
                SELECT id FROM public.followup_claims
                WHERE organization_id=:tenant AND source_visit_id=:visit
                  AND appointment_id=:target
            """),
            {"tenant": actor.organization_id, "visit": visit_id, "target": target},
        ).one_or_none()
        if claim is None:
            raise ContractError(404)
        if v2:
            event = connection.execute(
                text("""
                    SELECT actor_staff_id, actor_role, new_deadline,
                           authorization_revision, source_revision,
                           clinical_revision, appointment_revision, claim_revision,
                           policy_version
                    FROM public.followup_authorization_events
                    WHERE organization_id=:tenant AND claim_id=:claim AND id=:event
                      AND kind='EXTENSION'
                """),
                {
                    "tenant": actor.organization_id,
                    "claim": claim.id,
                    "event": _event_uuid(projection["event_id"]),
                },
            ).one_or_none()
            if (
                event is None
                or (event.actor_staff_id, event.actor_role)
                != (actor.staff_id, actor.role)
                or projection.get("claim_id") != str(claim.id)
                or projection.get("extended_deadline") != event.new_deadline.isoformat()
                or projection.get("revision") != event.authorization_revision
                or projection.get("authorization_revision")
                != event.authorization_revision
                or projection.get("source_decision_revision") != event.source_revision
                or projection.get("visit_revision") != event.clinical_revision
                or projection.get("claim_revision") != event.claim_revision
                or projection.get("appointment_revision") != event.appointment_revision
                or projection.get("followup_revision") != event.policy_version
                or revision != event.authorization_revision
            ):
                raise ContractError(403)
        elif actor.role != "DOCTOR_OWNER":
            raise ContractError(403)
    if v2 and projection.get("actor_role") != actor.role:
        raise ContractError(403)
    assert_same_request(stored["request_digest"], query.request_digest)
    return projection, revision


def recover_followup_receipt(
    connection: Connection,
    actor: AuthorizedStaff,
    visit_id: UUID,
    command: FollowupDecisionPatch
    | FollowupDecisionPatchV2
    | ExtendFollowup
    | ExtendFollowupV2,
    idempotency_key: str,
    operation: str,
) -> tuple[dict, int]:
    """Read the winner only after the caller has rolled back and reauthorized."""
    query = _receipt_query(
        actor,
        operation,
        idempotency_key,
        sha256(visit_id.bytes + normalize_typed_body(command)).hexdigest(),
    )
    stored = _replay_receipt(connection, query, visit_id)
    if stored is None:
        raise ContractError(409, "IDEMPOTENCY_CONFLICT")
    return stored


def _event_uuid(identifier: str | None) -> UUID:
    try:
        return UUID(identifier)
    except (TypeError, ValueError) as invalid:
        raise ContractError(403) from invalid


def _expected(source: _SourceContext) -> dict:
    return {
        "source_revision": source.source_revision,
        "clinical_revision": source.visit_revision,
        "policy_version": source.policy_version,
    }


def _sql_write(connection: Connection, statement: str, parameters: dict) -> int:
    try:
        return connection.scalar(text(statement), parameters)
    except DBAPIError as invalid:
        sqlstate = getattr(invalid.orig, "sqlstate", None)
        if sqlstate == "40001":
            raise ContractError(409, "STALE_REVISION") from invalid
        if sqlstate == "42501":
            raise ContractError(403) from invalid
        if sqlstate == "23514":
            raise ContractError(409) from invalid
        raise


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
    command: FollowupDecisionPatch | FollowupDecisionPatchV2,
    idempotency_key: str,
) -> tuple[dict, int]:
    receipt = _receipt_query(
        actor,
        "visits.followup_decision",
        idempotency_key,
        sha256(visit_id.bytes + normalize_typed_body(command)).hexdigest(),
    )
    cached = _replay_receipt(connection, receipt, visit_id)
    if cached is not None:
        return cached
    if isinstance(command, FollowupDecisionPatch) and not isinstance(
        command, FollowupDecisionPatchV2
    ):
        _source_reference(connection, actor, visit_id)
        raise ContractError(409)
    source = _locked_source(connection, actor, visit_id)
    cached = _replay_receipt(connection, receipt, visit_id)
    if cached is not None:
        return cached
    require_matching_revision(command.expected_visit_revision, source.visit_revision)
    require_matching_revision(
        command.expected_source_decision_revision, source.source_revision
    )
    require_matching_revision(
        command.expected_followup_revision, source.followup_revision
    )
    decision = command.followup_decision.model_dump(mode="json", exclude_none=True)
    if decision["kind"] == "DEADLINE" and (
        command.followup_decision.deadline_date < source.completion_date
    ):
        raise ContractError(422)
    if decision == source.decision:
        raise ContractError(409)
    if (
        actor.role == "RECEPTIONIST"
        and source.decision_actor_role == "DOCTOR_OWNER"
        and (source.decision or {}).get("kind") in ("OPTED_OUT", "DEADLINE")
    ):
        raise ContractError(403)
    revision = _sql_write(
        connection,
        """
            SELECT public.record_followup_source_decision(
                :tenant, :visit, CAST(:expected AS jsonb), CAST(:decision AS jsonb))
        """,
        {
            "tenant": actor.organization_id,
            "visit": visit_id,
            "expected": json.dumps(_expected(source)),
            "decision": json.dumps(decision, allow_nan=False),
        },
    )
    event = connection.execute(
        text("""
            SELECT id, actor_role FROM public.followup_source_decision_events
            WHERE organization_id=:tenant AND source_visit_id=:visit
              AND source_revision=:revision
        """),
        {"tenant": actor.organization_id, "visit": visit_id, "revision": revision},
    ).one()
    projection = {
        "visit_id": str(visit_id),
        "followup_decision": decision,
        "revision": revision,
        "source_decision_revision": revision,
        "visit_revision": source.visit_revision,
        "followup_revision": source.followup_revision,
        "event_id": str(event.id),
        "actor_role": event.actor_role,
    }
    _store_receipt(connection, receipt, projection, revision)
    return projection, revision


def extend_followup(
    connection: Connection,
    actor: AuthorizedStaff,
    visit_id: UUID,
    command: ExtendFollowup | ExtendFollowupV2,
    idempotency_key: str,
) -> tuple[dict, int]:
    receipt = _receipt_query(
        actor,
        "visits.extend_followup",
        idempotency_key,
        sha256(visit_id.bytes + normalize_typed_body(command)).hexdigest(),
    )
    cached = _replay_receipt(connection, receipt, visit_id)
    if cached is not None:
        return cached
    if isinstance(command, ExtendFollowup) and not isinstance(
        command, ExtendFollowupV2
    ):
        _source_reference(connection, actor, visit_id)
        raise ContractError(409)
    source = _locked_source(connection, actor, visit_id, command.appointment_id)
    cached = _replay_receipt(connection, receipt, visit_id)
    if cached is not None:
        return cached
    require_matching_revision(command.expected_visit_revision, source.visit_revision)
    require_matching_revision(
        command.expected_source_decision_revision, source.source_revision
    )
    require_matching_revision(
        command.expected_followup_revision, source.followup_revision
    )
    claim = source.claim
    if claim["state"] != "ACTIVE":
        raise ContractError(409)
    require_matching_revision(command.expected_claim_revision, claim["revision"])
    require_matching_revision(
        command.expected_appointment_revision, source.appointment_revision
    )
    latest_event = (
        connection.execute(
            text("""
                SELECT authorization_revision, new_deadline
                FROM public.followup_authorization_events
                WHERE organization_id=:tenant AND claim_id=:claim
                ORDER BY authorization_revision DESC LIMIT 1
            """),
            {"tenant": actor.organization_id, "claim": claim["id"]},
        )
        .mappings()
        .one_or_none()
    )
    current_authorization_revision = (
        latest_event["authorization_revision"] if latest_event else 0
    )
    require_matching_revision(
        command.expected_authorization_revision, current_authorization_revision
    )
    legacy_deadline = connection.scalar(
        text("""
            SELECT max(extended_deadline) FROM public.followup_extensions
            WHERE organization_id=:tenant AND claim_id=:claim
        """),
        {"tenant": actor.organization_id, "claim": claim["id"]},
    )
    latest_deadline = max(
        deadline
        for deadline in (
            claim["approved_deadline"],
            latest_event["new_deadline"] if latest_event else None,
            legacy_deadline,
        )
        if deadline is not None
    )
    if command.extended_deadline < source.completion_date:
        raise ContractError(422)
    try:
        extension_must_not_shorten(latest_deadline, command.extended_deadline)
    except ValueError as invalid:
        raise ContractError(422) from invalid
    if (source.decision or {}).get("kind") == "OPTED_OUT" or (
        actor.role == "RECEPTIONIST"
        and source.decision_actor_role == "DOCTOR_OWNER"
        and (source.decision or {}).get("kind") == "DEADLINE"
        and command.extended_deadline
        > date.fromisoformat(source.decision["deadline_date"])
    ):
        raise ContractError(409)
    revision = _sql_write(
        connection,
        """
            SELECT public.extend_followup_claim_ordered(
                :tenant, :claim, CAST(:expected AS jsonb), CAST(:extension AS jsonb))
        """,
        {
            "tenant": actor.organization_id,
            "claim": claim["id"],
            "expected": json.dumps(
                {
                    **_expected(source),
                    "claim_revision": command.expected_claim_revision,
                    "authorization_revision": command.expected_authorization_revision,
                    "appointment_revision": command.expected_appointment_revision,
                }
            ),
            "extension": json.dumps(
                {
                    "deadline_date": command.extended_deadline.isoformat(),
                    "reason": command.reason,
                },
                allow_nan=False,
            ),
        },
    )
    event = connection.execute(
        text("""
            SELECT id, actor_role FROM public.followup_authorization_events
            WHERE organization_id=:tenant AND claim_id=:claim
              AND authorization_revision=:revision AND kind='EXTENSION'
        """),
        {"tenant": actor.organization_id, "claim": claim["id"], "revision": revision},
    ).one()
    projection = {
        "visit_id": str(visit_id),
        "appointment_id": str(claim["appointment_id"]),
        "extended_deadline": command.extended_deadline.isoformat(),
        "revision": revision,
        "claim_revision": claim["revision"],
        "claim_id": str(claim["id"]),
        "authorization_revision": revision,
        "source_decision_revision": source.source_revision,
        "visit_revision": source.visit_revision,
        "followup_revision": source.followup_revision,
        "appointment_revision": source.appointment_revision,
        "event_id": str(event.id),
        "actor_role": event.actor_role,
    }
    _store_receipt(connection, receipt, projection, revision)
    return projection, revision

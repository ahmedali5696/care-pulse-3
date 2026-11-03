"""B-10 Doctor decisions and exact confirmed-claim extensions on real PostgreSQL."""

import asyncio
import json
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from hmac import new
from uuid import UUID, uuid4

import pytest
from carepulse.db import transaction
from carepulse.identity.authorization import AuthorizedStaff
from carepulse.identity.contracts import ContractError
from carepulse.identity.receipts import ReceiptConflict
from carepulse.identity.signing import canonical_payload
from carepulse.main import app
from carepulse.visits.eligibility import extend_followup, record_followup_decision
from carepulse.visits.eligibility_contracts import ExtendFollowup, FollowupDecisionPatch
from pydantic import ValidationError
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from test_appointment_persistence import appointment_storage as appointment_storage  # noqa: PLC0414
from test_followup_persistence import _claim, _outcome, _source
from test_patients import patient_backend as patient_backend  # noqa: PLC0414
from test_pricing import _patch, pricing_request
from test_request_boundary import boundary_data as boundary_data  # noqa: PLC0414
from test_visit_persistence import _receptionist, _seed, _visit


def _actor(tenant, staff, role="DOCTOR_OWNER"):
    return AuthorizedStaff(tenant, staff, role, False, 0, 0, 0)


def _doctor_context(connection, staff):
    connection.execute(
        text("SELECT set_config('app.staff_id', :staff, true)"),
        {"staff": str(staff)},
    )


def _decision(kind, revision, *, deadline=None, reason=None):
    details = {"kind": kind}
    if deadline is not None:
        details["deadline_date"] = deadline.isoformat()
    if reason is not None:
        details["reason"] = reason
    return FollowupDecisionPatch.model_validate(
        {"followup_decision": details, "expected_visit_revision": revision}
    )


def _extension(source, target, *, revision=1, claim_revision=1, deadline=None):
    return ExtendFollowup.model_validate(
        {
            "appointment_id": str(target),
            "extended_deadline": (
                deadline or source["day"] + timedelta(days=9)
            ).isoformat(),
            "reason": "Synthetic Doctor extension",
            "expected_visit_revision": revision,
            "expected_claim_revision": claim_revision,
        }
    )


def _signed_visit_request(
    clinic,
    actor,
    *,
    path,
    method,
    body,
    key,
    omit_key_header=False,
    purpose="VISIT_FOLLOWUP",
):
    """Exercise the actual API boundary with a fresh signed nonce per request."""
    encoded = json.dumps(body, separators=(",", ":")).encode()
    envelope = replace(
        actor,
        method=method,
        path=path,
        query="",
        body_sha256=sha256(encoded).hexdigest(),
        purpose=purpose,
        timestamp=str(int(datetime.now(UTC).timestamp() * 1000)),
        nonce=f"synthetic_{uuid4().hex}",
        idempotency_key=key,
    )
    fields = (
        "protocol_version",
        "key_id",
        "service",
        "organization_id",
        "staff_id",
        "session_id",
        "role",
        "security_revision",
        "timestamp",
        "nonce",
        "purpose",
        "idempotency_key",
    )
    headers = [
        (
            f"x-carepulse-{field.replace('_', '-')}".encode(),
            str(getattr(envelope, field)).encode(),
        )
        for field in fields
        if not (omit_key_header and field == "idempotency_key")
    ]
    headers.extend(
        (
            (b"content-type", b"application/json"),
            (b"x-carepulse-service-key", clinic.config.service_key.encode()),
            (
                b"x-carepulse-signature",
                new(
                    clinic.config.service_key.encode(),
                    canonical_payload(envelope),
                    sha256,
                )
                .hexdigest()
                .encode(),
            ),
        )
    )
    scope = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "scheme": "http",
        "method": method,
        "path": path,
        "raw_path": path.encode(),
        "query_string": b"",
        "root_path": "",
        "headers": headers,
        "client": ("synthetic", 1),
        "server": ("api", 8000),
    }
    messages = []

    async def receive():
        return {"type": "http.request", "body": encoded, "more_body": False}

    async def send(message):
        messages.append(message)

    asyncio.run(app(scope, receive, send))
    return messages[0]["status"], json.loads(
        b"".join(message.get("body", b"") for message in messages[1:])
    )


def _visit_effects(clinic, visit_id, operation):
    with transaction(clinic.runtime, clinic.tenant) as connection:
        return connection.execute(
            text("""
                SELECT
                    (SELECT count(*) FROM public.clinical_revisions
                      WHERE organization_id=:tenant AND visit_id=:visit),
                    (SELECT count(*) FROM public.clinical_audit a
                      JOIN public.clinical_revisions r
                        ON r.id::text=a.correlation_id
                       AND r.organization_id=a.organization_id
                      WHERE a.organization_id=:tenant AND r.visit_id=:visit),
                    (SELECT count(*) FROM public.followup_extensions
                      WHERE organization_id=:tenant AND source_visit_id=:visit),
                    (SELECT count(*) FROM public.idempotency_receipts
                      WHERE organization_id=:tenant AND operation=:operation)
            """),
            {"tenant": clinic.tenant, "visit": visit_id, "operation": operation},
        ).one()


@pytest.mark.parametrize("kind", ["DEFAULT", "OPTED_OUT", "DEADLINE"])
def test_doctor_records_only_decision_slice_with_clinical_evidence(
    appointment_storage, kind
):
    runtime, _, (tenant, _), (doctor, _) = appointment_storage
    with transaction(runtime, tenant) as connection:
        appointment = _seed(connection, tenant, doctor, complete=True)
        visit = _visit(connection, appointment)
        completed = connection.scalar(
            text("""
            SELECT (occurred_at AT TIME ZONE 'Africa/Cairo')::date
            FROM public.appointment_history
            WHERE appointment_id=:id AND new_status='Completed'
        """),
            {"id": appointment},
        )
    command = _decision(
        kind,
        0,
        deadline=completed + timedelta(days=14) if kind == "DEADLINE" else None,
        reason="Synthetic clinical judgment" if kind != "DEFAULT" else None,
    )
    with transaction(runtime, tenant) as connection:
        _doctor_context(connection, doctor)
        result = record_followup_decision(
            connection, _actor(tenant, doctor), visit.id, command, str(uuid4())
        )
        assert result["revision"] == 1
        record = connection.execute(
            text("""
            SELECT r.change_kind, r.old_values, r.new_values, r.reason,
                   a.doctor_staff_id, a.clinical_revision, a.correlation_id
            FROM public.clinical_revisions r
            JOIN public.clinical_audit a ON a.organization_id=r.organization_id
                 AND a.correlation_id=r.id::text
            WHERE r.visit_id=:visit
        """),
            {"visit": visit.id},
        ).one()
        assert record.change_kind == (
            "DECISION_DEFAULT" if kind == "DEFAULT" else "DECISION_EXCEPTION"
        )
        assert record.old_values["followup_decision"] is None
        assert record.new_values["followup_decision"] == result["followup_decision"]
        assert record.doctor_staff_id == doctor and record.clinical_revision == 1
        assert record.reason == command.followup_decision.reason
        assert (
            connection.execute(
                text("""
            SELECT examination, findings, diagnosis, medical_notes, treatment, tests
            FROM public.visits WHERE id=:visit
        """),
                {"visit": visit.id},
            ).one()
            == (None,) * 6
        )


@pytest.mark.parametrize(
    "case", ["before_completion", "stale", "receptionist", "operator", "disabled"]
)
def test_invalid_or_unauthorized_decision_rolls_back_all_evidence(
    appointment_storage, case
):
    runtime, migration, (tenant, _), (doctor, _) = appointment_storage
    with transaction(runtime, tenant) as connection:
        appointment = _seed(connection, tenant, doctor, complete=True)
        visit = _visit(connection, appointment)
        completed = connection.scalar(
            text("""
            SELECT (occurred_at AT TIME ZONE 'Africa/Cairo')::date
            FROM public.appointment_history
            WHERE appointment_id=:id AND new_status='Completed'
        """),
            {"id": appointment},
        )
    receptionist = _receptionist(migration, tenant) if case == "receptionist" else None
    acting_staff = receptionist or doctor
    role = (
        "RECEPTIONIST"
        if receptionist
        else "OPERATOR"
        if case == "operator"
        else "DOCTOR_OWNER"
    )
    command = _decision(
        "DEADLINE",
        int(case == "stale"),
        deadline=completed + timedelta(days=-1 if case == "before_completion" else 8),
        reason="Synthetic exception",
    )
    with transaction(runtime, tenant) as connection:
        _doctor_context(connection, acting_staff)
        if case == "disabled":
            connection.execute(
                text("""
                    UPDATE public.staff_accounts
                    SET enabled=false, security_revision=security_revision+1
                    WHERE id=:id
                """),
                {"id": doctor},
            )
        failure_type = DBAPIError if case == "disabled" else ContractError
        with pytest.raises(failure_type) as denied, connection.begin_nested():
            record_followup_decision(
                connection,
                _actor(tenant, acting_staff, role),
                visit.id,
                command,
                str(uuid4()),
            )
        if isinstance(denied.value, ContractError):
            assert denied.value.status_code == (
                403
                if case in ("receptionist", "operator")
                else 409
                if case == "stale"
                else 422
            )
        assert (
            connection.scalar(
                text("SELECT clinical_revision FROM public.visits WHERE id=:id"),
                {"id": visit.id},
            )
            == 0
        )
        assert (
            connection.scalar(
                text(
                    "SELECT count(*) FROM public.clinical_revisions WHERE visit_id=:id"
                ),
                {"id": visit.id},
            )
            == 0
        )
        assert (
            connection.scalar(
                text(
                    "SELECT count(*) FROM public.idempotency_receipts WHERE operation='visits.followup_decision' AND organization_id=:tenant"
                ),
                {"tenant": tenant},
            )
            == 0
        )


def test_strict_decision_input_excludes_clinical_edit_and_missing_reason():
    with pytest.raises(ValidationError):
        FollowupDecisionPatch.model_validate(
            {
                "followup_decision": {"kind": "OPTED_OUT", "reason": " "},
                "expected_visit_revision": 0,
            }
        )
    with pytest.raises(ValidationError):
        FollowupDecisionPatch.model_validate(
            {
                "followup_decision": {"kind": "DEFAULT"},
                "expected_visit_revision": 0,
                "diagnosis": "override",
            }
        )
    with pytest.raises(ValidationError):
        ExtendFollowup.model_validate(
            {
                "appointment_id": str(uuid4()),
                "extended_deadline": "2026-11-10",
                "reason": "",
                "expected_visit_revision": 1,
                "expected_claim_revision": 1,
            }
        )


@pytest.mark.parametrize(
    ("operation", "method", "success_status"),
    [
        ("decision", "PATCH", 200),
        ("extension", "POST", 201),
    ],
)
def test_signed_doctor_followup_receipt_recovery_and_revocation(
    patient_backend, operation, method, success_status
):
    clinic = patient_backend
    doctor = UUID(clinic.owner.staff_id)
    with transaction(clinic.runtime, clinic.tenant) as connection:
        source = _source(connection, clinic.tenant, doctor)
        confirmed = _claim(connection, clinic.tenant, source)
    visit_id = source["visit"]
    path = f"/v1/visits/{visit_id}"
    if operation == "decision":
        body = _decision(
            "OPTED_OUT", 1, reason="Synthetic revised Doctor decision"
        ).model_dump(mode="json")
        changed = {
            **body,
            "followup_decision": {
                "kind": "OPTED_OUT",
                "reason": "Different clinical reason",
            },
        }
        receipt_operation = "visits.followup_decision"
    else:
        path += "/extend-followup"
        body = _extension(source, confirmed["id"]).model_dump(mode="json")
        changed = {**body, "reason": "Different clinical reason"}
        receipt_operation = "visits.extend_followup"
    before = _visit_effects(clinic, visit_id, receipt_operation)
    assert before == (1, 0, 0, 0)

    for missing in ({"key": ""}, {"key": str(uuid4()), "omit_key_header": True}):
        status, refused = _signed_visit_request(
            clinic, clinic.owner, path=path, method=method, body=body, **missing
        )
        assert status == (401 if missing.get("omit_key_header") else 422), refused
        assert "data" not in refused
    for actor, wrong_method, wrong_purpose in (
        (clinic.receptionist, method, "VISIT_FOLLOWUP"),
        (clinic.owner, "GET", "VISIT_FOLLOWUP"),
        (clinic.owner, method, "APPOINTMENT_BOOKING"),
    ):
        status, refused = _signed_visit_request(
            clinic,
            actor,
            path=path,
            method=wrong_method,
            body=body,
            key=str(uuid4()),
            purpose=wrong_purpose,
        )
        assert status == 403, refused
        assert "data" not in refused
    assert _visit_effects(clinic, visit_id, receipt_operation) == before

    key = str(uuid4())
    status, accepted = _signed_visit_request(
        clinic, clinic.owner, path=path, method=method, body=body, key=key
    )
    assert status == success_status, accepted
    assert accepted["revision"] == 2
    assert accepted["data"]["visit_id"] == str(visit_id)
    assert accepted["data"]["revision"] == 2
    after = _visit_effects(clinic, visit_id, receipt_operation)
    assert after == (2, 1, int(operation == "extension"), 1)

    price_status, price = pricing_request(
        clinic,
        clinic.owner,
        method="PATCH",
        body=_patch(standard_new_exam_egp="125.00"),
        key=str(uuid4()),
    )
    assert price_status == 200, price
    assert _signed_visit_request(
        clinic, clinic.owner, path=path, method=method, body=body, key=key
    ) == (success_status, accepted)
    conflict, denied = _signed_visit_request(
        clinic, clinic.owner, path=path, method=method, body=changed, key=key
    )
    assert conflict == 409
    assert denied["error"]["code"] == "IDEMPOTENCY_CONFLICT"
    assert "data" not in denied
    assert _visit_effects(clinic, visit_id, receipt_operation) == after

    with clinic.migration.begin() as connection:
        connection.execute(
            text('DELETE FROM public."session" WHERE id=:id'),
            {"id": clinic.owner.session_id},
        )
    revoked, denied = _signed_visit_request(
        clinic, clinic.owner, path=path, method=method, body=body, key=key
    )
    assert revoked in (401, 403), denied
    assert "data" not in denied
    assert _visit_effects(clinic, visit_id, receipt_operation) == after


def test_source_decision_edit_preserves_confirmed_claim_and_replays_receipt(
    appointment_storage,
):
    runtime, _, (tenant, _), (doctor, _) = appointment_storage
    with transaction(runtime, tenant) as connection:
        source = _source(connection, tenant, doctor)
        confirmed = _claim(connection, tenant, source)
    command = _decision("OPTED_OUT", 1, reason="Synthetic later opt-out")
    key = str(uuid4())
    with transaction(runtime, tenant) as connection:
        _doctor_context(connection, doctor)
        original = connection.execute(
            text("""
            SELECT c.approved_deadline, c.policy_snapshot, s.entitlement_provenance,
                   s.base_egp, s.net_egp, a.service_date
            FROM public.followup_claims c
            JOIN public.appointment_price_snapshots s ON s.appointment_id=c.appointment_id
            JOIN public.appointments a ON a.id=c.appointment_id
            WHERE c.id=:claim
        """),
            {"claim": confirmed["claim"]},
        ).one()
        result = record_followup_decision(
            connection, _actor(tenant, doctor), source["visit"], command, key
        )
        assert result["revision"] == 2
    with transaction(runtime, tenant) as connection:
        _doctor_context(connection, doctor)
        assert (
            record_followup_decision(
                connection, _actor(tenant, doctor), source["visit"], command, key
            )
            == result
        )
        with pytest.raises(ReceiptConflict):
            record_followup_decision(
                connection,
                _actor(tenant, doctor),
                source["visit"],
                _decision("DEFAULT", 2),
                key,
            )
        frozen = connection.execute(
            text("""
            SELECT c.approved_deadline, c.policy_snapshot, s.entitlement_provenance,
                   s.base_egp, s.net_egp, a.service_date
            FROM public.followup_claims c
            JOIN public.appointment_price_snapshots s ON s.appointment_id=c.appointment_id
            JOIN public.appointments a ON a.id=c.appointment_id
            WHERE c.id=:claim
        """),
            {"claim": confirmed["claim"]},
        ).one()
        assert frozen == original


def test_extension_preserves_confirmed_target_and_records_linked_evidence(
    appointment_storage,
):
    runtime, _, (tenant, _), (doctor, _) = appointment_storage
    with transaction(runtime, tenant) as connection:
        source = _source(connection, tenant, doctor)
        confirmed = _claim(connection, tenant, source)
    command = _extension(source, confirmed["id"])
    key = str(uuid4())
    with transaction(runtime, tenant) as connection:
        _doctor_context(connection, doctor)
        frozen = connection.execute(
            text("""
            SELECT c.state, c.revision, c.approved_deadline, c.policy_snapshot,
                   s.entitlement_provenance, s.base_egp, s.net_egp,
                   a.service_date, a.status
            FROM public.followup_claims c
            JOIN public.appointment_price_snapshots s ON s.appointment_id=c.appointment_id
            JOIN public.appointments a ON a.id=c.appointment_id
            WHERE c.id=:claim
        """),
            {"claim": confirmed["claim"]},
        ).one()
        result = extend_followup(
            connection, _actor(tenant, doctor), source["visit"], command, key
        )
        assert result["revision"] == 2
        record = connection.execute(
            text("""
            SELECT e.prior_deadline, e.extended_deadline,
                   e.reason AS extension_reason,
                   e.expected_source_revision, e.expected_claim_revision,
                   r.change_kind, r.reason AS revision_reason,
                   r.revision, a.correlation_id
            FROM public.followup_extensions e
            JOIN public.clinical_revisions r ON r.id=e.clinical_revision_id
            JOIN public.clinical_audit a ON a.id=e.clinical_audit_id
            WHERE e.claim_id=:claim
        """),
            {"claim": confirmed["claim"]},
        ).one()
        assert record.prior_deadline == source["day"] + timedelta(days=7)
        assert record.extended_deadline == source["day"] + timedelta(days=9)
        assert record.extension_reason == record.revision_reason == command.reason
        assert record.change_kind == "EXTENSION" and record.revision == 2
        assert (
            record.expected_source_revision == 1 and record.expected_claim_revision == 1
        )
        assert (
            connection.execute(
                text("""
            SELECT c.state, c.revision, c.approved_deadline, c.policy_snapshot,
                   s.entitlement_provenance, s.base_egp, s.net_egp,
                   a.service_date, a.status
            FROM public.followup_claims c
            JOIN public.appointment_price_snapshots s ON s.appointment_id=c.appointment_id
            JOIN public.appointments a ON a.id=c.appointment_id
            WHERE c.id=:claim
        """),
                {"claim": confirmed["claim"]},
            ).one()
            == frozen
        )
        assert frozen.state == "ACTIVE"
        assert frozen.approved_deadline == source["day"] + timedelta(days=7)
    with transaction(runtime, tenant) as connection:
        _doctor_context(connection, doctor)
        assert (
            extend_followup(
                connection, _actor(tenant, doctor), source["visit"], command, key
            )
            == result
        )
        repeated_deadline = _extension(
            source,
            confirmed["id"],
            revision=2,
            deadline=source["day"] + timedelta(days=9),
        )
        renewed = extend_followup(
            connection,
            _actor(tenant, doctor),
            source["visit"],
            repeated_deadline,
            str(uuid4()),
        )
        assert renewed["revision"] == 3
        deadlines = connection.execute(
            text("""
            SELECT prior_deadline, extended_deadline
            FROM public.followup_extensions WHERE claim_id=:claim
            ORDER BY expected_source_revision
        """),
            {"claim": confirmed["claim"]},
        ).all()
        assert len(deadlines) == 2
        assert deadlines[1] == (command.extended_deadline, command.extended_deadline)


@pytest.mark.parametrize(
    "case",
    ["wrong_target", "stale_source", "stale_claim", "shorten", "released", "consumed"],
)
def test_extension_denials_leave_no_new_evidence(appointment_storage, case):
    runtime, _, (tenant, _), (doctor, _) = appointment_storage
    with transaction(runtime, tenant) as connection:
        source = _source(connection, tenant, doctor)
        confirmed = _claim(connection, tenant, source)
        if case in ("released", "consumed"):
            _outcome(
                connection,
                tenant,
                doctor,
                confirmed,
                status="Cancelled" if case == "released" else "Completed",
            )
    command = _extension(
        source,
        uuid4() if case == "wrong_target" else confirmed["id"],
        revision=0 if case == "stale_source" else 1,
        claim_revision=0 if case == "stale_claim" else 1,
        deadline=source["day"] + timedelta(days=6) if case == "shorten" else None,
    )
    with transaction(runtime, tenant) as connection:
        _doctor_context(connection, doctor)
        with pytest.raises(ContractError) as denied:
            extend_followup(
                connection,
                _actor(tenant, doctor),
                source["visit"],
                command,
                str(uuid4()),
            )
        assert denied.value.status_code == (422 if case == "shorten" else 409)
        if case in ("stale_source", "stale_claim"):
            assert denied.value.code == "STALE_REVISION"
        assert (
            connection.scalar(
                text(
                    "SELECT count(*) FROM public.followup_extensions WHERE claim_id=:claim"
                ),
                {"claim": confirmed["claim"]},
            )
            == 0
        )
        assert (
            connection.scalar(
                text("SELECT clinical_revision FROM public.visits WHERE id=:visit"),
                {"visit": source["visit"]},
            )
            == 1
        )

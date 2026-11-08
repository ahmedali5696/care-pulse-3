"""B-10 signed source decisions and confirmed extensions on real PostgreSQL."""

import asyncio
import json
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from hmac import new
from queue import Queue
from threading import Event
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest
from carepulse.db import transaction
from carepulse.identity.authorization import AuthorizedStaff
from carepulse.identity.contracts import ContractError, normalize_typed_body
from carepulse.identity.receipts import Receipt, ReceiptConflict, insert_tenant_receipt
from carepulse.identity.signing import canonical_payload
from carepulse.main import app
from carepulse.visits.eligibility import extend_followup, record_followup_decision
from carepulse.visits.eligibility_contracts import (
    ExtendFollowupV2,
    FollowupDecisionPatch,
    FollowupDecisionPatchV2,
)
from pydantic import ValidationError
from sqlalchemy import event, text
from test_appointment_persistence import appointment_storage as appointment_storage  # noqa: PLC0414
from test_booking import booking_clinic as booking_clinic  # noqa: PLC0414
from test_booking import booking_request
from test_booking_races import _wait_for_postgres_lock
from test_database import admin as admin  # noqa: PLC0414
from test_database import disposable_project as disposable_project  # noqa: PLC0414
from test_database import empty_database as empty_database  # noqa: PLC0414
from test_followup_persistence import (
    _claim,
    _v2_source,
)
from test_followup_persistence import (
    _source as _legacy_source,
)
from test_followups import followup_body, terminal_claim
from test_followups import followup_clinic as followup_clinic  # noqa: PLC0414
from test_patients import patient_backend as patient_backend  # noqa: PLC0414
from test_pricing import _patch, pricing_request
from test_request_boundary import boundary_data as boundary_data  # noqa: PLC0414
from test_visit_persistence import _receptionist


def _actor(tenant, staff, role="DOCTOR_OWNER", *, manager=False):
    return AuthorizedStaff(tenant, staff, role, False, 0, 0, 0, manager)


def _staff_context(connection, staff):
    connection.execute(
        text("SELECT set_config('app.staff_id', :actor, true)"),
        {"actor": str(staff)},
    )


def _source(clinic):
    with transaction(clinic.runtime, clinic.tenant) as connection:
        return _v2_source(connection, clinic.tenant, UUID(clinic.owner.staff_id))


def _decision(
    kind,
    revision=0,
    *,
    visit_revision=0,
    followup_revision=1,
    deadline=None,
    reason=None,
):
    decision = {"kind": kind}
    if deadline is not None:
        decision["deadline_date"] = deadline.isoformat()
    if reason is not None:
        decision["reason"] = reason
    return FollowupDecisionPatchV2.model_validate(
        {
            "followup_decision": decision,
            "expected_source_decision_revision": revision,
            "expected_visit_revision": visit_revision,
            "expected_followup_revision": followup_revision,
        }
    )


def _extension(
    source,
    appointment,
    *,
    source_revision=0,
    visit_revision=0,
    followup_revision=1,
    claim_revision=1,
    authorization_revision=0,
    appointment_revision=0,
    deadline=None,
):
    return ExtendFollowupV2.model_validate(
        {
            "appointment_id": str(appointment),
            "extended_deadline": (
                deadline or source["day"] + timedelta(days=9)
            ).isoformat(),
            "reason": "Synthetic confirmed extension",
            "expected_source_decision_revision": source_revision,
            "expected_visit_revision": visit_revision,
            "expected_followup_revision": followup_revision,
            "expected_claim_revision": claim_revision,
            "expected_authorization_revision": authorization_revision,
            "expected_appointment_revision": appointment_revision,
        }
    )


def _confirmed(clinic, source):
    status, reply = booking_request(
        clinic, clinic.owner, body=followup_body(source, offset=1), key=str(uuid4())
    )
    assert status == 201, reply
    saved = reply["data"]
    with transaction(clinic.runtime, clinic.tenant) as connection:
        claim = connection.scalar(
            text("""
                SELECT id FROM public.followup_claims
                WHERE organization_id=:tenant AND appointment_id=:appointment
            """),
            {"tenant": clinic.tenant, "appointment": UUID(saved["id"])},
        )
    assert claim is not None
    return {"id": UUID(saved["id"]), "claim": claim, "saved": saved}


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
    encoded = (
        body.encode()
        if isinstance(body, str)
        else json.dumps(body, separators=(",", ":")).encode()
    )
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


def _effects(clinic, visit, operation):
    with transaction(clinic.runtime, clinic.tenant) as connection:
        _staff_context(connection, UUID(clinic.owner.staff_id))
        return connection.execute(
            text("""
            SELECT
                (SELECT clinical_revision FROM public.visits
                 WHERE organization_id=:tenant AND id=:visit),
                (SELECT count(*) FROM public.clinical_revisions
                 WHERE organization_id=:tenant AND visit_id=:visit),
                (SELECT count(*) FROM public.followup_source_decision_events
                 WHERE organization_id=:tenant AND source_visit_id=:visit),
                (SELECT count(*) FROM public.followup_authorization_events
                 WHERE organization_id=:tenant AND source_visit_id=:visit
                   AND kind='EXTENSION'),
                (SELECT count(*) FROM public.idempotency_receipts
                 WHERE organization_id=:tenant AND operation=:operation)
        """),
            {"tenant": clinic.tenant, "visit": visit, "operation": operation},
        ).one()


def _frozen_claim(connection, claim):
    return connection.execute(
        text("""
        SELECT c.state, c.revision, c.approved_deadline, c.policy_snapshot,
               p.entitlement_provenance, p.base_egp, p.net_egp,
               a.service_date, a.status, a.revision AS appointment_revision
        FROM public.followup_claims c
        JOIN public.appointment_price_snapshots p
          ON (p.organization_id,p.appointment_id)=(c.organization_id,c.appointment_id)
        JOIN public.appointments a
          ON (a.organization_id,a.id)=(c.organization_id,c.appointment_id)
        WHERE c.id=:claim
    """),
        {"claim": claim},
    ).one()


@pytest.mark.parametrize("kind", ["DEFAULT", "OPTED_OUT", "DEADLINE"])
def test_doctor_decision_appends_actual_operational_evidence_without_clinical_change(
    appointment_storage, kind
):
    runtime, migration, (tenant, _), (doctor, _) = appointment_storage
    with transaction(runtime, tenant) as connection:
        source = _v2_source(connection, tenant, doctor)
    command = _decision(
        kind,
        deadline=source["day"] + timedelta(days=14) if kind == "DEADLINE" else None,
        reason="Synthetic Doctor restriction" if kind != "DEFAULT" else None,
    )
    with transaction(runtime, tenant) as connection:
        _staff_context(connection, doctor)
        projection, revision = record_followup_decision(
            connection, _actor(tenant, doctor), source["visit"], command, str(uuid4())
        )
        assert (projection["revision"], revision, projection["visit_revision"]) == (
            1,
            1,
            0,
        )
        assert projection["followup_decision"] == command.followup_decision.model_dump(
            mode="json", exclude_none=True
        )
        assert connection.execute(
            text(
                "SELECT clinical_revision, followup_decision FROM public.visits WHERE id=:visit"
            ),
            {"visit": source["visit"]},
        ).one() == (0, None)
        assert (
            connection.scalar(
                text(
                    "SELECT count(*) FROM public.clinical_revisions WHERE visit_id=:visit"
                ),
                {"visit": source["visit"]},
            )
            == 0
        )
        connection.execute(text("SET CONSTRAINTS ALL IMMEDIATE"))
    with migration.connect() as connection:
        event = connection.execute(
            text("""
            SELECT e.source_revision, e.actor_staff_id, e.actor_role,
                   e.before_decision, e.decision, e.clinical_revision,
                   a.before_state, a.after_state
            FROM public.followup_source_decision_events e
            JOIN public.sensitive_audit a ON a.id=e.sensitive_audit_id
            WHERE e.organization_id=:tenant AND e.source_visit_id=:visit
        """),
            {"tenant": tenant, "visit": source["visit"]},
        ).one()
    assert (event.source_revision, event.actor_staff_id, event.actor_role) == (
        1,
        doctor,
        "DOCTOR_OWNER",
    )
    assert event.before_decision is None
    assert event.decision == projection["followup_decision"]
    assert event.clinical_revision == 0
    assert event.before_state["source_revision"] == 0
    assert event.after_state["source_revision"] == 1


@pytest.mark.parametrize("manager", [False, True])
def test_receptionist_can_amend_own_decision_with_authorship_preserved(
    appointment_storage, manager
):
    runtime, migration, (tenant, _), (doctor, _) = appointment_storage
    staff = _receptionist(migration, tenant)
    if manager:
        with migration.begin() as connection:
            connection.execute(
                text(
                    "UPDATE public.staff_accounts SET operational_manager=true, security_revision=security_revision+1 WHERE id=:staff"
                ),
                {"staff": staff},
            )
    with transaction(runtime, tenant) as connection:
        source = _v2_source(connection, tenant, doctor)
    actor = _actor(tenant, staff, "RECEPTIONIST", manager=manager)
    with transaction(runtime, tenant) as connection:
        _staff_context(connection, staff)
        first, _ = record_followup_decision(
            connection,
            actor,
            source["visit"],
            _decision(
                "DEADLINE",
                deadline=source["day"] + timedelta(days=12),
                reason="Receptionist operational decision",
            ),
            str(uuid4()),
        )
        second, _ = record_followup_decision(
            connection, actor, source["visit"], _decision("DEFAULT", 1), str(uuid4())
        )
        assert (first["revision"], second["revision"]) == (1, 2)
        assert connection.execute(
            text(
                "SELECT clinical_revision, followup_decision FROM public.visits WHERE id=:visit"
            ),
            {"visit": source["visit"]},
        ).one() == (0, None)
    with migration.connect() as connection:
        events = connection.execute(
            text("""
            SELECT source_revision, actor_staff_id, actor_role, before_decision, decision
            FROM public.followup_source_decision_events
            WHERE organization_id=:tenant AND source_visit_id=:visit
            ORDER BY source_revision
        """),
            {"tenant": tenant, "visit": source["visit"]},
        ).all()
    assert [
        (row.source_revision, row.actor_staff_id, row.actor_role) for row in events
    ] == [(1, staff, "RECEPTIONIST"), (2, staff, "RECEPTIONIST")]
    assert events[1].before_decision == events[0].decision
    assert events[1].decision == {"kind": "DEFAULT"}


@pytest.mark.parametrize("restriction", ["OPTED_OUT", "DEADLINE"])
def test_receptionist_cannot_replace_actual_doctor_restriction(
    appointment_storage, restriction
):
    runtime, migration, (tenant, _), (doctor, _) = appointment_storage
    staff = _receptionist(migration, tenant)
    with transaction(runtime, tenant) as connection:
        source = _v2_source(connection, tenant, doctor)
    with transaction(runtime, tenant) as connection:
        _staff_context(connection, doctor)
        record_followup_decision(
            connection,
            _actor(tenant, doctor),
            source["visit"],
            _decision(
                restriction,
                deadline=source["day"] + timedelta(days=3)
                if restriction == "DEADLINE"
                else None,
                reason="Doctor clinical restriction",
            ),
            str(uuid4()),
        )
    with transaction(runtime, tenant) as connection:
        _staff_context(connection, staff)
        with pytest.raises(ContractError) as denied:
            record_followup_decision(
                connection,
                _actor(tenant, staff, "RECEPTIONIST"),
                source["visit"],
                _decision("DEFAULT", 1),
                str(uuid4()),
            )
        assert denied.value.status_code == 403
        assert (
            connection.scalar(
                text(
                    "SELECT count(*) FROM public.followup_source_decision_events WHERE source_visit_id=:visit"
                ),
                {"visit": source["visit"]},
            )
            == 1
        )


@pytest.mark.parametrize(
    ("case", "status", "code"),
    [
        ("before_completion", 422, None),
        ("stale_source", 409, "STALE_REVISION"),
        ("stale_visit", 409, "STALE_REVISION"),
        ("stale_policy", 409, "STALE_REVISION"),
        ("operator", 403, None),
        ("disabled", 403, None),
    ],
)
def test_denied_decision_keeps_clinical_operational_and_receipt_evidence_empty(
    appointment_storage, case, status, code
):
    runtime, _, (tenant, _), (doctor, _) = appointment_storage
    with transaction(runtime, tenant) as connection:
        source = _v2_source(connection, tenant, doctor)
    command = _decision(
        "DEADLINE",
        revision=int(case == "stale_source"),
        visit_revision=int(case == "stale_visit"),
        followup_revision=2 if case == "stale_policy" else 1,
        deadline=source["day"]
        + timedelta(days=-1 if case == "before_completion" else 8),
        reason="Synthetic operational decision",
    )
    with transaction(runtime, tenant) as connection:
        _staff_context(connection, doctor)
        if case == "disabled":
            connection.execute(
                text(
                    "UPDATE public.staff_accounts SET enabled=false, security_revision=security_revision+1 WHERE id=:staff"
                ),
                {"staff": doctor},
            )
        with pytest.raises(ContractError) as denied, connection.begin_nested():
            record_followup_decision(
                connection,
                _actor(
                    tenant, doctor, "OPERATOR" if case == "operator" else "DOCTOR_OWNER"
                ),
                source["visit"],
                command,
                str(uuid4()),
            )
        assert (denied.value.status_code, denied.value.code) == (status, code)
        if case == "disabled":
            connection.execute(
                text(
                    "UPDATE public.staff_accounts SET enabled=true, security_revision=security_revision+1 WHERE id=:staff"
                ),
                {"staff": doctor},
            )
        assert connection.execute(
            text(
                "SELECT clinical_revision, followup_decision FROM public.visits WHERE id=:visit"
            ),
            {"visit": source["visit"]},
        ).one() == (0, None)
        assert (
            connection.scalar(
                text(
                    "SELECT count(*) FROM public.followup_source_decision_events WHERE source_visit_id=:visit"
                ),
                {"visit": source["visit"]},
            )
            == 0
        )
        assert (
            connection.scalar(
                text("""
            SELECT count(*) FROM public.idempotency_receipts
            WHERE organization_id=:tenant AND operation='visits.followup_decision'
        """),
                {"tenant": tenant},
            )
            == 0
        )


def test_v2_dto_rejects_clinical_fields_missing_reason_and_partial_tokens():
    for fields in (
        {
            "followup_decision": {"kind": "OPTED_OUT", "reason": " "},
            "expected_source_decision_revision": 0,
            "expected_visit_revision": 0,
            "expected_followup_revision": 1,
        },
        {
            "followup_decision": {"kind": "DEFAULT"},
            "expected_visit_revision": 0,
            "expected_source_decision_revision": 0,
            "expected_followup_revision": 1,
            "diagnosis": "override",
        },
        {
            "followup_decision": {"kind": "DEFAULT"},
            "expected_visit_revision": 0,
            "expected_source_decision_revision": 0,
        },
    ):
        with pytest.raises(ValidationError):
            FollowupDecisionPatchV2.model_validate(fields)
    with pytest.raises(ValidationError):
        ExtendFollowupV2.model_validate(
            {
                "appointment_id": str(uuid4()),
                "extended_deadline": "2026-11-10",
                "reason": "",
                "expected_source_decision_revision": 0,
                "expected_visit_revision": 0,
                "expected_followup_revision": 1,
                "expected_claim_revision": 1,
                "expected_authorization_revision": 0,
                "expected_appointment_revision": 0,
            }
        )


@pytest.mark.parametrize(
    ("operation", "method", "success_status"),
    [("decision", "PATCH", 200), ("extension", "POST", 201)],
)
@pytest.mark.parametrize("staff_kind", ["owner", "receptionist"])
def test_signed_v2_receipt_replay_conflict_and_revoked_session(
    followup_clinic, operation, method, success_status, staff_kind
):
    clinic = followup_clinic
    source = _source(clinic)
    confirmed = _confirmed(clinic, source) if operation == "extension" else None
    actor = getattr(clinic, staff_kind)
    path = f"/v1/visits/{source['visit']}"
    if operation == "decision":
        command = _decision("OPTED_OUT", reason="Actual staff operational reason")
        changed_decision = {"kind": "DEFAULT"}
        receipt_operation = "visits.followup_decision"
    else:
        path += "/extend-followup"
        command = _extension(source, confirmed["id"])
        receipt_operation = "visits.extend_followup"
    body = command.model_dump(mode="json", exclude_unset=True)
    changed = (
        {**body, "followup_decision": changed_decision}
        if operation == "decision"
        else {**body, "reason": "Different operational reason"}
    )
    before = _effects(clinic, source["visit"], receipt_operation)
    assert before == (0, 0, 0, 0, 0)

    for options, expected in (
        ({"key": ""}, 422),
        ({"key": str(uuid4()), "omit_key_header": True}, 401),
    ):
        refused_status, refused = _signed_visit_request(
            clinic, actor, path=path, method=method, body=body, **options
        )
        assert refused_status == expected, refused
        assert "data" not in refused
    for wrong_method, wrong_purpose in (
        ("GET", "VISIT_FOLLOWUP"),
        (method, "APPOINTMENT_BOOKING"),
    ):
        refused_status, refused = _signed_visit_request(
            clinic,
            actor,
            path=path,
            method=wrong_method,
            body=body,
            key=str(uuid4()),
            purpose=wrong_purpose,
        )
        assert refused_status == 403, refused
        assert "data" not in refused
    assert _effects(clinic, source["visit"], receipt_operation) == before

    key = str(uuid4())
    accepted_status, accepted = _signed_visit_request(
        clinic, actor, path=path, method=method, body=body, key=key
    )
    assert accepted_status == success_status, accepted
    assert accepted["revision"] == accepted["data"]["revision"] == 1
    assert accepted["data"]["visit_revision"] == 0
    assert accepted["data"]["actor_role"] == (
        "DOCTOR_OWNER" if staff_kind == "owner" else "RECEPTIONIST"
    )
    assert UUID(accepted["data"]["event_id"])
    after = _effects(clinic, source["visit"], receipt_operation)
    assert after == (
        0,
        0,
        int(operation == "decision"),
        int(operation == "extension"),
        1,
    )

    price_status, price = pricing_request(
        clinic,
        clinic.owner,
        method="PATCH",
        body=_patch(expected_price_revision=3, standard_new_exam_egp="125.00"),
        key=str(uuid4()),
    )
    assert price_status == 200, price
    assert _signed_visit_request(
        clinic, actor, path=path, method=method, body=body, key=key
    ) == (success_status, accepted)
    conflict_status, refused = _signed_visit_request(
        clinic, actor, path=path, method=method, body=changed, key=key
    )
    assert conflict_status == 409, refused
    assert refused["error"]["code"] == "IDEMPOTENCY_CONFLICT"
    assert "data" not in refused
    assert _effects(clinic, source["visit"], receipt_operation) == after

    with clinic.migration.begin() as connection:
        connection.execute(
            text('DELETE FROM public."session" WHERE id=:id'),
            {"id": actor.session_id},
        )
    revoked_status, refused = _signed_visit_request(
        clinic, actor, path=path, method=method, body=body, key=key
    )
    assert revoked_status in (401, 403), refused
    assert "data" not in refused
    assert _effects(clinic, source["visit"], receipt_operation) == after


@pytest.mark.parametrize(
    "revoke_first", [True, False], ids=["revoke-first", "retry-first"]
)
def test_signed_decision_receipt_replay_serializes_with_staff_revocation(
    patient_backend, revoke_first
):
    clinic = patient_backend
    source = _source(clinic)
    body = _decision("DEFAULT").model_dump(mode="json", exclude_unset=True)
    changed = {**body, "followup_decision": {"kind": "OPTED_OUT", "reason": "changed"}}
    path = f"/v1/visits/{source['visit']}"
    key = str(uuid4())
    accepted = _signed_visit_request(
        clinic, clinic.owner, path=path, method="PATCH", body=body, key=key
    )
    assert accepted[0] == 200, accepted
    saved_query = text("""
        SELECT request_digest, result_projection, result_revision,
          (SELECT count(*) FROM public.followup_source_decision_events
           WHERE organization_id=:tenant AND source_visit_id=:visit) AS events
        FROM public.idempotency_receipts
        WHERE organization_id=:tenant AND idempotency_key=:key
          AND operation='visits.followup_decision'
    """)
    saved_params = {"tenant": clinic.tenant, "visit": source["visit"], "key": key}
    with clinic.migration.connect() as connection:
        frozen = connection.execute(saved_query, saved_params).one()
    runtime_pid = Queue(maxsize=1)
    receipt_reached = Event()
    allow_receipt = Event()

    def observe_sql(connection, cursor, statement, parameters, context, executemany):
        marker = (
            "FOR UPDATE OF s"
            if revoke_first
            else "FROM public.idempotency_receipts AS r"
        )
        if marker in statement and runtime_pid.empty():
            runtime_pid.put(cursor.connection.info.backend_pid)
            if not revoke_first:
                receipt_reached.set()
                assert allow_receipt.wait(timeout=10)

    def retry():
        return _signed_visit_request(
            clinic, clinic.owner, path=path, method="PATCH", body=body, key=key
        )

    def revoke():
        with clinic.migration.begin() as connection:
            revoker_pid = connection.scalar(text("SELECT pg_backend_pid()"))
            revocation_started.put(revoker_pid)
            connection.execute(
                text("""
                    UPDATE public.staff_accounts SET enabled=false
                    WHERE organization_id=:tenant AND id=:staff
                """),
                {"tenant": clinic.tenant, "staff": UUID(clinic.owner.staff_id)},
            )

    revocation_started = Queue(maxsize=1)
    event.listen(clinic.runtime, "before_cursor_execute", observe_sql)
    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            if revoke_first:
                with clinic.migration.connect() as holder:
                    tx = holder.begin()
                    try:
                        holder_pid = holder.scalar(text("SELECT pg_backend_pid()"))
                        holder.execute(
                            text("""
                                UPDATE public.staff_accounts SET enabled=false
                                WHERE organization_id=:tenant AND id=:staff
                            """),
                            {
                                "tenant": clinic.tenant,
                                "staff": UUID(clinic.owner.staff_id),
                            },
                        )
                        in_flight = pool.submit(retry)
                        waiter_pid = runtime_pid.get(timeout=10)
                        with clinic.runtime.connect() as observer:
                            _wait_for_postgres_lock(observer, waiter_pid, holder_pid)
                    finally:
                        tx.commit()
                assert in_flight.result(timeout=15)[0] == 403
            else:
                in_flight = pool.submit(retry)
                assert receipt_reached.wait(timeout=10)
                holder_pid = runtime_pid.get(timeout=10)
                revoked = pool.submit(revoke)
                waiter_pid = revocation_started.get(timeout=10)
                with clinic.migration.connect() as observer:
                    _wait_for_postgres_lock(observer, waiter_pid, holder_pid)
                allow_receipt.set()
                assert in_flight.result(timeout=15) == accepted
                revoked.result(timeout=15)
    finally:
        allow_receipt.set()
        event.remove(clinic.runtime, "before_cursor_execute", observe_sql)

    denied_codes = []
    for candidate in (body, changed):
        denied = _signed_visit_request(
            clinic, clinic.owner, path=path, method="PATCH", body=candidate, key=key
        )
        assert denied[0] == 403, denied
        assert "data" not in denied[1]
        denied_codes.append(denied[1]["error"]["code"])
    assert denied_codes == ["FORBIDDEN", "FORBIDDEN"]
    with clinic.migration.connect() as connection:
        assert connection.execute(saved_query, saved_params).one() == frozen


@pytest.mark.parametrize(
    "bad_body",
    [
        '{"followup_decision":{"kind":"DEFAULT","kind":"OPTED_OUT"},"expected_source_decision_revision":0,"expected_visit_revision":0,"expected_followup_revision":1}',
        '{"followup_decision":{"kind":"DEFAULT"},"expected_source_decision_revision":0,"expected_visit_revision":0}',
        '{"followup_decision":{"kind":"DEFAULT"},"expected_source_decision_revision":0,"expected_visit_revision":0,"expected_followup_revision":1,"medical_notes":"hidden"}',
        '{"followup_decision":{"kind":"DEFAULT"},"expected_source_decision_revision":null,"expected_visit_revision":0,"expected_followup_revision":1}',
    ],
)
def test_signed_partial_duplicate_or_hybrid_v2_request_denies_without_evidence(
    patient_backend, bad_body
):
    clinic = patient_backend
    source = _source(clinic)
    status, refused = _signed_visit_request(
        clinic,
        clinic.owner,
        path=f"/v1/visits/{source['visit']}",
        method="PATCH",
        body=bad_body,
        key=str(uuid4()),
    )
    assert status == 422, refused
    assert "data" not in refused
    assert _effects(clinic, source["visit"], "visits.followup_decision") == (
        0,
        0,
        0,
        0,
        0,
    )


def test_signed_committed_v1_replays_exact_original_digest_including_null_distinction(
    patient_backend,
):
    clinic = patient_backend
    source = _source(clinic)
    visit_id = source["visit"]
    key = str(uuid4())
    command = FollowupDecisionPatch.model_validate(
        {"followup_decision": {"kind": "DEFAULT"}, "expected_visit_revision": 0}
    )
    body = command.model_dump(mode="json", exclude_unset=True)
    historical = {
        "visit_id": str(visit_id),
        "followup_decision": {"kind": "DEFAULT"},
        "revision": 0,
    }
    digest = sha256(visit_id.bytes + normalize_typed_body(command)).hexdigest()
    with transaction(clinic.runtime, clinic.tenant) as connection:
        _staff_context(connection, UUID(clinic.owner.staff_id))
        insert_tenant_receipt(
            connection,
            Receipt(
                actor_id=UUID(clinic.owner.staff_id),
                operation="visits.followup_decision",
                idempotency_key=key,
                request_digest=digest,
                result_projection=historical,
                result_revision=0,
                organization_id=clinic.tenant,
                staff_id=UUID(clinic.owner.staff_id),
            ),
        )
    path = f"/v1/visits/{visit_id}"
    assert _signed_visit_request(
        clinic, clinic.owner, path=path, method="PATCH", body=body, key=key
    ) == (200, {"data": historical, "revision": 0})
    with_null = {**body, "followup_decision": {"kind": "DEFAULT", "reason": None}}
    conflict_status, conflict = _signed_visit_request(
        clinic, clinic.owner, path=path, method="PATCH", body=with_null, key=key
    )
    assert conflict_status == 409, conflict
    assert conflict["error"]["code"] == "IDEMPOTENCY_CONFLICT"
    unavailable_status, unavailable = _signed_visit_request(
        clinic, clinic.owner, path=path, method="PATCH", body=body, key=str(uuid4())
    )
    assert unavailable_status == 409, unavailable
    for candidate in (body, with_null):
        hidden_status, hidden = _signed_visit_request(
            clinic,
            clinic.owner,
            path=f"/v1/visits/{uuid4()}",
            method="PATCH",
            body=candidate,
            key=key,
        )
        assert hidden_status == 404, hidden
        assert "data" not in hidden
    with transaction(clinic.runtime, clinic.tenant) as connection:
        stored = connection.execute(
            text("""
            SELECT request_digest, result_projection, result_revision
            FROM public.idempotency_receipts WHERE organization_id=:tenant
              AND operation='visits.followup_decision' AND idempotency_key=:key
        """),
            {"tenant": clinic.tenant, "key": key},
        ).one()
    assert stored == (digest, historical, 0)
    assert _effects(clinic, visit_id, "visits.followup_decision") == (0, 0, 0, 0, 1)


def test_signed_pre0029_v1_extension_receipt_survives_upgrade_and_authority_checks(
    patient_backend, admin, empty_database, monkeypatch
):
    # Local import avoids the historical migration test's fixture-helper import cycle.
    from test_followup_migration import _historical_extension, _predecessor
    from test_visit_migration import _upgrade

    migration, runtime, tenant, doctor = _predecessor(admin, empty_database)
    try:
        _upgrade(migration, "0024_b06_followup_window")
        session_id = f"synthetic-b10-v1-extension-{uuid4()}"
        with migration.begin() as connection:
            connection.execute(
                text("""
                    INSERT INTO public."session"
                        (id, "userId", token, "expiresAt", "createdAt", "updatedAt",
                         "securityRevision", "absoluteDeadline", "lastActivity")
                    SELECT :session, s.auth_user_id, :token,
                           now() + interval '1 hour', now(), now(), 0,
                           now() + interval '1 hour', now()
                    FROM public.staff_accounts s
                    WHERE s.organization_id=:tenant AND s.id=:doctor
                """),
                {
                    "session": session_id,
                    "token": str(uuid4()),
                    "tenant": tenant,
                    "doctor": doctor,
                },
            )
        with transaction(runtime, tenant) as connection:
            source = _legacy_source(connection, tenant, doctor)
            claim = _claim(connection, tenant, source)
            key = str(uuid4())
            command, original = _historical_extension(
                connection, tenant, doctor, source, claim, key
            )
        typed_body = command.model_dump(mode="json", exclude_unset=True)
        digest = sha256(
            source["visit"].bytes + normalize_typed_body(command)
        ).hexdigest()
        receipt_sql = text("""
            SELECT request_digest, result_projection, result_revision
            FROM public.idempotency_receipts
            WHERE organization_id=:tenant AND operation='visits.extend_followup'
              AND idempotency_key=:key
        """)
        receipt_args = {"tenant": tenant, "key": key}
        with migration.connect() as connection:
            original_receipt = connection.execute(receipt_sql, receipt_args).one()
        assert original_receipt == (digest, original, 2)
        _upgrade(migration, "0029_b10_followup_writers")
        with migration.connect() as connection:
            assert (
                connection.execute(receipt_sql, receipt_args).one() == original_receipt
            )

        actor = replace(
            patient_backend.owner,
            organization_id=str(tenant),
            staff_id=str(doctor),
            session_id=session_id,
        )
        clinic = SimpleNamespace(
            tenant=tenant,
            runtime=runtime,
            migration=migration,
            owner=actor,
            config=patient_backend.config,
        )
        monkeypatch.setattr(app.state, "db_engine", runtime)
        path = f"/v1/visits/{source['visit']}/extend-followup"
        before = _effects(clinic, source["visit"], "visits.extend_followup")
        assert _signed_visit_request(
            clinic, actor, path=path, method="POST", body=typed_body, key=key
        ) == (201, {"data": original, "revision": 2})
        changed = {**typed_body, "reason": "Different reason"}
        status, conflict = _signed_visit_request(
            clinic, actor, path=path, method="POST", body=changed, key=key
        )
        assert status == 409, conflict
        assert conflict["error"]["code"] == "IDEMPOTENCY_CONFLICT"
        assert (
            _signed_visit_request(
                clinic,
                actor,
                path=path,
                method="POST",
                body=typed_body,
                key=str(uuid4()),
            )[0]
            == 409
        )
        for candidate in (typed_body, changed):
            status, refusal = _signed_visit_request(
                clinic,
                actor,
                path=f"/v1/visits/{uuid4()}/extend-followup",
                method="POST",
                body=candidate,
                key=key,
            )
            assert status == 404, refusal
            assert "data" not in refusal
        status, refusal = _signed_visit_request(
            clinic,
            actor,
            path=path,
            method="POST",
            body=changed,
            key=key,
            purpose="APPOINTMENT_BOOKING",
        )
        assert status == 403, refusal
        assert "data" not in refusal
        with migration.begin() as connection:
            connection.execute(
                text('DELETE FROM public."session" WHERE id=:session'),
                {"session": session_id},
            )
        for candidate in (typed_body, changed):
            status, refusal = _signed_visit_request(
                clinic, actor, path=path, method="POST", body=candidate, key=key
            )
            assert status in (401, 403), refusal
            assert "data" not in refusal
        with migration.connect() as connection:
            assert (
                connection.execute(receipt_sql, receipt_args).one() == original_receipt
            )
            assert (
                connection.scalar(
                    text(
                        "SELECT count(*) FROM public.followup_extensions WHERE claim_id=:claim"
                    ),
                    {"claim": claim["claim"]},
                )
                == 1
            )
        assert _effects(clinic, source["visit"], "visits.extend_followup") == before
    finally:
        runtime.dispose()
        migration.dispose()


@pytest.mark.parametrize(
    "corruption",
    [
        "extra_field",
        "altered_decision",
        "altered_revision",
        "nested_followup_revision",
        "boolean_followup_revision",
        "wrong_followup_revision",
        "nested_reason",
    ],
)
def test_invalid_saved_v2_decision_receipt_denies_before_digest_conflict(
    patient_backend, corruption
):
    clinic = patient_backend
    source = _source(clinic)
    path = f"/v1/visits/{source['visit']}"
    command = _decision("DEFAULT")
    body = command.model_dump(mode="json", exclude_unset=True)
    status, accepted = _signed_visit_request(
        clinic, clinic.owner, path=path, method="PATCH", body=body, key=str(uuid4())
    )
    assert status == 200, accepted
    key = str(uuid4())
    saved_projection = dict(accepted["data"])
    if corruption == "extra_field":
        saved_projection["medical_notes"] = "restricted narrative"
    elif corruption == "altered_decision":
        saved_projection["followup_decision"] = {
            "kind": "OPTED_OUT",
            "reason": "forged",
        }
    elif corruption == "altered_revision":
        saved_projection["source_decision_revision"] += 1
    elif corruption == "nested_followup_revision":
        saved_projection["followup_revision"] = {"clinical_notes": "private"}
    elif corruption == "boolean_followup_revision":
        saved_projection["followup_revision"] = True
    elif corruption == "wrong_followup_revision":
        saved_projection["followup_revision"] += 1
    else:
        saved_projection["followup_decision"] = {
            "kind": "OPTED_OUT",
            "reason": {"clinical_notes": "private"},
        }
    with transaction(clinic.runtime, clinic.tenant) as connection:
        _staff_context(connection, UUID(clinic.owner.staff_id))
        insert_tenant_receipt(
            connection,
            Receipt(
                actor_id=UUID(clinic.owner.staff_id),
                operation="visits.followup_decision",
                idempotency_key=key,
                request_digest=sha256(
                    source["visit"].bytes + normalize_typed_body(command)
                ).hexdigest(),
                result_projection=saved_projection,
                result_revision=accepted["revision"],
                organization_id=clinic.tenant,
                staff_id=UUID(clinic.owner.staff_id),
            ),
        )
    with clinic.migration.connect() as connection:
        frozen = connection.execute(
            text("""
            SELECT result_projection, request_digest, result_revision
            FROM public.idempotency_receipts
            WHERE organization_id=:tenant AND operation='visits.followup_decision'
              AND idempotency_key=:key
        """),
            {"tenant": clinic.tenant, "key": key},
        ).one()
    for candidate in (
        body,
        {**body, "followup_decision": {"kind": "OPTED_OUT", "reason": "changed"}},
    ):
        denial_status, denied = _signed_visit_request(
            clinic, clinic.owner, path=path, method="PATCH", body=candidate, key=key
        )
        assert denial_status == 403, denied
        assert "data" not in denied
    with clinic.migration.connect() as connection:
        assert (
            connection.execute(
                text("""
            SELECT result_projection, request_digest, result_revision
            FROM public.idempotency_receipts
            WHERE organization_id=:tenant AND operation='visits.followup_decision'
              AND idempotency_key=:key
        """),
                {"tenant": clinic.tenant, "key": key},
            ).one()
            == frozen
        )
    assert _effects(clinic, source["visit"], "visits.followup_decision") == (
        0,
        0,
        1,
        0,
        2,
    )


@pytest.mark.parametrize(
    "corruption",
    [
        "deadline",
        "nested_followup_revision",
        "boolean_followup_revision",
        "wrong_followup_revision",
        "nested_claim_revision",
    ],
)
def test_saved_v2_extension_receipt_must_match_immutable_authorization_event(
    followup_clinic, corruption
):
    clinic = followup_clinic
    source = _source(clinic)
    confirmed = _confirmed(clinic, source)
    command = _extension(source, confirmed["id"])
    body = command.model_dump(mode="json", exclude_unset=True)
    path = f"/v1/visits/{source['visit']}/extend-followup"
    status, accepted = _signed_visit_request(
        clinic, clinic.owner, path=path, method="POST", body=body, key=str(uuid4())
    )
    assert status == 201, accepted
    altered = dict(accepted["data"])
    if corruption == "deadline":
        altered["extended_deadline"] = (source["day"] + timedelta(days=10)).isoformat()
    elif corruption == "nested_followup_revision":
        altered["followup_revision"] = {"clinical_notes": "private"}
    elif corruption == "boolean_followup_revision":
        altered["followup_revision"] = False
    elif corruption == "wrong_followup_revision":
        altered["followup_revision"] += 1
    else:
        altered["claim_revision"] = ["private clinical details"]
    key = str(uuid4())
    with transaction(clinic.runtime, clinic.tenant) as connection:
        _staff_context(connection, UUID(clinic.owner.staff_id))
        insert_tenant_receipt(
            connection,
            Receipt(
                actor_id=UUID(clinic.owner.staff_id),
                operation="visits.extend_followup",
                idempotency_key=key,
                request_digest=sha256(
                    source["visit"].bytes + normalize_typed_body(command)
                ).hexdigest(),
                result_projection=altered,
                result_revision=accepted["revision"],
                organization_id=clinic.tenant,
                staff_id=UUID(clinic.owner.staff_id),
            ),
        )
    for attempted in (body, {**body, "reason": "Different reason"}):
        denied, response = _signed_visit_request(
            clinic,
            clinic.owner,
            path=path,
            method="POST",
            body=attempted,
            key=key,
        )
        assert denied == 403, response
        assert "data" not in response
    assert _effects(clinic, source["visit"], "visits.extend_followup") == (
        0,
        0,
        0,
        1,
        2,
    )


def test_receptionist_cannot_recover_legacy_doctor_reason_from_saved_receipt(
    patient_backend,
):
    clinic = patient_backend
    source = _source(clinic)
    visit_id = source["visit"]
    key = str(uuid4())
    command = FollowupDecisionPatch.model_validate(
        {
            "followup_decision": {
                "kind": "OPTED_OUT",
                "reason": "Protected Doctor narrative",
            },
            "expected_visit_revision": 0,
        }
    )
    payload = command.model_dump(mode="json", exclude_unset=True)
    digest = sha256(visit_id.bytes + normalize_typed_body(command)).hexdigest()
    receptionist = UUID(clinic.receptionist.staff_id)
    with transaction(clinic.runtime, clinic.tenant) as connection:
        _staff_context(connection, receptionist)
        insert_tenant_receipt(
            connection,
            Receipt(
                actor_id=receptionist,
                operation="visits.followup_decision",
                idempotency_key=key,
                request_digest=digest,
                result_projection={
                    "visit_id": str(visit_id),
                    "revision": 0,
                    "followup_decision": payload["followup_decision"],
                },
                result_revision=0,
                organization_id=clinic.tenant,
                staff_id=receptionist,
            ),
        )
    for candidate in (payload, {**payload, "expected_visit_revision": 1}):
        status, denied = _signed_visit_request(
            clinic,
            clinic.receptionist,
            path=f"/v1/visits/{visit_id}",
            method="PATCH",
            body=candidate,
            key=key,
        )
        assert status == 403, denied
        assert "data" not in denied
    assert _effects(clinic, visit_id, "visits.followup_decision") == (0, 0, 0, 0, 1)


def test_post_confirmation_doctor_restriction_leaves_original_entitlement_frozen(
    followup_clinic,
):
    clinic = followup_clinic
    source = _source(clinic)
    confirmed = _confirmed(clinic, source)
    command = _decision("OPTED_OUT", reason="Doctor later restriction")
    actor = _actor(clinic.tenant, UUID(clinic.owner.staff_id))
    key = str(uuid4())
    with transaction(clinic.runtime, clinic.tenant) as connection:
        _staff_context(connection, actor.staff_id)
        frozen = _frozen_claim(connection, confirmed["claim"])
        original = record_followup_decision(
            connection, actor, source["visit"], command, key
        )
        assert original[1] == 1
    with transaction(clinic.runtime, clinic.tenant) as connection:
        _staff_context(connection, actor.staff_id)
        assert (
            record_followup_decision(connection, actor, source["visit"], command, key)
            == original
        )
        with pytest.raises(ReceiptConflict):
            record_followup_decision(
                connection, actor, source["visit"], _decision("DEFAULT", 1), key
            )
        assert _frozen_claim(connection, confirmed["claim"]) == frozen


def test_confirmed_extension_appends_authorization_without_clinical_or_price_change(
    followup_clinic,
):
    clinic = followup_clinic
    source = _source(clinic)
    confirmed = _confirmed(clinic, source)
    command = _extension(source, confirmed["id"])
    actor = _actor(clinic.tenant, UUID(clinic.owner.staff_id))
    key = str(uuid4())
    with transaction(clinic.runtime, clinic.tenant) as connection:
        _staff_context(connection, actor.staff_id)
        frozen = _frozen_claim(connection, confirmed["claim"])
        original = extend_followup(connection, actor, source["visit"], command, key)
        assert original[1] == original[0]["revision"] == 1
        assert (original[0]["claim_revision"], original[0]["visit_revision"]) == (1, 0)
        assert _frozen_claim(connection, confirmed["claim"]) == frozen
        assert connection.execute(
            text(
                "SELECT clinical_revision, followup_decision FROM public.visits WHERE id=:visit"
            ),
            {"visit": source["visit"]},
        ).one() == (0, None)
        connection.execute(text("SET CONSTRAINTS ALL IMMEDIATE"))
    with transaction(clinic.runtime, clinic.tenant) as connection:
        _staff_context(connection, actor.staff_id)
        assert (
            extend_followup(connection, actor, source["visit"], command, key)
            == original
        )
        repeated = extend_followup(
            connection,
            actor,
            source["visit"],
            _extension(source, confirmed["id"], authorization_revision=1),
            str(uuid4()),
        )
        assert repeated[1] == 2
        assert _frozen_claim(connection, confirmed["claim"]) == frozen
    with clinic.migration.connect() as connection:
        events = connection.execute(
            text("""
            SELECT e.authorization_revision, e.kind, e.prior_deadline,
                   e.new_deadline, e.claim_revision, e.clinical_revision,
                   e.actor_staff_id, e.actor_role, a.before_state, a.after_state
            FROM public.followup_authorization_events e
            JOIN public.sensitive_audit a ON a.id=e.sensitive_audit_id
            WHERE e.organization_id=:tenant AND e.claim_id=:claim
            ORDER BY e.authorization_revision
        """),
            {"tenant": clinic.tenant, "claim": confirmed["claim"]},
        ).all()
    assert len(events) == 2
    assert [event.authorization_revision for event in events] == [1, 2]
    assert [event.kind for event in events] == ["EXTENSION", "EXTENSION"]
    assert [event.prior_deadline for event in events] == [
        source["day"] + timedelta(days=7),
        source["day"] + timedelta(days=9),
    ]
    assert all(
        event.new_deadline == source["day"] + timedelta(days=9) for event in events
    )
    assert all(
        (
            event.claim_revision,
            event.clinical_revision,
            event.actor_staff_id,
            event.actor_role,
        )
        == (1, 0, actor.staff_id, "DOCTOR_OWNER")
        for event in events
    )


@pytest.mark.parametrize(
    ("case", "status", "code"),
    [
        ("wrong_target", 409, None),
        ("stale_source", 409, "STALE_REVISION"),
        ("stale_visit", 409, "STALE_REVISION"),
        ("stale_policy", 409, "STALE_REVISION"),
        ("stale_claim", 409, "STALE_REVISION"),
        ("stale_authorization", 409, "STALE_REVISION"),
        ("stale_appointment", 409, "STALE_REVISION"),
        ("shorten", 422, None),
        ("released", 409, None),
        ("consumed", 409, None),
    ],
)
def test_denied_extension_preserves_claim_and_has_no_new_authorization_evidence(
    followup_clinic, case, status, code
):
    clinic = followup_clinic
    source = _source(clinic)
    confirmed = _confirmed(clinic, source)
    if case in ("released", "consumed"):
        terminal_claim(
            clinic,
            confirmed["saved"],
            status="Cancelled" if case == "released" else "Completed",
        )
    command = _extension(
        source,
        uuid4() if case == "wrong_target" else confirmed["id"],
        source_revision=int(case == "stale_source"),
        visit_revision=int(case == "stale_visit"),
        followup_revision=2 if case == "stale_policy" else 1,
        claim_revision=0 if case == "stale_claim" else 1,
        authorization_revision=int(case == "stale_authorization"),
        appointment_revision=2 if case == "stale_appointment" else 0,
        deadline=source["day"] + timedelta(days=6) if case == "shorten" else None,
    )
    with transaction(clinic.runtime, clinic.tenant) as connection:
        _staff_context(connection, UUID(clinic.owner.staff_id))
        frozen = _frozen_claim(connection, confirmed["claim"])
        with pytest.raises(ContractError) as denied:
            extend_followup(
                connection,
                _actor(clinic.tenant, UUID(clinic.owner.staff_id)),
                source["visit"],
                command,
                str(uuid4()),
            )
        assert (denied.value.status_code, denied.value.code) == (status, code)
        assert _frozen_claim(connection, confirmed["claim"]) == frozen
        assert (
            connection.scalar(
                text("""
            SELECT count(*) FROM public.followup_authorization_events
            WHERE organization_id=:tenant AND claim_id=:claim AND kind='EXTENSION'
        """),
                {"tenant": clinic.tenant, "claim": confirmed["claim"]},
            )
            == 0
        )
        assert (
            connection.scalar(
                text("SELECT clinical_revision FROM public.visits WHERE id=:visit"),
                {"visit": source["visit"]},
            )
            == 0
        )
        assert (
            connection.scalar(
                text("""
            SELECT count(*) FROM public.idempotency_receipts
            WHERE organization_id=:tenant AND operation='visits.extend_followup'
        """),
                {"tenant": clinic.tenant},
            )
            == 0
        )

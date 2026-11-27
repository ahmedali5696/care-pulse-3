"""T041: signed B-16 Visit lifecycle, evidence, and disjoint authority."""

import json
from dataclasses import replace
from datetime import UTC, datetime
from hashlib import sha256
from uuid import UUID, uuid4

import pytest
import test_followup_decisions as signed_visit_fixture
from carepulse.identity.authorization import protected_transaction
from carepulse.identity.contracts import ContractError
from runtime_identity_fixture import registered_test_request, signed_test_transaction
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from test_booking import booking_clinic as booking_clinic  # noqa: PLC0414
from test_booking import booking_request
from test_followup_decisions import _signed_visit_request
from test_followups import followup_body
from test_followups_races import followup_clinic as followup_clinic  # noqa: PLC0414
from test_patients import patient_backend as patient_backend  # noqa: PLC0414
from test_pricing import _fresh_receptionist_session, _owner_changes_manager
from test_request_boundary import boundary_data as boundary_data  # noqa: PLC0414
from test_schedule import CAIRO
from test_w3_admission import _admit, admission_effects, admit_body, waiting_case
from test_w3_checkin import queue_request
from test_w3_receipt_boundary import _foreign_appointment

CLINICAL = {
    "examination",
    "findings",
    "diagnosis",
    "medical_notes",
    "treatment",
    "tests",
    "recommendation",
}
PURPOSES = {
    "read": ("GET", "", "VISIT_READ"),
    "clinical": ("PATCH", "", "VISIT_CLINICAL"),
    "source": ("PATCH", "", "VISIT_FOLLOWUP"),
    "complete": ("POST", "/complete", "VISIT_COMPLETE"),
    "cancel": ("POST", "/cancel-exceptional", "VISIT_CANCEL_EXCEPTIONAL"),
}


def visit_request(
    clinic, actor, visit, operation, body=None, *, key=None, purpose=None
):
    method, suffix, default_purpose = PURPOSES[operation]
    return queue_request(
        clinic,
        actor,
        method=method,
        path=f"/v1/visits/{visit}{suffix}",
        purpose=purpose or default_purpose,
        body=body,
        key="" if method == "GET" else (key or str(uuid4())),
    )


def detail(clinic, visit, actor=None):
    status, result = visit_request(clinic, actor or clinic.owner, visit, "read")
    assert status == 200, result
    return result["data"]


def admitted(clinic):
    waiting = waiting_case(clinic)
    projection = _admit(
        clinic,
        clinic.owner,
        admit_body(clinic, waiting, physical_presence_confirmed=True),
    )
    return UUID(projection["visit_id"])


def terminal_body(projection, *, cancel=False):
    fields = ("appointment_revision", "visit_revision")
    if projection["claim_revision"] is not None:
        fields += (
            "claim_revision",
            "source_decision_revision",
            "followup_revision",
            "authorization_revision",
        )
    body = {
        f"expected_{field}": projection[field]
        for field in fields
        if projection[field] is not None
    }
    return {
        **body,
        **(
            {"reason": "Synthetic exceptional cancellation"}
            if cancel
            else {"examination_occurred": True}
        ),
    }


def effects(clinic, *, tenant=None):
    tenant = tenant or clinic.tenant
    # admission_effects includes number assignments and appointment_day_counters.
    snapshot = admission_effects(clinic, tenant=tenant)
    with clinic.migration.connect() as conn:
        for table in (
            "completion_attestations",
            "clinical_revisions",
            "clinical_audit",
            "followup_source_state",
            "followup_source_decision_events",
            "followup_authorization_events",
        ):
            snapshot[table] = list(
                conn.scalars(
                    text(
                        f"SELECT to_jsonb(r) FROM public.{table} r WHERE organization_id=:t ORDER BY to_jsonb(r)::text"
                    ),
                    {"t": tenant},
                )
            )
    return snapshot


def assert_denied(clinic, actor, visit, operation, body, expected, **kwargs):
    before = effects(clinic)
    status, result = visit_request(clinic, actor, visit, operation, body, **kwargs)
    assert status == expected, result
    assert set(result) == {"error"}
    assert effects(clinic) == before


@pytest.mark.parametrize("role", ["owner", "receptionist", "manager"])
def test_completion_attests_actual_actor_and_replays(booking_clinic, role):
    clinic = booking_clinic
    visit = admitted(clinic)
    actor = getattr(clinic, role, None)
    if role == "manager":
        _owner_changes_manager(clinic, True, 0)
        actor = _fresh_receptionist_session(clinic, 1)
    projection = detail(clinic, visit)
    body = terminal_body(projection)
    key = str(uuid4())
    before = effects(clinic)
    status, reply = visit_request(clinic, actor, visit, "complete", body, key=key)
    assert status == 200, reply
    after = effects(clinic)
    (attestation,) = after["completion_attestations"]
    assert (
        attestation["organization_id"],
        attestation["visit_id"],
        attestation["appointment_id"],
        attestation["actor_staff_id"],
        attestation["actor_role"],
        attestation["examination_occurred"],
    ) == (
        str(clinic.tenant),
        str(visit),
        projection["appointment_id"],
        actor.staff_id,
        actor.role,
        True,
    )
    assert datetime.fromisoformat(attestation["completed_at"]).microsecond == 0
    assert (
        reply["data"]["completion_attestation"]["completed_at"]
        == attestation["completed_at"]
    )
    assert after["visits"] == before["visits"]
    assert after["appointment_price_snapshots"] == before["appointment_price_snapshots"]
    assert (
        after["appointment_number_assignments"]
        == before["appointment_number_assignments"]
    )
    assert after["clinical_revisions"] == before["clinical_revisions"]
    for table in (
        "clinical_audit",
        "followup_source_decision_events",
        "appointment_day_counters",
    ):
        assert after[table] == before[table]
    assert detail(clinic, visit, actor)["status"] == "Completed"
    for table in (
        "appointment_history",
        "sensitive_audit",
        "outbox_events",
        "idempotency_receipts",
    ):
        assert len(after[table]) == len(before[table]) + 1, table
    assert visit_request(clinic, actor, visit, "complete", body, key=key) == (
        200,
        reply,
    )
    assert effects(clinic) == after
    assert_denied(clinic, actor, visit, "complete", body, 409)


@pytest.mark.parametrize("assertion", [None, False, 1, "true"])
def test_completion_requires_explicit_positive_json_boolean(booking_clinic, assertion):
    clinic = booking_clinic
    visit = admitted(clinic)
    body = terminal_body(detail(clinic, visit))
    if assertion is None:
        del body["examination_occurred"]
    else:
        body["examination_occurred"] = assertion
    assert_denied(clinic, clinic.receptionist, visit, "complete", body, 422)


@pytest.mark.parametrize("terminal", ["complete", "cancel"])
def test_doctor_clinical_projection_and_terminal_correction(booking_clinic, terminal):
    clinic = booking_clinic
    visit = admitted(clinic)
    body = {
        "expected_visit_revision": 0,
        "diagnosis": "Synthetic diagnosis",
        "recommendation": "Synthetic recommendation",
    }
    assert_denied(clinic, clinic.receptionist, visit, "clinical", body, 403)
    key = str(uuid4())
    status, reply = visit_request(
        clinic, clinic.owner, visit, "clinical", body, key=key
    )
    assert status == 200, reply
    assert visit_request(clinic, clinic.owner, visit, "clinical", body, key=key) == (
        200,
        reply,
    )
    assert CLINICAL <= detail(clinic, visit).keys()
    assert not CLINICAL & detail(clinic, visit, clinic.receptionist).keys()
    current = detail(clinic, visit)
    assert current["recommendation"] == body["recommendation"]
    command = terminal_body(current, cancel=terminal == "cancel")
    if terminal == "cancel":
        assert_denied(clinic, clinic.receptionist, visit, terminal, command, 403)
    status, reply = visit_request(clinic, clinic.owner, visit, terminal, command)
    assert status == 200, reply
    before = effects(clinic)
    status, reply = visit_request(
        clinic,
        clinic.owner,
        visit,
        "clinical",
        {"expected_visit_revision": 1, "diagnosis": "Corrected synthetic diagnosis"},
    )
    assert status == 200, reply
    after = effects(clinic)
    assert after["appointments"] == before["appointments"]
    assert after["appointment_price_snapshots"] == before["appointment_price_snapshots"]
    assert after["followup_claims"] == before["followup_claims"]
    assert after["followup_source_state"] == before["followup_source_state"]
    for table in (
        "followup_source_decision_events",
        "appointment_day_counters",
        "appointment_number_assignments",
    ):
        assert after[table] == before[table]
    (correction,) = [row for row in after["clinical_revisions"] if row["revision"] == 2]
    assert correction["old_values"]["diagnosis"] == body["diagnosis"]
    assert correction["new_values"]["diagnosis"] == "Corrected synthetic diagnosis"
    assert correction["new_values"]["recommendation"] == body["recommendation"]
    assert (correction["actor_staff_id"], correction["actor_role"]) == (
        clinic.owner.staff_id,
        "DOCTOR_OWNER",
    )
    assert datetime.fromisoformat(correction["occurred_at"]).microsecond == 0
    (audit,) = [
        row for row in after["clinical_audit"] if row not in before["clinical_audit"]
    ]
    assert (
        audit["correlation_id"],
        audit["doctor_staff_id"],
        audit["clinical_revision"],
        audit["occurred_at"],
    ) == (correction["id"], clinic.owner.staff_id, 2, correction["occurred_at"])
    assert_denied(clinic, clinic.owner, visit, "clinical", body, 409)
    assert not CLINICAL & detail(clinic, visit, clinic.receptionist).keys()


def test_family_mismatch_and_wrong_target_never_reach_writers(
    booking_clinic, monkeypatch
):
    clinic = booking_clinic
    visit = admitted(clinic)
    clinical = {"expected_visit_revision": 0, "medical_notes": "Synthetic narrative"}
    source = {"expected_visit_revision": 0, "followup_decision": {"kind": "DEFAULT"}}
    assert_denied(
        clinic, clinic.owner, visit, "clinical", clinical, 403, purpose="VISIT_FOLLOWUP"
    )
    assert_denied(
        clinic, clinic.owner, visit, "source", source, 403, purpose="VISIT_CLINICAL"
    )
    assert_denied(clinic, clinic.owner, visit, "clinical", {**clinical, **source}, 422)
    assert_denied(clinic, clinic.owner, uuid4(), "read", None, 404)
    assert_denied(clinic, clinic.owner, uuid4(), "clinical", clinical, 404)
    foreign_appointment = _foreign_appointment(clinic)
    with clinic.migration.connect() as conn:
        foreign_visit = conn.scalar(
            text(
                "SELECT id FROM public.visits WHERE appointment_id=:id AND organization_id=:tenant"
            ),
            {"id": foreign_appointment, "tenant": clinic.other},
        )
    assert foreign_visit is not None
    foreign_before = effects(clinic, tenant=clinic.other)
    assert_denied(clinic, clinic.owner, foreign_visit, "read", None, 404)
    assert_denied(clinic, clinic.owner, foreign_visit, "clinical", clinical, 404)
    before = effects(clinic)
    path = f"/v1/visits/{visit}"
    real_app = signed_visit_fixture.app
    for forged_header in (b"x-carepulse-signature", b"x-carepulse-db-proof"):

        async def tampered_request(scope, receive, send, forged_header=forged_header):
            scope["headers"] = [
                (name, b"0" * 64 if name == forged_header else value)
                for name, value in scope["headers"]
            ]
            await real_app(scope, receive, send)

        with monkeypatch.context() as patch:
            patch.setattr(signed_visit_fixture, "app", tampered_request)
            status, denied = _signed_visit_request(
                clinic,
                clinic.owner,
                path=path,
                method="PATCH",
                body=clinical,
                purpose="VISIT_CLINICAL",
                key=str(uuid4()),
            )
        assert status == 401 and denied["error"]["code"] == "UNAUTHENTICATED", denied
        assert effects(clinic) == before
    for purpose, raw, expected in (
        (
            "VISIT_CLINICAL",
            '{"expected_visit_revision":0,"diagnosis":"one","diagnosis":"two"}',
            422,
        ),
        (
            "VISIT_FOLLOWUP",
            '{"expected_visit_revision":0,"followup_decision":{"kind":"DEFAULT","kind":"OPTED_OUT"}}',
            422,
        ),
        (
            "VISIT_CLINICAL",
            '{"expected_visit_revision":0,"diagnosis":"one","followup_decision":{"kind":"DEFAULT"}}',
            422,
        ),
        ("VISIT_FOLLOWUP", '{"expected_visit_revision":0,"diagnosis":"one"}', 403),
        (
            "VISIT_CLINICAL",
            '{"expected_visit_revision":0,"followup_decision":{"kind":"DEFAULT"}}',
            403,
        ),
    ):
        status, response = _signed_visit_request(
            clinic,
            clinic.owner,
            path=path,
            method="PATCH",
            body=raw,
            purpose=purpose,
            key=str(uuid4()),
        )
        assert status == expected, response
        payload = raw.encode()
        identity = replace(
            clinic.owner,
            method="PATCH",
            path=path,
            query="",
            purpose=purpose,
            body_sha256=sha256(payload).hexdigest(),
            timestamp=str(int(datetime.now(UTC).timestamp() * 1000)),
            nonce=f"synthetic_{uuid4().hex}",
            idempotency_key=str(uuid4()),
        )
        # Directly signed proof must fail activation, before a writer can run.
        with registered_test_request(clinic, identity, payload):
            with (
                pytest.raises(ContractError) as denied,
                protected_transaction(
                    clinic.runtime, identity, clinic.config.policy.session_idle_minutes
                ),
            ):
                pytest.fail("Invalid signed family activated")
            assert denied.value.status_code == 403
    assert effects(clinic) == before
    key = str(uuid4())
    status, receipt = visit_request(
        clinic, clinic.owner, visit, "clinical", clinical, key=key
    )
    assert status == 200, receipt
    committed = effects(clinic)
    assert_denied(
        clinic, clinic.owner, foreign_visit, "clinical", clinical, 404, key=key
    )
    assert_denied(
        clinic,
        clinic.owner,
        visit,
        "clinical",
        {**clinical, "medical_notes": "Changed"},
        409,
        key=key,
    )
    assert_denied(clinic, clinic.owner, visit, "source", clinical, 403, key=key)
    with signed_test_transaction(
        clinic,
        clinic.owner,
        method="PATCH",
        path=path,
        purpose="VISIT_CLINICAL",
        body=clinical,
        key=key,
    ) as (conn, _):
        with pytest.raises(DBAPIError) as denied, conn.begin_nested():
            conn.scalar(text("SELECT public.f01_b16_replay()"))
        assert denied.value.orig.sqlstate == "42501"
    # Exercise the actual receipt recovery stage, including the aborted primary
    # transaction required by F01; no mocked recovery path.
    payload = json.dumps(clinical, separators=(",", ":")).encode()
    identity = replace(
        clinic.owner,
        method="PATCH",
        path=path,
        query="",
        purpose="VISIT_CLINICAL",
        body_sha256=sha256(payload).hexdigest(),
        timestamp=str(int(datetime.now(UTC).timestamp() * 1000)),
        nonce=f"synthetic_{uuid4().hex}",
        idempotency_key=key,
    )
    idle = clinic.config.policy.session_idle_minutes
    with registered_test_request(clinic, identity, payload):
        with (
            pytest.raises(RuntimeError, match="synthetic rollback"),
            protected_transaction(clinic.runtime, identity, idle),
        ):
            raise RuntimeError("synthetic rollback")
        with protected_transaction(
            clinic.runtime, identity, idle, stage="RECEIPT_RECOVERY"
        ) as (conn, _):
            saved = conn.scalar(text("SELECT public.f01_b16_replay()"))
            assert saved == {
                "projection": receipt["data"],
                "revision": receipt["revision"],
                "replayed": True,
            }
    assert effects(clinic) == committed
    with clinic.migration.begin() as conn:
        conn.execute(
            text('DELETE FROM public."session" WHERE id=:id'),
            {"id": clinic.owner.session_id},
        )
    assert_denied(clinic, clinic.owner, visit, "clinical", clinical, 401, key=key)
    assert effects(clinic, tenant=clinic.other) == foreign_before


def test_admin_completed_new_exam_qualifies_without_doctor_decision(followup_clinic):
    clinic = followup_clinic
    visit = admitted(clinic)
    original = detail(clinic, visit)
    status, result = visit_request(
        clinic, clinic.receptionist, visit, "complete", terminal_body(original)
    )
    assert status == 200, result
    after = effects(clinic)
    (saved_visit,) = after["visits"]
    assert saved_visit["followup_decision"] is None
    assert after["clinical_revisions"] == []
    (attestation,) = after["completion_attestations"]
    day = datetime.fromisoformat(attestation["completed_at"]).astimezone(CAIRO).date()
    source = {"patient": original["patient_id"], "visit": visit, "day": day}
    status, result = booking_request(
        clinic,
        clinic.receptionist,
        body=followup_body(source, offset=2, base="40.00"),
        key=str(uuid4()),
    )
    assert status == 201, result
    assert result["data"]["service_type"] == "FOLLOW_UP"
    (claim,) = effects(clinic)["followup_claims"]
    assert claim["source_visit_id"] == str(visit)
    assert claim["state"] == "ACTIVE"

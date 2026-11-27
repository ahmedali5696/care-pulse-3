"""B-16 real browser sessions, BFF signing and disjoint Visit authority."""

import json
from hashlib import sha256
from urllib.error import HTTPError
from urllib.request import Request, urlopen
from uuid import UUID, uuid4

from carepulse.db import transaction
from carepulse.identity.contracts import normalize_typed_body
from carepulse.identity.receipts import Receipt, insert_tenant_receipt
from carepulse.visits.eligibility_contracts import FollowupDecisionPatch
from sqlalchemy import create_engine, text
from test_patients import PATIENT, browser_request
from test_patients import invited_patients as invited_patients  # noqa: PLC0414
from test_pricing import _patch as pricing_patch
from test_sessions import _WEB, _database, _mutation_headers
from test_w3_bff import _denied


def _raw_patch(cookie, path, body, key, *, claimed_purpose=None):
    headers = {
        **_mutation_headers(),
        "Cookie": cookie,
        "Content-Type": "application/json",
        "Idempotency-Key": key,
    }
    if claimed_purpose:
        headers["X-CarePulse-Purpose"] = claimed_purpose
    try:
        response = urlopen(
            Request(_WEB + path, data=body, headers=headers, method="PATCH"), timeout=6
        )
    except HTTPError as denied:
        response = denied
    with response:
        return response.status, json.loads(response.read(8192))


def _proofs(tenant, key):
    with _database(bootstrap=True) as connection:
        return connection.execute(
            "SELECT purpose,body_sha256 FROM f01_private.requests WHERE organization_id=%s AND idempotency_key=%s ORDER BY id",
            (tenant, key),
        ).fetchall()


def _effects(tenant):
    with _database() as connection:
        return {
            table: connection.execute(
                f"SELECT to_jsonb(r) FROM public.{table} r WHERE organization_id=%s ORDER BY to_jsonb(r)::text",
                (tenant,),
            ).fetchall()
            for table in (
                "appointments",
                "visits",
                "clinical_revisions",
                "completion_attestations",
                "appointment_history",
                "followup_source_decision_events",
                "followup_claims",
                "idempotency_receipts",
                "sensitive_audit",
                "outbox_events",
            )
        }


def _admitted_visit(sessions):
    owner, staff = sessions.owner, sessions.receptionist
    status, pricing = browser_request(
        owner,
        "/api/settings/pricing",
        method="PATCH",
        body=pricing_patch(standard_new_exam_egp="100.00"),
        key=str(uuid4()),
    )
    assert status == 200, pricing
    status, patient = browser_request(
        staff, method="POST", body=PATIENT, key=str(uuid4())
    )
    assert status == 201, patient
    status, intake = browser_request(
        staff,
        "/api/intake/emergency",
        method="POST",
        body={
            "patient_id": patient["data"]["id"],
            "physical_presence_confirmed": True,
            "discount": {
                "discount_type": "NONE",
                "discount_value": "0",
                "discount_reason": None,
            },
            "expected_base_egp": "100.00",
            "expected_net_egp": "100.00",
            "expected_price_revision": pricing["revision"],
        },
        key=str(uuid4()),
    )
    assert status == 201, intake
    appointment = intake["data"]
    day = appointment["service_date"]
    status, queue = browser_request(owner, f"/api/queue?date={day}&lane=EMERGENCY")
    assert status == 200, queue
    status, mode = browser_request(owner, "/api/settings/admission")
    assert status == 200, mode
    (selected,) = queue["data"]["entries"]
    command = {
        "appointment_id": appointment["id"],
        "patient_id": patient["data"]["id"],
        "date": day,
        "lane": "EMERGENCY",
        "expected_appointment_revision": selected["appointment_revision"],
        "expected_queue_day_revision": queue["revision"],
        "expected_control_revision": queue["data"]["control_revision"],
        "expected_capacity_revision": queue["data"]["capacity_policy_version"],
        "expected_admission_revision": mode["revision"],
        "expected_queue_entry_revision": selected["queue_entry_revision"],
        "expected_head_appointment_id": selected["appointment_id"],
        "physical_presence_confirmed": True,
    }
    status, admitted = browser_request(
        owner, "/api/queue/admit", method="POST", body=command, key=str(uuid4())
    )
    assert status == 200, admitted
    assert admitted["data"]["status"] == "In Examination"
    return admitted["data"]


def _seed_legacy_receipt(tenant, actor, visit, key, body):
    command = FollowupDecisionPatch.model_validate(body)
    projection = {
        "visit_id": str(visit),
        "followup_decision": {"kind": "DEFAULT"},
        "revision": 0,
    }

    def migration_connection():
        connection = _database()
        connection.commit()
        return connection

    engine = create_engine("postgresql+psycopg://", creator=migration_connection)
    try:
        with transaction(engine, tenant) as connection:
            connection.execute(
                text("SELECT set_config('app.staff_id', :actor, true)"),
                {"actor": str(actor)},
            )
            insert_tenant_receipt(
                connection,
                Receipt(
                    actor_id=actor,
                    organization_id=tenant,
                    staff_id=actor,
                    operation="visits.followup_decision",
                    idempotency_key=key,
                    request_digest=sha256(
                        visit.bytes + normalize_typed_body(command)
                    ).hexdigest(),
                    result_projection=projection,
                    result_revision=0,
                ),
            )
    finally:
        engine.dispose()
    return {"data": projection, "revision": 0}


def test_browser_visit_patch_families_sign_original_bytes_and_preserve_source_replay(
    invited_patients,
):
    sessions = invited_patients
    owner, staff, foreign = sessions.owner, sessions.receptionist, sessions.other
    status, owner_identity = browser_request(owner, "/api/session")
    assert status == 200, owner_identity
    status, staff_identity = browser_request(staff, "/api/session")
    assert status == 200, staff_identity
    admitted = _admitted_visit(sessions)
    visit = UUID(admitted["visit_id"])
    path = f"/api/visits/{visit}"
    status, initial = browser_request(owner, path)
    assert status == 200, initial
    assert initial["data"]["status"] == "In Examination"
    clinical = {
        "expected_visit_revision": initial["revision"],
        "diagnosis": "Synthetic private diagnosis — تشخيص تجريبي",
        "recommendation": "Synthetic private recommendation",
    }
    raw = json.dumps(clinical, ensure_ascii=False, indent=2).encode()
    key = str(uuid4())
    before = _effects(sessions.clinic)
    for invalid in (
        b'{"expected_visit_revision":0,"diagnosis":"x","diagno\\u0073is":"y"}',
        b'{"expected_visit_revision":0,"followup_decision":{"kind":"DEFAULT","kind":"DEFAULT"}}',
        json.dumps({**clinical, "followup_decision": {"kind": "DEFAULT"}}).encode(),
        json.dumps({**clinical, "purpose": "VISIT_FOLLOWUP"}).encode(),
        b'{"expected_visit_revision":0.0,"diagnosis":"x"}',
        b'{"expected_visit_revision":0,"followup_decision":{"kind":"DEADLINE","deadline_date":"2026-02-30","reason":"Synthetic"}}',
    ):
        _denied(_raw_patch(owner, path, invalid, key), 422, "INVALID_INPUT")
        assert _proofs(sessions.clinic, key) == []
    _denied(
        _raw_patch(staff, path, raw, key, claimed_purpose="VISIT_FOLLOWUP"),
        403,
        "FORBIDDEN",
    )
    assert _proofs(sessions.clinic, key) == [
        ("VISIT_CLINICAL", sha256(raw).hexdigest())
    ]
    _denied(browser_request(foreign, path), 404, "NOT_FOUND")
    assert _effects(sessions.clinic) == before

    clinical_key = str(uuid4())
    status, written = _raw_patch(
        owner, path, raw, clinical_key, claimed_purpose="VISIT_FOLLOWUP"
    )
    assert status == 200, written
    assert written["revision"] == initial["revision"] + 1
    assert _proofs(sessions.clinic, clinical_key) == [
        ("VISIT_CLINICAL", sha256(raw).hexdigest())
    ]
    status, doctor_view = browser_request(owner, path)
    assert status == 200, doctor_view
    assert doctor_view["data"]["diagnosis"] == clinical["diagnosis"]
    assert doctor_view["data"]["recommendation"] == clinical["recommendation"]
    status, operational = browser_request(staff, path)
    assert status == 200, operational
    assert (
        not {
            "examination",
            "findings",
            "diagnosis",
            "medical_notes",
            "treatment",
            "tests",
            "recommendation",
        }
        & operational["data"].keys()
    )
    assert clinical["diagnosis"] not in json.dumps(operational, ensure_ascii=False)
    assert operational["data"]["visit_id"] == str(visit)

    terminal = {
        "examination_occurred": True,
        "expected_appointment_revision": operational["data"]["appointment_revision"],
        "expected_visit_revision": operational["revision"],
    }
    terminal_key = str(uuid4())
    _denied(
        browser_request(
            staff,
            path + "/complete",
            method="POST",
            body={**terminal, "examination_occurred": False},
            key=terminal_key,
        ),
        422,
        "INVALID_INPUT",
    )
    status, completed = browser_request(
        staff, path + "/complete", method="POST", body=terminal, key=terminal_key
    )
    assert status == 200, completed
    assert completed["data"]["status"] == "Completed"
    assert (
        completed["data"]["completion_attestation"]["actor_staff_id"]
        == staff_identity["data"]["id"]
    )
    assert completed["data"]["completion_attestation"]["actor_role"] == "RECEPTIONIST"

    # Legacy B-10 receipts are historical fixtures; all replay and new writes go through the BFF.
    legacy = {"followup_decision": {"kind": "DEFAULT"}, "expected_visit_revision": 0}
    legacy_key = str(uuid4())
    expected = _seed_legacy_receipt(
        sessions.clinic, UUID(owner_identity["data"]["id"]), visit, legacy_key, legacy
    )
    assert browser_request(
        owner, path, method="PATCH", body=legacy, key=legacy_key
    ) == (200, expected)
    _denied(
        browser_request(
            owner,
            path,
            method="PATCH",
            body={**legacy, "followup_decision": {"kind": "DEFAULT", "reason": None}},
            key=legacy_key,
        ),
        409,
        "IDEMPOTENCY_CONFLICT",
    )
    status, pricing = browser_request(owner, "/api/settings/pricing")
    assert status == 200, pricing
    source = {
        "followup_decision": {
            "kind": "OPTED_OUT",
            "reason": "Synthetic operational reason",
        },
        "expected_visit_revision": written["revision"],
        "expected_source_decision_revision": 0,
        "expected_followup_revision": pricing["data"]["followup_revision"],
    }
    source_raw = json.dumps(source, indent=2).encode()
    source_key = str(uuid4())
    status, decided = _raw_patch(
        staff, path, source_raw, source_key, claimed_purpose="VISIT_CLINICAL"
    )
    assert status == 200, decided
    assert decided["data"]["actor_role"] == "RECEPTIONIST"
    assert _proofs(sessions.clinic, source_key) == [
        ("VISIT_FOLLOWUP", sha256(source_raw).hexdigest())
    ]
    committed = _effects(sessions.clinic)
    assert _raw_patch(staff, path, source_raw, source_key) == (200, decided)
    assert _raw_patch(owner, path, raw, clinical_key) == (200, written)
    assert _effects(sessions.clinic) == committed

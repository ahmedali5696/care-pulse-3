"""B-05 private signed API and separately selected real invited-session acceptance."""

import asyncio
import json
import secrets
from dataclasses import replace
from datetime import UTC, datetime
from hashlib import sha256
from hmac import new
from types import SimpleNamespace
from urllib.parse import urlencode
from uuid import UUID, uuid4

import pytest
from carepulse.db import transaction
from carepulse.identity.contracts import ContractError
from carepulse.identity.signing import canonical_payload, canonical_query
from carepulse.main import app
from carepulse.patients.contracts import INTAKE_FIELDS, ReadPatient
from carepulse.patients.lifecycle import (
    lock_patient_for_existing_appointment,
    lock_patient_for_new_appointment,
)
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from test_request_boundary import _signed
from test_request_boundary import boundary_data as boundary_data  # noqa: PLC0414 - pytest fixture export

PATIENT = {
    "name_parts": ["Synthetic", "Family", "Patient"],
    "date_of_birth": "2000-02-29",
}


@pytest.fixture
def patient_backend(boundary_data):
    clinic = boundary_data
    clinic.owner = _signed(clinic)[0]
    staff, user, session = (
        uuid4(),
        f"synthetic-b05-{uuid4()}",
        f"synthetic-b05-{uuid4()}",
    )
    email = f"synthetic-b05-{uuid4()}@example.invalid"
    with clinic.migration.begin() as connection:
        connection.execute(
            text("""
            INSERT INTO public."user" (id, name, email, "emailVerified", "createdAt", "updatedAt")
            VALUES (:id, 'Synthetic Receptionist', :email, true, now(), now())
        """),
            {"id": user, "email": email},
        )
        connection.execute(
            text("""
            INSERT INTO public.staff_accounts (id, organization_id, auth_user_id, verified_email, role, enabled, verified)
            VALUES (:id, :tenant, :user, :email, 'RECEPTIONIST', true, true)
        """),
            {"id": staff, "tenant": clinic.tenant, "user": user, "email": email},
        )
        connection.execute(
            text("""
            INSERT INTO public."session" (id, "userId", token, "expiresAt", "createdAt", "updatedAt", "securityRevision", "absoluteDeadline", "lastActivity")
            VALUES (:id, :user, :token, now() + interval '1 hour', now(), now(), 0, now() + interval '1 hour', now())
        """),
            {"id": session, "user": user, "token": f"synthetic-{uuid4()}"},
        )
    clinic.receptionist = replace(
        clinic.owner, staff_id=str(staff), session_id=session, role="RECEPTIONIST"
    )
    app.state.service_config, app.state.db_engine = clinic.config, clinic.runtime
    return clinic


def private_request(
    clinic, actor, path="/v1/patients", *, method="GET", body=None, key="", query=None
):
    encoded = (
        json.dumps(body, separators=(",", ":")).encode() if body is not None else b""
    )
    identity = replace(
        actor,
        method=method,
        path=path,
        query=canonical_query(urlencode(query or {}, doseq=True)),
        body_sha256=sha256(encoded).hexdigest(),
        purpose="PATIENT_REGISTRY",
        timestamp=str(int(datetime.now(UTC).timestamp() * 1000)),
        nonce=f"synthetic_{uuid4().hex}",
        idempotency_key=key,
    )
    attributes = (
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
            f"x-carepulse-{name.replace('_', '-')}".encode(),
            str(getattr(identity, name)).encode(),
        )
        for name in attributes
    ]
    headers += [
        (b"content-type", b"application/json"),
        (b"x-carepulse-service-key", clinic.config.service_key.encode()),
        (
            b"x-carepulse-signature",
            new(clinic.config.service_key.encode(), canonical_payload(identity), sha256)
            .hexdigest()
            .encode(),
        ),
    ]
    scope = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "scheme": "http",
        "method": method,
        "path": path,
        "raw_path": path.encode(),
        "query_string": identity.query.encode(),
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


def create_patient(clinic, actor=None, **fields):
    status, response = private_request(
        clinic,
        actor or clinic.owner,
        method="POST",
        body={**PATIENT, **fields},
        key=str(uuid4()),
    )
    assert status == 201, response
    return response["data"]


def effects(clinic):
    with clinic.migration.connect() as connection:
        return tuple(
            connection.scalar(
                text(
                    f"SELECT count(*) FROM public.{table} WHERE organization_id = :tenant"
                ),
                {"tenant": clinic.tenant},
            )
            for table in (
                "patients",
                "sensitive_audit",
                "idempotency_receipts",
                "delivery_jobs",
                "outbox_events",
            )
        )


@pytest.mark.parametrize("role", ["owner", "receptionist"])
def test_both_roles_create_read_edit_all_intake_without_clinical_expansion(
    patient_backend, role
):
    clinic = patient_backend
    actor = getattr(clinic, role)
    age = (
        {"date_of_birth": None, "declared_age": "0", "age_reference_date": "2026-01-01"}
        if role == "receptionist"
        else {}
    )
    patient = create_patient(
        clinic, actor, **age, **{field: "Synthetic intake" for field in INTAKE_FIELDS}
    )
    path = f"/v1/patients/{patient['id']}"
    assert set(patient) == set(ReadPatient.model_fields)
    assert patient["phone_raw"] is None and patient["phone_normalized"] is None
    assert private_request(clinic, actor, path) == (
        200,
        {"data": patient, "revision": 0},
    )
    status, updated = private_request(
        clinic,
        actor,
        path,
        method="PATCH",
        body={
            "expected_demographic_revision": 0,
            **{field: None for field in INTAKE_FIELDS},
        },
    )
    assert status == 200 and updated["revision"] == 1
    assert all(updated["data"][field] is None for field in INTAKE_FIELDS)
    before = effects(clinic)
    assert (
        private_request(
            clinic,
            actor,
            path,
            method="PATCH",
            body={"expected_demographic_revision": 0, "phone_raw": "stale"},
        )[0]
        == 409
    )
    assert effects(clinic) == before


def test_family_pages_cursor_scope_and_independent_age_phone_edit(patient_backend):
    clinic = patient_backend
    family = [
        create_patient(
            clinic,
            phone_raw="010 1234 5678",
            name_parts=["Synthetic", "Family", "Patient", str(index)],
        )
        for index in range(3)
    ]
    query = {"phone": "+201012345678", "limit": 1}
    status, first = private_request(clinic, clinic.owner, query=query)
    assert status == 200
    cursor = first["data"]["next_cursor"]
    found = [first["data"]["items"][0]["id"]]
    while cursor:
        status, page = private_request(
            clinic, clinic.owner, query={**query, "cursor": cursor}
        )
        assert status == 200
        found.extend(patient["id"] for patient in page["data"]["items"])
        cursor = page["data"]["next_cursor"]
    assert found == sorted(patient["id"] for patient in family)
    cursor = first["data"]["next_cursor"]
    for actor, changed in (
        (clinic.receptionist, query),
        (clinic.owner, {**query, "phone": "different"}),
        (clinic.owner, {**query, "name": "changed"}),
    ):
        assert (
            private_request(clinic, actor, query={**changed, "cursor": cursor})[0]
            == 422
        )
    tampered = ("A" if cursor[0] != "A" else "B") + cursor[1:]
    assert (
        private_request(clinic, clinic.owner, query={**query, "cursor": tampered})[0]
        == 422
    )
    selected = family[0]
    status, edited = private_request(
        clinic,
        clinic.receptionist,
        f"/v1/patients/{selected['id']}",
        method="PATCH",
        body={
            "expected_demographic_revision": 0,
            "phone_raw": "+44 20 7946 0958",
            "date_of_birth": None,
            "declared_age": "26.5",
            "age_reference_date": "2026-01-01",
        },
    )
    assert status == 200
    assert (
        edited["data"]["date_of_birth"] is None
        and edited["data"]["declared_age"] == "26.5"
    )
    assert edited["data"]["phone_normalized"] == "+442079460958"
    assert all(
        private_request(clinic, clinic.owner, f"/v1/patients/{patient['id']}")[1][
            "data"
        ]["phone_raw"]
        == "010 1234 5678"
        for patient in family[1:]
    )
    assert (
        len(
            private_request(clinic, clinic.owner, query={"name": "family"})[1]["data"][
                "items"
            ]
        )
        == 3
    )
    assert (
        len(
            private_request(clinic, clinic.owner, query={"phone": "01012345678"})[1][
                "data"
            ]["items"]
        )
        == 2
    )
    assert (
        private_request(clinic, clinic.owner, query={"name": "%"})[1]["data"]["items"]
        == []
    )


@pytest.mark.parametrize(
    "forged",
    [
        {"organization_id": str(uuid4())},
        {"id": str(uuid4())},
        {"archived": True},
        {"phone_normalized": "+201012345678"},
        {"demographic_revision": 9},
        {"lifecycle_revision": 9},
        {"diagnosis": "forbidden"},
        {"visits": [{"diagnosis": "forbidden"}]},
        {"preliminary_medical_history": {"diagnosis": "forbidden"}},
        {"name_parts": [" ", "Two", "Three"]},
        {"name_parts": ["Two", "Parts"]},
        {"date_of_birth": None},
        {"date_of_birth": "9999-01-01"},
        {
            "date_of_birth": None,
            "declared_age": "-1",
            "age_reference_date": "2026-01-01",
        },
        {
            "date_of_birth": None,
            "declared_age": "NaN",
            "age_reference_date": "2026-01-01",
        },
    ],
)
def test_forged_clinical_or_invalid_demographics_have_no_partial_effect(
    patient_backend, forged
):
    clinic = patient_backend
    patient = create_patient(clinic)
    before = effects(clinic)
    assert (
        private_request(
            clinic,
            clinic.receptionist,
            method="POST",
            body={**PATIENT, **forged},
            key=str(uuid4()),
        )[0]
        == 422
    )
    assert (
        private_request(
            clinic,
            clinic.receptionist,
            f"/v1/patients/{patient['id']}",
            method="PATCH",
            body={"expected_demographic_revision": 0, **forged},
        )[0]
        == 422
    )
    assert effects(clinic) == before


def test_foreign_id_search_and_nested_query_are_non_oracular(patient_backend):
    clinic = patient_backend
    foreign = uuid4()
    with clinic.migration.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO public.patients (id, organization_id, name_parts, date_of_birth) VALUES (:id, :tenant, ARRAY['Foreign','Family','Patient'], DATE '2000-01-01')"
            ),
            {"id": foreign, "tenant": clinic.other},
        )
    for patient_id in (foreign, uuid4()):
        path = f"/v1/patients/{patient_id}"
        assert private_request(clinic, clinic.receptionist, path)[0] == 404
        assert (
            private_request(
                clinic,
                clinic.receptionist,
                path,
                method="PATCH",
                body={"expected_demographic_revision": 0, "phone_raw": "forbidden"},
            )[0]
            == 404
        )
    assert (
        private_request(clinic, clinic.owner, query={"name": "Foreign"})[1]["data"][
            "items"
        ]
        == []
    )
    for query in (
        {"organization_id": str(clinic.other)},
        {"include": "visits"},
        {"limit": 101},
        {"name": ["one", "two"]},
    ):
        assert private_request(clinic, clinic.owner, query=query)[0] == 422
    own = create_patient(clinic)
    assert (
        private_request(
            clinic,
            clinic.receptionist,
            f"/v1/patients/{own['id']}",
            query={"include": "visits"},
        )[0]
        == 422
    )


def test_create_receipt_conflict_and_revoked_retry_disclose_no_cached_patient(
    patient_backend,
):
    clinic = patient_backend
    key = str(uuid4())
    first = private_request(
        clinic, clinic.receptionist, method="POST", body=PATIENT, key=key
    )
    assert first[0] == 201
    before = effects(clinic)
    assert (
        private_request(
            clinic, clinic.receptionist, method="POST", body=PATIENT, key=key
        )
        == first
    )
    assert (
        private_request(
            clinic,
            clinic.receptionist,
            method="POST",
            body={**PATIENT, "phone_raw": "different"},
            key=key,
        )[0]
        == 409
    )
    with clinic.migration.begin() as connection:
        connection.execute(
            text(
                "UPDATE public.staff_accounts SET enabled=false, security_revision=security_revision+1 WHERE id=:id"
            ),
            {"id": clinic.receptionist.staff_id},
        )
    denied, response = private_request(
        clinic, clinic.receptionist, method="POST", body=PATIENT, key=key
    )
    assert denied in (401, 403) and "data" not in response
    assert effects(clinic) == before


def test_issue_12_owner_unarchive_restores_guard_and_retains_reference(patient_backend):
    clinic = patient_backend
    patient = create_patient(clinic)
    path = f"/v1/patients/{patient['id']}"
    marker = f"synthetic_b05_reference_{uuid4().hex}"
    with clinic.migration.begin() as connection:
        connection.execute(
            text(
                f"CREATE TABLE public.{marker} (organization_id uuid, patient_id uuid, FOREIGN KEY (organization_id, patient_id) REFERENCES public.patients(organization_id, id))"
            )
        )
        connection.execute(
            text(f"INSERT INTO public.{marker} VALUES (:tenant, :patient)"),
            {"tenant": clinic.tenant, "patient": patient["id"]},
        )
    try:
        assert (
            private_request(
                clinic,
                clinic.owner,
                path + "/archive",
                method="POST",
                body={"reason": "Synthetic archive", "expected_lifecycle_revision": 0},
            )[0]
            == 200
        )
        before = effects(clinic)
        assert private_request(clinic, clinic.owner)[1]["data"]["items"] == []
        assert (
            private_request(clinic, clinic.owner, query={"archived": "true"})[1][
                "data"
            ]["items"][0]["id"]
            == patient["id"]
        )
        assert (
            private_request(clinic, clinic.receptionist, query={"archived": "true"})[0]
            == 403
        )
        assert private_request(clinic, clinic.receptionist, path)[0] == 404
        assert (
            private_request(
                clinic,
                clinic.receptionist,
                path,
                method="PATCH",
                body={"expected_demographic_revision": 0, "phone_raw": "denied"},
            )[0]
            == 404
        )
        for operation in ("archive", "unarchive"):
            assert (
                private_request(
                    clinic,
                    clinic.receptionist,
                    path + "/" + operation,
                    method="POST",
                    body={"reason": "denied", "expected_lifecycle_revision": 1},
                )[0]
                == 403
            )
        assert (
            private_request(
                clinic,
                clinic.owner,
                path + "/unarchive",
                method="POST",
                body={"reason": "stale", "expected_lifecycle_revision": 0},
            )[0]
            == 409
        )
        assert effects(clinic) == before
        with transaction(clinic.runtime, clinic.tenant) as connection:
            with pytest.raises(ContractError) as archived:
                lock_patient_for_new_appointment(
                    connection, clinic.tenant, UUID(patient["id"])
                )
            assert archived.value.status_code == 409
            assert lock_patient_for_existing_appointment(
                connection, clinic.tenant, UUID(patient["id"])
            ).archived
        status, updated = private_request(
            clinic,
            clinic.owner,
            path,
            method="PATCH",
            body={
                "expected_demographic_revision": 0,
                "phone_raw": "Owner archived edit",
                "drug_allergies": "Synthetic change",
            },
        )
        assert (
            status == 200
            and updated["data"]["archived"]
            and updated["data"]["demographic_revision"] == 1
        )
        status, restored = private_request(
            clinic,
            clinic.owner,
            path + "/unarchive",
            method="POST",
            body={"reason": "Synthetic restoration", "expected_lifecycle_revision": 1},
        )
        assert (
            status == 200
            and restored["data"]["lifecycle_revision"] == 2
            and not restored["data"]["archived"]
        )
        with transaction(clinic.runtime, clinic.tenant) as connection:
            assert not lock_patient_for_new_appointment(
                connection, clinic.tenant, UUID(patient["id"])
            ).archived
            assert (
                connection.scalar(
                    text("SELECT archived_at FROM public.patients WHERE id=:id"),
                    {"id": patient["id"]},
                )
                is None
            )
        with clinic.migration.begin() as connection:
            assert connection.scalar(text(f"SELECT count(*) FROM public.{marker}")) == 1
            audit = (
                connection.execute(
                    text(
                        "SELECT action, reason, before_state, after_state FROM public.sensitive_audit WHERE organization_id=:tenant ORDER BY occurred_at, id"
                    ),
                    {"tenant": clinic.tenant},
                )
                .mappings()
                .all()
            )
            assert {row["action"] for row in audit} == {
                "patients.create",
                "patients.archive",
                "patients.update",
                "patients.unarchive",
            }
            restored_audit = next(
                row for row in audit if row["action"] == "patients.unarchive"
            )
            assert restored_audit["reason"] == "Synthetic restoration"
            assert (
                restored_audit["before_state"]["lifecycle_revision"] == 1
                and restored_audit["after_state"]["lifecycle_revision"] == 2
            )
            with pytest.raises(DBAPIError), connection.begin_nested():
                connection.execute(
                    text(
                        "UPDATE public.sensitive_audit SET reason='forged' WHERE organization_id=:tenant"
                    ),
                    {"tenant": clinic.tenant},
                )
    finally:
        with clinic.migration.begin() as connection:
            connection.execute(text(f"DROP TABLE public.{marker}"))


def browser_request(cookie, path="/api/patients", *, method="GET", body=None, key=None):
    from test_sessions import _mutation_headers, _request

    headers = _mutation_headers()
    if key:
        headers["Idempotency-Key"] = key
    status, _, raw = _request(
        path, method=method, payload=body, cookie=cookie, headers=headers
    )
    return status, json.loads(raw)


@pytest.fixture
def invited_patients(disposable_ci):
    from test_sessions import _database, _login, _request

    def invite_owner():
        operator, clinic, invitation = uuid4(), uuid4(), uuid4()
        email, password = f"{uuid4()}@example.invalid", f"Synthetic-B05-{uuid4()}!"
        proof = secrets.token_urlsafe(32)
        with _database() as connection:
            connection.execute(
                "DELETE FROM public.auth_login_attempts WHERE dimension='source'"
            )
            connection.execute(
                "INSERT INTO public.operator_identities (id, credential_digest, issued_by_reference, issued_at) VALUES (%s,%s,'synthetic-b05',now())",
                (operator, f"synthetic:{operator}"),
            )
            connection.execute(
                "INSERT INTO public.organizations (id, branch, created_by_operator_id) VALUES (%s,'Synthetic B-05 invited clinic',%s)",
                (clinic, operator),
            )
            connection.execute(
                "INSERT INTO public.invitations (id,organization_id,purpose,target_email,proof_digest,issued_by_operator_id,expires_at) VALUES (%s,%s,'FIRST_OWNER',%s,%s,%s,now()+interval '20 minutes')",
                (
                    invitation,
                    clinic,
                    email,
                    sha256(proof.encode()).hexdigest(),
                    operator,
                ),
            )
        status, accepted = browser_request(
            None,
            "/api/auth/invite-accept",
            method="POST",
            body={
                "organization_id": str(clinic),
                "invitation_id": str(invitation),
                "proof": proof,
                "name": "Synthetic B05 Owner",
                "password": password,
            },
        )
        assert status == 200 and accepted["data"]["state"] == "ACTIVE"
        cookie = _login(SimpleNamespace(email=email, password=password))
        status, _, raw = _request("/api/session", cookie=cookie)
        assert status == 200
        return SimpleNamespace(
            clinic=clinic, cookie=cookie, staff=json.loads(raw)["data"]["id"]
        )

    owner, other = invite_owner(), invite_owner()
    address, password = (
        f"{uuid4()}@example.invalid",
        f"Synthetic-B05-Reception-{uuid4()}!",
    )
    status, created = browser_request(
        owner.cookie,
        "/api/staff/receptionists",
        method="POST",
        body={
            "email": address,
            "name": "Synthetic B05 Receptionist",
            "reason": "Synthetic hire",
        },
        key=str(uuid4()),
    )
    assert status == 201
    invitation, proof = uuid4(), secrets.token_urlsafe(32)
    with _database() as connection:
        connection.execute(
            "INSERT INTO public.invitations (id,organization_id,staff_id,purpose,target_email,proof_digest,issued_by_staff_id,expires_at) VALUES (%s,%s,%s,'RECEPTIONIST',%s,%s,%s,now()+interval '20 minutes')",
            (
                invitation,
                owner.clinic,
                created["data"]["id"],
                address,
                sha256(proof.encode()).hexdigest(),
                owner.staff,
            ),
        )
    status, accepted = browser_request(
        None,
        "/api/auth/invite-accept",
        method="POST",
        body={
            "organization_id": str(owner.clinic),
            "invitation_id": str(invitation),
            "proof": proof,
            "name": "Synthetic B05 Receptionist",
            "password": password,
        },
    )
    assert status == 200 and accepted["data"]["verified"]
    receptionist = _login(SimpleNamespace(email=address, password=password))
    return SimpleNamespace(
        owner=owner.cookie,
        receptionist=receptionist,
        other=other.cookie,
        clinic=owner.clinic,
    )


@pytest.mark.b05_browser
@pytest.mark.parametrize("role", ["owner", "receptionist"])
def test_invited_roles_family_intake_archived_visibility_and_foreign_cursor(
    invited_patients, role
):
    sessions = invited_patients
    cookie = getattr(sessions, role)
    fields = {
        **PATIENT,
        "phone_raw": "01012345678",
        **{field: "Synthetic intake" for field in INTAKE_FIELDS},
    }
    key = str(uuid4())
    status, first = browser_request(cookie, method="POST", body=fields, key=key)
    assert status == 201
    assert set(first["data"]) == set(ReadPatient.model_fields)
    assert browser_request(cookie, method="POST", body=fields, key=key) == (
        status,
        first,
    )
    assert (
        browser_request(
            cookie, method="POST", body={**fields, "phone_raw": "changed"}, key=key
        )[0]
        == 409
    )
    status, second = browser_request(
        cookie, method="POST", body=fields, key=str(uuid4())
    )
    assert status == 201 and first["data"]["id"] != second["data"]["id"]
    status, page = browser_request(cookie, "/api/patients?phone=01012345678&limit=1")
    assert status == 200 and len(page["data"]["items"]) == 1
    cursor = page["data"]["next_cursor"]
    status, next_page = browser_request(
        cookie,
        "/api/patients?"
        + urlencode({"phone": "01012345678", "limit": 1, "cursor": cursor}),
    )
    assert status == 200
    assert {page["data"]["items"][0]["id"], next_page["data"]["items"][0]["id"]} == {
        first["data"]["id"],
        second["data"]["id"],
    }
    assert (
        browser_request(
            sessions.other,
            "/api/patients?"
            + urlencode({"phone": "01012345678", "limit": 1, "cursor": cursor}),
        )[0]
        == 422
    )
    path = f"/api/patients/{first['data']['id']}"
    assert browser_request(sessions.other, path)[0] == 404
    status, updated = browser_request(
        cookie,
        path,
        method="PATCH",
        body={
            "expected_demographic_revision": 0,
            "phone_raw": None,
            "date_of_birth": None,
            "declared_age": "0",
            "age_reference_date": "2026-01-01",
            **{field: "Updated intake" for field in INTAKE_FIELDS},
        },
    )
    assert status == 200 and updated["data"]["date_of_birth"] is None
    assert all(updated["data"][field] == "Updated intake" for field in INTAKE_FIELDS)
    assert (
        browser_request(cookie, f"/api/patients/{second['data']['id']}")[1]["data"][
            "phone_raw"
        ]
        == fields["phone_raw"]
    )
    for forbidden in (
        {"diagnosis": "hidden"},
        {"visits": [{"diagnosis": "hidden"}]},
        {"archived": True},
        {"organization_id": str(uuid4())},
    ):
        assert (
            browser_request(
                cookie,
                path,
                method="PATCH",
                body={"expected_demographic_revision": 1, **forbidden},
            )[0]
            == 422
        )
    assert browser_request(cookie, path + "?include=visits")[0] == 422
    if role == "receptionist":
        assert (
            browser_request(
                cookie,
                path + "/archive",
                method="POST",
                body={"expected_lifecycle_revision": 0, "reason": "Denied"},
            )[0]
            == 403
        )
    assert (
        browser_request(
            sessions.owner,
            path + "/archive",
            method="POST",
            body={"expected_lifecycle_revision": 0, "reason": "Synthetic archive"},
        )[0]
        == 200
    )
    assert first["data"]["id"] not in {
        patient["id"] for patient in browser_request(cookie)[1]["data"]["items"]
    }
    status, archived_listing = browser_request(cookie, "/api/patients?archived=true")
    assert status == (200 if role == "owner" else 403)
    if role == "owner":
        assert first["data"]["id"] in {
            patient["id"] for patient in archived_listing["data"]["items"]
        }
    expected = 200 if role == "owner" else 404
    assert browser_request(cookie, path)[0] == expected
    status, archived_edit = browser_request(
        cookie,
        path,
        method="PATCH",
        body={"expected_demographic_revision": 1, "drug_allergies": "Archived intake"},
    )
    assert status == expected
    if role == "owner":
        assert (
            archived_edit["data"]["archived"]
            and archived_edit["data"]["demographic_revision"] == 2
        )
    else:
        assert browser_request(cookie, method="POST", body=fields, key=key)[0] == 404
        assert (
            browser_request(
                cookie,
                path + "/unarchive",
                method="POST",
                body={"expected_lifecycle_revision": 1, "reason": "Denied"},
            )[0]
            == 403
        )
    assert (
        browser_request(
            sessions.owner,
            path + "/unarchive",
            method="POST",
            body={"expected_lifecycle_revision": 1, "reason": "Synthetic unarchive"},
        )[0]
        == 200
    )
    assert browser_request(sessions.receptionist, path)[0] == 200

    from test_sessions import _database

    with _database() as connection:
        before = tuple(
            connection.execute(
                f"SELECT count(*) FROM public.{table} WHERE organization_id=%s",
                (sessions.clinic,),
            ).fetchone()[0]
            for table in ("patients", "sensitive_audit", "idempotency_receipts")
        )
    assert browser_request(cookie, "/api/auth/logout", method="POST")[0] == 200
    status, revoked = browser_request(cookie, method="POST", body=fields, key=key)
    assert status == 401 and "data" not in revoked
    with _database() as connection:
        after = tuple(
            connection.execute(
                f"SELECT count(*) FROM public.{table} WHERE organization_id=%s",
                (sessions.clinic,),
            ).fetchone()[0]
            for table in ("patients", "sensitive_audit", "idempotency_receipts")
        )
    assert after == before

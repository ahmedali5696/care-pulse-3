"""B-04 Receptionist management through real invited Owner and BFF sessions."""

import json
import secrets
from hashlib import sha256
from types import SimpleNamespace
from uuid import uuid4

import pytest
from test_sessions import (
    _database,
    _login,
    _mutation_headers,
    _password_hash,
    _request,
    _seed_owner,
)


@pytest.fixture
def owner_session(disposable_ci):
    with _database() as connection:
        connection.execute(
            "DELETE FROM public.auth_login_attempts WHERE dimension='source'"
        )
        owner = _seed_owner(connection)
    return owner, _login(owner)


def _staff_command(path, cookie, fields=None, *, method="POST", key=None):
    headers = _mutation_headers()
    if key:
        headers["Idempotency-Key"] = key
    status, _, body = _request(
        path,
        method=method,
        payload=fields,
        cookie=cookie,
        headers=headers,
    )
    return status, json.loads(body)


def _changes(staff, reason="Synthetic staff management"):
    return {
        "reason": reason,
        "expected_revision": staff["revision"],
        "expected_security_revision": staff["security_revision"],
    }


def test_owner_creates_invites_and_revokes_without_partial_sender_job(owner_session):
    owner, cookie = owner_session
    email = f"synthetic-staff-{uuid4()}@example.invalid"
    key = str(uuid4())
    create = {"email": email, "name": "Synthetic Receptionist", "reason": "Hire"}
    status, created = _staff_command(
        "/api/staff/receptionists", cookie, create, key=key
    )
    assert status == 201, created
    staff = created["data"]
    assert staff["email"] == email
    assert staff["enabled"] is True
    assert staff["verified"] is False
    assert staff["bulk_reschedule"] is False
    assert staff["revision"] == 0
    assert staff["security_revision"] == 0

    status, replay = _staff_command("/api/staff/receptionists", cookie, create, key=key)
    assert status == 201
    assert replay["data"] == staff
    status, mismatch = _staff_command(
        "/api/staff/receptionists",
        cookie,
        {**create, "reason": "Different"},
        key=key,
    )
    assert status == 409, mismatch

    staff_path = f"/api/staff/receptionists/{staff['id']}"
    status, listing = _staff_command("/api/staff/receptionists", cookie, method="GET")
    assert status == 200
    assert any(entry["id"] == staff["id"] for entry in listing["data"]["items"])
    status, viewed = _staff_command(staff_path, cookie, method="GET")
    assert status == 200
    assert viewed["data"] == staff

    status, issued = _staff_command(
        f"{staff_path}/invite",
        cookie,
        _changes(staff),
    )
    assert status == 200, issued
    invited = issued["data"]
    assert invited["invitation_state"] == "ISSUED"
    assert invited["revision"] == staff["revision"] + 1
    with _database() as connection:
        delivery = connection.execute(
            """
            SELECT j.id FROM public.delivery_jobs j
            JOIN public.protected_delivery_material m ON m.job_id = j.id
            JOIN public.invitations i ON i.id = j.invitation_id
            WHERE j.organization_id = %s AND j.staff_id = %s
              AND i.state = 'ISSUED' AND j.purpose = 'RECEPTIONIST'
        """,
            (owner.clinic, staff["id"]),
        ).fetchall()
    assert len(delivery) == 1
    status, denied = _staff_command(
        f"{staff_path}/invite",
        cookie,
        _changes(staff),
    )
    assert status == 409, denied

    status, revoked = _staff_command(
        f"{staff_path}/revoke-invite",
        cookie,
        _changes(invited),
    )
    assert status == 200, revoked
    assert revoked["data"]["invitation_state"] == "REVOKED"
    with _database() as connection:
        assert (
            connection.execute(
                """
            SELECT count(*) FROM public.invitations
             WHERE staff_id = %s AND state = 'ISSUED'
        """,
                (staff["id"],),
            ).fetchone()[0]
            == 0
        )
        assert (
            connection.execute(
                """
            SELECT count(*) FROM public.delivery_jobs
             WHERE organization_id = %s AND staff_id = %s
        """,
                (owner.clinic, staff["id"]),
            ).fetchone()[0]
            == 1
        )


def test_owner_grant_and_disable_immediately_deny_two_old_sessions(owner_session):
    owner, owner_cookie = owner_session
    staff_id = uuid4()
    native_user_id = f"synthetic-b04-{uuid4()}"
    email = f"synthetic-b04-{uuid4()}@example.invalid"
    password = f"Synthetic-B04-{uuid4()}!"
    with _database() as connection:
        connection.execute(
            """
            INSERT INTO public."user"
                (id, name, email, "emailVerified", "createdAt", "updatedAt")
            VALUES (%s, 'Synthetic Receptionist', %s, true, clock_timestamp(), clock_timestamp())
        """,
            (native_user_id, email),
        )
        connection.execute(
            """
            INSERT INTO public."account"
                (id, "accountId", "providerId", "userId", password, "createdAt", "updatedAt")
            VALUES (%s, %s, 'credential', %s, %s, clock_timestamp(), clock_timestamp())
        """,
            (
                f"synthetic-b04-account-{uuid4()}",
                native_user_id,
                native_user_id,
                _password_hash(password),
            ),
        )
        connection.execute(
            """
            INSERT INTO public.staff_accounts
                (id, organization_id, auth_user_id, verified_email, role, enabled, verified)
            VALUES (%s, %s, %s, %s, 'RECEPTIONIST', true, true)
        """,
            (staff_id, owner.clinic, native_user_id, email),
        )
    recipient = SimpleNamespace(email=email, password=password, staff=staff_id)
    first_cookie = _login(recipient)
    second_cookie = _login(recipient)
    staff_path = f"/api/staff/receptionists/{staff_id}"

    status, restricted = _staff_command(
        f"{staff_path}/permissions",
        first_cookie,
        {
            "reason": "Self escalation",
            "expected_revision": 0,
            "expected_security_revision": 0,
            "bulk_reschedule": True,
        },
        method="PATCH",
    )
    assert status == 403, restricted

    status, granted = _staff_command(
        f"{staff_path}/permissions",
        owner_cookie,
        {
            "reason": "Authorize bulk reschedule",
            "expected_revision": 0,
            "expected_security_revision": 0,
            "bulk_reschedule": True,
        },
        method="PATCH",
    )
    assert status == 200, granted
    assert granted["data"]["bulk_reschedule"] is True
    assert granted["data"]["security_revision"] == 1
    for old_cookie in (first_cookie, second_cookie):
        status, _, _ = _request("/api/session", cookie=old_cookie)
        assert status == 401

    refreshed_cookie = _login(recipient)
    status, withdrawn = _staff_command(
        f"{staff_path}/permissions",
        owner_cookie,
        {**_changes(granted["data"]), "bulk_reschedule": False},
        method="PATCH",
    )
    assert status == 200, withdrawn
    assert withdrawn["data"]["bulk_reschedule"] is False
    status, _, _ = _request("/api/session", cookie=refreshed_cookie)
    assert status == 401

    status, disabled = _staff_command(
        f"{staff_path}/disable",
        owner_cookie,
        _changes(withdrawn["data"]),
    )
    assert status == 200, disabled
    assert disabled["data"]["enabled"] is False
    status, enabled = _staff_command(
        f"{staff_path}/enable",
        owner_cookie,
        _changes(disabled["data"]),
    )
    assert status == 200, enabled
    assert enabled["data"]["enabled"] is True
    assert enabled["data"]["security_revision"] > disabled["data"]["security_revision"]
    status, _, _ = _request("/api/session", cookie=refreshed_cookie)
    assert status == 401
    with _database() as connection:
        # Synthetic test source shares its request IP across unrelated users.
        connection.execute(
            "DELETE FROM public.auth_login_attempts WHERE dimension='source'"
        )
    assert _login(recipient)


def test_foreign_owner_cannot_discover_staff_or_delegate_receptionist(owner_session):
    owner, cookie = owner_session
    with _database() as connection:
        foreign = _seed_owner(connection)
        staff_id = uuid4()
        user_id = f"synthetic-foreign-{uuid4()}"
        email = f"synthetic-foreign-{uuid4()}@example.invalid"
        connection.execute(
            """
            INSERT INTO public."user"
                (id, name, email, "emailVerified", "createdAt", "updatedAt")
            VALUES (%s, 'Foreign staff', %s, false, clock_timestamp(), clock_timestamp())
        """,
            (user_id, email),
        )
        connection.execute(
            """
            INSERT INTO public.staff_accounts
                (id, organization_id, auth_user_id, verified_email, role, enabled, verified)
            VALUES (%s, %s, %s, %s, 'RECEPTIONIST', true, false)
        """,
            (staff_id, foreign.clinic, user_id, email),
        )
    assert owner.clinic != foreign.clinic
    path = f"/api/staff/receptionists/{staff_id}"
    status, _ = _staff_command(path, cookie, method="GET")
    assert status == 404
    status, _ = _staff_command(
        f"{path}/permissions",
        cookie,
        {
            "reason": "Cross clinic",
            "expected_revision": 0,
            "expected_security_revision": 0,
            "bulk_reschedule": True,
        },
        method="PATCH",
    )
    assert status == 404
    status, _ = _staff_command(
        "/api/staff/receptionists",
        cookie,
        {"email": email, "name": "Foreign staff", "reason": "Duplicate link"},
        key=str(uuid4()),
    )
    assert status == 409


def test_receptionist_invite_acceptance_single_use_with_real_bff(owner_session):
    owner, owner_cookie = owner_session
    email = f"synthetic-accept-{uuid4()}@example.invalid"
    new_staff = {
        "email": email,
        "name": "Synthetic invited Receptionist",
        "reason": "Receptionist invited",
    }
    status, created = _staff_command(
        "/api/staff/receptionists", owner_cookie, new_staff, key=str(uuid4())
    )
    assert status == 201, created
    staff = created["data"]
    proof = secrets.token_urlsafe(32)
    invitation = uuid4()
    with _database() as connection:
        connection.execute(
            """
            INSERT INTO public.invitations
                (id, organization_id, staff_id, purpose, target_email,
                 proof_digest, expires_at, issued_by_staff_id)
            VALUES (%s, %s, %s, 'RECEPTIONIST', %s,
                    %s, clock_timestamp() + interval '20 minutes', %s)
        """,
            (
                invitation,
                owner.clinic,
                staff["id"],
                email,
                sha256(proof.encode()).hexdigest(),
                owner.staff,
            ),
        )
        lifecycle = connection.execute(
            "SELECT lifecycle_revision FROM public.organizations WHERE id = %s",
            (owner.clinic,),
        ).fetchone()[0]
    acceptance = {
        "organization_id": str(owner.clinic),
        "invitation_id": str(invitation),
        "proof": proof,
        "name": new_staff["name"],
        "password": f"Synthetic-Accept-{uuid4()}!",
    }
    status, denied = _staff_command(
        "/api/auth/invite-accept",
        None,
        {**acceptance, "proof": secrets.token_urlsafe(32)},
    )
    assert status == 403, denied
    status, accepted = _staff_command("/api/auth/invite-accept", None, acceptance)
    assert status == 200, accepted
    assert accepted["data"]["id"] == staff["id"]
    assert accepted["data"]["enabled"] is True
    assert accepted["data"]["verified"] is True
    assert accepted["data"]["bulk_reschedule"] is False
    assert accepted["data"]["security_revision"] == 1
    status, denied = _staff_command("/api/auth/invite-accept", None, acceptance)
    assert status == 403, denied
    with _database() as connection:
        row = connection.execute(
            """
            SELECT i.state, i.accepted_by_user_id, s.auth_user_id,
                   s.verified, s.bulk_reschedule, o.lifecycle_revision
            FROM public.invitations i
            JOIN public.staff_accounts s ON s.id = i.staff_id
            JOIN public.organizations o ON o.id = i.organization_id
            WHERE i.id = %s
        """,
            (invitation,),
        ).fetchone()
    assert row[0] == "ACCEPTED"
    assert row[1] == row[2]
    assert row[3] is True and row[4] is False
    assert row[5] == lifecycle
    assert _login(SimpleNamespace(email=email, password=acceptance["password"]))

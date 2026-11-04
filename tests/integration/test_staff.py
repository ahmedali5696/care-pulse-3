"""B-04 Receptionist management through real invited Owner and BFF sessions."""

import json
import secrets
import time
from concurrent.futures import ThreadPoolExecutor
from hashlib import sha256
from threading import Barrier
from types import SimpleNamespace
from uuid import uuid4

import pytest
from carepulse.db import transaction
from carepulse.identity.authorization import AuthorizedStaff
from carepulse.identity.staff import list_receptionists
from sqlalchemy import event
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


def test_receptionist_listing_paginates_full_projection_in_one_query(
    owner_session, role_engine
):
    owner, cookie = owner_session
    expected = {}
    with _database() as connection:
        for index in range(101):
            staff_id = uuid4()
            user_id = f"synthetic-list-{uuid4()}"
            email = f"synthetic-list-{uuid4()}@example.invalid"
            enabled, verified, bulk = index % 2 == 0, index % 3 == 0, index % 5 == 0
            connection.execute(
                'INSERT INTO public."user" '
                '(id, name, email, "emailVerified", "createdAt", "updatedAt") '
                "VALUES (%s, %s, %s, true, clock_timestamp(), clock_timestamp())",
                (user_id, "Synthetic Receptionist", email),
            )
            connection.execute(
                """INSERT INTO public.staff_accounts
                       (id, organization_id, auth_user_id, verified_email, role,
                        enabled, verified, bulk_reschedule)
                     VALUES (%s, %s, %s, %s, 'RECEPTIONIST', %s, %s, %s)""",
                (staff_id, owner.clinic, user_id, email, enabled, verified, bulk),
            )
            expected[str(staff_id)] = {
                "id": str(staff_id),
                "email": email,
                "enabled": enabled,
                "verified": verified,
                "operational_manager": False,
                "revision": 0,
                "security_revision": 0,
                "invitation_state": None,
            }
        first_staff = min(expected)
        for days_ago in (2, 1):
            invitation_id = uuid4()
            connection.execute(
                """INSERT INTO public.invitations
                       (id, organization_id, staff_id, purpose, target_email,
                        proof_digest, issued_at, expires_at, issued_by_staff_id)
                     VALUES (%s, %s, %s, 'RECEPTIONIST', %s, %s,
                             clock_timestamp() - (%s * interval '1 day'),
                             clock_timestamp() + interval '1 day', %s)""",
                (
                    invitation_id,
                    owner.clinic,
                    first_staff,
                    expected[first_staff]["email"],
                    sha256(str(uuid4()).encode()).hexdigest(),
                    days_ago,
                    owner.staff,
                ),
            )
            if days_ago == 2:
                connection.execute(
                    "UPDATE public.invitations SET state = 'REVOKED' WHERE id = %s",
                    (invitation_id,),
                )
        expected[first_staff]["invitation_state"] = "ISSUED"

    page_ids = []
    after = None
    page_sizes = (25, 25, 25, 25, 1)
    for page_index, expected_count in enumerate(page_sizes):
        path = "/api/staff/receptionists?limit=25"
        if after:
            path += f"&after={after}"
        status, page = _staff_command(path, cookie, method="GET")
        assert status == 200, page
        items = page["data"]["items"]
        assert len(items) == expected_count
        assert all(item == expected[item["id"]] for item in items)
        page_ids.extend(item["id"] for item in items)
        after = page["data"]["next_cursor"]
        assert after == (items[-1]["id"] if page_index < len(page_sizes) - 1 else None)
    assert page_ids == sorted(expected)

    engine = role_engine("carepulse_runtime")
    statements = []
    event.listen(engine, "before_cursor_execute", lambda *args: statements.append(1))
    try:
        actor = AuthorizedStaff(
            owner.clinic, owner.staff, "DOCTOR_OWNER", False, 0, 0, 0
        )
        with transaction(engine, owner.clinic) as connection:
            for limit in (1, 100):
                statements.clear()
                result = list_receptionists(connection, actor, None, limit)
                assert len(statements) == 1, (
                    f"{limit}-row page issued {len(statements)} queries"
                )
                assert len(result["items"]) == limit
    finally:
        engine.dispose()


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
    assert staff["operational_manager"] is False
    assert "bulk_reschedule" not in staff
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

    status, reissued = _staff_command(
        f"{staff_path}/invite", cookie, _changes(revoked["data"])
    )
    assert status == 200, reissued
    assert reissued["data"]["invitation_state"] == "ISSUED"
    with _database() as connection:
        states = connection.execute(
            """
            SELECT i.state FROM public.invitations i
            WHERE i.organization_id = %s AND i.staff_id = %s
            ORDER BY i.issued_at, i.id
            """,
            (owner.clinic, staff["id"]),
        ).fetchall()
        jobs = connection.execute(
            "SELECT count(*) FROM public.delivery_jobs WHERE organization_id = %s AND staff_id = %s",
            (owner.clinic, staff["id"]),
        ).fetchone()[0]
    assert [state for (state,) in states] == ["REVOKED", "ISSUED"]
    assert jobs == 2


def test_invited_receptionist_cannot_receive_manager_grant(owner_session):
    _, cookie = owner_session
    status, created = _staff_command(
        "/api/staff/receptionists",
        cookie,
        {
            "email": f"synthetic-pending-{uuid4()}@example.invalid",
            "name": "Pending receptionist",
            "reason": "Hire",
        },
        key=str(uuid4()),
    )
    assert status == 201, created
    staff = created["data"]
    path = f"/api/staff/receptionists/{staff['id']}"
    manager_change = {**_changes(staff), "operational_manager": True}
    status, _ = _staff_command(
        f"{path}/permissions", cookie, manager_change, method="PATCH"
    )
    assert status == 403
    status, issued = _staff_command(f"{path}/invite", cookie, _changes(staff))
    assert status == 200, issued
    status, _ = _staff_command(
        f"{path}/permissions",
        cookie,
        {**_changes(issued["data"]), "operational_manager": True},
        method="PATCH",
    )
    assert status == 403
    status, current = _staff_command(path, cookie, method="GET")
    assert status == 200
    assert current["data"]["operational_manager"] is False


def test_parallel_same_key_receptionist_creation_reuses_receipt(owner_session):
    owner, cookie = owner_session
    address = f"synthetic-create-race-{uuid4()}@example.invalid"
    command = {
        "email": address,
        "name": "Synthetic receptionist race",
        "reason": "Hire",
    }
    key = str(uuid4())
    barrier = Barrier(3)

    def create():
        barrier.wait()
        return _staff_command("/api/staff/receptionists", cookie, command, key=key)

    with ThreadPoolExecutor(max_workers=2) as pool:
        with _database() as holding:
            # Reads proceed while both competing native-user INSERTs must wait.
            holding.execute('LOCK TABLE public."user" IN SHARE MODE')
            futures = [pool.submit(create) for _ in range(2)]
            barrier.wait()
            blocked = 0
            deadline = time.monotonic() + 4
            try:
                while blocked < 2 and time.monotonic() < deadline:
                    with _database() as observer:
                        blocked = observer.execute(
                            """
                            SELECT count(*) FROM pg_locks
                             WHERE relation = 'public."user"'::regclass
                               AND mode = 'RowExclusiveLock' AND NOT granted
                            """
                        ).fetchone()[0]
                    if blocked < 2:
                        time.sleep(0.02)
            finally:
                holding.commit()
        assert blocked == 2, "Two native inserts never contended on the unique email"
        responses = [future.result(timeout=30) for future in futures]

    assert [status for status, _ in responses] == [201, 201]
    assert responses[0][1]["data"] == responses[1][1]["data"]
    with _database() as connection:
        counts = connection.execute(
            """
            SELECT
                (SELECT count(*) FROM public."user" WHERE email = %s),
                (SELECT count(*) FROM public.staff_accounts s
                    WHERE s.organization_id = %s
                      AND s.verified_email = %s
                      AND s.role = 'RECEPTIONIST'),
                (SELECT count(*) FROM public.idempotency_receipts r
                    WHERE r.organization_id = %s AND r.idempotency_key = %s)
            """,
            (address, owner.clinic, address, owner.clinic, key),
        ).fetchone()
    assert counts == (1, 1, 1)


def test_expired_receptionist_proof_is_rejected_and_replaced_by_owner(owner_session):
    owner, cookie = owner_session
    email = f"synthetic-expired-{uuid4()}@example.invalid"
    status, created = _staff_command(
        "/api/staff/receptionists",
        cookie,
        {"email": email, "name": "Synthetic expired proof", "reason": "Hire"},
        key=str(uuid4()),
    )
    assert status == 201, created
    staff = created["data"]
    expired_id, proof = uuid4(), secrets.token_urlsafe(32)
    with _database() as connection:
        connection.execute(
            """
            INSERT INTO public.invitations
                (id, organization_id, staff_id, purpose, target_email,
                 proof_digest, issued_at, expires_at, issued_by_staff_id)
            VALUES (%s, %s, %s, 'RECEPTIONIST', %s, %s,
                    clock_timestamp() - interval '20 minutes',
                    clock_timestamp() - interval '1 minute', %s)
            """,
            (
                expired_id,
                owner.clinic,
                staff["id"],
                email,
                sha256(proof.encode()).hexdigest(),
                owner.staff,
            ),
        )
    status, _ = _staff_command(
        "/api/auth/invite-accept",
        None,
        {
            "organization_id": str(owner.clinic),
            "invitation_id": str(expired_id),
            "proof": proof,
            "name": "Synthetic expired proof",
            "password": f"Synthetic-Expired-{uuid4()}!",
        },
    )
    assert status == 403
    staff_path = f"/api/staff/receptionists/{staff['id']}"
    status, reissued = _staff_command(f"{staff_path}/invite", cookie, _changes(staff))
    assert status == 200, reissued
    assert reissued["data"]["invitation_state"] == "ISSUED"
    with _database() as connection:
        rows = connection.execute(
            "SELECT id, state FROM public.invitations WHERE staff_id = %s",
            (staff["id"],),
        ).fetchall()
    assert len(rows) == 2
    assert dict(rows)[expired_id] == "REVOKED"
    assert len([1 for _, state in rows if state == "ISSUED"]) == 1


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

    for path, method, payload in (
        ("/api/staff/receptionists", "GET", None),
        (staff_path, "GET", None),
        (
            f"{staff_path}/disable",
            "POST",
            {
                "reason": "Unauthorized",
                "expected_revision": 0,
                "expected_security_revision": 0,
            },
        ),
    ):
        status, _ = _staff_command(path, first_cookie, payload, method=method)
        assert status == 403
    status, _ = _staff_command(
        f"/api/staff/receptionists/{owner.staff}/disable",
        owner_cookie,
        {
            "reason": "Owner mutation",
            "expected_revision": 0,
            "expected_security_revision": 0,
        },
    )
    assert status == 404
    status, _ = _staff_command(
        f"/api/staff/receptionists/{owner.staff}/permissions",
        owner_cookie,
        {
            "reason": "Owner cannot become Manager",
            "expected_revision": 0,
            "expected_security_revision": 0,
            "operational_manager": True,
        },
        method="PATCH",
    )
    assert status == 404
    status, _ = _staff_command(
        f"{staff_path}/permissions",
        owner_cookie,
        {
            "reason": "Admin delegation",
            "expected_revision": 0,
            "expected_security_revision": 0,
            "role": "DOCTOR_OWNER",
            "operational_manager": True,
        },
        method="PATCH",
    )
    assert status == 422
    status, legacy = _staff_command(
        f"{staff_path}/permissions",
        owner_cookie,
        {
            "reason": "Legacy grant must fail",
            "expected_revision": 0,
            "expected_security_revision": 0,
            "bulk_reschedule": True,
        },
        method="PATCH",
    )
    assert status == 422, legacy

    status, restricted = _staff_command(
        f"{staff_path}/permissions",
        first_cookie,
        {
            "reason": "Self escalation",
            "expected_revision": 0,
            "expected_security_revision": 0,
            "operational_manager": True,
        },
        method="PATCH",
    )
    assert status == 403, restricted

    status, granted = _staff_command(
        f"{staff_path}/permissions",
        owner_cookie,
        {
            "reason": "Delegate operational management",
            "expected_revision": 0,
            "expected_security_revision": 0,
            "operational_manager": True,
        },
        method="PATCH",
    )
    assert status == 200, granted
    assert granted["data"]["operational_manager"] is True
    assert "bulk_reschedule" not in granted["data"]
    assert granted["data"]["revision"] == 1
    assert granted["data"]["security_revision"] == 1
    with _database() as connection:
        recorded = connection.execute(
            """
            SELECT actor_staff_id, actor_role, reason, before_state, after_state
              FROM public.sensitive_audit
             WHERE organization_id = %s AND action = 'RECEPTIONIST_PERMISSIONS'
             ORDER BY occurred_at DESC LIMIT 1
            """,
            (owner.clinic,),
        ).fetchone()
    assert recorded is not None
    assert recorded[0] == owner.staff
    assert recorded[1] == "DOCTOR_OWNER"
    assert recorded[2] == "Delegate operational management"
    assert recorded[3]["operational_manager"] is False
    assert recorded[4]["operational_manager"] is True
    status, stale = _staff_command(
        f"{staff_path}/permissions",
        owner_cookie,
        {
            "reason": "Stale competing edit",
            "expected_revision": 0,
            "expected_security_revision": 0,
            "operational_manager": False,
        },
        method="PATCH",
    )
    assert status == 409, stale
    for old_cookie in (first_cookie, second_cookie):
        status, _, _ = _request("/api/session", cookie=old_cookie)
        assert status == 401

    refreshed_cookie = _login(recipient)
    with _database() as connection:
        connection.execute(
            "REVOKE EXECUTE ON FUNCTION public.delete_staff_sessions(uuid,text) "
            "FROM carepulse_runtime"
        )
    try:
        status, withdrawn = _staff_command(
            f"{staff_path}/permissions",
            owner_cookie,
            {**_changes(granted["data"]), "operational_manager": False},
            method="PATCH",
        )
        assert status == 200, withdrawn
        with _database() as connection:
            remaining = connection.execute(
                'SELECT count(*) FROM public."session" WHERE "userId" = %s',
                (native_user_id,),
            ).fetchone()[0]
        assert remaining >= 1, "Cleanup unexpectedly deleted the live native session"
        old_session_status, _, _ = _request("/api/session", cookie=refreshed_cookie)
        assert old_session_status == 401
    finally:
        with _database() as connection:
            connection.execute(
                "GRANT EXECUTE ON FUNCTION public.delete_staff_sessions(uuid,text) "
                "TO carepulse_runtime"
            )
    assert status == 200, withdrawn
    assert withdrawn["data"]["operational_manager"] is False
    assert withdrawn["data"]["revision"] == granted["data"]["revision"] + 1
    assert (
        withdrawn["data"]["security_revision"]
        == granted["data"]["security_revision"] + 1
    )
    status, _, _ = _request("/api/session", cookie=refreshed_cookie)
    assert status == 401

    status, disabled = _staff_command(
        f"{staff_path}/disable",
        owner_cookie,
        _changes(withdrawn["data"]),
    )
    assert status == 200, disabled
    assert disabled["data"]["enabled"] is False
    status, _ = _staff_command(
        f"{staff_path}/permissions",
        owner_cookie,
        {**_changes(disabled["data"]), "operational_manager": True},
        method="PATCH",
    )
    assert status == 403
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


def test_signed_manager_cannot_manage_staff_even_after_regrant(owner_session):
    owner, owner_cookie = owner_session
    receptionists = []
    with _database() as connection:
        for _ in range(3):
            receptionist = SimpleNamespace(
                staff=uuid4(),
                user=f"synthetic-m1-{uuid4()}",
                email=f"synthetic-m1-{uuid4()}@example.invalid",
                password=f"Synthetic-M1-{uuid4()}!",
            )
            connection.execute(
                'INSERT INTO public."user" '
                '(id, name, email, "emailVerified", "createdAt", "updatedAt") '
                "VALUES (%s, 'Synthetic M1 receptionist', %s, true, "
                "clock_timestamp(), clock_timestamp())",
                (receptionist.user, receptionist.email),
            )
            connection.execute(
                'INSERT INTO public."account" '
                '(id, "accountId", "providerId", "userId", password, "createdAt", "updatedAt") '
                "VALUES (%s, %s, 'credential', %s, %s, "
                "clock_timestamp(), clock_timestamp())",
                (
                    f"synthetic-m1-account-{uuid4()}",
                    receptionist.user,
                    receptionist.user,
                    _password_hash(receptionist.password),
                ),
            )
            connection.execute(
                """INSERT INTO public.staff_accounts
                       (id, organization_id, auth_user_id, verified_email,
                        role, enabled, verified)
                     VALUES (%s, %s, %s, %s, 'RECEPTIONIST', true, true)""",
                (
                    receptionist.staff,
                    owner.clinic,
                    receptionist.user,
                    receptionist.email,
                ),
            )
            receptionists.append(receptionist)

    manager, plain, delegated = receptionists
    manager_path = f"/api/staff/receptionists/{manager.staff}"
    plain_path = f"/api/staff/receptionists/{plain.staff}"
    delegated_path = f"/api/staff/receptionists/{delegated.staff}"
    initial = {"revision": 0, "security_revision": 0}
    original_cookie = _login(manager)
    assert _request("/api/session", cookie=original_cookie)[0] == 200

    status, granted_manager = _staff_command(
        f"{manager_path}/permissions",
        owner_cookie,
        {**_changes(initial), "operational_manager": True},
        method="PATCH",
    )
    assert status == 200, granted_manager
    assert granted_manager["data"]["operational_manager"] is True
    assert _request("/api/session", cookie=original_cookie)[0] == 401
    manager_cookie = _login(manager)
    status, _, session_body = _request("/api/session", cookie=manager_cookie)
    assert status == 200, session_body
    assert json.loads(session_body)["data"] == {
        "id": str(manager.staff),
        "organization_id": str(owner.clinic),
        "role": "RECEPTIONIST",
        "revision": 1,
    }

    status, granted_delegated = _staff_command(
        f"{delegated_path}/permissions",
        owner_cookie,
        {**_changes(initial), "operational_manager": True},
        method="PATCH",
    )
    assert status == 200, granted_delegated
    unapproved_email = f"synthetic-m1-denied-{uuid4()}@example.invalid"
    denied_actions = (
        ("/api/staff/receptionists", "GET", None, None),
        (plain_path, "GET", None, None),
        (
            "/api/staff/receptionists",
            "POST",
            {
                "email": unapproved_email,
                "name": "Denied hire",
                "reason": "No authority",
            },
            str(uuid4()),
        ),
        (f"{plain_path}/disable", "POST", _changes(initial), None),
        (f"{plain_path}/enable", "POST", _changes(initial), None),
        (
            f"{plain_path}/permissions",
            "PATCH",
            {**_changes(initial), "operational_manager": True},
            None,
        ),
        (
            f"{delegated_path}/permissions",
            "PATCH",
            {**_changes(granted_delegated["data"]), "operational_manager": False},
            None,
        ),
    )
    for path, method, payload, key in denied_actions:
        status, response = _staff_command(
            path, manager_cookie, payload, method=method, key=key
        )
        assert status == 403, (method, path, response)
    with _database() as connection:
        assert (
            connection.execute(
                'SELECT count(*) FROM public."user" WHERE email = %s',
                (unapproved_email,),
            ).fetchone()[0]
            == 0
        )
        assert connection.execute(
            "SELECT enabled, operational_manager FROM public.staff_accounts WHERE id = %s",
            (plain.staff,),
        ).fetchone() == (True, False)
        assert connection.execute(
            "SELECT operational_manager FROM public.staff_accounts WHERE id = %s",
            (delegated.staff,),
        ).fetchone() == (True,)

    status, revoked_delegated = _staff_command(
        f"{delegated_path}/permissions",
        owner_cookie,
        {**_changes(granted_delegated["data"]), "operational_manager": False},
        method="PATCH",
    )
    assert status == 200, revoked_delegated
    assert revoked_delegated["data"]["operational_manager"] is False
    status, disabled = _staff_command(
        f"{plain_path}/disable", owner_cookie, _changes(initial)
    )
    assert status == 200, disabled
    assert disabled["data"]["enabled"] is False
    status, enabled = _staff_command(
        f"{plain_path}/enable", owner_cookie, _changes(disabled["data"])
    )
    assert status == 200, enabled
    assert enabled["data"]["enabled"] is True

    status, revoked_manager = _staff_command(
        f"{manager_path}/permissions",
        owner_cookie,
        {**_changes(granted_manager["data"]), "operational_manager": False},
        method="PATCH",
    )
    assert status == 200, revoked_manager
    assert _request("/api/session", cookie=manager_cookie)[0] == 401
    ordinary_cookie = _login(manager)
    assert (
        _staff_command("/api/staff/receptionists", ordinary_cookie, method="GET")[0]
        == 403
    )
    status, regranted = _staff_command(
        f"{manager_path}/permissions",
        owner_cookie,
        {**_changes(revoked_manager["data"]), "operational_manager": True},
        method="PATCH",
    )
    assert status == 200, regranted
    assert regranted["data"]["security_revision"] == 3
    assert _request("/api/session", cookie=ordinary_cookie)[0] == 401
    with _database() as connection:
        # Synthetic CI shares one ingress source across the Owner and Manager.
        # Keep the real login throttle intact while isolating this authz test.
        connection.execute(
            "DELETE FROM public.auth_login_attempts WHERE dimension='source'"
        )
    regranted_cookie = _login(manager)
    assert (
        _staff_command("/api/staff/receptionists", regranted_cookie, method="GET")[0]
        == 403
    )
    assert (
        _staff_command(
            f"{plain_path}/permissions",
            regranted_cookie,
            {**_changes(enabled["data"]), "operational_manager": True},
            method="PATCH",
        )[0]
        == 403
    )


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
    status, visible = _staff_command("/api/staff/receptionists", cookie, method="GET")
    assert status == 200
    assert all(entry["id"] != str(staff_id) for entry in visible["data"]["items"])
    status, _ = _staff_command(path, cookie, method="GET")
    assert status == 404
    status, _ = _staff_command(
        f"{path}/permissions",
        cookie,
        {
            "reason": "Cross clinic",
            "expected_revision": 0,
            "expected_security_revision": 0,
            "operational_manager": True,
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
    assert accepted["data"]["operational_manager"] is False
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
    receptionist_cookie = _login(
        SimpleNamespace(email=email, password=acceptance["password"])
    )
    status, denial = _staff_command(
        "/api/staff/receptionists", receptionist_cookie, method="GET"
    )
    assert status == 403, denial


def test_owner_create_receipt_preserves_original_after_acceptance_and_manager_grant(
    owner_session,
):
    owner, owner_cookie = owner_session
    email = f"synthetic-replay-{uuid4()}@example.invalid"
    command = {"email": email, "name": "Synthetic replay staff", "reason": "Hire"}
    key = str(uuid4())
    status, created = _staff_command(
        "/api/staff/receptionists", owner_cookie, command, key=key
    )
    assert status == 201, created
    staff = created["data"]
    assert created["revision"] == staff["revision"] == 0
    assert staff["operational_manager"] is False

    path = f"/api/staff/receptionists/{staff['id']}"
    status, invited = _staff_command(f"{path}/invite", owner_cookie, _changes(staff))
    assert status == 200, invited
    status, revoked = _staff_command(
        f"{path}/revoke-invite", owner_cookie, _changes(invited["data"])
    )
    assert status == 200, revoked

    proof = secrets.token_urlsafe(32)
    invitation = uuid4()
    with _database() as connection:
        connection.execute(
            """INSERT INTO public.invitations
                 (id, organization_id, staff_id, purpose, target_email,
                  proof_digest, expires_at, issued_by_staff_id)
               VALUES (%s, %s, %s, 'RECEPTIONIST', %s, %s,
                       clock_timestamp() + interval '20 minutes', %s)""",
            (
                invitation,
                owner.clinic,
                staff["id"],
                email,
                sha256(proof.encode()).hexdigest(),
                owner.staff,
            ),
        )
    status, accepted = _staff_command(
        "/api/auth/invite-accept",
        None,
        {
            "organization_id": str(owner.clinic),
            "invitation_id": str(invitation),
            "proof": proof,
            "name": command["name"],
            "password": f"Synthetic-Replay-{uuid4()}!",
        },
    )
    assert status == 200, accepted
    assert accepted["data"]["verified"] is True
    assert accepted["data"]["security_revision"] > staff["security_revision"]

    status, granted = _staff_command(
        f"{path}/permissions",
        owner_cookie,
        {**_changes(accepted["data"]), "operational_manager": True},
        method="PATCH",
    )
    assert status == 200, granted
    assert granted["data"]["operational_manager"] is True
    assert granted["data"]["security_revision"] > accepted["data"]["security_revision"]
    status, current = _staff_command(path, owner_cookie, method="GET")
    assert status == 200, current
    assert current["data"] == granted["data"]
    assert current["revision"] == granted["revision"] == granted["data"]["revision"]
    assert current["data"]["invitation_state"] == "ACCEPTED"

    def counts():
        with _database() as connection:
            return connection.execute(
                """SELECT
                    (SELECT count(*) FROM public."user" WHERE email = %s),
                    (SELECT count(*) FROM public.staff_accounts
                     WHERE organization_id = %s AND verified_email = %s),
                    (SELECT count(*) FROM public.invitations
                     WHERE organization_id = %s AND staff_id = %s),
                    (SELECT count(*) FROM public.delivery_jobs
                     WHERE organization_id = %s AND staff_id = %s),
                    (SELECT count(*) FROM public.sensitive_audit
                     WHERE organization_id = %s),
                    (SELECT count(*) FROM public.idempotency_receipts
                     WHERE organization_id = %s AND idempotency_key = %s)""",
                (
                    email,
                    owner.clinic,
                    email,
                    owner.clinic,
                    staff["id"],
                    owner.clinic,
                    staff["id"],
                    owner.clinic,
                    owner.clinic,
                    key,
                ),
            ).fetchone()

    before_replay = counts()
    assert before_replay[:4] == (1, 1, 2, 1)
    assert before_replay[5] == 1
    status, replay = _staff_command(
        "/api/staff/receptionists", owner_cookie, command, key=key
    )
    assert status == 201, replay
    assert replay == created
    assert replay["data"]["operational_manager"] is False
    assert replay["data"]["verified"] is False
    assert replay["data"]["security_revision"] == 0
    assert replay["revision"] == 0
    assert counts() == before_replay


def test_competing_receptionist_acceptance_cannot_attach_two_credentials(owner_session):
    owner, cookie = owner_session
    email = f"synthetic-race-{uuid4()}@example.invalid"
    status, created = _staff_command(
        "/api/staff/receptionists",
        cookie,
        {"email": email, "name": "Synthetic competing staff", "reason": "Hire"},
        key=str(uuid4()),
    )
    assert status == 201, created
    staff = created["data"]
    invitation_id = uuid4()
    proof = secrets.token_urlsafe(32)
    with _database() as connection:
        connection.execute(
            """
            INSERT INTO public.invitations
              (id, organization_id, staff_id, purpose, target_email, proof_digest,
               expires_at, issued_by_staff_id)
            VALUES (%s, %s, %s, 'RECEPTIONIST', %s, %s,
                    clock_timestamp() + interval '20 minutes', %s)
            """,
            (
                invitation_id,
                owner.clinic,
                staff["id"],
                email,
                sha256(proof.encode()).hexdigest(),
                owner.staff,
            ),
        )
    base = {
        "organization_id": str(owner.clinic),
        "invitation_id": str(invitation_id),
        "proof": proof,
        "name": "Synthetic competing staff",
    }
    passwords = [f"Synthetic-Race-{uuid4()}!", f"Synthetic-Race-{uuid4()}!"]
    barrier = Barrier(3)

    def accept(password: str):
        barrier.wait()
        return password, _staff_command(
            "/api/auth/invite-accept", None, {**base, "password": password}
        )[0]

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(accept, password) for password in passwords]
        barrier.wait()
        outcomes = [future.result(timeout=30) for future in futures]

    assert len([status for _, status in outcomes if status == 200]) == 1
    assert all(status in (200, 403, 503) for _, status in outcomes)
    with _database() as connection:
        linked_credentials = connection.execute(
            """
            SELECT count(*) FROM public.account a
            JOIN public."user" u ON u.id = a."userId"
            WHERE u.email = %s AND a."providerId" = 'credential'
            """,
            (email,),
        ).fetchone()[0]
    assert linked_credentials == 1
    winner = next(password for password, status in outcomes if status == 200)
    assert _login(SimpleNamespace(email=email, password=winner))
    loser = next(password for password, status in outcomes if status != 200)
    rejected_login, _, _ = _request(
        "/api/auth/login",
        method="POST",
        payload={"email": email, "password": loser},
        headers=_mutation_headers(),
    )
    assert rejected_login == 401

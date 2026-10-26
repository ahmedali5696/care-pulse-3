"""B-04 recovery races through real BFF, Better Auth, and PostgreSQL barriers."""

import base64
import json
import secrets
from concurrent.futures import ThreadPoolExecutor
from hashlib import sha256
from threading import Barrier
from time import monotonic, sleep
from types import SimpleNamespace
from uuid import uuid4

import pytest
import test_provisioning
from carepulse.config import _load_policy
from carepulse.notifications.delivery_material import encrypt_delivery_token
from carepulse.provisioning.operators import authenticate_operator
from carepulse.provisioning.service import change_clinic_state
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from test_recovery import (
    _expire_reserved_attempt,
    _native,
    _operation,
    _reject_commit_for,
)
from test_sessions import _database, _login, _mutation_headers, _request, _seed_owner

operator_identity = test_provisioning.operator_identity


@pytest.fixture
def recovery_owner(disposable_ci):
    with _database() as connection:
        connection.execute(
            "DELETE FROM public.auth_login_attempts WHERE dimension='source'"
        )
        return _seed_owner(connection)


def _issued_proof(owner, lifetime_seconds=900):
    """Persist native proof and a sealed job; issuance itself is tested in T056."""
    token = secrets.token_urlsafe(18)
    digest = sha256(token.encode()).hexdigest()
    identifier = (
        base64.urlsafe_b64encode(sha256(f"reset-password:{token}".encode()).digest())
        .decode()
        .rstrip("=")
    )
    reference, job = f"synthetic-reset-{uuid4()}", uuid4()
    private_key = rsa.generate_private_key(public_exponent=65537, key_size=3072)
    public_key = private_key.public_key().public_bytes(
        serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo
    )
    with _database() as connection:
        expiry = connection.execute(
            "SELECT clock_timestamp() + (%s * interval '1 second')",
            (lifetime_seconds,),
        ).fetchone()[0]
        context = {
            "job_id": str(job),
            "proof_reference": digest,
            "purpose": "PASSWORD_RESET",
            "tenant_id": str(owner.clinic),
            "committed_recipient": owner.email,
            "expiry_epoch_ms": str(int(expiry.timestamp() * 1000)),
        }
        sealed = encrypt_delivery_token(
            token, context, "synthetic-recovery-race", public_key
        )
        connection.execute(
            """
            INSERT INTO public.verification
              (id, identifier, value, "expiresAt", "createdAt", "updatedAt")
            VALUES (%s, %s, %s, %s, clock_timestamp(), clock_timestamp())
            """,
            (reference, identifier, owner.user, expiry),
        )
        connection.execute(
            """
            INSERT INTO public.delivery_jobs
              (id, organization_id, staff_id, verification_id, proof_reference,
               purpose, recipient, template, logical_key, proof_expires_at)
            VALUES (%s, %s, %s, %s, %s, 'PASSWORD_RESET',
                    %s, 'PASSWORD_RESET', %s, %s)
            """,
            (
                job,
                owner.clinic,
                owner.staff,
                reference,
                digest,
                owner.email,
                str(job),
                expiry,
            ),
        )
        connection.execute(
            """
            INSERT INTO public.protected_delivery_material
              (job_id, format_version, delivery_key_id, ciphertext_base64url)
            VALUES (%s, 1, %s, %s)
            """,
            (job, sealed["key_id"], sealed["ciphertext_base64url"]),
        )
    return token, digest


def _reset(token, password, *, timeout=6):
    status, _, body = _request(
        "/api/auth/reset-complete",
        method="POST",
        payload={"token": token, "newPassword": password},
        headers=_mutation_headers(),
        timeout=timeout,
    )
    return status, json.loads(body)


def _wait_for_organization_waiters(
    connection, count, role="carepulse_runtime", query="lock_active_organization"
):
    deadline = monotonic() + 4
    while monotonic() < deadline:
        # PostgreSQL caches statistics snapshots across queries in a transaction.
        connection.execute("SELECT pg_stat_clear_snapshot()")
        waiting = connection.execute(
            """
            SELECT count(DISTINCT pid) FROM pg_stat_activity
             WHERE datname = 'carepulse' AND usename = %s
               AND wait_event_type = 'Lock'
               AND query LIKE %s
               AND cardinality(pg_blocking_pids(pid)) > 0
            """,
            (role, f"%{query}%"),
        ).fetchone()[0]
        if waiting >= count:
            return
        sleep(0.02)
    pytest.fail(f"Only {waiting} of {count} PostgreSQL clinic-lock waiters observed")


def _wait_for_native_session_delete(connection):
    deadline = monotonic() + 4
    while monotonic() < deadline:
        connection.execute("SELECT pg_stat_clear_snapshot()")
        blocked = connection.execute(
            """
            SELECT count(*) FROM pg_locks l
            JOIN pg_stat_activity a ON a.pid = l.pid
            WHERE l.relation = 'public."session"'::regclass
              AND l.mode = 'RowExclusiveLock' AND NOT l.granted
              AND a.usename = 'carepulse_auth'
            """
        ).fetchone()[0]
        if blocked == 1:
            return
        sleep(0.02)
    pytest.fail("Native recovery never reached the session deletion lock")


def test_expiry_inside_native_transaction_rolls_back_every_auth_effect(recovery_owner):
    owner = recovery_owner
    stale_cookie = _login(owner)
    token, digest = _issued_proof(owner, lifetime_seconds=12)
    with _database() as connection:
        password_before = connection.execute(
            'SELECT password FROM public."account" WHERE "userId"=%s',
            (owner.user,),
        ).fetchone()[0]
        expires_at = connection.execute(
            'SELECT "expiresAt" FROM public.verification WHERE value=%s AND '
            "identifier = %s",
            (
                owner.user,
                base64.urlsafe_b64encode(
                    sha256(f"reset-password:{token}".encode()).digest()
                )
                .decode()
                .rstrip("="),
            ),
        ).fetchone()[0]

    with ThreadPoolExecutor(max_workers=1) as executor:
        with _database() as blocker:
            blocker.execute('LOCK TABLE public."session" IN SHARE MODE')
            pending = executor.submit(
                _reset, token, f"Synthetic-Expired-During-Auth-{uuid4()}!", timeout=20
            )
            try:
                _wait_for_native_session_delete(blocker)
                assert (
                    blocker.execute("SELECT clock_timestamp()").fetchone()[0]
                    < expires_at
                )
                # The PostgreSQL lock is the synchronization point; this wait
                # only reaches the actual proof deadline before releasing it.
                blocker.execute(
                    "SELECT pg_sleep(GREATEST(0, EXTRACT(EPOCH FROM (%s::timestamptz - clock_timestamp()))))",
                    (expires_at,),
                )
            finally:
                blocker.commit()
        assert pending.result(timeout=25)[0] == 503

    with _database() as connection:
        native = connection.execute(
            'SELECT password, (SELECT count(*) FROM public."session" WHERE "userId"=%s) '
            'FROM public."account" WHERE "userId"=%s',
            (owner.user, owner.user),
        ).fetchone()
        operation = connection.execute(
            """SELECT r.id, r.state, r.auth_fence, s.security_revision,
                      s.reset_block_operation_id,
                      (SELECT count(*) FROM public.auth_recovery_completions c WHERE c.id=r.id),
                      (SELECT count(*) FROM public.sensitive_audit a
                        WHERE a.recovery_operation_id=r.id AND a.action='PASSWORD_RESET_SUCCEEDED')
                 FROM public.recovery_operations r
                 JOIN public.staff_accounts s ON s.id=r.staff_id
                WHERE r.proof_digest=%s""",
            (digest,),
        ).fetchone()
        assert (
            connection.execute(
                'SELECT count(*) FROM public.verification WHERE value=%s AND "expiresAt"<=clock_timestamp()',
                (owner.user,),
            ).fetchone()[0]
            == 1
        )
    assert native == (password_before, 1)
    assert operation[1:4] == ("RESERVED", 0, 1)
    assert operation[4] == operation[0] and operation[5:] == (0, 0)
    assert _request("/api/session", cookie=stale_cookie)[0] == 401


@pytest.mark.parametrize("same_password", [True, False], ids=["matching", "competing"])
def test_parallel_recovery_proof_has_one_credential_effect_and_audit(
    recovery_owner, same_password
):
    owner = recovery_owner
    first_cookie = _login(owner)
    second_cookie = _login(owner)
    token, digest = _issued_proof(owner)
    passwords = [f"Synthetic-Recovery-{uuid4()}!" for _ in range(2)]
    if same_password:
        passwords[1] = passwords[0]
    ready = Barrier(3)

    def attempt(password):
        ready.wait(timeout=6)
        return _reset(token, password)

    with ThreadPoolExecutor(max_workers=2) as executor:
        with _database() as blocker:
            blocker.execute(
                "SELECT id FROM public.organizations WHERE id = %s FOR UPDATE",
                (owner.clinic,),
            )
            futures = [executor.submit(attempt, password) for password in passwords]
            ready.wait(timeout=6)
            try:
                _wait_for_organization_waiters(blocker, 2)
            finally:
                blocker.commit()
        responses = [future.result(timeout=25) for future in futures]

    statuses = [status for status, _ in responses]
    assert sorted(statuses) == ([200, 200] if same_password else [200, 409]), responses
    if not same_password:
        assert (
            responses[statuses.index(409)][1]["error"]["code"] == "IDEMPOTENCY_CONFLICT"
        )
    with _database() as connection:
        evidence = connection.execute(
            """
            SELECT r.id, r.state, r.auth_fence,
                   (SELECT count(*) FROM public.auth_recovery_completions c WHERE c.id = r.id),
                   (SELECT count(*) FROM public.sensitive_audit a
                     WHERE a.recovery_operation_id = r.id AND a.action = 'PASSWORD_RESET_SUCCEEDED'),
                   (SELECT count(*) FROM public.sensitive_audit a
                     WHERE a.recovery_operation_id = r.id AND a.action = 'PASSWORD_RESET_RESERVED'),
                   (SELECT count(*) FROM public."session" s WHERE s."userId" = %s),
                   st.reset_block_operation_id
              FROM public.recovery_operations r
              JOIN public.staff_accounts st ON st.id = r.staff_id
             WHERE r.organization_id = %s AND r.proof_digest = %s
            """,
            (owner.user, owner.clinic, digest),
        ).fetchall()
    assert len(evidence) == 1
    operation, state, fence, completions, successes, reserved, sessions, block = (
        evidence[0]
    )
    assert operation is not None
    assert state == "FINALIZED" and fence == 1
    assert (completions, successes, reserved, sessions, block) == (1, 1, 1, 0, None)
    for cookie in (first_cookie, second_cookie):
        assert _request("/api/session", cookie=cookie)[0] == 401

    winner_password = passwords[statuses.index(200)]
    with _database() as connection:
        connection.execute(
            "DELETE FROM public.auth_login_attempts WHERE dimension='source'"
        )
    assert _login(SimpleNamespace(email=owner.email, password=winner_password))
    if not same_password:
        loser_password = passwords[statuses.index(409)]
        status, _, _ = _request(
            "/api/auth/login",
            method="POST",
            payload={"email": owner.email, "password": loser_password},
            headers=_mutation_headers(),
        )
        assert status == 401


def test_committed_operator_suspend_wins_before_recovery_reservation(
    recovery_owner, request
):
    owner = recovery_owner
    _, operator_engine, actor, secret = request.getfixturevalue("operator_identity")
    verified = authenticate_operator(operator_engine, actor, secret, _load_policy())
    old_cookie = _login(owner)
    token, digest = _issued_proof(owner)
    with _database() as connection:
        lifecycle = connection.execute(
            "SELECT lifecycle_revision FROM public.organizations WHERE id = %s",
            (owner.clinic,),
        ).fetchone()[0]
        credential = connection.execute(
            'SELECT password FROM public."account" WHERE "userId" = %s',
            (owner.user,),
        ).fetchone()[0]

    with ThreadPoolExecutor(max_workers=2) as executor:
        with _database() as blocker:
            blocker.execute(
                "SELECT id FROM public.organizations WHERE id = %s FOR UPDATE",
                (owner.clinic,),
            )
            suspended = executor.submit(
                change_clinic_state,
                operator_engine,
                verified,
                owner.clinic,
                lifecycle,
                "SUSPENDED",
                "Deterministic recovery race",
            )
            try:
                _wait_for_organization_waiters(
                    blocker, 1, "carepulse_operator", "operator_change_clinic_state"
                )
                reset = executor.submit(_reset, token, f"Synthetic-Denied-{uuid4()}!")
                _wait_for_organization_waiters(blocker, 1)
            finally:
                blocker.commit()
        assert suspended.result(timeout=25)["state"] == "SUSPENDED"
        assert reset.result(timeout=25)[0] in (403, 409)

    with _database() as connection:
        result = connection.execute(
            """
            SELECT o.state, s.reset_block_operation_id,
                   (SELECT count(*) FROM public.recovery_operations r
                    WHERE r.organization_id = o.id AND r.proof_digest = %s),
                   (SELECT password FROM public."account" a WHERE a."userId" = %s),
                   (SELECT count(*) FROM public.sensitive_audit a
                    WHERE a.organization_id = o.id AND a.action = 'PASSWORD_RESET_SUCCEEDED')
              FROM public.organizations o JOIN public.staff_accounts s
                ON s.organization_id = o.id AND s.id = %s WHERE o.id = %s
            """,
            (digest, owner.user, owner.staff, owner.clinic),
        ).fetchone()
    assert result == ("SUSPENDED", None, 0, credential, 0)
    assert _request("/api/session", cookie=old_cookie)[0] == 401


def test_operator_revocation_after_native_fence_blocks_stale_finalization(
    recovery_owner, request
):
    owner = recovery_owner
    _, operator_engine, actor, secret = request.getfixturevalue("operator_identity")
    verified = authenticate_operator(operator_engine, actor, secret, _load_policy())
    stale_cookie = _login(owner)
    token, digest = _issued_proof(owner)
    new_password = f"Synthetic-Fenced-{uuid4()}!"
    with _database() as connection:
        lifecycle = connection.execute(
            "SELECT lifecycle_revision FROM public.organizations WHERE id = %s",
            (owner.clinic,),
        ).fetchone()[0]
        credential_before = connection.execute(
            'SELECT password FROM public."account" WHERE "userId"=%s',
            (owner.user,),
        ).fetchone()[0]

    with ThreadPoolExecutor(max_workers=2) as executor:
        with _database() as blocker:
            blocker.execute('LOCK TABLE public."session" IN SHARE MODE')
            reset = executor.submit(_reset, token, new_password)
            try:
                _wait_for_native_session_delete(blocker)

                with _database() as observer:
                    operation = observer.execute(
                        """
                        SELECT id, state, auth_fence
                          FROM public.recovery_operations WHERE proof_digest = %s
                        """,
                        (digest,),
                    ).fetchone()
                assert operation[1:] == ("RESERVED", 0)
                suspended = executor.submit(
                    change_clinic_state,
                    operator_engine,
                    verified,
                    owner.clinic,
                    lifecycle,
                    "SUSPENDED",
                    "Revoke while auth transaction runs",
                )
                _wait_for_organization_waiters(
                    blocker, 1, "carepulse_operator", "operator_change_clinic_state"
                )
            finally:
                blocker.commit()
        assert suspended.result(timeout=25)["state"] == "SUSPENDED"
        assert reset.result(timeout=25)[0] == 409

    with _database() as connection:
        durable = connection.execute(
            """
            SELECT r.state, r.invalidated, r.auth_fence,
                   s.reset_block_operation_id,
                   (SELECT count(*) FROM public.auth_recovery_completions c WHERE c.id=r.id),
                   (SELECT count(*) FROM public.sensitive_audit a
                     WHERE a.recovery_operation_id=r.id AND a.action='PASSWORD_RESET_SUCCEEDED'),
                   (SELECT count(*) FROM public."session" ss WHERE ss."userId"=%s)
              FROM public.recovery_operations r
              JOIN public.staff_accounts s ON s.id=r.staff_id
             WHERE r.proof_digest=%s
            """,
            (owner.user, digest),
        ).fetchone()
        credential_after = connection.execute(
            'SELECT password FROM public."account" WHERE "userId"=%s',
            (owner.user,),
        ).fetchone()[0]
    assert durable == ("AUTH_DONE", True, 1, operation[0], 1, 0, 0)
    assert credential_after != credential_before
    assert _request("/api/session", cookie=stale_cookie)[0] == 401


def test_two_distinct_proofs_cannot_replace_unresolved_native_reset(recovery_owner):
    owner = recovery_owner
    stale_cookie = _login(owner)
    proof_a, digest_a = _issued_proof(owner)
    proof_b, digest_b = _issued_proof(owner)
    password_a = f"Synthetic-First-Proof-{uuid4()}!"
    password_b = f"Synthetic-Second-Proof-{uuid4()}!"
    with (
        _reject_commit_for(owner.clinic, "sensitive_audit"),
        ThreadPoolExecutor(max_workers=2) as executor,
    ):
        with _database() as blocker:
            blocker.execute('LOCK TABLE public."session" IN SHARE MODE')
            first = executor.submit(_reset, proof_a, password_a)
            try:
                _wait_for_native_session_delete(blocker)
                second = executor.submit(_reset, proof_b, password_b)
                _wait_for_organization_waiters(blocker, 1)
            finally:
                blocker.commit()
        first_status, _ = first.result(timeout=25)
        second_status, second_response = second.result(timeout=25)
    assert first_status == 503
    assert second_status == 409
    assert second_response["error"]["code"] == "RESET_IN_PROGRESS"

    with _database() as connection:
        operations = connection.execute(
            """SELECT r.proof_digest, r.id, r.state, r.auth_fence,
                      (SELECT count(*) FROM public.auth_recovery_completions c WHERE c.id=r.id),
                      (SELECT count(*) FROM public.sensitive_audit a
                         WHERE a.recovery_operation_id=r.id
                           AND a.action='PASSWORD_RESET_SUCCEEDED')
                 FROM public.recovery_operations r WHERE r.organization_id=%s
                   AND r.proof_digest IN (%s,%s)""",
            (owner.clinic, digest_a, digest_b),
        ).fetchall()
        native_proof_b = connection.execute(
            """SELECT count(*) FROM public."verification" v
                 JOIN public.delivery_jobs j ON j.verification_id=v.id
                WHERE j.organization_id=%s AND j.proof_reference=%s""",
            (owner.clinic, digest_b),
        ).fetchone()[0]
        block, revision = connection.execute(
            "SELECT reset_block_operation_id, security_revision "
            "FROM public.staff_accounts WHERE id=%s",
            (owner.staff,),
        ).fetchone()
    assert len(operations) == 1 and operations[0][0] == digest_a
    assert operations[0][2:] == ("AUTH_DONE", 1, 1, 0)
    assert native_proof_b == 1 and block == operations[0][1] and revision == 1
    assert _request("/api/session", cookie=stale_cookie)[0] == 401
    assert _reset(proof_a, password_a) == (200, {"data": {"success": True}})


def test_duplicate_auth_done_finalizers_keep_one_success_and_revision(recovery_owner):
    owner = recovery_owner
    token, digest = _issued_proof(owner)
    password = f"Synthetic-Finalizer-{uuid4()}!"
    # The supported native effect commits; only the later Python success audit fails.
    with _reject_commit_for(owner.clinic, "sensitive_audit"):
        assert _reset(token, password)[0] == 503
    with _database() as connection:
        before = connection.execute(
            """SELECT r.id, r.state, r.auth_fence, s.security_revision,
                      (SELECT count(*) FROM public.auth_recovery_completions c
                        WHERE c.id=r.id)
                 FROM public.recovery_operations r
                 JOIN public.staff_accounts s ON s.id=r.staff_id
                WHERE r.proof_digest=%s""",
            (digest,),
        ).fetchone()
    assert before[1:] == ("AUTH_DONE", 1, 1, 1)

    ready = Barrier(3)

    def finalize():
        ready.wait(timeout=6)
        return _reset(token, password)

    with ThreadPoolExecutor(max_workers=2) as executor:
        with _database() as blocker:
            blocker.execute(
                "SELECT id FROM public.organizations WHERE id=%s FOR UPDATE",
                (owner.clinic,),
            )
            futures = [executor.submit(finalize) for _ in range(2)]
            ready.wait(timeout=6)
            try:
                _wait_for_organization_waiters(blocker, 2)
            finally:
                blocker.commit()
        assert [future.result(timeout=25)[0] for future in futures] == [200, 200]
    with _database() as connection:
        after = connection.execute(
            """SELECT r.state, r.auth_fence, s.security_revision, s.reset_block_operation_id,
                      (SELECT count(*) FROM public.auth_recovery_completions c
                        WHERE c.id=r.id),
                      (SELECT count(*) FROM public.sensitive_audit a
                        WHERE a.recovery_operation_id=r.id
                          AND a.action='PASSWORD_RESET_SUCCEEDED')
                 FROM public.recovery_operations r
                 JOIN public.staff_accounts s ON s.id=r.staff_id
                WHERE r.id=%s""",
            (before[0],),
        ).fetchone()
    assert after == ("FINALIZED", 1, 1, None, 1, 1)


@pytest.mark.parametrize("first_winner", ["operator", "replacement"])
def test_replacement_competes_with_suspension_and_stale_finalizer(
    recovery_owner, request, first_winner
):
    owner = recovery_owner
    _, operator_engine, actor, secret = request.getfixturevalue("operator_identity")
    verified = authenticate_operator(operator_engine, actor, secret, _load_policy())
    proof_a, _ = _issued_proof(owner)
    password_a = f"Synthetic-Revoked-A-{uuid4()}!"
    with _reject_commit_for(owner.clinic, "sensitive_audit"):
        assert _reset(proof_a, password_a)[0] == 503
    before = _operation(owner, proof_a)
    assert before["state"] == "AUTH_DONE" and before["completions"] == 1
    with _database() as connection:
        lifecycle = connection.execute(
            "SELECT lifecycle_revision FROM public.organizations WHERE id=%s",
            (owner.clinic,),
        ).fetchone()[0]
    assert (
        change_clinic_state(
            operator_engine,
            verified,
            owner.clinic,
            lifecycle,
            "SUSPENDED",
            "Invalidate A's native completion",
        )["state"]
        == "SUSPENDED"
    )
    assert (
        change_clinic_state(
            operator_engine,
            verified,
            owner.clinic,
            lifecycle + 1,
            "ACTIVE",
            "Allow a fresh verified recovery proof",
        )["state"]
        == "ACTIVE"
    )
    proof_b, _ = _issued_proof(owner)
    password_b = f"Synthetic-Replacement-B-{uuid4()}!"
    completed_a_hash = _native(owner)[0]

    with ThreadPoolExecutor(max_workers=3) as executor:
        with _database() as blocker:
            if first_winner == "operator":
                blocker.execute(
                    "SELECT id FROM public.organizations WHERE id=%s FOR UPDATE",
                    (owner.clinic,),
                )
                suspension = executor.submit(
                    change_clinic_state,
                    operator_engine,
                    verified,
                    owner.clinic,
                    lifecycle + 2,
                    "SUSPENDED",
                    "Operator wins new proof transfer",
                )
                _wait_for_organization_waiters(
                    blocker, 1, "carepulse_operator", "operator_change_clinic_state"
                )
                replacement = executor.submit(_reset, proof_b, password_b)
                stale_a = executor.submit(_reset, proof_a, password_a)
                _wait_for_organization_waiters(blocker, 2)
            else:
                blocker.execute('LOCK TABLE public."session" IN SHARE MODE')
                replacement = executor.submit(_reset, proof_b, password_b)
                _wait_for_native_session_delete(blocker)
                suspension = executor.submit(
                    change_clinic_state,
                    operator_engine,
                    verified,
                    owner.clinic,
                    lifecycle + 2,
                    "SUSPENDED",
                    "Revoke after B native fence",
                )
                stale_a = executor.submit(_reset, proof_a, password_a)
                _wait_for_organization_waiters(
                    blocker, 1, "carepulse_operator", "operator_change_clinic_state"
                )
                _wait_for_organization_waiters(blocker, 1)
            blocker.commit()
        assert suspension.result(timeout=25)["state"] == "SUSPENDED"
        assert replacement.result(timeout=25)[0] in (403, 409)
        assert stale_a.result(timeout=25)[0] in (403, 409)

    previous, current = _operation(owner, proof_a), _operation(owner, proof_b)
    assert previous["completions"] == 1 and previous["successes"] == 0
    assert previous["invalidated"]
    assert previous["auth_fence"] == 1
    if first_winner == "operator":
        assert current is None and previous["state"] == "AUTH_DONE"
        assert previous["block"] == previous["id"]
        assert previous["security_revision"] == before["security_revision"] == 1
        assert _native(owner) == (completed_a_hash, 0)
    else:
        assert previous["state"] == "ABORTED"
        assert current["state"] == "AUTH_DONE" and current["invalidated"]
        assert current["auth_fence"] == 1
        assert current["completions"] == 1 and current["successes"] == 0
        assert current["block"] == current["id"]
        assert current["security_revision"] == before["security_revision"] + 1
        assert _native(owner)[0] != completed_a_hash
        assert _native(owner)[1] == 0


def test_expired_fresh_proof_cannot_abort_an_expired_blocked_predecessor(
    recovery_owner,
):
    owner = recovery_owner
    proof_a, _ = _issued_proof(owner)
    password_a = f"Synthetic-Abandoned-A-{uuid4()}!"
    with _reject_commit_for(owner.clinic, "auth_recovery_completions"):
        assert _reset(proof_a, password_a)[0] == 503
    _expire_reserved_attempt(owner, proof_a)
    before = _operation(owner, proof_a)
    native_before = _native(owner)
    proof_b, digest_b = _issued_proof(owner, lifetime_seconds=5)
    with _database() as connection:
        deadline = connection.execute(
            "SELECT proof_expires_at FROM public.delivery_jobs WHERE proof_reference=%s",
            (digest_b,),
        ).fetchone()[0]
        connection.execute(
            "SELECT pg_sleep(GREATEST(0, EXTRACT(EPOCH FROM (%s::timestamptz - clock_timestamp())) + 0.05))",
            (deadline,),
        )
        assert connection.execute(
            "SELECT clock_timestamp() >= %s", (deadline,)
        ).fetchone()[0]
    status, _ = _reset(proof_b, f"Synthetic-Expired-B-{uuid4()}!")
    assert status == 409
    after = _operation(owner, proof_a)
    assert after["state"] == "RESERVED"
    assert after["block"] == before["id"]
    assert after["security_revision"] == before["security_revision"] == 1
    assert after["completions"] == after["successes"] == 0
    assert _operation(owner, proof_b) is None
    assert _native(owner) == native_before

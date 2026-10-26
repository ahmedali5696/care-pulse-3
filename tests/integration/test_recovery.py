"""B-04 real Owner recovery across the public BFF, PostgreSQL, sender and Mailpit.

CI invokes this suite in a disposable *sender test invocation* with read-only
bootstrap test credentials. The running sender and API services remain isolated.
"""

import json
import os
import re
from contextlib import contextmanager
from hashlib import sha256
from pathlib import Path
from types import SimpleNamespace
from unicodedata import normalize
from urllib.parse import parse_qs, urlsplit
from urllib.request import urlopen
from uuid import uuid4

import pytest
from carepulse.config import _load_policy
from carepulse.notifications.email_sender import _settings, send_one_job
from carepulse.provisioning.operators import VerifiedOperator
from carepulse.provisioning.service import change_clinic_state
from psycopg.rows import dict_row
from sqlalchemy import URL, create_engine
from test_sessions import (
    _database,
    _login,
    _mutation_headers,
    _password_hash,
    _request,
    _seed_owner,
)


@pytest.fixture(scope="module")
def sender_lab(disposable_ci):
    if os.getenv("CI") != "true":
        pytest.skip("Real recovery acceptance requires disposable Compose CI")
    assert Path("/run/secrets/postgres_password").is_file()
    assert Path(os.environ["CP_DELIVERY_PRIVATE_KEY_FILE"]).is_file()
    assert os.environ["CP_DB_USER"] == "carepulse_sender"
    sender = _settings()
    with urlopen("http://mail:8025/api/v1/messages", timeout=6) as response:
        assert response.status == 200
    return sender


@pytest.fixture
def owner(sender_lab):
    with _database() as connection:
        # The CI ingress source is shared across otherwise independent owners.
        connection.execute(
            "DELETE FROM public.auth_login_attempts WHERE dimension='source'"
        )
        return _seed_owner(connection)


def _post(path, payload):
    status, headers, content = _request(
        path,
        method="POST",
        payload=payload,
        headers=_mutation_headers(),
    )
    assert headers["Cache-Control"] == "no-store"
    assert headers.get("Set-Cookie") is None
    assert b"password" not in content.lower() and b"proof" not in content.lower()
    return status, json.loads(content)


def _issue(owner, sender_lab):
    status, response = _post("/api/auth/reset-request", {"email": owner.email})
    assert status == 200, response
    assert response == {"data": {"acknowledged": True}}
    with _database() as connection:
        jobs = connection.execute(
            """SELECT id, proof_reference, verification_id, state
                 FROM public.delivery_jobs
                WHERE organization_id=%s AND recipient=%s AND purpose='PASSWORD_RESET'
                ORDER BY created_at DESC, id DESC""",
            (owner.clinic, owner.email),
        ).fetchall()
    assert jobs and jobs[0][3] == "READY"
    job, digest, verification_id, _ = jobs[0]
    assert send_one_job(owner.clinic, job, sender_lab) == "SENT"
    token = _mail_token_for_job(owner.email, digest)
    assert sha256(token.encode()).hexdigest() == digest
    assert len(token) == 24
    with _database() as connection:
        assert (
            connection.execute(
                "SELECT count(*) FROM public.verification WHERE id=%s AND value=%s",
                (verification_id, owner.user),
            ).fetchone()[0]
            == 1
        )
    return token


def _mail_token_for_job(recipient, digest):
    """Select the new message when an Owner has multiple valid recovery proofs."""
    with urlopen("http://mail:8025/api/v1/messages", timeout=6) as response:
        messages = json.load(response)["messages"]
    matching = []
    for entry in messages:
        if not any(target["Address"] == recipient for target in entry["To"]):
            continue
        with urlopen(
            f"http://mail:8025/api/v1/message/{entry['ID']}", timeout=6
        ) as response:
            delivered = json.load(response)
        assert delivered["Subject"] == "[SYNTHETIC ONLY] CarePulse reset"
        link = re.search(r"https?://[^\s<>]+/auth/reset#[^\s<>]+", delivered["Text"])
        if link:
            proof = parse_qs(urlsplit(link.group()).fragment)["proof"][0]
            if sha256(proof.encode()).hexdigest() == digest:
                matching.append(proof)
    assert len(matching) == 1
    return matching[0]


def _complete(token, password):
    return _post("/api/auth/reset-complete", {"token": token, "newPassword": password})


def _operation(owner, token):
    with _database() as connection, connection.cursor(row_factory=dict_row) as cursor:
        cursor.execute(
            """SELECT r.id, r.state, r.invalidated, r.invalidation_reference,
                      r.proof_expires_at, r.auth_done_at, r.closed_at, r.auth_fence,
                      r.reserved_security_revision, s.security_revision,
                      s.reset_block_operation_id AS block,
                      (SELECT count(*) FROM public.auth_recovery_completions c
                        WHERE c.id=r.id) AS completions,
                      (SELECT count(*) FROM public.sensitive_audit a
                        WHERE a.recovery_operation_id=r.id
                          AND a.action='PASSWORD_RESET_SUCCEEDED') AS successes
                 FROM public.recovery_operations r
                 JOIN public.staff_accounts s ON s.id=r.staff_id
                WHERE r.organization_id=%s AND r.auth_user_id=%s
                  AND r.proof_digest=%s""",
            (owner.clinic, owner.user, sha256(token.encode()).hexdigest()),
        )
        return cursor.fetchone()


def _native(owner):
    with _database() as connection:
        return connection.execute(
            """SELECT a.password, (SELECT count(*) FROM public."session"
                                      WHERE "userId"=%s)
                 FROM public."account" a
                WHERE a."userId"=%s AND a."providerId"='credential'""",
            (owner.user, owner.user),
        ).fetchone()


@contextmanager
def _reject_commit_for(tenant, table):
    """Inject a PostgreSQL write failure after real native effects, not a mock."""
    name = f"recovery_fault_{uuid4().hex}"
    assert table in ("auth_recovery_completions", "sensitive_audit")
    condition = (
        f"NEW.organization_id='{tenant}'::uuid AND NEW.action='PASSWORD_RESET_SUCCEEDED'"
        if table == "sensitive_audit"
        else "EXISTS (SELECT 1 FROM public.recovery_operations r "
        f"WHERE r.id=NEW.id AND r.organization_id='{tenant}'::uuid)"
    )
    with _database() as connection:
        connection.execute(f"""
            CREATE FUNCTION public.{name}() RETURNS trigger
            LANGUAGE plpgsql AS $$
            BEGIN
                IF {condition} THEN
                    RAISE EXCEPTION 'synthetic reset commit failure' USING ERRCODE='P0001';
                END IF;
                RETURN NEW;
            END $$""")
        connection.execute(
            f"CREATE TRIGGER {name} BEFORE INSERT ON public.{table} "
            f"FOR EACH ROW EXECUTE FUNCTION public.{name}()"
        )
    try:
        yield
    finally:
        with _database() as connection:
            connection.execute(f"DROP TRIGGER {name} ON public.{table}")
            connection.execute(f"DROP FUNCTION public.{name}()")


@contextmanager
def _reject_issuance_write_for(tenant, table):
    """Reject one real issuance write so its entire native transaction rolls back."""
    assert table in ("delivery_jobs", "protected_delivery_material", "sensitive_audit")
    marker = f"issuance_failure_{uuid4().hex}"
    if table == "protected_delivery_material":
        condition = (
            "EXISTS (SELECT 1 FROM public.delivery_jobs j "
            f"WHERE j.id=NEW.job_id AND j.organization_id='{tenant}'::uuid "
            "AND j.purpose='PASSWORD_RESET')"
        )
    else:
        condition = f"NEW.organization_id='{tenant}'::uuid"
        if table == "sensitive_audit":
            condition += " AND NEW.action='PASSWORD_RESET_PROOF_ISSUED'"
        else:
            condition += " AND NEW.purpose='PASSWORD_RESET'"
    with _database() as connection:
        connection.execute(f"""
            CREATE FUNCTION public.{marker}() RETURNS trigger
            LANGUAGE plpgsql AS $$
            BEGIN
                IF {condition} THEN
                    RAISE EXCEPTION 'synthetic issuance rollback' USING ERRCODE='P0001';
                END IF;
                RETURN NEW;
            END $$""")
        connection.execute(
            f"CREATE TRIGGER {marker} BEFORE INSERT ON public.{table} "
            f"FOR EACH ROW EXECUTE FUNCTION public.{marker}()"
        )
    try:
        yield
    finally:
        with _database() as connection:
            connection.execute(f"DROP TRIGGER {marker} ON public.{table}")
            connection.execute(f"DROP FUNCTION public.{marker}()")


@pytest.mark.parametrize(
    "table", ["delivery_jobs", "protected_delivery_material", "sensitive_audit"]
)
def test_issuance_write_failure_keeps_native_proof_and_delivery_atomic(
    owner, sender_lab, table
):
    with _reject_issuance_write_for(owner.clinic, table):
        eligible = _post("/api/auth/reset-request", {"email": owner.email})
        unknown = _post(
            "/api/auth/reset-request", {"email": f"{uuid4()}@example.invalid"}
        )
        assert eligible == unknown == (200, {"data": {"acknowledged": True}})
    with _database() as connection:
        verification_count = connection.execute(
            "SELECT count(*) FROM public.verification WHERE value=%s",
            (owner.user,),
        ).fetchone()[0]
        delivery_count = connection.execute(
            "SELECT count(*) FROM public.delivery_jobs "
            "WHERE organization_id=%s AND purpose='PASSWORD_RESET'",
            (owner.clinic,),
        ).fetchone()[0]
        material_count = connection.execute(
            "SELECT count(*) FROM public.protected_delivery_material m "
            "JOIN public.delivery_jobs j ON j.id=m.job_id "
            "WHERE j.organization_id=%s AND j.purpose='PASSWORD_RESET'",
            (owner.clinic,),
        ).fetchone()[0]
        issuance_audit_count = connection.execute(
            "SELECT count(*) FROM public.sensitive_audit "
            "WHERE organization_id=%s AND action='PASSWORD_RESET_PROOF_ISSUED'",
            (owner.clinic,),
        ).fetchone()[0]
    assert (
        verification_count,
        delivery_count,
        material_count,
        issuance_audit_count,
    ) == (0, 0, 0, 0)
    # Positive control: removing the injected guard restores the actual
    # Better Auth -> encrypted material -> sender/Mailpit issuance journey.
    assert _issue(owner, sender_lab)


def test_recovery_request_source_limit_does_not_reveal_registered_owner(owner):
    unknown = f"missing-{uuid4()}@example.invalid"
    limit = _load_policy().recovery_max_attempts
    for _ in range(limit):
        assert _post("/api/auth/reset-request", {"email": unknown}) == (
            200,
            {"data": {"acknowledged": True}},
        )
    assert _post("/api/auth/reset-request", {"email": unknown})[0] == 429
    # A known Owner must not bypass the source fence or reveal eligibility.
    assert _post("/api/auth/reset-request", {"email": owner.email})[0] == 429
    with _database() as connection:
        assert (
            connection.execute(
                "SELECT count(*) FROM public.verification WHERE value=%s",
                (owner.user,),
            ).fetchone()[0]
            == 0
        )


@pytest.mark.parametrize(
    "path", ["/api/auth/reset-request", "/api/auth/reset-complete"]
)
@pytest.mark.parametrize("invalid_header", ["origin", "xhr", "fetch_site"])
def test_recovery_mutations_reject_foreign_origin_or_missing_csrf(
    owner, path, invalid_header
):
    headers = _mutation_headers()
    if invalid_header == "origin":
        headers["Origin"] = "https://attacker.example.invalid"
    elif invalid_header == "xhr":
        headers.pop("X-Requested-With")
    else:
        headers["Sec-Fetch-Site"] = "cross-site"
    payload = (
        {"email": owner.email}
        if path.endswith("reset-request")
        else {"token": "A" * 24, "newPassword": "Synthetic-CSRF-Only!"}
    )
    status, _, response = _request(
        path, method="POST", payload=payload, headers=headers
    )
    assert status == 403
    assert json.loads(response)["error"]["code"] == "FORBIDDEN"
    with _database() as connection:
        assert (
            connection.execute(
                "SELECT count(*) FROM public.verification WHERE value=%s",
                (owner.user,),
            ).fetchone()[0]
            == 0
        )


def test_recovery_ineligible_verified_native_user_gets_unknown_ack(owner):
    other = f"synthetic-nonowner-{uuid4()}"
    address = f"{uuid4()}@example.invalid"
    with _database() as connection:
        connection.execute(
            'INSERT INTO public."user" (id, name, email, "emailVerified", "createdAt", "updatedAt") '
            "VALUES (%s, %s, %s, true, clock_timestamp(), clock_timestamp())",
            (other, "Synthetic non-owner", address),
        )
        connection.execute(
            'INSERT INTO public."account" '
            '(id, "accountId", "providerId", "userId", password, "createdAt", "updatedAt") '
            "VALUES (%s, %s, 'credential', %s, %s, clock_timestamp(), clock_timestamp())",
            (
                f"synthetic-nonowner-account-{uuid4()}",
                other,
                other,
                _password_hash(f"Synthetic-Nonowner-{uuid4()}!"),
            ),
        )
    expected = (200, {"data": {"acknowledged": True}})
    assert _post("/api/auth/reset-request", {"email": address}) == expected
    assert (
        _post("/api/auth/reset-request", {"email": f"{uuid4()}@example.invalid"})
        == expected
    )
    with _database() as connection:
        assert (
            connection.execute(
                "SELECT count(*) FROM public.verification WHERE value=%s", (other,)
            ).fetchone()[0]
            == 0
        )
        assert (
            connection.execute(
                "SELECT count(*) FROM public.delivery_jobs WHERE recipient=%s",
                (address,),
            ).fetchone()[0]
            == 0
        )


def test_invalid_password_denies_before_proof_reservation(owner, sender_lab):
    token = _issue(owner, sender_lab)
    before = _native(owner)
    assert _complete(token, "short")[0] == 422
    assert _operation(owner, token) is None
    assert _native(owner) == before
    assert _complete(token, f"Synthetic-Valid-{uuid4()}!") == (
        200,
        {"data": {"success": True}},
    )


def test_same_proof_distinguishes_exact_unicode_password_bytes(owner, sender_lab):
    token = _issue(owner, sender_lab)
    composed = f"Synthetic-É-{uuid4()}!"
    decomposed = normalize("NFD", composed)
    assert composed != decomposed and composed.encode() != decomposed.encode()
    with _reject_commit_for(owner.clinic, "auth_recovery_completions"):
        assert _complete(token, composed)[0] == 503
    assert _complete(token, decomposed)[1]["error"]["code"] == "IDEMPOTENCY_CONFLICT"
    pending = _operation(owner, token)
    assert pending["state"] == "RESERVED" and pending["auth_fence"] == 0
    assert pending["completions"] == pending["successes"] == 0
    assert _complete(token, composed) == (200, {"data": {"success": True}})
    assert _operation(owner, token)["successes"] == 1


def test_missing_retained_fingerprint_version_keeps_reset_owned_block(
    owner, sender_lab
):
    token = _issue(owner, sender_lab)
    password = f"Synthetic-Unavailable-Key-{uuid4()}!"
    with _reject_commit_for(owner.clinic, "auth_recovery_completions"):
        assert _complete(token, password)[0] == 503
    before = _operation(owner, token)
    original_native = _native(owner)
    assert before["state"] == "RESERVED" and before["block"] == before["id"]

    # Synthetic migration fixture models a prior key version no longer mounted
    # in Web. This mutation is impossible for normal runtime roles.
    with _database() as connection:
        connection.execute(
            "ALTER TABLE public.recovery_operations DISABLE TRIGGER recovery_operations_mutation_guard"
        )
        try:
            connection.execute(
                """UPDATE public.recovery_operations
                      SET fingerprint_key_version='retired-test-key-unavailable'
                    WHERE id=%s AND state='RESERVED'""",
                (before["id"],),
            )
            connection.execute("SET CONSTRAINTS ALL IMMEDIATE")
        finally:
            connection.execute(
                "ALTER TABLE public.recovery_operations ENABLE TRIGGER recovery_operations_mutation_guard"
            )
    assert _complete(token, password)[0] == 503
    after = _operation(owner, token)
    assert after["state"] == "RESERVED" and after["block"] == before["id"]
    assert after["security_revision"] == before["security_revision"]
    assert after["auth_fence"] == after["completions"] == after["successes"] == 0
    assert _native(owner) == original_native


def _expire_reserved_attempt(owner, token):
    """Advance persisted proof deadlines to the real database's past in disposable CI.

    Reservation's immutable expiry is guarded in production; the bootstrap
    fixture temporarily lifts only its mutation guard, then restores it.
    All runtime requests still use real PostgreSQL clock_timestamp().
    """
    operation = _operation(owner, token)
    assert operation and operation["state"] == "RESERVED"
    assert operation["completions"] == 0 and operation["auth_fence"] == 0
    with _database() as connection:
        connection.execute(
            "ALTER TABLE public.recovery_operations DISABLE TRIGGER recovery_operations_mutation_guard"
        )
        try:
            connection.execute(
                """UPDATE public.recovery_operations
                      SET created_at=clock_timestamp()-interval '2 hours',
                          proof_expires_at=clock_timestamp()-interval '1 hour'
                    WHERE id=%s AND state='RESERVED' AND auth_fence=0""",
                (operation["id"],),
            )
            # Deferred recovery/FK checks must drain before trigger DDL resumes.
            connection.execute("SET CONSTRAINTS ALL IMMEDIATE")
        finally:
            connection.execute(
                "ALTER TABLE public.recovery_operations ENABLE TRIGGER recovery_operations_mutation_guard"
            )


def test_delivered_owner_reset_revokes_native_sessions_and_replay_is_idempotent(
    owner, sender_lab
):
    stale = _login(owner)
    token = _issue(owner, sender_lab)
    new_password = f"Synthetic-Recovery-{uuid4()}!"
    assert _complete(token, new_password) == (200, {"data": {"success": True}})
    operation = _operation(owner, token)
    assert operation["state"] == "FINALIZED"
    assert operation["completions"] == operation["successes"] == 1
    assert operation["block"] is None
    assert (
        operation["security_revision"] == operation["reserved_security_revision"] == 1
    )
    assert _native(owner)[1] == 0
    assert _request("/api/session", cookie=stale)[0] == 401
    assert _complete(token, new_password) == (200, {"data": {"success": True}})
    assert _complete(token, f"Different-{uuid4()}!")[0] == 409
    assert _operation(owner, token)["successes"] == 1
    assert (
        _request(
            "/api/auth/login",
            method="POST",
            payload={"email": owner.email, "password": owner.password},
            headers=_mutation_headers(),
        )[0]
        == 401
    )
    with _database() as connection:
        connection.execute(
            "DELETE FROM public.auth_login_attempts WHERE dimension='source'"
        )
    assert _login(SimpleNamespace(email=owner.email, password=new_password))


def test_invalid_and_strictly_expired_proof_never_reserves(owner, sender_lab):
    assert _complete("A" * 24, f"Synthetic-Invalid-{uuid4()}!")[0] == 409
    unknown = _post("/api/auth/reset-request", {"email": f"{uuid4()}@example.invalid"})
    assert unknown == (200, {"data": {"acknowledged": True}})
    token = _issue(owner, sender_lab)
    with _database() as connection:
        connection.execute(
            """UPDATE public.verification SET "expiresAt"=clock_timestamp()
                 WHERE value=%s AND id=(
                   SELECT verification_id FROM public.delivery_jobs
                    WHERE organization_id=%s AND proof_reference=%s)""",
            (owner.user, owner.clinic, sha256(token.encode()).hexdigest()),
        )
    assert _complete(token, f"Synthetic-Expired-{uuid4()}!")[0] == 409
    assert _operation(owner, token) is None
    with _database() as connection:
        assert connection.execute(
            "SELECT security_revision, reset_block_operation_id FROM public.staff_accounts WHERE id=%s",
            (owner.staff,),
        ).fetchone() == (0, None)


def test_native_completion_write_failure_rolls_back_credential_and_consumption(
    owner, sender_lab
):
    stale_cookie = _login(owner)
    token = _issue(owner, sender_lab)
    original = _native(owner)
    assert original[1] == 1
    new_password = f"Synthetic-Rollback-{uuid4()}!"
    with _reject_commit_for(owner.clinic, "auth_recovery_completions"):
        assert _complete(token, new_password)[0] == 503
    attempt = _operation(owner, token)
    assert attempt["state"] == "RESERVED"
    assert attempt["auth_fence"] == attempt["completions"] == 0
    assert attempt["block"] == attempt["id"] and attempt["security_revision"] == 1
    assert _native(owner) == original
    assert _request("/api/session", cookie=stale_cookie)[0] == 401
    assert _complete(token, new_password) == (200, {"data": {"success": True}})
    assert _operation(owner, token)["completions"] == 1
    assert _native(owner)[1] == 0


def test_lost_finalization_ack_retries_consumed_proof_without_rechanging_password(
    owner, sender_lab
):
    token = _issue(owner, sender_lab)
    password = f"Synthetic-Retry-{uuid4()}!"
    with _reject_commit_for(owner.clinic, "sensitive_audit"):
        assert _complete(token, password)[0] == 503
    committed = _operation(owner, token)
    assert committed["state"] == "AUTH_DONE"
    assert committed["block"] == committed["id"]
    assert committed["completions"] == 1 and committed["successes"] == 0
    saved_hash = _native(owner)[0]
    assert _complete(token, password) == (200, {"data": {"success": True}})
    assert _native(owner)[0] == saved_hash
    assert _operation(owner, token)["successes"] == 1


def test_committed_auth_done_can_finalize_after_proof_expiry(owner, sender_lab):
    token = _issue(owner, sender_lab)
    password = f"Synthetic-Expired-Finalizer-{uuid4()}!"
    with _reject_commit_for(owner.clinic, "sensitive_audit"):
        assert _complete(token, password)[0] == 503
    before = _operation(owner, token)
    assert before["state"] == "AUTH_DONE" and before["completions"] == 1
    saved_native = _native(owner)

    # The native effect already committed; only the finalization deadline is
    # moved into the past in this disposable migration-owned fixture.
    with _database() as connection:
        connection.execute(
            "ALTER TABLE public.recovery_operations DISABLE TRIGGER recovery_operations_mutation_guard"
        )
        try:
            connection.execute(
                """UPDATE public.recovery_operations
                     SET proof_expires_at = auth_done_at + interval '1 microsecond'
                   WHERE id=%s AND state='AUTH_DONE' AND auth_done_at IS NOT NULL""",
                (before["id"],),
            )
            connection.execute("SET CONSTRAINTS ALL IMMEDIATE")
        finally:
            connection.execute(
                "ALTER TABLE public.recovery_operations ENABLE TRIGGER recovery_operations_mutation_guard"
            )

    assert _complete(token, password) == (200, {"data": {"success": True}})
    after = _operation(owner, token)
    assert after["state"] == "FINALIZED" and after["successes"] == 1
    assert after["completions"] == before["completions"] == 1
    assert after["auth_fence"] == before["auth_fence"] == 1
    assert after["security_revision"] == before["security_revision"] == 1
    assert after["block"] is None and _native(owner) == saved_native
    assert _complete(token, f"Synthetic-Other-{uuid4()}!")[0] == 409


def test_expired_incomplete_attempt_transfers_owned_block_only_with_new_proof(
    owner, sender_lab
):
    first = _issue(owner, sender_lab)
    original_hash = _native(owner)[0]
    with _reject_commit_for(owner.clinic, "auth_recovery_completions"):
        assert _complete(first, f"Synthetic-Abandoned-{uuid4()}!")[0] == 503
    blocked = _operation(owner, first)
    assert blocked["state"] == "RESERVED" and blocked["block"] == blocked["id"]
    second = _issue(owner, sender_lab)
    replacement_password = f"Synthetic-Replacement-{uuid4()}!"
    assert _complete(second, replacement_password)[0] == 409
    assert _operation(owner, second) is None
    assert _native(owner)[0] == original_hash
    _expire_reserved_attempt(owner, first)
    assert _complete("A" * 24, replacement_password)[0] == 409
    assert _operation(owner, first)["state"] == "RESERVED"
    with _reject_commit_for(owner.clinic, "auth_recovery_completions"):
        assert _complete(second, replacement_password)[0] == 503
    after_transfer = _operation(owner, second)
    assert after_transfer["state"] == "RESERVED"
    assert after_transfer["block"] == after_transfer["id"]
    assert after_transfer["security_revision"] == 2
    assert _operation(owner, first)["state"] == "ABORTED"
    assert _complete(second, replacement_password) == (200, {"data": {"success": True}})
    abandoned = _operation(owner, first)
    replaced = _operation(owner, second)
    assert abandoned["state"] == "ABORTED" and abandoned["invalidated"]
    assert abandoned["completions"] == abandoned["successes"] == 0
    assert replaced["state"] == "FINALIZED" and replaced["successes"] == 1
    assert replaced["security_revision"] == 2
    assert _complete(first, f"Synthetic-Abandoned-{uuid4()}!")[0] == 409


def test_expired_prior_verification_cleanup_does_not_block_replacement(
    owner, sender_lab
):
    first = _issue(owner, sender_lab)
    with _reject_commit_for(owner.clinic, "auth_recovery_completions"):
        assert _complete(first, f"Synthetic-Expired-A-{uuid4()}!")[0] == 503
    _expire_reserved_attempt(owner, first)

    second = _issue(owner, sender_lab)
    next_password = f"Synthetic-Cleanup-B-{uuid4()}!"
    with _reject_commit_for(owner.clinic, "auth_recovery_completions"):
        assert _complete(second, next_password)[0] == 503
    assert _operation(owner, first)["state"] == "ABORTED"
    current = _operation(owner, second)
    assert current["state"] == "RESERVED" and current["block"] == current["id"]

    with _database() as connection:
        old_verification = connection.execute(
            """SELECT verification_id FROM public.delivery_jobs
                WHERE organization_id=%s AND purpose='PASSWORD_RESET'
                  AND proof_reference=%s""",
            (owner.clinic, sha256(first.encode()).hexdigest()),
        ).fetchone()[0]
        connection.execute(
            """UPDATE public.verification
                  SET "expiresAt"=clock_timestamp()-interval '1 second'
                WHERE id=%s""",
            (old_verification,),
        )
        connection.execute(
            "SELECT set_config('app.organization_id', %s, true)",
            (str(owner.clinic),),
        )
        # Disposable bootstrap fixture executes with the actual auth session
        # identity; the running Web service never receives bootstrap credentials.
        connection.execute("SET SESSION AUTHORIZATION carepulse_auth")
        assert (
            connection.execute("SELECT session_user").fetchone()[0] == "carepulse_auth"
        )
        removed = connection.execute(
            'DELETE FROM public."verification" WHERE id=%s',
            (old_verification,),
        )
        assert removed.rowcount == 1

    assert _operation(owner, second)["state"] == "RESERVED"
    assert _complete(second, next_password) == (200, {"data": {"success": True}})
    assert _operation(owner, second)["state"] == "FINALIZED"
    assert _operation(owner, second)["successes"] == 1


def test_revocation_invalidated_auth_done_requires_distinct_proof_after_reactivation(
    owner, sender_lab
):
    first = _issue(owner, sender_lab)
    first_password = f"Synthetic-Revoked-{uuid4()}!"
    with _reject_commit_for(owner.clinic, "sensitive_audit"):
        assert _complete(first, first_password)[0] == 503
    prior = _operation(owner, first)
    assert prior["state"] == "AUTH_DONE" and prior["completions"] == 1
    with _database() as connection:
        actor, lifecycle = connection.execute(
            "SELECT created_by_operator_id, lifecycle_revision FROM public.organizations WHERE id=%s",
            (owner.clinic,),
        ).fetchone()
        secret = f"synthetic-operator-{uuid4()}".encode()
        digest = sha256(secret).hexdigest()
        connection.execute(
            "UPDATE public.operator_identities SET credential_digest=%s WHERE id=%s",
            (digest, actor),
        )
    engine = create_engine(
        URL.create(
            "postgresql+psycopg",
            username="carepulse_bootstrap",
            password=Path("/run/secrets/postgres_password").read_text().strip(),
            host="db",
            database="carepulse",
        ),
        hide_parameters=True,
    )
    try:
        operator = VerifiedOperator(actor, digest)
        suspended = change_clinic_state(
            engine,
            operator,
            owner.clinic,
            lifecycle,
            "SUSPENDED",
            "Recovery revocation",
        )
        assert suspended["revision"] == lifecycle + 1
        invalidated = _operation(owner, first)
        assert invalidated["state"] == "AUTH_DONE" and invalidated["invalidated"]
        assert invalidated["block"] == invalidated["id"]
        assert (
            invalidated["invalidation_reference"] == f"OPERATOR_SUSPEND:{lifecycle + 1}"
        )
        assert _complete(first, first_password)[0] in (409, 503)
        reactivated = change_clinic_state(
            engine,
            operator,
            owner.clinic,
            lifecycle + 1,
            "ACTIVE",
            "Owner recovery remains blocked",
        )
        assert reactivated["revision"] == lifecycle + 2
    finally:
        engine.dispose()
    second = _issue(owner, sender_lab)
    next_password = f"Synthetic-After-Reactivation-{uuid4()}!"
    with _reject_commit_for(owner.clinic, "auth_recovery_completions"):
        assert _complete(second, next_password)[0] == 503
    transferred = _operation(owner, second)
    old = _operation(owner, first)
    assert old["state"] == "ABORTED" and old["completions"] == 1
    assert old["successes"] == 0
    assert (
        transferred["state"] == "RESERVED" and transferred["block"] == transferred["id"]
    )
    assert transferred["security_revision"] == 2
    assert _complete(second, next_password) == (200, {"data": {"success": True}})
    old = _operation(owner, first)
    current = _operation(owner, second)
    assert old["state"] == "ABORTED" and old["completions"] == 1
    assert old["successes"] == 0 and old["invalidated"]
    assert current["state"] == "FINALIZED" and current["successes"] == 1
    assert current["security_revision"] == 2 and current["block"] is None
    assert _complete(first, first_password)[0] == 409
    assert _native(owner)[1] == 0

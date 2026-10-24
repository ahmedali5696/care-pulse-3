"""B-02 real Better Auth and BFF session checks in disposable CI."""

import json
import os
import re
import secrets
import unicodedata
from concurrent.futures import ThreadPoolExecutor
from hashlib import scrypt, sha256
from pathlib import Path
from time import monotonic, sleep
from types import SimpleNamespace
from urllib.error import HTTPError
from urllib.request import Request, urlopen
from uuid import UUID, uuid4

import psycopg
import pytest

_WEB = "http://web:3000"
_POLICY = json.loads(
    (Path(__file__).parents[1] / "fixtures/foundation-policy.json").read_text()
)


@pytest.fixture(scope="module", autouse=True)
def disposable_project():
    if os.getenv("CI") != "true":
        pytest.skip("Live Better Auth checks require disposable Compose CI")
    project = os.environ["CP_CI_PROJECT"]
    assert re.fullmatch(r"carepulse-ci-[0-9]+-[0-9]+", project)
    config = json.loads(Path(os.environ["CP_COMPOSE_CONFIG_PATH"]).read_text())
    assert config["name"] == project
    assert not config["services"]["db"].get("ports")
    assert not config["services"]["api"].get("ports")


def _database():
    return psycopg.connect(
        host="db",
        dbname="carepulse",
        user="carepulse_bootstrap",
        password=Path("/run/secrets/postgres_password").read_text().strip(),
    )


def _password_hash(password):
    # Same NFKC/scrypt encoding as installed @better-auth/utils 0.5.0 (Node export).
    salt = secrets.token_hex(16)
    digest = scrypt(
        unicodedata.normalize("NFKC", password).encode(),
        salt=salt.encode(),
        n=16384,
        r=16,
        p=1,
        dklen=64,
        maxmem=128 * 16384 * 16 * 2,
    )
    return f"{salt}:{digest.hex()}"


def _seed_owner(connection):
    operator, clinic, staff = uuid4(), uuid4(), uuid4()
    user = f"synthetic-b02-{uuid4()}"
    email = f"{uuid4()}@example.invalid"
    password = f"Synthetic-B02-{uuid4()}!"
    invitation = uuid4()
    connection.execute(
        """INSERT INTO public.operator_identities
               (id, credential_digest, issued_by_reference, issued_at)
             VALUES (%s, %s, 'synthetic-b02-ci', clock_timestamp())""",
        (operator, f"synthetic:{operator}"),
    )
    connection.execute(
        "INSERT INTO public.organizations (id, branch, created_by_operator_id) VALUES (%s, %s, %s)",
        (clinic, "Synthetic B-02 clinic", operator),
    )
    connection.execute(
        """INSERT INTO public."user" (id, name, email, "emailVerified", "createdAt", "updatedAt")
             VALUES (%s, 'Synthetic B-02 Owner', %s, true, clock_timestamp(), clock_timestamp())""",
        (user, email),
    )
    connection.execute(
        """INSERT INTO public."account"
               (id, "accountId", "providerId", "userId", password, "createdAt", "updatedAt")
             VALUES (%s, %s, 'credential', %s, %s, clock_timestamp(), clock_timestamp())""",
        (f"synthetic-b02-account-{uuid4()}", user, user, _password_hash(password)),
    )
    connection.execute(
        """INSERT INTO public.staff_accounts
               (id, organization_id, auth_user_id, verified_email, role, enabled, verified)
             VALUES (%s, %s, %s, %s, 'DOCTOR_OWNER', true, true)""",
        (staff, clinic, user, email),
    )
    connection.execute(
        """INSERT INTO public.invitations
               (id, organization_id, purpose, target_email, proof_digest,
                issued_by_operator_id, expires_at)
             VALUES (%s, %s, 'FIRST_OWNER', %s, %s, %s, clock_timestamp() + interval '1 hour')""",
        (
            invitation,
            clinic,
            email,
            sha256(str(invitation).encode()).hexdigest(),
            operator,
        ),
    )
    connection.execute(
        "UPDATE public.invitations SET state='ACCEPTED', accepted_by_user_id=%s WHERE id=%s",
        (user, invitation),
    )
    connection.execute(
        "UPDATE public.organizations SET state='ACTIVE' WHERE id=%s", (clinic,)
    )
    return SimpleNamespace(
        staff=staff, clinic=clinic, user=user, email=email, password=password
    )


@pytest.fixture
def owner(disposable_project):
    with _database() as connection:
        # Shared source limiter must not carry another test's attempts into this case.
        connection.execute(
            "DELETE FROM public.auth_login_attempts WHERE dimension='source'"
        )
        return _seed_owner(connection)


def _request(path, *, method="GET", payload=None, cookie=None, headers=None):
    supplied = dict(headers or {})
    if cookie:
        supplied["Cookie"] = cookie
    if payload is not None:
        supplied.setdefault("Content-Type", "application/json")
        encoded = json.dumps(payload).encode()
    else:
        encoded = b"" if method == "POST" else None
    try:
        response = urlopen(
            Request(_WEB + path, data=encoded, headers=supplied, method=method),
            timeout=6,
        )
    except HTTPError as error:
        response = error
    with response:
        return response.status, response.headers, response.read(8192)


def _mutation_headers():
    return {
        "Origin": _WEB,
        "X-Requested-With": "XMLHttpRequest",
        "Sec-Fetch-Site": "same-origin",
    }


def _login(owner):
    status, headers, body = _request(
        "/api/auth/login",
        method="POST",
        payload={"email": owner.email, "password": owner.password},
        headers=_mutation_headers(),
    )
    assert status == 200, body
    assert headers["Cache-Control"] == "no-store"
    assert b"token" not in body.lower() and b"password" not in body.lower()
    cookies = headers.get_all("Set-Cookie") or []
    native = next(
        header for header in cookies if header.startswith("better-auth.session_token=")
    )
    assert "httponly" in native.lower()
    assert "samesite=" in native.lower()
    assert "secure" not in native.lower()  # Synthetic CI runs over private HTTP.
    return native.split(";", 1)[0]


def _denied(response, status, code):
    actual, headers, body = response
    assert actual == status, body
    assert headers["Cache-Control"] == "no-store"
    error = json.loads(body)["error"]
    assert set(error) == {"code", "message", "request_id"}
    assert error["code"] == code
    assert UUID(error["request_id"]).version == 4


def _session_token(connection, user):
    return connection.execute(
        'SELECT id, token, "securityRevision" FROM public."session" WHERE "userId"=%s',
        (user,),
    ).fetchone()


def test_verified_login_session_and_logout_revoke_native_row(owner):
    cookie = _login(owner)
    status, headers, body = _request("/api/session", cookie=cookie)
    assert status == 200, body
    assert headers["Cache-Control"] == "no-store"
    assert json.loads(body)["data"] == {
        "id": str(owner.staff),
        "organization_id": str(owner.clinic),
        "role": "DOCTOR_OWNER",
        "bulk_reschedule": False,
        "revision": 0,
    }
    with _database() as connection:
        assert _session_token(connection, owner.user) is not None
    status, headers, body = _request(
        "/api/auth/logout",
        method="POST",
        cookie=cookie,
        headers=_mutation_headers(),
    )
    assert status == 200, body
    assert any("Max-Age=0" in header for header in headers.get_all("Set-Cookie") or [])
    with _database() as connection:
        assert _session_token(connection, owner.user) is None
    _denied(_request("/api/session", cookie=cookie), 401, "UNAUTHENTICATED")


def test_anonymous_session_and_event_stream_remain_closed(disposable_project):
    for path in ("/api/session", "/api/events/stream"):
        _denied(_request(path), 401, "UNAUTHENTICATED")


@pytest.mark.parametrize(
    "headers",
    [
        {},
        {"Origin": "http://attacker.invalid", "X-Requested-With": "XMLHttpRequest"},
        {"Origin": _WEB},
        {
            "Origin": _WEB,
            "X-Requested-With": "XMLHttpRequest",
            "Sec-Fetch-Site": "cross-site",
        },
    ],
)
def test_login_rejects_missing_or_cross_origin_csrf(owner, headers):
    _denied(
        _request(
            "/api/auth/login",
            method="POST",
            payload={"email": owner.email, "password": owner.password},
            headers=headers,
        ),
        403,
        "FORBIDDEN",
    )
    with _database() as connection:
        assert _session_token(connection, owner.user) is None


def test_browser_authority_headers_do_not_change_own_tenant(owner):
    with _database() as connection:
        foreign = _seed_owner(connection)
    cookie = _login(owner)
    status, _, body = _request(
        "/api/session",
        cookie=cookie,
        headers={
            "X-CarePulse-Organization-Id": str(foreign.clinic),
            "X-CarePulse-Staff-Id": str(foreign.staff),
            "X-CarePulse-Role": "RECEPTIONIST",
        },
    )
    assert status == 200, body
    assert json.loads(body)["data"]["organization_id"] == str(owner.clinic)
    _denied(
        _request(
            "/api/backend/__foreign_unlisted__",
            cookie=cookie,
            headers={
                "X-CarePulse-Organization-Id": str(foreign.clinic),
            },
        ),
        403,
        "FORBIDDEN",
    )


@pytest.mark.parametrize(
    "age_seconds,expected",
    [
        (_POLICY["session_idle_minutes"] * 60 - 60, 200),
        (_POLICY["session_idle_minutes"] * 60, 401),
        (_POLICY["session_idle_minutes"] * 60 + 60, 401),
    ],
)
def test_idle_deadline_is_strict(owner, age_seconds, expected):
    cookie = _login(owner)
    with _database() as connection:
        connection.execute(
            'UPDATE public."session" SET "lastActivity"=clock_timestamp()-(%s * interval \'1 second\') '
            'WHERE "userId"=%s',
            (age_seconds, owner.user),
        )
    response = _request("/api/session", cookie=cookie)
    if expected == 401:
        _denied(response, 401, "UNAUTHENTICATED")
    else:
        assert response[0] == 200, response[2]


@pytest.mark.parametrize("offset_seconds,expected", [(60, 200), (0, 401), (-60, 401)])
def test_absolute_deadline_is_strict_with_native_session(
    owner, offset_seconds, expected
):
    cookie = _login(owner)
    with _database() as connection:
        session_id, token, revision = _session_token(connection, owner.user)
        connection.execute('DELETE FROM public."session" WHERE id=%s', (session_id,))
        connection.execute(
            """INSERT INTO public."session"
                 (id, "userId", token, "expiresAt", "createdAt", "updatedAt",
                  "securityRevision", "absoluteDeadline", "lastActivity")
               VALUES (%s, %s, %s, clock_timestamp() + interval '1 hour',
                       clock_timestamp(), clock_timestamp(), %s,
                       clock_timestamp() + (%s * interval '1 second'), clock_timestamp())""",
            (session_id, owner.user, token, revision, offset_seconds),
        )
    response = _request("/api/session", cookie=cookie)
    if expected == 401:
        _denied(response, 401, "UNAUTHENTICATED")
    else:
        assert response[0] == 200, response[2]


def test_revoked_revision_blocks_existing_cookie(owner):
    cookie = _login(owner)
    with _database() as connection:
        connection.execute(
            "UPDATE public.staff_accounts SET security_revision=security_revision+1 WHERE id=%s",
            (owner.staff,),
        )
    _denied(_request("/api/session", cookie=cookie), 401, "UNAUTHENTICATED")
    _denied(_request("/api/events/stream", cookie=cookie), 401, "UNAUTHENTICATED")


def test_inflight_old_password_login_cannot_issue_stale_cookie(owner):
    with ThreadPoolExecutor(max_workers=1) as pool:
        with _database() as connection:
            connection.execute('LOCK TABLE public."session" IN SHARE MODE')
            attempt = pool.submit(
                _request,
                "/api/auth/login",
                method="POST",
                payload={"email": owner.email, "password": owner.password},
                headers=_mutation_headers(),
            )
            deadline = monotonic() + 3
            while monotonic() < deadline:
                waiting = connection.execute("""
                    SELECT EXISTS (
                      SELECT 1 FROM pg_locks
                       WHERE pid <> pg_backend_pid() AND NOT granted
                         AND relation = 'public."session"'::regclass
                         AND mode = 'RowExclusiveLock'
                    )
                """).fetchone()[0]
                if waiting:
                    break
                if attempt.done():
                    pytest.fail(
                        f"Sign-in finished before session INSERT lock: {attempt.result()}"
                    )
                sleep(0.025)
            else:
                pytest.fail("No in-flight session insertion observed")
            connection.execute(
                "UPDATE public.staff_accounts SET security_revision=security_revision+1 WHERE id=%s",
                (owner.staff,),
            )
        status, headers, body = attempt.result(timeout=6)
    assert status == 401, body
    assert not any(
        "session_token=" in value for value in headers.get_all("Set-Cookie") or []
    )
    with _database() as connection:
        assert (
            connection.execute(
                'SELECT count(*) FROM public."session" WHERE "userId"=%s '
                'AND "securityRevision"=(SELECT security_revision FROM public.staff_accounts WHERE id=%s)',
                (owner.user, owner.staff),
            ).fetchone()[0]
            == 0
        )


def test_deleted_session_cannot_be_revived_by_parallel_activity(owner):
    cookie = _login(owner)
    with ThreadPoolExecutor(max_workers=3) as pool:
        reads = [pool.submit(_request, "/api/session", cookie=cookie) for _ in range(3)]
        with _database() as connection:
            connection.execute(
                'DELETE FROM public."session" WHERE "userId"=%s', (owner.user,)
            )
        for read in reads:
            assert read.result()[0] in (200, 401)
    _denied(_request("/api/session", cookie=cookie), 401, "UNAUTHENTICATED")
    with _database() as connection:
        assert _session_token(connection, owner.user) is None


def test_wrong_password_throttle_blocks_subsequent_correct_login(owner):
    for _ in range(_POLICY["login_max_attempts"]):
        response = _request(
            "/api/auth/login",
            method="POST",
            payload={"email": owner.email, "password": "wrong-password"},
            headers=_mutation_headers(),
        )
        assert response[0] in (401, 429), response[2]
    _denied(
        _request(
            "/api/auth/login",
            method="POST",
            payload={"email": owner.email, "password": owner.password},
            headers=_mutation_headers(),
        ),
        429,
        "RATE_LIMITED",
    )


def test_locked_native_session_storage_fails_closed(owner):
    cookie = _login(owner)
    with _database() as connection:
        connection.execute('LOCK TABLE public."session" IN ACCESS EXCLUSIVE MODE')
        _denied(_request("/api/session", cookie=cookie), 503, "UNAVAILABLE")
    assert _request("/api/session", cookie=cookie)[0] == 200


def test_logout_requires_origin_and_csrf(owner):
    cookie = _login(owner)
    _denied(
        _request("/api/auth/logout", method="POST", cookie=cookie), 403, "FORBIDDEN"
    )
    assert _request("/api/session", cookie=cookie)[0] == 200

"""Actual immutable 0040 Web HTTP and PostgreSQL admission, including live ACL drift."""

from __future__ import annotations

import json
import os
import time
from hashlib import sha256
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

import psycopg

from ops.compat.manifest import SOURCE_HEAD
from tests.integration.compat_ci_helpers import (
    build_manifest,
    connect_role,
    ensure_roles_provisioned,
    get_cluster_id,
    grant_db_access,
    require_compat_ci,
    upgrade_to_revision,
)
from tests.integration.compat_matrix_fixture import (
    initial_activate_matched_target,
    provision_target_control,
)
from tests.integration.test_sessions import _seed_owner

SOURCE = "http://source-web:3000"
WRONG_PEER = f"http://{os.environ.get('CP_CI_PROJECT', 'unset')}-source-peer:3000"
EMAIL = "source-web-runtime@example.invalid"
PASSWORD = "Synthetic-Source-Web-0040-Test-Only!"
PROTECTED_TABLES = (
    '"session"',
    "appointments",
    "idempotency_receipts",
    "request_nonces",
    "sensitive_audit",
    "outbox_events",
    "delivery_jobs",
)


def prepare_source_web_http_lab() -> None:
    """Run once in the disposable observer before starting original image services."""
    require_compat_ci()
    admin = connect_role("carepulse_bootstrap", autocommit=True)
    try:
        ensure_roles_provisioned(admin, source_only=True, separate_source_secrets=True)
        grant_db_access(admin, "carepulse", source_only=True)
        cluster_id = get_cluster_id(admin)
    finally:
        admin.close()

    upgrade_to_revision("carepulse", SOURCE_HEAD)
    source = build_manifest(
        generation="source",
        target_id="compat-target-1",
        cluster_id=cluster_id,
        db_name="carepulse",
    )
    candidate = build_manifest(
        generation="candidate",
        target_id="compat-target-1",
        cluster_id=cluster_id,
        db_name="carepulse",
    )
    migration = connect_role("carepulse_migration")
    try:
        provision_target_control(
            migration,
            target_id="compat-target-1",
            cluster_id=cluster_id,
            db_name="carepulse",
            source_manifest_id=source.manifest_id,
            target_manifest_id=candidate.manifest_id,
        )
        assert (
            initial_activate_matched_target(
                migration, manifest=source, expected_target_id="compat-target-1"
            )
            == 1
        )
    finally:
        migration.close()

    seed = connect_role("carepulse_bootstrap")
    try:
        _seed_owner(seed, email=EMAIL, password=PASSWORD)
        seed.commit()
    finally:
        seed.close()


def _http(
    base: str, path: str, *, login: bool = False, cookie: str = "", timeout: float = 5
):
    headers = {"Cookie": cookie} if cookie else {}
    data = None
    if login:
        headers.update(
            {
                "Origin": base,
                "X-Requested-With": "XMLHttpRequest",
                "Sec-Fetch-Site": "same-origin",
                "Content-Type": "application/json",
            }
        )
        data = json.dumps({"email": EMAIL, "password": PASSWORD}).encode()
    try:
        response = urlopen(
            Request(
                base + path,
                headers=headers,
                data=data,
                method="POST" if login else "GET",
            ),
            timeout=timeout,
        )
    except HTTPError as error:
        response = error
    with response:
        return response.status, response.headers, response.read(8192)


def _ready(base: str, path: str = "/health/live") -> None:
    deadline = time.monotonic() + 20
    while time.monotonic() < deadline:
        try:
            if _http(base, path, timeout=2)[0] == 200:
                return
        except (URLError, TimeoutError):
            pass
        time.sleep(0.25)  # Startup polling only; never used to prove a race.
    raise AssertionError(f"Web process did not serve {path}")


def _unavailable(result) -> None:
    status, headers, body = result
    assert status == 503, (status, body)
    assert headers.get("Cache-Control") == "no-store"
    assert not headers.get_all("Set-Cookie")
    assert len(body) < 1024
    assert not any(
        marker in body.lower()
        for marker in (
            b"password",
            b"traceback",
            b"select ",
            b"sqlstate",
            b"carepulse_",
            EMAIL.encode(),
        )
    )


def _effects(conn: psycopg.Connection) -> tuple[int, ...]:
    return tuple(
        conn.execute(f"SELECT count(*) AS n FROM public.{table}").fetchone()["n"]
        for table in PROTECTED_TABLES
    )


def _web_attempts(conn: psycopg.Connection) -> int:
    return conn.execute(
        """SELECT count(*) AS n FROM compat_control.work_evidence
           WHERE target_id='compat-target-1' AND generation='source'
             AND service='web' AND invocation_class='http'"""
    ).fetchone()["n"]


def _protected_digest(conn: psycopg.Connection) -> str:
    """Compare all protected row values without disclosing data in test output."""
    rows = [
        [
            item["value"]
            for item in conn.execute(
                f"SELECT to_jsonb(t) AS value FROM public.{table} t ORDER BY to_jsonb(t)::text"
            )
        ]
        for table in PROTECTED_TABLES
    ]
    payload = json.dumps(rows, sort_keys=True, default=str)
    return sha256(payload.encode()).hexdigest()


def test_original_source_web_http_rechecks_live_native_and_runtime_privileges() -> None:
    require_compat_ci()
    web_ip = os.environ["CP_SOURCE_WEB_DB_IP"]
    _ready(SOURCE)
    _ready(WRONG_PEER)
    admin = connect_role("carepulse_bootstrap", autocommit=True)
    try:
        control = admin.execute(
            "SELECT phase, active_generation, alembic_head FROM compat_control.read_target_control()"
        ).fetchone()
        assert control == {
            "phase": "SOURCE_OPEN",
            "active_generation": "source",
            "alembic_head": SOURCE_HEAD,
        }
        _ready(SOURCE, "/health/ready")
        original_runtime_sessions = admin.execute(
            """SELECT count(*) AS n FROM pg_stat_activity
               WHERE datname=current_database() AND client_addr=%s::inet
                 AND usename='carepulse_runtime'""",
            (web_ip,),
        ).fetchone()["n"]
        assert original_runtime_sessions >= 2  # Guard pool + original Next Web pool.
        login_status, login_headers, login_body = _http(
            SOURCE, "/api/auth/login", login=True
        )
        assert login_status == 200, (login_status, login_body)
        assert json.loads(login_body)["data"]["role"] == "DOCTOR_OWNER"
        cookies = login_headers.get_all("Set-Cookie") or []
        cookie = next(
            header.split(";", 1)[0]
            for header in cookies
            if header.startswith("better-auth.session_token=")
        )
        assert _http(SOURCE, "/api/session", cookie=cookie)[0] == 200

        roles = admin.execute(
            """SELECT usename, count(*) AS sessions FROM pg_stat_activity
               WHERE datname = current_database()
                 AND client_addr = %s::inet
                 AND usename IN ('carepulse_runtime', 'carepulse_auth')
               GROUP BY usename""",
            (web_ip,),
        ).fetchall()
        assert {row["usename"] for row in roles} == {
            "carepulse_runtime",
            "carepulse_auth",
        }
        assert all(row["sessions"] > 0 for row in roles)
        original = _effects(admin)
        assert _web_attempts(admin) >= 2
        source_pid = admin.execute(
            """SELECT pid FROM pg_stat_activity
               WHERE datname=current_database() AND client_addr=%s::inet
                 AND usename='carepulse_runtime'""",
            (web_ip,),
        ).fetchall()
        assert source_pid
        auth_pid = admin.execute(
            """SELECT pid FROM pg_stat_activity
               WHERE datname=current_database() AND client_addr=%s::inet
                 AND usename='carepulse_auth'""",
            (web_ip,),
        ).fetchall()
        assert auth_pid

        # Native-auth ACL drift is checked on an already-used auth pool, not a VM mock.
        previous_protected = _protected_digest(admin)
        try:
            admin.execute("GRANT SELECT ON public.patients TO carepulse_auth")
            assert (
                admin.execute(
                    "SELECT count(*) AS n FROM pg_stat_activity WHERE pid = ANY(%s)",
                    ([r["pid"] for r in auth_pid],),
                ).fetchone()["n"]
                > 0
            )
            _unavailable(_http(SOURCE, "/api/session", cookie=cookie))
            assert _effects(admin) == original
            assert _protected_digest(admin) == previous_protected
        finally:
            admin.execute("REVOKE SELECT ON public.patients FROM carepulse_auth")
        assert _http(SOURCE, "/api/session", cookie=cookie)[0] == 200

        # An already-running source Web must recheck its runtime connection too.
        admitted_web_attempts = _web_attempts(admin)
        previous_protected = _protected_digest(admin)
        try:
            admin.execute("REVOKE SELECT ON public.patients FROM carepulse_runtime")
            assert (
                admin.execute(
                    "SELECT count(*) AS n FROM pg_stat_activity WHERE pid = ANY(%s)",
                    ([r["pid"] for r in source_pid],),
                ).fetchone()["n"]
                > 0
            )
            _unavailable(_http(SOURCE, "/health/ready"))
            _unavailable(_http(SOURCE, "/api/session", cookie=cookie))
            assert _http(SOURCE, "/health/live")[0] == 200
            assert _effects(admin) == original
            assert _protected_digest(admin) == previous_protected
            assert _web_attempts(admin) == admitted_web_attempts
        finally:
            admin.execute("GRANT SELECT ON public.patients TO carepulse_runtime")
        assert _http(SOURCE, "/health/ready")[0] == 200
        assert _http(SOURCE, "/api/session", cookie=cookie)[0] == 200

        for mutation, undo in (
            (
                "UPDATE compat_control.target_control SET phase='DRAINING'",
                "UPDATE compat_control.target_control SET phase='SOURCE_OPEN'",
            ),
            (
                "UPDATE public.alembic_version SET version_num='0041_f01_runtime_identity'",
                f"UPDATE public.alembic_version SET version_num='{SOURCE_HEAD}'",
            ),
        ):
            admitted_web_attempts = _web_attempts(admin)
            previous_protected = _protected_digest(admin)
            try:
                admin.execute(mutation)
                _unavailable(_http(SOURCE, "/health/ready"))
                _unavailable(_http(SOURCE, "/api/session", cookie=cookie))
                assert _http(SOURCE, "/health/live")[0] == 200
                assert _effects(admin) == original
                assert _protected_digest(admin) == previous_protected
                assert _web_attempts(admin) == admitted_web_attempts
            finally:
                admin.execute(undo)
            assert _http(SOURCE, "/health/ready")[0] == 200

        # A source process with an incompatible API peer stays fenced.
        admitted_web_attempts = _web_attempts(admin)
        previous_protected = _protected_digest(admin)
        assert _http(WRONG_PEER, "/health/live")[0] == 200
        _unavailable(_http(WRONG_PEER, "/health/ready"))
        _unavailable(_http(WRONG_PEER, "/api/session", cookie=cookie))
        assert _effects(admin) == original
        assert _protected_digest(admin) == previous_protected
        assert _web_attempts(admin) == admitted_web_attempts
        role_counts = {row["usename"]: row["sessions"] for row in roles}
        print(
            "SOURCE_WEB_HTTP 0040/SOURCE_OPEN login=200 session=200 "
            f"runtime_sessions={role_counts['carepulse_runtime']} "
            f"auth_sessions={role_counts['carepulse_auth']} "
            f"protected_effect_counts={original} delta=0 "
            "ACL/phase/head/peer_denied=503 live=200"
        )
    finally:
        admin.close()

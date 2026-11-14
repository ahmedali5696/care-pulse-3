"""Disposable PostgreSQL proof helpers; all credentials and timings are synthetic."""

import hashlib
import hmac
import json
import os
import time
import uuid

import psycopg


def connect(role="runtime", autocommit=False):
    user = "postgres" if role == "admin" else f"f01_{role}"
    password = "postgres-test" if role == "admin" else f"{role}-test"
    return psycopg.connect(
        host=os.environ.get("F01_DB_HOST", "127.0.0.1"),
        dbname="f01",
        user=user,
        password=password,
        autocommit=autocommit,
        connect_timeout=5,
        options="-c statement_timeout=15000 -c lock_timeout=12000",
    )


def admin():
    return connect("admin", autocommit=True)


def gate():
    return connect("gate", autocommit=True)


def scalar(conn, sql, params=()):
    return conn.execute(sql, params).fetchone()[0]


def payload(**kwargs):
    fields = {
        "protocol_version": "carepulse-request-v1",
        "key_id": "http-a",
        "service": "web",
        "method": "POST",
        "path": "/proof",
        "query": "",
        "body_sha256": hashlib.sha256(b"").hexdigest(),
        "organization_id": "A",
        "staff_id": "doctor-a",
        "session_id": "sa",
        "role": "DOCTOR_OWNER",
        "security_revision": "1",
        "timestamp": str(time.time_ns() // 1_000_000),
        "nonce": uuid.uuid4().hex,
        "purpose": "NOTE_READ",
        "idempotency_key": "p1",
    }
    assert kwargs.keys() <= fields.keys(), kwargs.keys() - fields.keys()
    fields.update({k: str(v) for k, v in kwargs.items()})
    return json.dumps(
        list(fields.values()), ensure_ascii=False, separators=(",", ":")
    ).encode()


def mac(p, key="db-a"):
    secret = {"db-a": b"A" * 32, "db-b": b"B" * 32}[key]
    message = b"carepulse-db-context-v1\0" + key.encode() + b"\0" + p
    return hmac.digest(secret, message, "sha256")


def proof(g, p=None, key="db-a"):
    p = payload() if p is None else p
    return scalar(g, "SELECT f01_api.register_proof(%s, %s, %s)", (p, key, mac(p, key)))


def transaction_id(r):
    return r.execute("SELECT pg_backend_pid(), pg_current_xact_id()::text").fetchone()


def bind(g, r, request, stage="PRIMARY"):
    pid, xid = transaction_id(r)
    g.execute("SELECT f01_api.bind(%s, %s, %s, %s::xid8)", (request, stage, pid, xid))
    return pid, xid


def activate(r):
    r.execute("SELECT f01_api.activate()")


def denied(conn, sql, params=()):
    try:
        with conn.transaction():
            conn.execute(sql, params).fetchall() if sql.lstrip().upper().startswith(
                "SELECT"
            ) else conn.execute(sql, params)
    except psycopg.Error as exc:
        # Only expected security, constraint or isolation denials count, never a
        # missing object, malformed query, timeout or unrelated database failure.
        assert exc.sqlstate in {
            "42501",
            "28000",
            "23505",
            "23514",
            "22023",
            "P0001",
            "25001",
            "55000",
            "0A000",
        }, (sql, exc.sqlstate, str(exc))
        return exc.sqlstate
    raise AssertionError(f"unexpected success: {sql}")


def wait_blocked(observer, blocked_pid, blocker_pid):
    deadline = time.monotonic() + 8
    while time.monotonic() < deadline:
        if scalar(
            observer,
            "SELECT %s = ANY(pg_blocking_pids(%s))",
            (blocker_pid, blocked_pid),
        ):
            print(
                f"observed PG lock: pid {blocked_pid} blocked by {blocker_pid}",
                flush=True,
            )
            return
        time.sleep(0.02)
    raise AssertionError(f"no PostgreSQL blocking edge {blocked_pid} -> {blocker_pid}")


def wait_until_expired(observer, timestamp_ms):
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        if scalar(
            observer,
            "SELECT extract(epoch FROM clock_timestamp()) * 1000 > %s",
            (timestamp_ms + 60_000,),
        ):
            return
        time.sleep(0.02)
    raise AssertionError("proof did not reach the synthetic expiry boundary")

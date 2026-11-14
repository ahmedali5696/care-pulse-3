"""Criteria 1–5, using actual PostgreSQL client connections and ordered races."""

import base64
import json
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from queue import Queue

import psycopg
from helper import (
    activate,
    admin,
    bind,
    connect,
    denied,
    gate,
    mac,
    payload,
    proof,
    scalar,
    transaction_id,
    wait_blocked,
    wait_until_expired,
)
from sqlalchemy import create_engine


def context(r):
    return r.execute("SELECT * FROM f01_api.context()").fetchall()


def empty_context(r):
    assert context(r) == []
    assert scalar(r, "SELECT count(*) FROM f01_data.patients") == 0


def expected_actor(r):
    assert context(r) == [("A", "doctor-a", "DOCTOR_OWNER", "NOTE_READ", "PRIMARY")]
    assert r.execute("SELECT org, id FROM f01_data.patients").fetchall() == [
        ("A", "p1")
    ]


def criterion_1(a, g):
    assert scalar(a, "SHOW server_version").split()[0] == "17.11"
    assert scalar(a, "SHOW max_prepared_transactions") == "0"
    version = scalar(a, "SELECT extversion FROM pg_extension WHERE extname='pgcrypto'")
    node = json.loads(Path("/node.json").read_text())
    p = base64.b64decode(node["payload"])
    node_mac = bytes.fromhex(node["mac"])
    assert mac(p) == node_mac
    assert (
        scalar(g, "SELECT f01_api.register_proof(%s,%s,%s)", (p, "db-a", node_mac)) > 0
    )
    p = payload()
    query = "SELECT f01_api.register_proof(%s,%s,%s)"
    nonce = json.loads(p)[13]
    denied(g, query, (p, "db-a", bytes(32)))
    denied(g, query, (p, "db-a", mac(p)[:-1]))
    denied(g, query, (p, "db-a", None))
    denied(g, query, (p, "db-b", mac(p)))
    denied(g, query, (p, "missing", mac(p)))
    changed = p.replace(b'"doctor-a"', b'"doctor-b"')
    assert changed != p
    denied(g, query, (changed, "db-a", mac(p)))
    denied(g, query, (p + b" ", "db-a", mac(p)))
    forged = payload(protocol_version="unexpected")
    denied(g, query, (forged, "db-a", mac(forged)))
    malformed = json.loads(payload())
    malformed[11] = 1
    forged = json.dumps(malformed, separators=(",", ":")).encode()
    denied(g, query, (forged, "db-a", mac(forged)))
    future = payload(timestamp=int(time.time() * 1000) + 10_000)
    denied(g, query, (future, "db-a", mac(future)))
    # Failed verification did not burn the valid nonce.
    assert nonce_counts(a, nonce) == (0, 0)
    assert proof(g, p) > 0
    assert nonce_counts(a, nonce) == (1, 1)
    print(
        f"C1 PASS PostgreSQL 17.11 pgcrypto={version}; Node/Python/PG exact MAC; invalid proofs denied"
    )


def criterion_2(a, g):
    roles = a.execute("""
        SELECT rolname, rolcanlogin, rolsuper, rolbypassrls, rolcreaterole, rolcreatedb
        FROM pg_roles WHERE rolname IN
          ('carepulse_identity_owner','f01_gate','f01_runtime','f01_operator')
        ORDER BY rolname
        """).fetchall()
    assert len(roles) == 4
    for name, login, sup, bypass, create_role, create_db in roles:
        assert login == (name != "carepulse_identity_owner")
        assert not any((sup, bypass, create_role, create_db))
    private = a.execute("""
        SELECT c.oid, c.relname FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace
        WHERE n.nspname='f01_private' AND c.relkind='r'
        """).fetchall()
    assert private
    register_oid = scalar(
        a, "SELECT 'f01_api.register_proof(bytea,text,bytea)'::regprocedure::oid"
    )
    for role in ("gate", "runtime", "operator"):
        with connect(role, autocommit=True) as r:
            assert (
                r.execute("SELECT session_user, current_user").fetchone()
                == (f"f01_{role}",) * 2
            )
            for oid, name in private:
                for permission in (
                    "SELECT",
                    "INSERT",
                    "UPDATE",
                    "DELETE",
                    "TRUNCATE",
                    "REFERENCES",
                    "TRIGGER",
                ):
                    assert not scalar(
                        r,
                        "SELECT has_table_privilege(current_user,%s::oid,%s)",
                        (oid, permission),
                    ), (role, name, permission)
                denied(r, f'SELECT * FROM f01_private."{name}"')
                for permission in ("SELECT", "INSERT", "UPDATE", "REFERENCES"):
                    assert not scalar(
                        r,
                        "SELECT has_any_column_privilege(current_user,%s::oid,%s)",
                        (oid, permission),
                    ), (role, name, permission)
            denied(r, "SET ROLE carepulse_identity_owner")
            denied(r, "SET SESSION AUTHORIZATION carepulse_identity_owner")
            assert not scalar(
                r, "SELECT has_schema_privilege(current_user,'f01_private','CREATE')"
            )
            assert not scalar(
                r, "SELECT has_schema_privilege(current_user,'f01_crypto','USAGE')"
            )
            denied(r, "SELECT f01_crypto.hmac('a'::bytea, 'b'::bytea, 'sha256')")
            denied(r, "UPDATE f01_private.keys SET status='ACTIVE'")
            denied(r, "DELETE FROM f01_private.nonces")
            denied(r, "TRUNCATE f01_private.bindings")
            if role != "gate":
                assert not scalar(
                    r,
                    "SELECT has_function_privilege(current_user,%s::oid,'EXECUTE')",
                    (register_oid,),
                )
                p = payload()
                denied(
                    r, "SELECT f01_api.register_proof(%s,%s,%s)", (p, "db-a", mac(p))
                )
                denied(
                    r,
                    "SELECT f01_api.bind(1,'PRIMARY',pg_backend_pid(),pg_current_xact_id())",
                )
                denied(r, "SET ROLE f01_gate")
            if role != "runtime":
                denied(r, "SELECT f01_api.activate()")
                denied(r, "SELECT * FROM f01_data.patients")
    assert scalar(
        g,
        "SELECT has_function_privilege(current_user,'f01_api.register_proof(bytea,text,bytea)','EXECUTE')",
    )
    print(
        f"C2 PASS real logins, NOLOGIN owner, effective ACL on {len(private)} private tables; privilege escalation denied"
    )


def nonce_counts(a, nonce):
    return a.execute(
        """
        SELECT (SELECT count(*) FROM f01_private.nonces WHERE http_key_id='http-a' AND nonce=%s),
               (SELECT count(*) FROM f01_private.requests WHERE http_key_id='http-a' AND nonce=%s)
        """,
        (nonce, nonce),
    ).fetchone()


def criterion_3(a, g):
    p = payload()
    registered = proof(g, p)
    assert registered > 0
    for key in ("db-a", "db-b"):
        denied(g, "SELECT f01_api.register_proof(%s,%s,%s)", (p, key, mac(p, key)))
    assert nonce_counts(a, json.loads(p)[13]) == (1, 1)

    p = payload()
    with connect("gate") as first, ThreadPoolExecutor(max_workers=1) as pool:
        first_pid = scalar(first, "SELECT pg_backend_pid()")
        request = proof(first, p)
        pid = Queue()

        def competing_registration():
            with gate() as second:
                pid.put(scalar(second, "SELECT pg_backend_pid()"))
                return denied(
                    second,
                    "SELECT f01_api.register_proof(%s,%s,%s)",
                    (p, "db-b", mac(p, "db-b")),
                )

        future = pool.submit(competing_registration)
        try:
            wait_blocked(a, pid.get(timeout=5), first_pid)
        finally:
            first.commit()
        assert future.result(timeout=5)
        assert request > 0
        assert nonce_counts(a, json.loads(p)[13]) == (1, 1)
    p = payload()
    with connect("gate") as first:
        proof(first, p)
        first.rollback()
    assert nonce_counts(a, json.loads(p)[13]) == (0, 0)
    assert proof(g, p) > 0
    assert nonce_counts(a, json.loads(p)[13]) == (1, 1)
    print(
        "C3 PASS concurrent nonce registration serialized across DB-proof keys; duplicate denied; registration rollback atomic"
    )


def criterion_4(a, g):
    q1 = proof(g)
    q2 = proof(
        g,
        payload(
            organization_id="B",
            staff_id="doctor-b",
            session_id="sb",
            idempotency_key="p2",
        ),
    )
    with connect() as r, connect() as other:
        pid, xid = bind(g, r, q1)
        actual = a.execute(
            """
            SELECT b.database_oid = d.oid, b.login_oid = 'f01_runtime'::regrole::oid,
                   b.pid, b.xid::text, pg_xact_status(b.xid)
            FROM f01_private.bindings b CROSS JOIN pg_database d
            WHERE b.request_id=%s AND b.stage='PRIMARY' AND d.datname=current_database()
            """,
            (q1,),
        ).fetchone()
        assert actual == (True, True, pid, xid, "in progress")
        empty_context(r)
        pid2, xid2 = transaction_id(other)
        denied(g, "SELECT f01_api.bind(%s,'PRIMARY',%s,%s::xid8)", (q2, pid, xid))
        denied(g, "SELECT f01_api.bind(%s,'PRIMARY',%s,%s::xid8)", (q1, pid2, xid2))
        denied(other, "SELECT f01_api.activate()")
        empty_context(other)
        activate(r)
        expected_actor(r)
        denied(r, "SELECT f01_api.activate()")
        r.execute(
            "SELECT set_config('app.organization_id','B',true), set_config('app.staff_id','doctor-b',true), set_config('app.role','DOCTOR_OWNER',true)"
        )
        expected_actor(r)
        r.execute("SET LOCAL row_security=off")
        denied(r, "SELECT * FROM f01_data.patients")
        r.execute("SET LOCAL row_security=on")
        expected_actor(r)
        denied(r, "SET ROLE f01_gate")
        r.commit()
        assert scalar(a, "SELECT pg_xact_status(%s::xid8)", (xid,)) == "committed"
        empty_context(r)
        denied(r, "SELECT f01_api.activate()")
        other.rollback()

    # A mismatched PID/XID pair may be refused by the gate; if the trusted gate
    # records it, neither real runtime transaction may activate that binding.
    q = proof(g)
    with connect() as first, connect() as second:
        _, first_xid = transaction_id(first)
        second_pid, _ = transaction_id(second)
        try:
            g.execute(
                "SELECT f01_api.bind(%s,'PRIMARY',%s,%s::xid8)",
                (q, second_pid, first_xid),
            )
        except psycopg.Error as exc:
            assert exc.sqlstate == "42501", exc
        for runtime in (first, second):
            denied(runtime, "SELECT f01_api.activate()")
            empty_context(runtime)

    with connect() as r:
        r.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ")
        bind(g, r, proof(g))
        denied(r, "SELECT f01_api.activate()")
        empty_context(r)
        r.rollback()
    print(
        "C4 PASS immutable request/stage and database transaction binding; reactivation, identity switch and wrong isolation denied"
    )


def criterion_5(a, g):
    p = payload()
    q = proof(g, p)
    with connect() as r:
        bind(g, r, q)
        r.execute("SAVEPOINT before_activate")
        activate(r)
        expected_actor(r)
        r.execute("SAVEPOINT after_activate")
        r.execute("SELECT set_config('app.organization_id','B',true)")
        r.execute("ROLLBACK TO after_activate")
        expected_actor(r)
        r.execute("ROLLBACK TO before_activate")
        empty_context(r)
        # Activation is transactional, but the gate-committed binding is not.
        # A savepoint rollback cannot bind/activate another tenant on this xid.
        pid, xid = transaction_id(r)
        denied(g, "SELECT f01_api.bind(%s,'PRIMARY',%s,%s::xid8)", (proof(g), pid, xid))
        r.rollback()
        assert scalar(a, "SELECT pg_xact_status(%s::xid8)", (xid,)) == "aborted"
        pid, xid = transaction_id(r)
        denied(g, "SELECT f01_api.bind(%s,'PRIMARY',%s,%s::xid8)", (q, pid, xid))
        empty_context(r)
        r.rollback()
    assert nonce_counts(a, json.loads(p)[13]) == (1, 1)
    denied(g, "SELECT f01_api.register_proof(%s,%s,%s)", (p, "db-a", mac(p)))

    # SQLAlchemy's actual QueuePool returns the same backend to another borrower.
    engine = create_engine(
        "postgresql+psycopg://", creator=connect, pool_size=1, max_overflow=0
    )
    try:
        for commit in (True, False):
            lease = engine.raw_connection()
            try:
                r = lease.driver_connection
                q = proof(g)
                old_pid, old_xid = bind(g, r, q)
                activate(r)
                expected_actor(r)
                if commit:
                    r.commit()
            finally:
                lease.close()
            lease = engine.raw_connection()
            try:
                r = lease.driver_connection
                pid, xid = transaction_id(r)
                assert pid == old_pid and xid != old_xid
                empty_context(r)
                denied(r, "SELECT f01_api.activate()")
                denied(
                    g, "SELECT f01_api.bind(%s,'PRIMARY',%s,%s::xid8)", (q, pid, xid)
                )
            finally:
                lease.close()
    finally:
        engine.dispose()

    stamp = (
        int(scalar(a, "SELECT extract(epoch FROM clock_timestamp()) * 1000")) - 55_000
    )
    with connect() as r:
        bind(g, r, proof(g, payload(timestamp=stamp)))
        activate(r)
        expected_actor(r)
        wait_until_expired(a, stamp)
        empty_context(r)
        r.rollback()
    expired = payload(timestamp=int(time.time() * 1000) - 61_000)
    denied(
        g, "SELECT f01_api.register_proof(%s,%s,%s)", (expired, "db-a", mac(expired))
    )
    print(
        "C5 PASS savepoint identity, commit/rollback, same pooled backend reuse and live transaction expiry"
    )


def run():
    with admin() as a, gate() as g:
        criterion_1(a, g)
        criterion_2(a, g)
        criterion_3(a, g)
        criterion_4(a, g)
        criterion_5(a, g)


if __name__ == "__main__":
    run()

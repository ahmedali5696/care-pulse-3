"""Exercise live authorization, RLS, stage effects and ordered PostgreSQL races."""

import hashlib
import json
from concurrent.futures import ThreadPoolExecutor

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


def context(r):
    return r.execute("SELECT * FROM f01_api.context()").fetchall()


def patients(r):
    return r.execute(
        "SELECT org, id FROM f01_data.patients ORDER BY org, id"
    ).fetchall()


def start(g, r, stage="PRIMARY", **claims):
    request = proof(g, payload(**claims))
    bind(g, r, request, stage)
    activate(r)
    return request


def start_write(g, r, body, **claims):
    return start(
        g,
        r,
        purpose="NOTE_WRITE",
        body_sha256=hashlib.sha256(body.encode()).hexdigest(),
        **claims,
    )


def denied_activation(g, r, **claims):
    request = proof(g, payload(**claims))
    bind(g, r, request)
    denied(r, "SELECT f01_api.activate()")
    assert context(r) == []
    r.rollback()


def note(a, target="p1"):
    return scalar(a, "SELECT note FROM f01_data.patients WHERE id = %s", (target,))


def check_live_and_rls(g, a):
    with connect() as r:
        assert context(r) == []
        assert patients(r) == []
        r.rollback()
        for claims in (
            {"organization_id": "B"},
            {"organization_id": "missing"},
            {"staff_id": "doctor-b"},
            {"staff_id": "missing"},
            {"session_id": "sb"},
            {"session_id": "missing"},
            {"security_revision": "2"},
            {"role": "RECEPTIONIST"},
        ):
            denied_activation(g, r, **claims)

        for update, restore in (
            (
                "UPDATE f01_private.organizations SET status='DISABLED' WHERE id='A'",
                "UPDATE f01_private.organizations SET status='ACTIVE' WHERE id='A'",
            ),
            (
                "UPDATE f01_private.staff SET status='DISABLED' WHERE id='doctor-a'",
                "UPDATE f01_private.staff SET status='ACTIVE' WHERE id='doctor-a'",
            ),
            (
                "UPDATE f01_private.staff SET email_verified=false WHERE id='doctor-a'",
                "UPDATE f01_private.staff SET email_verified=true WHERE id='doctor-a'",
            ),
            (
                "UPDATE f01_private.staff SET reset_required=true WHERE id='doctor-a'",
                "UPDATE f01_private.staff SET reset_required=false WHERE id='doctor-a'",
            ),
            (
                "UPDATE f01_private.sessions SET status='REVOKED' WHERE id='sa'",
                "UPDATE f01_private.sessions SET status='ACTIVE' WHERE id='sa'",
            ),
            (
                "UPDATE f01_private.staff SET security_revision=2 WHERE id='doctor-a'",
                "UPDATE f01_private.staff SET security_revision=1 WHERE id='doctor-a'",
            ),
            (
                "UPDATE f01_private.sessions SET security_revision=2 WHERE id='sa'",
                "UPDATE f01_private.sessions SET security_revision=1 WHERE id='sa'",
            ),
            (
                "UPDATE f01_private.sessions SET expires_at=clock_timestamp()-interval '1 second' WHERE id='sa'",
                "UPDATE f01_private.sessions SET expires_at=clock_timestamp()+interval '1 hour' WHERE id='sa'",
            ),
            (
                "UPDATE f01_private.sessions SET idle_expires_at=clock_timestamp()-interval '1 second' WHERE id='sa'",
                "UPDATE f01_private.sessions SET idle_expires_at=clock_timestamp()+interval '1 hour' WHERE id='sa'",
            ),
        ):
            a.execute(update)
            try:
                denied_activation(g, r)
            finally:
                a.execute(restore)

        start(g, r, body_sha256=hashlib.sha256(b"wrong-purpose").hexdigest())
        assert context(r) == [("A", "doctor-a", "DOCTOR_OWNER", "NOTE_READ", "PRIMARY")]
        assert patients(r) == [("A", "p1")]
        denied(r, "SELECT f01_api.write_note('p1', 'wrong-purpose')")
        denied(r, "UPDATE f01_data.patients SET note='direct-update' WHERE id='p1'")
        r.commit()
        assert context(r) == []
        r.rollback()

        start(
            g,
            r,
            organization_id="B",
            staff_id="doctor-b",
            session_id="sb",
            idempotency_key="p2",
        )
        assert patients(r) == [("B", "p2")]
        r.commit()

        start(g, r, staff_id="reception-a", session_id="ra", role="RECEPTIONIST")
        assert context(r) == [
            ("A", "reception-a", "RECEPTIONIST", "NOTE_READ", "PRIMARY")
        ]
        assert patients(r) == []
        r.rollback()

        start_write(
            g,
            r,
            "wrong-role",
            staff_id="reception-a",
            session_id="ra",
            role="RECEPTIONIST",
        )
        assert patients(r) == []
        denied(r, "SELECT f01_api.write_note('p1', 'wrong-role')")
        r.rollback()

        before = note(a)
        other = note(a, "p2")
        start_write(g, r, "signed-cross-tenant", idempotency_key="p2")
        denied(r, "SELECT f01_api.write_note('p2', 'signed-cross-tenant')")
        r.rollback()
        assert note(a, "p2") == other
        start_write(g, r, "committed-note")
        assert patients(r) == []
        denied(r, "SELECT f01_api.write_note('p2', 'committed-note')")
        denied(r, "SELECT f01_api.write_note('p1', 'wrong-body')")
        denied(r, "UPDATE f01_data.patients SET note='direct-update' WHERE id='p1'")
        r.execute("SELECT f01_api.write_note('p1', 'committed-note')")
        r.commit()
        assert note(a) == "committed-note" != before
        assert note(a, "p2") == other
        deny_rebind(
            g,
            r,
            scalar(
                a,
                "SELECT request_id FROM f01_private.receipts WHERE organization_id='A' AND target='p1'",
            ),
            "RECEIPT_RECOVERY",
        )
    assert scalar(
        a,
        "SELECT relrowsecurity AND relforcerowsecurity FROM pg_class WHERE oid='f01_data.patients'::regclass",
    )
    assert scalar(
        a,
        "SELECT count(*)=4 AND bool_and(relrowsecurity AND relforcerowsecurity) FROM pg_class WHERE oid IN ('f01_private.organizations'::regclass,'f01_private.staff'::regclass,'f01_private.sessions'::regclass,'f01_private.invitations'::regclass)",
    )
    print(
        "criterion 6: live org/staff/session/revision claims and tenant-scoped live context; PASS",
        flush=True,
    )
    print(
        "criterion 7: FORCE RLS, role and purpose filters, denied direct write, restricted cross-tenant writer; PASS",
        flush=True,
    )


def deny_rebind(g, r, request, stage):
    pid, xid = transaction_id(r)
    denied(g, "SELECT f01_api.bind(%s, %s, %s, %s::xid8)", (request, stage, pid, xid))
    r.rollback()


def check_stages(g, a):
    with connect() as r:
        for purpose, invalid_stage in (
            ("NOTE_READ", "RECEIPT_RECOVERY"),
            ("NOTE_WRITE", "RECEIPT_RECOVERY"),
            ("INVITE", "INVITATION_APPLY"),
            ("INVITE", "PRIMARY"),
            ("SECURITY_CHANGE", "SESSION_CLEANUP"),
        ):
            deny_rebind(g, r, proof(g, payload(purpose=purpose)), invalid_stage)
        before = note(a, "p2")
        request = start_write(
            g,
            r,
            "rolled-back-note",
            organization_id="B",
            staff_id="doctor-b",
            session_id="sb",
            idempotency_key="p2",
        )
        r.execute("SELECT f01_api.write_note('p2', 'rolled-back-note')")
        denied(r, "SELECT f01_api.read_receipt()")
        denied(r, "SELECT f01_api.activate()")
        r.rollback()
        deny_rebind(g, r, request, "PRIMARY")
        assert note(a, "p2") == before
        assert (
            scalar(
                a,
                "SELECT count(*) FROM f01_private.receipts WHERE request_id=%s",
                (request,),
            )
            == 0
        )
        bind(g, r, request, "RECEIPT_RECOVERY")
        activate(r)
        assert context(r)[0][-1] == "RECEIPT_RECOVERY"
        assert patients(r) == []
        denied(r, "SELECT f01_api.write_note('p2', 'rolled-back-note')")
        receipt = scalar(r, "SELECT f01_api.read_receipt()")
        assert receipt is None
        denied(r, "SELECT f01_api.activate()")
        r.commit()
        deny_rebind(g, r, request, "RECEIPT_RECOVERY")
        assert note(a, "p2") == before
        print(
            f"receipt recovery after PRIMARY abort, no write; result={receipt!r}",
            flush=True,
        )

        # The exact receipt survives a conflicting repeat; a different body cannot recover it.
        before = note(a)
        request = start_write(g, r, before)
        assert denied(r, "SELECT f01_api.write_note('p1', %s)", (before,)) == "23505"
        r.rollback()
        assert note(a) == before
        bind(g, r, request, "RECEIPT_RECOVERY")
        activate(r)
        assert scalar(r, "SELECT f01_api.read_receipt()") == before
        r.commit()

        request = start_write(g, r, "aborted-new-note")
        assert (
            denied(r, "SELECT f01_api.write_note('p1', 'aborted-new-note')") == "23505"
        )
        r.rollback()
        assert note(a) == before
        bind(g, r, request, "RECEIPT_RECOVERY")
        activate(r)
        assert scalar(r, "SELECT f01_api.read_receipt()") is None
        r.commit()

        request = start_write(
            g,
            r,
            before,
            organization_id="B",
            staff_id="doctor-b",
            session_id="sb",
            idempotency_key="p1",
        )
        r.rollback()
        bind(g, r, request, "RECEIPT_RECOVERY")
        activate(r)
        assert scalar(r, "SELECT f01_api.read_receipt()") is None
        r.commit()

        invite_claims = {
            "organization_id": "C",
            "staff_id": "invite-c",
            "session_id": "ic",
            "purpose": "INVITE",
        }
        denied_activation(
            g, r, organization_id="C", staff_id="invite-c", session_id="ic"
        )
        for incorrect in (
            dict(invite_claims, session_id="wrong"),
            dict(invite_claims, organization_id="B"),
            dict(invite_claims, idempotency_key="p2"),
        ):
            bad_request = proof(g, payload(**incorrect))
            bind(g, r, bad_request, "INVITATION_PREPARE")
            denied(r, "SELECT f01_api.activate()")
            assert context(r) == []
            r.rollback()
        a.execute(
            "UPDATE f01_private.invitations SET status='REVOKED' WHERE organization_id='C'"
        )
        try:
            bad_request = proof(g, payload(**invite_claims))
            bind(g, r, bad_request, "INVITATION_PREPARE")
            denied(r, "SELECT f01_api.activate()")
            assert context(r) == []
            r.rollback()
        finally:
            a.execute(
                "UPDATE f01_private.invitations SET status='PENDING' WHERE organization_id='C'"
            )
        aborted_prepare = start(g, r, "INVITATION_PREPARE", **invite_claims)
        assert scalar(r, "SELECT f01_api.prepare_invitation()") == "p1"
        r.rollback()
        deny_rebind(g, r, aborted_prepare, "INVITATION_APPLY")
        before_invites = a.execute(
            "SELECT organization_id,target,staff_id,credential_id,expires_at,request_id,status FROM f01_private.invitations ORDER BY organization_id,target"
        ).fetchall()
        before_staff = a.execute(
            "SELECT organization_id,id,role,status,security_revision,email_verified,reset_required FROM f01_private.staff ORDER BY organization_id,id"
        ).fetchall()
        before_orgs = a.execute(
            "SELECT id,status FROM f01_private.organizations ORDER BY id"
        ).fetchall()
        request = start(g, r, "INVITATION_PREPARE", **invite_claims)
        assert context(r)[0][-1] == "INVITATION_PREPARE"
        assert patients(r) == []
        denied(r, "SELECT f01_api.apply_invitation()")
        prepared = scalar(r, "SELECT f01_api.prepare_invitation()")
        assert prepared == "p1"
        denied(r, "SELECT f01_api.activate()")
        r.commit()
        deny_rebind(g, r, request, "INVITATION_PREPARE")
        assert (
            a.execute(
                "SELECT organization_id,target,staff_id,credential_id,expires_at,request_id,status FROM f01_private.invitations ORDER BY organization_id,target"
            ).fetchall()
            == before_invites
        )
        assert (
            a.execute(
                "SELECT organization_id,id,role,status,security_revision,email_verified,reset_required FROM f01_private.staff ORDER BY organization_id,id"
            ).fetchall()
            == before_staff
        )
        assert (
            a.execute(
                "SELECT id,status FROM f01_private.organizations ORDER BY id"
            ).fetchall()
            == before_orgs
        )
        bind(g, r, request, "INVITATION_APPLY")
        activate(r)
        assert context(r)[0][-1] == "INVITATION_APPLY"
        assert patients(r) == []
        denied(r, "SELECT f01_api.prepare_invitation()")
        r.execute("SELECT f01_api.apply_invitation()")
        denied(r, "SELECT f01_api.activate()")
        r.commit()
        deny_rebind(g, r, request, "INVITATION_APPLY")
        assert (
            scalar(
                a,
                "SELECT status FROM f01_private.invitations WHERE request_id=%s",
                (request,),
            )
            == "APPLIED"
        )
        assert a.execute(
            "SELECT status,email_verified FROM f01_private.staff WHERE organization_id='C' AND id='invite-c'"
        ).fetchone() == ("ACTIVE", True)
        assert (
            scalar(a, "SELECT status FROM f01_private.organizations WHERE id='C'")
            == "ACTIVE"
        )
        assert a.execute(
            "SELECT organization_id,id,role,status,security_revision,email_verified,reset_required FROM f01_private.staff WHERE organization_id<>'C' ORDER BY organization_id,id"
        ).fetchall() == [row for row in before_staff if row[0] != "C"]
        assert a.execute(
            "SELECT id,status FROM f01_private.organizations WHERE id<>'C' ORDER BY id"
        ).fetchall() == [row for row in before_orgs if row[0] != "C"]
        assert a.execute(
            "SELECT organization_id,target,staff_id,credential_id,expires_at,request_id,status FROM f01_private.invitations WHERE organization_id<>'C' ORDER BY organization_id,target"
        ).fetchall() == [row for row in before_invites if row[0] != "C"]
        print(f"committed invitation prepare/apply; prepared={prepared!r}", flush=True)

        aborted_change = start(g, r, purpose="SECURITY_CHANGE")
        r.execute("SELECT f01_api.change_security()")
        r.rollback()
        deny_rebind(g, r, aborted_change, "SESSION_CLEANUP")
        assert (
            scalar(
                a, "SELECT security_revision FROM f01_private.staff WHERE id='doctor-a'"
            )
            == 1
        )
        request = start(g, r, purpose="SECURITY_CHANGE")
        assert patients(r) == []
        denied(r, "SELECT f01_api.cleanup_sessions()")
        r.execute("SELECT f01_api.change_security()")
        denied(r, "SELECT f01_api.activate()")
        r.commit()
        deny_rebind(g, r, request, "PRIMARY")
        assert (
            scalar(
                a, "SELECT security_revision FROM f01_private.staff WHERE id='doctor-a'"
            )
            == 2
        )
        a.execute(
            "INSERT INTO f01_private.sessions (organization_id,staff_id,id,status,security_revision,expires_at) VALUES ('A','doctor-a','sa-new','ACTIVE',2,clock_timestamp()+interval '1 day')"
        )
        denied_activation(g, r)
        bind(g, r, request, "SESSION_CLEANUP")
        activate(r)
        assert context(r)[0][-1] == "SESSION_CLEANUP"
        assert patients(r) == []
        denied(r, "SELECT f01_api.change_security()")
        assert scalar(r, "SELECT f01_api.cleanup_sessions()") > 0
        denied(r, "SELECT f01_api.activate()")
        r.commit()
        deny_rebind(g, r, request, "SESSION_CLEANUP")
        assert (
            scalar(
                a,
                "SELECT count(*) FROM f01_private.sessions WHERE organization_id='A' AND staff_id='doctor-a' AND id='sa'",
            )
            == 0
        )
        assert a.execute(
            "SELECT id,security_revision FROM f01_private.sessions WHERE id IN ('sa-new','ra','sb') ORDER BY id"
        ).fetchall() == [("ra", 1), ("sa-new", 2), ("sb", 1)]
        start(g, r, session_id="sa-new", security_revision="2")
        assert patients(r) == [("A", "p1")]
        r.commit()
        denied_activation(g, r)
    print(
        "criterion 8: all five stages, exactly once, commit and abort transitions; PASS",
        flush=True,
    )


def ordered_revocation(g, a, update, restore):
    # Registration and binding commit before either contender takes its locks.
    with (
        connect() as r,
        connect("admin") as changer,
        ThreadPoolExecutor(max_workers=1) as pool,
    ):
        rpid, _ = bind(g, r, proof(g))
        cpid = scalar(changer, "SELECT pg_backend_pid()")
        activate(r)
        future = pool.submit(changer.execute, update)
        try:
            wait_blocked(a, cpid, rpid)
            assert patients(r) == [("A", "p1")]
        finally:
            r.commit()
        future.result(timeout=5)
        changer.commit()
        try:
            denied_activation(g, r)
        finally:
            a.execute(restore)

        rpid, _ = bind(g, r, proof(g))
        changer.execute(update)
        future = pool.submit(denied, r, "SELECT f01_api.activate()")
        try:
            wait_blocked(a, rpid, cpid)
        finally:
            changer.commit()
        try:
            assert future.result(timeout=5)
            assert context(r) == []
        finally:
            r.rollback()
            a.execute(restore)


def check_locks_and_cleanup(g, a):
    for update, restore in (
        (
            "UPDATE f01_private.sessions SET status='REVOKED' WHERE id='sa'",
            "UPDATE f01_private.sessions SET status='ACTIVE' WHERE id='sa'",
        ),
        (
            "UPDATE f01_private.staff SET status='DISABLED' WHERE id='doctor-a'",
            "UPDATE f01_private.staff SET status='ACTIVE' WHERE id='doctor-a'",
        ),
        (
            "UPDATE f01_private.organizations SET status='DISABLED' WHERE id='A'",
            "UPDATE f01_private.organizations SET status='ACTIVE' WHERE id='A'",
        ),
    ):
        ordered_revocation(g, a, update, restore)
    # A revoked key cannot register a fresh proof; use an already-bound proof in
    # both orderings to establish that the activation itself rechecks the key.
    with (
        connect() as r,
        connect() as next_r,
        connect("admin") as changer,
        ThreadPoolExecutor(max_workers=1) as pool,
    ):
        rpid, _ = bind(g, r, proof(g))
        bind(g, next_r, proof(g))
        cpid = scalar(changer, "SELECT pg_backend_pid()")
        activate(r)
        future = pool.submit(
            changer.execute,
            "UPDATE f01_private.keys SET status='REVOKED' WHERE key_id='db-a'",
        )
        try:
            wait_blocked(a, cpid, rpid)
            assert patients(r) == [("A", "p1")]
        finally:
            r.commit()
        future.result(timeout=5)
        changer.commit()
        try:
            denied(next_r, "SELECT f01_api.activate()")
            assert context(next_r) == []
            p = payload()
            denied(g, "SELECT f01_api.register_proof(%s,'db-a',%s)", (p, mac(p)))
        finally:
            next_r.rollback()
            a.execute("UPDATE f01_private.keys SET status='ACTIVE' WHERE key_id='db-a'")

        rpid, _ = bind(g, r, proof(g))
        changer.execute(
            "UPDATE f01_private.keys SET status='RETIRED' WHERE key_id='db-a'"
        )
        future = pool.submit(denied, r, "SELECT f01_api.activate()")
        try:
            wait_blocked(a, rpid, cpid)
        finally:
            changer.commit()
        try:
            assert future.result(timeout=5)
            assert context(r) == []
        finally:
            r.rollback()
            a.execute("UPDATE f01_private.keys SET status='ACTIVE' WHERE key_id='db-a'")

        cutoff = int(
            scalar(a, "SELECT floor(extract(epoch FROM clock_timestamp()) * 1000)")
        )
        prior = payload(timestamp=cutoff - 1)
        prior_registered = proof(g, prior)
        delayed = payload(timestamp=cutoff - 1)
        at_cutoff = payload(timestamp=cutoff)
        after_cutoff = payload(timestamp=cutoff + 1)
        # A previously registered proof with a future-dated claim is still
        # subject to the newly established issuance cutoff at activation.
        late_payload = payload(timestamp=cutoff + 1)
        late_registered = proof(g, late_payload)
        register = "SELECT f01_api.register_proof(%s,%s,%s)"
        changer.execute(
            "UPDATE f01_private.keys SET status='RETIRING', issuance_cutoff_ms=%s WHERE key_id='db-a'",
            (cutoff,),
        )
        gate_pid = scalar(g, "SELECT pg_backend_pid()")
        future = pool.submit(
            denied, g, register, (after_cutoff, "db-a", mac(after_cutoff))
        )
        try:
            wait_blocked(a, gate_pid, cpid)
        finally:
            changer.commit()
        try:
            assert future.result(timeout=5) == "42501"
            assert denied(g, register, (at_cutoff, "db-a", mac(at_cutoff))) == "42501"
            for p in (at_cutoff, after_cutoff):
                nonce = json.loads(p)[13]
                assert not scalar(
                    a,
                    "SELECT EXISTS (SELECT 1 FROM f01_private.nonces WHERE http_key_id='http-a' AND nonce=%s)",
                    (nonce,),
                )
                assert not scalar(
                    a,
                    "SELECT EXISTS (SELECT 1 FROM f01_private.requests WHERE http_key_id='http-a' AND nonce=%s)",
                    (nonce,),
                )
            bind(g, r, prior_registered)
            activate(r)
            assert patients(r) == [("A", "p1")]
            r.commit()
            bind(g, r, late_registered)
            assert denied(r, "SELECT f01_api.activate()") == "42501"
            assert context(r) == []
            r.rollback()
            assert (
                denied(g, register, (late_payload, "db-b", mac(late_payload, "db-b")))
                == "23505"
            )
            bind(g, r, proof(g, delayed))
            activate(r)
            assert patients(r) == [("A", "p1")]
            r.commit()
            for p in (prior, delayed):
                for key in ("db-a", "db-b"):
                    assert denied(g, register, (p, key, mac(p, key))) == "23505"
            bind(g, r, proof(g, after_cutoff, key="db-b"))
            activate(r)
            assert patients(r) == [("A", "p1")]
            r.commit()
            assert (
                denied(g, register, (after_cutoff, "db-b", mac(after_cutoff, "db-b")))
                == "23505"
            )
            assert (
                denied(g, register, (after_cutoff, "db-a", mac(after_cutoff)))
                == "42501"
            )
            print(
                "C9 retiring cutoff PASS: pre-cutoff registration/activation accepted; at/after cutoff denied; rotation lock and cross-key replay verified",
                flush=True,
            )
        finally:
            a.execute(
                "UPDATE f01_private.keys SET status='ACTIVE', issuance_cutoff_ms=NULL WHERE key_id='db-a'"
            )

    exists = "SELECT EXISTS (SELECT 1 FROM f01_private.requests WHERE id=%s)"
    stamp = (
        int(scalar(a, "SELECT extract(epoch FROM clock_timestamp()) * 1000")) - 55_000
    )
    idle_payload = payload(timestamp=stamp)
    idle = proof(g, idle_payload)
    locked = proof(g, payload(timestamp=stamp))
    active = proof(g, payload(timestamp=stamp))
    eligible = proof(g, payload(timestamp=stamp, purpose="NOTE_WRITE"))
    unknown = proof(g, payload(timestamp=stamp, purpose="NOTE_WRITE"))
    live = proof(g)
    nonce = a.execute(
        "SELECT http_key_id, nonce FROM f01_private.requests WHERE id=%s", (idle,)
    ).fetchone()
    with connect() as r, connect("admin") as lock_holder:
        # PostgreSQL's unavailable/non-normal transaction status must never be
        # treated as an abort. Only the fixture administrator can inject it.
        assert scalar(a, "SELECT pg_xact_status('0'::xid8)") is None
        pid, _ = transaction_id(r)
        denied(g, "SELECT f01_api.bind(%s,'PRIMARY',%s,'0'::xid8)", (unknown, pid))
        a.execute(
            """
            INSERT INTO f01_private.bindings(request_id,stage,database_oid,login_oid,pid,xid)
            VALUES (%s,'PRIMARY',(SELECT oid FROM pg_database WHERE datname=current_database()),
                    'f01_runtime'::regrole::oid,%s,'0'::xid8)
            """,
            (unknown, pid),
        )
        deny_rebind(g, r, unknown, "RECEIPT_RECOVERY")
        bind(g, r, eligible)
        r.rollback()
        bind(g, r, active)
        lock_holder.execute(
            "SELECT id FROM f01_private.requests WHERE id=%s FOR UPDATE", (locked,)
        )
        scalar(g, "SELECT f01_api.cleanup()")
        assert all(
            scalar(a, exists, (request,)) for request in (idle, active, eligible, live)
        )
        wait_until_expired(a, stamp)
        assert scalar(g, "SELECT f01_api.cleanup()") > 0
        assert not scalar(a, exists, (idle,))
        assert scalar(a, exists, (locked,))
        assert scalar(a, exists, (active,))
        assert not scalar(a, exists, (eligible,))
        assert scalar(a, exists, (unknown,))
        assert scalar(a, exists, (live,))
        denied(r, "SELECT f01_api.activate()")
        assert context(r) == []
        r.rollback()
        lock_holder.commit()
    scalar(g, "SELECT f01_api.cleanup()")
    assert not scalar(a, exists, (active,))
    assert not scalar(a, exists, (locked,))
    assert scalar(a, exists, (live,))
    assert not scalar(a, exists, (eligible,))
    assert scalar(a, exists, (unknown,))
    assert not scalar(
        a,
        "SELECT EXISTS (SELECT 1 FROM f01_private.nonces WHERE http_key_id=%s AND nonce=%s)",
        nonce,
    )
    assert (
        denied(
            g,
            "SELECT f01_api.register_proof(%s,'db-a',%s)",
            (idle_payload, mac(idle_payload)),
        )
        == "42501"
    )
    print(
        "criterion 9: ordered revocation/rotation lock edges, retiring key verification, live/active/unknown binding retention, expired idle cleanup; PASS",
        flush=True,
    )


def run():
    with admin() as a, gate() as g:
        check_live_and_rls(g, a)
        check_locks_and_cleanup(g, a)
        check_stages(g, a)


if __name__ == "__main__":
    run()

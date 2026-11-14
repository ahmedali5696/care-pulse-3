"""F-01 adversarial evidence using real service logins in disposable CI."""

from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from hashlib import sha256
from hmac import new
from pathlib import Path
from queue import Queue
from secrets import token_bytes
from uuid import UUID, uuid4

import pytest
from alembic.config import Config
from alembic.script import ScriptDirectory
from carepulse.identity.authorization import protected_transaction
from carepulse.identity.contracts import ContractError
from carepulse.identity.receipts import (
    Receipt,
    insert_tenant_receipt,
    lookup_tenant_receipt,
)
from carepulse.identity.runtime_identity import bound_transaction, current_request
from carepulse.identity.signing import canonical_payload
from carepulse.identity.staff import change_receptionist, change_receptionist_manager
from carepulse.identity.staff_contracts import SetReceptionistGrant, StaffCommand
from carepulse.notifications.evidence import append_sensitive_audit
from postgres_locks import wait_for_postgres_lock
from runtime_identity_fixture import (
    db_proof_headers,
    registered_test_request,
    signed_test_transaction,
)
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError, OperationalError
from test_appointment_persistence import _patient
from test_patients import patient_backend as patient_backend  # noqa: PLC0414
from test_request_boundary import _signed
from test_request_boundary import boundary_data as boundary_data  # noqa: PLC0414


def _register(connection, envelope, body, headers):
    proof = dict(headers)
    return connection.scalar(
        text("SELECT public.f01_register_proof(:payload, :key, :mac, :body)"),
        {
            "payload": canonical_payload(envelope),
            "key": proof[b"x-carepulse-db-key-id"].decode("ascii"),
            "mac": bytes.fromhex(proof[b"x-carepulse-db-proof"].decode("ascii")),
            "body": body,
        },
    )


@pytest.fixture
def proof_keys(boundary_data):
    clinic = boundary_data
    keys = [(f"synthetic-{uuid4().hex}", token_bytes(32)) for _ in range(2)]
    with clinic.migration.begin() as connection:
        connection.execute(text("SET LOCAL ROLE carepulse_identity_owner"))
        connection.execute(
            text(
                "INSERT INTO f01_private.keys (key_id, secret, status) VALUES (:id, :secret, 'ACTIVE')"
            ),
            [{"id": key, "secret": secret} for key, secret in keys],
        )
    try:
        yield keys
    finally:
        with clinic.migration.begin() as connection:
            connection.execute(text("SET LOCAL ROLE carepulse_identity_owner"))
            for key, _ in keys:
                connection.execute(
                    text("DELETE FROM f01_private.requests WHERE database_key_id=:key"),
                    {"key": key},
                )
                connection.execute(
                    text("DELETE FROM f01_private.keys WHERE key_id=:key"), {"key": key}
                )


def _proof_for(envelope, key):
    key_id, secret = key
    payload = (
        b"carepulse-db-context-v1\x00"
        + key_id.encode()
        + b"\x00"
        + canonical_payload(envelope)
    )
    return [
        (b"x-carepulse-db-key-id", key_id.encode()),
        (b"x-carepulse-db-proof", new(secret, payload, sha256).hexdigest().encode()),
    ]


def _nonce_counts(clinic, identity):
    with clinic.migration.begin() as connection:
        connection.execute(text("SET LOCAL ROLE carepulse_identity_owner"))
        return connection.execute(
            text("""
                SELECT (SELECT count(*) FROM f01_private.nonces WHERE http_key_id=:key AND nonce=:nonce),
                       (SELECT count(*) FROM f01_private.requests WHERE http_key_id=:key AND nonce=:nonce)
            """),
            {"key": identity.key_id, "nonce": identity.nonce},
        ).one()


def _bind(clinic, request, pid, xid, stage="PRIMARY"):
    with clinic.gate.begin() as gate:
        gate.execute(
            text("SELECT public.f01_bind(:request, :stage, :pid, CAST(:xid AS xid8))"),
            {"request": request, "stage": stage, "pid": pid, "xid": xid},
        )


def _transaction_id(connection):
    return connection.execute(
        text("SELECT pg_backend_pid(), pg_current_xact_id()::text")
    ).one()


def _deny_stage(clinic, request, stage):
    with clinic.runtime.begin() as connection:
        with pytest.raises(DBAPIError) as denied:
            _bind(clinic, request, *_transaction_id(connection), stage=stage)
        assert denied.value.orig.sqlstate == "42501"
        assert connection.scalar(text("SELECT public.f01_org()")) is None


def _assert_stage(connection, clinic, request, stage):
    assert connection.execute(
        text(
            "SELECT organization_id, staff_id, session_id, request_id, stage FROM public.f01_context()"
        )
    ).one() == (str(clinic.tenant), str(clinic.staff), clinic.session, request, stage)


@pytest.mark.parametrize("outcome", ["aborted", "committed"])
def test_receipt_recovery_requires_aborted_primary_and_is_read_only(
    boundary_data, outcome
):
    clinic = boundary_data
    with clinic.migration.begin() as connection:
        patient = _patient(connection, clinic.tenant)
    identity, body, _ = _signed(
        clinic,
        command={"patient_id": str(patient)},
        path="/v1/appointments",
        purpose="APPOINTMENT_BOOKING",
    )
    receipt = Receipt(
        actor_id=clinic.staff,
        staff_id=clinic.staff,
        organization_id=clinic.tenant,
        operation="appointments.book",
        idempotency_key=identity.idempotency_key,
        request_digest=sha256(body).hexdigest(),
        result_projection={"id": str(uuid4())},
        result_revision=0,
    )
    # A separate successful request owns the receipt; recovery must only read it.
    winner = replace(identity, nonce=f"synthetic_{uuid4().hex}")
    with (
        registered_test_request(clinic, winner, body),
        bound_transaction(clinic.runtime, winner) as connection,
    ):
        insert_tenant_receipt(connection, receipt)
    with clinic.gate.begin() as gate:
        request = _register(gate, identity, body, db_proof_headers(identity))
    for stage in (
        "RECEIPT_RECOVERY",
        "INVITATION_PREPARE",
        "INVITATION_APPLY",
        "SESSION_CLEANUP",
    ):
        _deny_stage(clinic, request, stage)
    with clinic.runtime.connect() as primary:
        _bind(clinic, request, *_transaction_id(primary))
        primary.execute(text("SELECT public.f01_activate()"))
        _assert_stage(primary, clinic, request, "PRIMARY")
        _deny_stage(clinic, request, "RECEIPT_RECOVERY")
        if outcome == "aborted":
            primary.rollback()
        else:
            primary.commit()
    _deny_stage(clinic, request, "PRIMARY")
    if outcome == "committed":
        _deny_stage(clinic, request, "RECEIPT_RECOVERY")
        return
    with clinic.runtime.begin() as connection:
        _bind(clinic, request, *_transaction_id(connection), stage="RECEIPT_RECOVERY")
        connection.execute(text("SELECT public.f01_activate()"))
        _assert_stage(connection, clinic, request, "RECEIPT_RECOVERY")
        stored = lookup_tenant_receipt(
            connection,
            clinic.tenant,
            clinic.staff,
            receipt.operation,
            receipt.idempotency_key,
        )
        assert (
            stored is not None
            and stored["result_projection"] == receipt.result_projection
        )
        with pytest.raises(DBAPIError) as denied, connection.begin_nested():
            insert_tenant_receipt(connection, receipt)
        assert denied.value.orig.sqlstate == "42501"
        with pytest.raises(DBAPIError) as denied, connection.begin_nested():
            connection.execute(
                text("""INSERT INTO public.appointment_day_counters
                    (organization_id, service_date, last_number)
                    VALUES (:tenant, CURRENT_DATE, 1)"""),
                {"tenant": clinic.tenant},
            )
        assert denied.value.orig.sqlstate == "42501"
        assert (
            connection.scalar(
                text("SELECT count(*) FROM public.appointment_day_counters")
            )
            == 0
        )
    _deny_stage(clinic, request, "RECEIPT_RECOVERY")


@pytest.mark.parametrize("outcome", ["aborted", "unactivated", "committed"])
def test_invitation_apply_requires_committed_activated_prepare(
    patient_backend, outcome
):
    clinic = patient_backend
    target = UUID(clinic.receptionist.staff_id)
    with clinic.migration.begin() as connection:
        patient = _patient(connection, clinic.tenant)
    identity, body, _ = _signed(
        clinic,
        path=f"/v1/staff/receptionists/{target}/invite",
        purpose="STAFF_MANAGEMENT",
        command={
            "expected_revision": 0,
            "expected_security_revision": 0,
            "reason": "Synthetic invitation stage",
        },
    )
    with clinic.gate.begin() as gate:
        request = _register(gate, identity, body, db_proof_headers(identity))
    for stage in ("PRIMARY", "INVITATION_APPLY", "RECEIPT_RECOVERY", "SESSION_CLEANUP"):
        _deny_stage(clinic, request, stage)
    with clinic.runtime.connect() as prepare:
        _bind(clinic, request, *_transaction_id(prepare), stage="INVITATION_PREPARE")
        if outcome != "unactivated":
            prepare.execute(text("SELECT public.f01_activate()"))
            _assert_stage(prepare, clinic, request, "INVITATION_PREPARE")
            assert prepare.scalar(
                text("SELECT verified_email FROM public.staff_accounts WHERE id=:id"),
                {"id": target},
            )
            with pytest.raises(DBAPIError) as denied, prepare.begin_nested():
                prepare.execute(
                    text(
                        "UPDATE public.staff_accounts SET updated_at=clock_timestamp() WHERE id=:id"
                    ),
                    {"id": target},
                )
            assert denied.value.orig.sqlstate == "42501"
        _deny_stage(clinic, request, "INVITATION_APPLY")
        if outcome == "aborted":
            prepare.rollback()
        else:
            prepare.commit()
    _deny_stage(clinic, request, "INVITATION_PREPARE")
    if outcome != "committed":
        _deny_stage(clinic, request, "INVITATION_APPLY")
        return
    with clinic.runtime.begin() as apply:
        _bind(clinic, request, *_transaction_id(apply), stage="INVITATION_APPLY")
        apply.execute(text("SELECT public.f01_activate()"))
        _assert_stage(apply, clinic, request, "INVITATION_APPLY")
        assert (
            apply.scalar(
                text("SELECT count(*) FROM public.patients WHERE id=:id"),
                {"id": patient},
            )
            == 0
        )
        with pytest.raises(DBAPIError) as denied, apply.begin_nested():
            apply.scalar(
                text("SELECT public.delete_staff_sessions(:tenant, :user)"),
                {"tenant": clinic.tenant, "user": clinic.native_user},
            )
        assert denied.value.orig.sqlstate == "42501"
    _deny_stage(clinic, request, "INVITATION_APPLY")


@pytest.mark.parametrize(
    "outcome",
    [
        "aborted",
        "no_evidence",
        "audit_only",
        "write_only",
        "savepoint_rollback",
        "committed",
    ],
)
def test_session_cleanup_requires_committed_matching_revocation(
    patient_backend, outcome
):
    clinic = patient_backend
    target = UUID(clinic.receptionist.staff_id)
    with clinic.migration.connect() as connection:
        user = connection.scalar(
            text("SELECT auth_user_id FROM public.staff_accounts WHERE id=:id"),
            {"id": target},
        )
    command = StaffCommand(
        expected_revision=0,
        expected_security_revision=0,
        reason="Synthetic stage withdrawal",
    )
    identity, body, _ = _signed(
        clinic,
        path=f"/v1/staff/receptionists/{target}/disable",
        purpose="STAFF_MANAGEMENT",
        command=command.model_dump(),
    )
    cleanup = text("SELECT public.delete_staff_sessions(:tenant, :user)")
    params = {"tenant": clinic.tenant, "user": user}
    if outcome == "audit_only":
        # A real prior withdrawal cannot authorize a different primary request.
        with clinic.migration.begin() as connection:
            connection.execute(
                text(
                    "UPDATE public.staff_accounts SET enabled=false, security_revision=1 WHERE id=:id"
                ),
                {"id": target},
            )
    with registered_test_request(clinic, identity, body):
        request = current_request.get().request_id
        _deny_stage(clinic, request, "SESSION_CLEANUP")
        try:
            with protected_transaction(
                clinic.runtime, identity, clinic.config.policy.session_idle_minutes
            ) as (connection, actor):
                _assert_stage(connection, clinic, request, "PRIMARY")
                with pytest.raises(DBAPIError) as denied, connection.begin_nested():
                    connection.scalar(cleanup, params)
                assert denied.value.orig.sqlstate == "42501"
                if outcome == "audit_only":
                    append_sensitive_audit(
                        connection,
                        {
                            "organization_id": clinic.tenant,
                            "actor_staff_id": clinic.staff,
                            "actor_role": "DOCTOR_OWNER",
                            "action": "RECEPTIONIST_DISABLE",
                            "reason": command.reason,
                            "correlation_id": str(uuid4()),
                            "before_state": {"id": str(target), "enabled": True},
                            "after_state": {"id": str(target), "enabled": False},
                        },
                    )
                elif outcome == "write_only":
                    assert (
                        connection.execute(
                            text(
                                "UPDATE public.staff_accounts SET enabled=false, security_revision=1 WHERE id=:id"
                            ),
                            {"id": target},
                        ).rowcount
                        == 1
                    )
                elif outcome == "savepoint_rollback":
                    savepoint = connection.begin_nested()
                    _, _, native_user = change_receptionist(
                        connection, actor, target, command, "disable"
                    )
                    assert native_user == user
                    savepoint.rollback()
                elif outcome != "no_evidence":
                    _, _, native_user = change_receptionist(
                        connection, actor, target, command, "disable"
                    )
                    assert native_user == user
                _deny_stage(clinic, request, "SESSION_CLEANUP")
                if outcome == "aborted":
                    raise RuntimeError("synthetic primary abort")
        except RuntimeError as error:
            assert outcome == "aborted" and str(error) == "synthetic primary abort"
        _deny_stage(clinic, request, "PRIMARY")
        if outcome != "committed":
            _deny_stage(clinic, request, "SESSION_CLEANUP")
        else:
            # The earlier failed savepoint leaves a subtransaction active in PostgreSQL.
            # Inspect committed evidence using the same restricted verifier role.
            with clinic.migration.begin() as observer:
                observer.execute(text("SET LOCAL ROLE carepulse_identity_owner"))
                evidence = observer.execute(
                    text("""
                        SELECT pg_xact_status(b.xid), s.enabled, s.security_revision,
                               a.before_state->>'enabled', a.after_state->>'enabled',
                               a.xmin = b.xid::xid,
                               f01_private.cleanup_proven(r, b.xid),
                               p.revoked_staff_id=s.id, p.revocation_audit_id=a.id
                        FROM f01_private.requests r
                        JOIN f01_private.bindings b ON b.request_id=r.id AND b.stage='PRIMARY'
                        JOIN f01_private.activations p ON p.request_id=b.request_id AND p.stage=b.stage
                        JOIN public.staff_accounts s ON s.id=r.target_id AND s.organization_id=r.organization_id
                        JOIN public.sensitive_audit a ON a.organization_id=r.organization_id
                            AND a.actor_staff_id=r.staff_id AND a.actor_role=r.role
                            AND a.action='RECEPTIONIST_DISABLE'
                            AND a.before_state->>'id'=r.target_id::text
                            AND a.after_state->>'id'=r.target_id::text
                        WHERE r.id=:request
                    """),
                    {"request": request},
                ).one()
                assert evidence == (
                    "committed",
                    False,
                    1,
                    "true",
                    "false",
                    False,
                    True,
                    True,
                    True,
                )
            with bound_transaction(
                clinic.runtime, identity, stage="SESSION_CLEANUP"
            ) as connection:
                _assert_stage(connection, clinic, request, "SESSION_CLEANUP")
                with pytest.raises(DBAPIError) as denied, connection.begin_nested():
                    connection.scalar(
                        cleanup, {"tenant": clinic.tenant, "user": clinic.native_user}
                    )
                assert denied.value.orig.sqlstate == "42501"
                with pytest.raises(DBAPIError) as denied, connection.begin_nested():
                    connection.execute(
                        text(
                            "UPDATE public.staff_accounts SET updated_at=clock_timestamp() WHERE id=:id"
                        ),
                        {"id": target},
                    )
                assert denied.value.orig.sqlstate == "42501"
                assert connection.scalar(cleanup, params) == 1
            _deny_stage(clinic, request, "SESSION_CLEANUP")
    with clinic.migration.connect() as connection:
        assert (
            connection.scalar(
                text('SELECT count(*) FROM public."session" WHERE id=:id'),
                {"id": clinic.session},
            )
            == 1
        )
        assert connection.scalar(
            text('SELECT count(*) FROM public."session" WHERE id=:id'),
            {"id": clinic.receptionist.session_id},
        ) == (0 if outcome == "committed" else 1)


def test_manager_withdrawal_cleanup_uses_committed_primary_evidence(patient_backend):
    clinic = patient_backend
    target = UUID(clinic.receptionist.staff_id)
    path = f"/v1/staff/receptionists/{target}/permissions"
    grant = SetReceptionistGrant(
        reason="Synthetic grant",
        operational_manager=True,
        expected_revision=0,
        expected_security_revision=0,
    )
    with signed_test_transaction(
        clinic,
        clinic.owner,
        method="PATCH",
        path=path,
        purpose="STAFF_MANAGEMENT",
        body=grant.model_dump(),
    ) as (connection, actor):
        _, _, user = change_receptionist_manager(connection, actor, target, grant)
        assert user is None
        request = current_request.get().request_id
    _deny_stage(clinic, request, "SESSION_CLEANUP")
    revoke = SetReceptionistGrant(
        reason="Synthetic withdrawal",
        operational_manager=False,
        expected_revision=1,
        expected_security_revision=1,
    )
    identity, body, _ = _signed(
        clinic, path=path, purpose="STAFF_MANAGEMENT", command=revoke.model_dump()
    )
    identity = replace(identity, method="PATCH")
    with registered_test_request(clinic, identity, body):
        request = current_request.get().request_id
        with protected_transaction(
            clinic.runtime, identity, clinic.config.policy.session_idle_minutes
        ) as (connection, actor):
            with connection.begin_nested():
                _, _, user = change_receptionist_manager(
                    connection, actor, target, revoke
                )
            assert user is not None
            _deny_stage(clinic, request, "SESSION_CLEANUP")
        with bound_transaction(
            clinic.runtime, identity, stage="SESSION_CLEANUP"
        ) as connection:
            assert (
                connection.scalar(
                    text("SELECT public.delete_staff_sessions(:tenant, :user)"),
                    {"tenant": clinic.tenant, "user": user},
                )
                == 1
            )
        _deny_stage(clinic, request, "SESSION_CLEANUP")
    with clinic.migration.connect() as connection:
        assert (
            connection.scalar(
                text('SELECT count(*) FROM public."session" WHERE id=:id'),
                {"id": clinic.session},
            )
            == 1
        )
        assert (
            connection.scalar(
                text('SELECT count(*) FROM public."session" WHERE id=:id'),
                {"id": clinic.receptionist.session_id},
            )
            == 0
        )


def _assert_context(connection, clinic, request):
    assert connection.execute(
        text(
            "SELECT organization_id, staff_id, stage, request_id FROM public.f01_context()"
        )
    ).one() == (str(clinic.tenant), str(clinic.staff), "PRIMARY", request)
    assert connection.scalars(text("SELECT id FROM public.organizations")).all() == [
        clinic.tenant
    ]


def test_cross_key_nonce_registration_serializes_and_rolls_back(
    boundary_data, proof_keys
):
    clinic = boundary_data
    first_key, second_key = proof_keys
    identity, body, _ = _signed(clinic)
    waiting_pid = Queue()

    def compete():
        with pytest.raises(DBAPIError) as denied, clinic.gate.begin() as connection:
            waiting_pid.put(connection.scalar(text("SELECT pg_backend_pid()")))
            _register(connection, identity, body, _proof_for(identity, second_key))
        return denied.value.orig.sqlstate

    with ThreadPoolExecutor(max_workers=1) as pool:
        with clinic.gate.begin() as first:
            holder = first.scalar(text("SELECT pg_backend_pid()"))
            request = _register(first, identity, body, _proof_for(identity, first_key))
            future = pool.submit(compete)
            with clinic.migration.connect() as observer:
                wait_for_postgres_lock(
                    observer, waiting_pid.get(timeout=5), holder, timeout=5
                )
        assert future.result(timeout=10) == "23505"
    assert request > 0
    assert _nonce_counts(clinic, identity) == (1, 1)

    rolled_back, body, _ = _signed(clinic)
    with clinic.gate.connect() as connection:
        _register(connection, rolled_back, body, _proof_for(rolled_back, first_key))
        connection.rollback()
    assert _nonce_counts(clinic, rolled_back) == (0, 0)
    with clinic.gate.begin() as connection:
        assert (
            _register(
                connection, rolled_back, body, _proof_for(rolled_back, second_key)
            )
            > 0
        )
    assert _nonce_counts(clinic, rolled_back) == (1, 1)


def test_binding_and_savepoints_cannot_switch_request_or_transaction(boundary_data):
    clinic = boundary_data
    identity, body, _ = _signed(clinic)
    replacement, replacement_body, _ = _signed(clinic, tenant=clinic.other)
    with clinic.gate.begin() as gate:
        request = _register(gate, identity, body, db_proof_headers(identity))
        other_request = _register(
            gate, replacement, replacement_body, db_proof_headers(replacement)
        )
    with clinic.runtime.connect() as connection, clinic.runtime.connect() as other:
        pid, xid = _transaction_id(connection)
        other_pid, other_xid = _transaction_id(other)
        with pytest.raises(DBAPIError) as denied:
            _bind(clinic, other_request, other_pid, xid)
        assert denied.value.orig.sqlstate == "42501"
        _bind(clinic, request, pid, xid)
        with pytest.raises(DBAPIError) as denied:
            _bind(clinic, request, other_pid, other_xid)
        assert denied.value.orig.sqlstate in ("42501", "23505")
        with pytest.raises(DBAPIError) as denied, other.begin_nested():
            other.execute(text("SELECT public.f01_activate()"))
        assert denied.value.orig.sqlstate == "42501"
        assert other.scalar(text("SELECT public.f01_org()")) is None
        assert connection.scalar(text("SELECT public.f01_org()")) is None

        before = connection.begin_nested()
        connection.execute(text("SELECT public.f01_activate()"))
        _assert_context(connection, clinic, request)
        after = connection.begin_nested()
        connection.execute(
            text("SELECT set_config('app.organization_id', :tenant, true)"),
            {"tenant": str(clinic.other)},
        )
        after.rollback()
        _assert_context(connection, clinic, request)
        before.rollback()
        assert connection.scalar(text("SELECT public.f01_org()")) is None
        assert connection.scalar(text("SELECT count(*) FROM public.organizations")) == 0
        assert _transaction_id(connection) == (pid, xid)
        with pytest.raises(DBAPIError) as denied:
            _bind(clinic, other_request, pid, xid)
        assert denied.value.orig.sqlstate in ("42501", "23505")
        connection.execute(
            text("SELECT set_config('app.organization_id', :tenant, true)"),
            {"tenant": str(clinic.other)},
        )
        connection.execute(text("SELECT public.f01_activate()"))
        _assert_context(connection, clinic, request)
        assert _transaction_id(connection) == (pid, xid)
        with pytest.raises(DBAPIError) as denied, connection.begin_nested():
            connection.execute(text("SELECT public.f01_activate()"))
        assert denied.value.orig.sqlstate == "23505"
        _assert_context(connection, clinic, request)
        connection.rollback()
        assert (
            connection.scalar(
                text("SELECT pg_xact_status(CAST(:xid AS xid8))"), {"xid": xid}
            )
            == "aborted"
        )
        new_pid, new_xid = _transaction_id(connection)
        assert new_pid == pid and new_xid != xid
        with pytest.raises(DBAPIError) as denied:
            _bind(clinic, request, new_pid, new_xid)
        assert denied.value.orig.sqlstate == "42501"
        assert connection.scalar(text("SELECT public.f01_org()")) is None


def test_retiring_key_cutoff_is_rechecked_at_registration_and_activation(
    boundary_data, proof_keys
):
    clinic = boundary_data
    retiring, active = proof_keys
    with clinic.migration.connect() as connection:
        cutoff = connection.scalar(
            text("SELECT floor(extract(epoch FROM clock_timestamp()) * 1000)::bigint")
        )

    def issued(offset):
        envelope, body, _ = _signed(clinic)
        return replace(envelope, timestamp=str(cutoff + offset)), body

    prior, prior_body = issued(-1)
    late, late_body = issued(1)
    delayed, delayed_body = issued(-1)
    exact, exact_body = issued(0)
    after, after_body = issued(1)
    with clinic.gate.begin() as gate:
        prior_id = _register(gate, prior, prior_body, _proof_for(prior, retiring))
        late_id = _register(gate, late, late_body, _proof_for(late, retiring))
    waiting_pid = Queue()

    def register_after_cutoff():
        with pytest.raises(DBAPIError) as denied, clinic.gate.begin() as gate:
            waiting_pid.put(gate.scalar(text("SELECT pg_backend_pid()")))
            _register(gate, after, after_body, _proof_for(after, retiring))
        return denied.value.orig.sqlstate

    with ThreadPoolExecutor(max_workers=1) as pool:
        with clinic.migration.begin() as changer:
            changer.execute(text("SET LOCAL ROLE carepulse_identity_owner"))
            holder = changer.scalar(text("SELECT pg_backend_pid()"))
            changer.execute(
                text(
                    "UPDATE f01_private.keys SET status='RETIRING', issuance_cutoff_ms=:cutoff WHERE key_id=:key"
                ),
                {"cutoff": cutoff, "key": retiring[0]},
            )
            future = pool.submit(register_after_cutoff)
            with clinic.migration.connect() as observer:
                wait_for_postgres_lock(
                    observer, waiting_pid.get(timeout=5), holder, timeout=5
                )
        assert future.result(timeout=10) == "42501"

    with pytest.raises(DBAPIError) as denied, clinic.gate.begin() as gate:
        _register(gate, exact, exact_body, _proof_for(exact, retiring))
    assert denied.value.orig.sqlstate == "42501"
    assert _nonce_counts(clinic, exact) == _nonce_counts(clinic, after) == (0, 0)
    with clinic.gate.begin() as gate:
        delayed_id = _register(
            gate, delayed, delayed_body, _proof_for(delayed, retiring)
        )
        active_id = _register(gate, after, after_body, _proof_for(after, active))
    for request in (prior_id, delayed_id, active_id):
        with clinic.runtime.begin() as connection:
            _bind(clinic, request, *_transaction_id(connection))
            connection.execute(text("SELECT public.f01_activate()"))
            _assert_context(connection, clinic, request)
    with clinic.runtime.begin() as connection:
        _bind(clinic, late_id, *_transaction_id(connection))
        with pytest.raises(DBAPIError) as denied, connection.begin_nested():
            connection.execute(text("SELECT public.f01_activate()"))
        assert denied.value.orig.sqlstate == "42501"
        assert connection.scalar(text("SELECT public.f01_org()")) is None
    for envelope, body in (
        (prior, prior_body),
        (delayed, delayed_body),
        (late, late_body),
        (after, after_body),
    ):
        with pytest.raises(DBAPIError) as denied, clinic.gate.begin() as gate:
            _register(gate, envelope, body, _proof_for(envelope, active))
        assert denied.value.orig.sqlstate == "23505"
        assert _nonce_counts(clinic, envelope) == (1, 1)


def _spoof_gucs(connection, clinic):
    connection.execute(
        text("""
        SELECT set_config('app.organization_id', :tenant, true),
               set_config('app.staff_id', :staff, true),
               set_config('app.staff_role', 'DOCTOR_OWNER', true)
        """),
        {"tenant": str(clinic.tenant), "staff": str(clinic.staff)},
    )


@pytest.mark.parametrize(
    ("resource", "first"),
    [
        ("organization", "revocation"),
        ("staff", "activation"),
        ("staff", "revocation"),
        ("session", "activation"),
        ("session", "revocation"),
        ("key", "activation"),
        ("key", "revocation"),
    ],
)
def test_live_revocation_and_activation_serialize(
    boundary_data, proof_keys, resource, first
):
    clinic = boundary_data
    key = proof_keys[0]
    update, params, sqlstate = {
        "organization": (
            "UPDATE public.organizations SET state='SUSPENDED' WHERE id=:id",
            {"id": clinic.tenant},
            "42501",
        ),
        "staff": (
            "UPDATE public.staff_accounts SET security_revision=security_revision+1 WHERE id=:id",
            {"id": clinic.staff},
            "28000",
        ),
        "session": (
            'DELETE FROM public."session" WHERE id=:id',
            {"id": clinic.session},
            "28000",
        ),
        "key": (
            "UPDATE f01_private.keys SET status='REVOKED' WHERE key_id=:id",
            {"id": key[0]},
            "42501",
        ),
    }[resource]
    identity, body, _ = _signed(clinic)
    next_identity, next_body, _ = _signed(clinic)
    with clinic.gate.begin() as gate:
        request = _register(gate, identity, body, _proof_for(identity, key))
        next_request = _register(
            gate, next_identity, next_body, _proof_for(next_identity, key)
        )

    # Trusted migration fixture supplies an actual committed change to live authority;
    # both activation attempts use the real API login and committed gate bindings.
    def revoke(connection):
        if resource == "key":
            connection.execute(text("SET LOCAL ROLE carepulse_identity_owner"))
        assert connection.execute(text(update), params).rowcount == 1

    def denied_activation(connection):
        with pytest.raises(DBAPIError) as denied, connection.begin_nested():
            connection.execute(text("SELECT public.f01_activate()"))
        assert denied.value.orig.sqlstate == sqlstate
        assert connection.scalar(text("SELECT public.f01_org()")) is None
        assert connection.scalar(text("SELECT count(*) FROM public.organizations")) == 0

    waiting_pid = Queue()

    def revoke_later():
        with clinic.migration.begin() as changer:
            waiting_pid.put(changer.scalar(text("SELECT pg_backend_pid()")))
            revoke(changer)

    def activate_later():
        with clinic.runtime.begin() as runtime:
            pid, xid = _transaction_id(runtime)
            _bind(clinic, request, pid, xid)
            waiting_pid.put(pid)
            denied_activation(runtime)

    with ThreadPoolExecutor(max_workers=1) as pool:
        if first == "activation":
            with clinic.runtime.begin() as runtime:
                pid, xid = _transaction_id(runtime)
                _bind(clinic, request, pid, xid)
                runtime.execute(text("SELECT public.f01_activate()"))
                _assert_context(runtime, clinic, request)
                future = pool.submit(revoke_later)
                with clinic.migration.connect() as observer:
                    wait_for_postgres_lock(
                        observer, waiting_pid.get(timeout=5), pid, timeout=5
                    )
                _assert_context(runtime, clinic, request)
        else:
            with clinic.migration.begin() as changer:
                pid = changer.scalar(text("SELECT pg_backend_pid()"))
                revoke(changer)
                future = pool.submit(activate_later)
                with clinic.migration.connect() as observer:
                    wait_for_postgres_lock(
                        observer, waiting_pid.get(timeout=5), pid, timeout=5
                    )
        future.result(timeout=10)
    with clinic.runtime.begin() as runtime:
        _bind(clinic, next_request, *_transaction_id(runtime))
        denied_activation(runtime)


def test_cleanup_skips_locked_and_in_progress_bindings(boundary_data):
    clinic = boundary_data
    registered = {}
    with clinic.gate.begin() as gate:
        for label in ("active", "locked", "aborted", "committed", "live"):
            identity, body, _ = _signed(clinic)
            registered[label] = (
                _register(gate, identity, body, db_proof_headers(identity)),
                identity,
            )
    ids = {label: pair[0] for label, pair in registered.items()}
    active, locked, aborted, committed, live = (
        ids[label] for label in ("active", "locked", "aborted", "committed", "live")
    )
    with clinic.runtime.connect() as runtime:
        _bind(clinic, aborted, *_transaction_id(runtime))
        runtime.rollback()
        _bind(clinic, committed, *_transaction_id(runtime))
        runtime.commit()
        _bind(clinic, active, *_transaction_id(runtime))
        # Age only these isolated requests, after the gate has bound actual xids.
        with clinic.migration.begin() as migration:
            migration.execute(text("SET LOCAL ROLE carepulse_identity_owner"))
            migration.execute(
                text(
                    "UPDATE f01_private.requests SET expires_at=clock_timestamp()-interval '1 second' WHERE id IN (:active,:locked,:aborted,:committed)"
                ),
                ids,
            )

        def remaining():
            with clinic.migration.begin() as observer:
                observer.execute(text("SET LOCAL ROLE carepulse_identity_owner"))
                return set(
                    observer.scalars(
                        text(
                            "SELECT id FROM f01_private.requests WHERE id IN (:active,:locked,:aborted,:committed,:live)"
                        ),
                        ids,
                    )
                )

        with clinic.migration.begin() as lock_holder:
            lock_holder.execute(text("SET LOCAL ROLE carepulse_identity_owner"))
            assert (
                lock_holder.scalar(
                    text(
                        "SELECT id FROM f01_private.requests WHERE id=:locked FOR UPDATE"
                    ),
                    ids,
                )
                == locked
            )
            with clinic.gate.begin() as gate:
                # The cleanup must skip—not wait for—the locked request.
                gate.execute(text("SET LOCAL lock_timeout='2s'"))
                assert gate.scalar(text("SELECT public.f01_cleanup()")) >= 2
            assert remaining() == {active, locked, live}
            with pytest.raises(DBAPIError) as denied, runtime.begin_nested():
                runtime.execute(text("SELECT public.f01_activate()"))
            assert denied.value.orig.sqlstate == "42501"
            assert runtime.scalar(text("SELECT public.f01_org()")) is None
        runtime.rollback()
    with clinic.gate.begin() as gate:
        assert gate.scalar(text("SELECT public.f01_cleanup()")) >= 2
    assert remaining() == {live}
    for label, (_, identity) in registered.items():
        assert _nonce_counts(clinic, identity) == (1, int(label == "live"))


def test_persisted_clock_regression_denies_gate_activation_and_context(boundary_data):
    clinic = boundary_data
    active_identity, active_body, _ = _signed(clinic)
    pending_identity, pending_body, _ = _signed(clinic)
    unbound_identity, unbound_body, _ = _signed(clinic)
    fresh_identity, fresh_body, _ = _signed(clinic)
    with clinic.gate.begin() as gate:
        active = _register(
            gate, active_identity, active_body, db_proof_headers(active_identity)
        )
        pending = _register(
            gate, pending_identity, pending_body, db_proof_headers(pending_identity)
        )
        unbound = _register(
            gate, unbound_identity, unbound_body, db_proof_headers(unbound_identity)
        )
    with (
        clinic.runtime.connect() as runtime,
        clinic.runtime.connect() as pending_runtime,
    ):
        _bind(clinic, active, *_transaction_id(runtime))
        _bind(clinic, pending, *_transaction_id(pending_runtime))
        runtime.execute(text("SELECT public.f01_activate()"))
        _assert_context(runtime, clinic, active)
        # No OS clock change: inject a future trusted high-water mark in the disposable DB.
        with clinic.migration.begin() as migration:
            migration.execute(text("SET LOCAL ROLE carepulse_identity_owner"))
            previous = migration.scalar(
                text(
                    "SELECT last_seen_at FROM f01_private.clock_state WHERE singleton FOR UPDATE"
                )
            )
            migration.execute(
                text(
                    "UPDATE f01_private.clock_state SET last_seen_at=clock_timestamp()+interval '1 hour' WHERE singleton"
                )
            )
        try:
            assert runtime.scalar(text("SELECT public.f01_org()")) is None
            assert (
                runtime.scalar(text("SELECT count(*) FROM public.organizations")) == 0
            )
            with pytest.raises(DBAPIError) as denied, clinic.gate.begin() as gate:
                _register(
                    gate, fresh_identity, fresh_body, db_proof_headers(fresh_identity)
                )
            assert denied.value.orig.sqlstate == "42501"
            assert _nonce_counts(clinic, fresh_identity) == (0, 0)
            _deny_stage(clinic, unbound, "PRIMARY")
            with pytest.raises(DBAPIError) as denied, clinic.gate.begin() as gate:
                gate.scalar(text("SELECT public.f01_cleanup()"))
            assert denied.value.orig.sqlstate == "42501"
            runtime.rollback()
            with pytest.raises(DBAPIError) as denied, pending_runtime.begin_nested():
                pending_runtime.execute(text("SELECT public.f01_activate()"))
            assert denied.value.orig.sqlstate == "42501"
            assert pending_runtime.scalar(text("SELECT public.f01_org()")) is None
        finally:
            with clinic.migration.begin() as migration:
                migration.execute(text("SET LOCAL ROLE carepulse_identity_owner"))
                migration.execute(
                    text(
                        "UPDATE f01_private.clock_state SET last_seen_at=GREATEST(:previous,clock_timestamp()) WHERE singleton"
                    ),
                    {"previous": previous},
                )
    with clinic.gate.begin() as gate:
        assert (
            _register(
                gate, fresh_identity, fresh_body, db_proof_headers(fresh_identity)
            )
            > 0
        )
    with clinic.runtime.begin() as runtime:
        _bind(clinic, unbound, *_transaction_id(runtime))
        runtime.execute(text("SELECT public.f01_activate()"))
        _assert_context(runtime, clinic, unbound)


def test_exact_head_restricts_legacy_login_and_service_role_elevation(role_engine):
    migration = role_engine("carepulse_migration")
    legacy = role_engine("carepulse_runtime")
    try:
        root = Path(__file__).resolve().parents[2]
        config = Config(str(root / "alembic.ini"))
        config.set_main_option("script_location", str(root / "db/migrations"))
        expected_head = ScriptDirectory.from_config(config).get_current_head()
        with migration.connect() as connection:
            assert (
                connection.scalar(
                    text("SELECT version_num FROM public.alembic_version")
                )
                == expected_head
            )
            assert connection.scalar(
                text(
                    "SELECT NOT rolcanlogin FROM pg_roles WHERE rolname='carepulse_runtime'"
                )
            )
        with pytest.raises(OperationalError):
            legacy.connect()
    finally:
        migration.dispose()
        legacy.dispose()


@pytest.mark.parametrize(
    "role", ["carepulse_api", "carepulse_web", "carepulse_worker", "carepulse_gate"]
)
def test_real_service_logins_cannot_forge_authority_with_gucs(
    boundary_data, role_engine, role
):
    clinic = boundary_data
    engine = role_engine(role)
    try:
        with engine.begin() as connection:
            assert connection.scalar(text("SELECT session_user")) == role
            assert connection.scalar(
                text(
                    "SELECT NOT rolsuper AND NOT rolbypassrls AND NOT rolcreaterole FROM pg_roles WHERE rolname=session_user"
                )
            )
            _spoof_gucs(connection, clinic)
            if role == "carepulse_api":
                assert connection.scalar(text("SELECT public.f01_org()")) is None
                assert (
                    connection.scalar(text("SELECT count(*) FROM public.organizations"))
                    == 0
                )
            else:
                with pytest.raises(DBAPIError) as denied, connection.begin_nested():
                    connection.execute(text("SELECT id FROM public.organizations"))
                assert denied.value.orig.sqlstate == "42501"
            with pytest.raises(DBAPIError) as denied, connection.begin_nested():
                connection.execute(text("SELECT public.f01_activate()"))
            assert denied.value.orig.sqlstate == "42501"
            for target in ("carepulse_identity_owner", "carepulse_migration"):
                assert not connection.scalar(
                    text("SELECT pg_has_role(session_user, :role, 'MEMBER')"),
                    {"role": target},
                )
            with pytest.raises(DBAPIError) as denied, connection.begin_nested():
                connection.execute(
                    text("UPDATE f01_private.activations SET revoked_staff_id=NULL")
                )
            assert denied.value.orig.sqlstate == "42501"
    finally:
        engine.dispose()


@pytest.mark.parametrize(
    "change", ["tenant", "staff", "session", "role", "purpose", "path", "body", "key"]
)
def test_gate_rejects_mutated_proof_envelope(boundary_data, change):
    clinic = boundary_data
    identity, body, _ = _signed(clinic)
    headers = db_proof_headers(identity)
    changes = {
        "tenant": {"organization_id": str(clinic.other)},
        "staff": {"staff_id": str(uuid4())},
        "session": {"session_id": "forged-session"},
        "role": {"role": "RECEPTIONIST"},
        "purpose": {"purpose": "APPOINTMENT_BOOKING"},
        "path": {"path": "/v1/appointments"},
    }
    if change in changes:
        identity = replace(identity, **changes[change])
    elif change == "body":
        body += b" "
    else:
        headers = [
            (name, b"missing-key" if name == b"x-carepulse-db-key-id" else value)
            for name, value in headers
        ]
    with pytest.raises(DBAPIError) as denied, clinic.gate.begin() as connection:
        _register(connection, identity, body, headers)
    assert denied.value.orig.sqlstate in ("42501", "22023")


def test_gate_rejects_valid_mac_for_unapproved_route_purpose(boundary_data):
    clinic = boundary_data
    identity, body, _ = _signed(clinic)
    identity = replace(identity, purpose="APPOINTMENT_BOOKING")
    with pytest.raises(DBAPIError) as denied, clinic.gate.begin() as connection:
        _register(connection, identity, body, db_proof_headers(identity))
    assert denied.value.orig.sqlstate in ("42501", "22023")


@pytest.mark.parametrize("change", ["tenant", "staff", "session", "role"])
def test_valid_signature_cannot_override_live_actor_authority(boundary_data, change):
    clinic = boundary_data
    identity, body, _ = _signed(clinic)
    identity = replace(
        identity,
        **{
            "tenant": {"organization_id": str(clinic.other)},
            "staff": {"staff_id": str(uuid4())},
            "session": {"session_id": "forged-session"},
            "role": {"role": "RECEPTIONIST"},
        }[change],
    )
    with (
        registered_test_request(clinic, identity, body),
        pytest.raises(ContractError) as denied,
        bound_transaction(clinic.runtime, identity),
    ):
        pytest.fail("Forged live authority activated")
    assert denied.value.status_code in (401, 403)


@pytest.mark.parametrize("outcome", ["commit", "rollback"])
def test_bound_identity_is_transaction_local_and_replay_is_denied(
    boundary_data, outcome
):
    clinic = boundary_data
    identity, body, _ = _signed(clinic)
    pid = None
    with registered_test_request(clinic, identity, body):
        try:
            with bound_transaction(clinic.runtime, identity) as connection:
                pid = connection.scalar(text("SELECT pg_backend_pid()"))
                assert connection.scalar(text("SELECT session_user")) == "carepulse_api"
                _spoof_gucs(connection, clinic)
                connection.execute(
                    text("SELECT set_config('app.organization_id', :tenant, true)"),
                    {"tenant": str(clinic.other)},
                )
                assert connection.execute(
                    text(
                        "SELECT organization_id, staff_id, purpose, stage FROM public.f01_context()"
                    )
                ).one() == (
                    str(clinic.tenant),
                    str(clinic.staff),
                    identity.purpose,
                    "PRIMARY",
                )
                assert connection.execute(
                    text("SELECT id FROM public.organizations")
                ).scalars().all() == [clinic.tenant]
                assert (
                    connection.scalar(
                        text("SELECT count(*) FROM public.clinic_policy_versions")
                    )
                    == 0
                )
                if outcome == "rollback":
                    raise RuntimeError("synthetic rollback")
        except RuntimeError as error:
            assert outcome == "rollback" and str(error) == "synthetic rollback"
        with clinic.runtime.begin() as connection:
            assert connection.scalar(text("SELECT pg_backend_pid()")) == pid
            assert connection.scalar(text("SELECT public.f01_org()")) is None
            assert (
                connection.scalar(text("SELECT count(*) FROM public.organizations"))
                == 0
            )
        with pytest.raises(ContractError), bound_transaction(clinic.runtime, identity):
            pytest.fail("Consumed primary stage activated again")
    with pytest.raises(DBAPIError) as denied, clinic.gate.begin() as connection:
        _register(connection, identity, body, db_proof_headers(identity))
    assert denied.value.orig.sqlstate in ("23505", "42501", "22023")


def test_cleanup_retains_unknown_transaction_and_nonce_tombstones(boundary_data):
    clinic = boundary_data
    unknown, unknown_body, _ = _signed(clinic)
    idle, idle_body, _ = _signed(clinic)
    with clinic.gate.begin() as connection:
        unknown_id = _register(
            connection, unknown, unknown_body, db_proof_headers(unknown)
        )
        idle_id = _register(connection, idle, idle_body, db_proof_headers(idle))

    # Only the trusted test migration role can inject unavailable xid status.
    with clinic.migration.begin() as connection:
        connection.execute(text("SET LOCAL ROLE carepulse_identity_owner"))
        assert connection.scalar(text("SELECT pg_xact_status('0'::xid8)")) is None
        connection.execute(
            text("""
                INSERT INTO f01_private.bindings
                    (request_id, stage, database_oid, login_oid, pid, xid)
                VALUES (:id, 'PRIMARY',
                    (SELECT oid FROM pg_database WHERE datname=current_database()),
                    'carepulse_api'::regrole::oid, pg_backend_pid(), '0'::xid8)
            """),
            {"id": unknown_id},
        )
        connection.execute(
            text("""
                UPDATE f01_private.requests SET expires_at=clock_timestamp()-interval '1 second'
                WHERE id IN (:unknown, :idle)
            """),
            {"unknown": unknown_id, "idle": idle_id},
        )
    try:
        with clinic.gate.begin() as connection:
            assert connection.scalar(text("SELECT public.f01_cleanup()")) >= 1
        with clinic.migration.begin() as connection:
            connection.execute(text("SET LOCAL ROLE carepulse_identity_owner"))
            assert connection.scalars(
                text(
                    "SELECT id FROM f01_private.requests WHERE id IN (:unknown,:idle)"
                ),
                {"unknown": unknown_id, "idle": idle_id},
            ).all() == [unknown_id]
            assert (
                connection.scalar(
                    text(
                        "SELECT count(*) FROM f01_private.nonces WHERE nonce IN (:unknown,:idle)"
                    ),
                    {"unknown": unknown.nonce, "idle": idle.nonce},
                )
                == 2
            )
    finally:
        with clinic.migration.begin() as connection:
            connection.execute(text("SET LOCAL ROLE carepulse_identity_owner"))
            connection.execute(
                text("DELETE FROM f01_private.requests WHERE id=:id"),
                {"id": unknown_id},
            )

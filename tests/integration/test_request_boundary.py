"""B-01: real PostgreSQL authority, replay and receipt transactions in disposable CI."""

import asyncio
import json
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from hashlib import sha256
from hmac import new
from pathlib import Path
from threading import Event
from types import SimpleNamespace
from uuid import uuid4

import pytest
from carepulse.config import ServiceConfig, _load_policy
from carepulse.db import transaction
from carepulse.identity.authorization import protected_transaction
from carepulse.identity.boundary import ClosedServiceBoundary
from carepulse.identity.contracts import (
    ContractError,
    ResourceFields,
    error_response,
    normalized_request_digest,
    project_visible_resource,
    success_response,
)
from carepulse.identity.receipts import (
    Receipt,
    ReceiptConflict,
    TenantReceiptQuery,
    insert_tenant_receipt,
    resolve_tenant_receipt,
)
from carepulse.identity.signing import (
    PROTOCOL_VERSION,
    CanonicalEnvelope,
    canonical_payload,
    raw_body_sha256,
)
from carepulse.notifications.evidence import append_outbox
from pydantic import BaseModel, ConfigDict
from sqlalchemy import text

_SECRET = "synthetic-B-01-key"
_PATH = "/v1/__synthetic_t023_internal__"
_PURPOSE = "synthetic.test"


class SyntheticRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    value: str


@pytest.fixture
def boundary_data(role_engine, tenant_factory, monkeypatch):
    monkeypatch.setenv(
        "CP_POLICY_FILE",
        str(Path(__file__).parents[1] / "fixtures/foundation-policy.json"),
    )
    policy = _load_policy()
    operator_id, create_tenant = tenant_factory
    active = create_tenant("Synthetic B-01 active")
    other = create_tenant("Synthetic B-01 provisioning")
    migration = role_engine("carepulse_migration")
    runtime = role_engine("carepulse_runtime")
    staff_id = uuid4()
    native_user = f"synthetic-b01-{uuid4()}"
    session_id = f"synthetic-b01-session-{uuid4()}"
    email = f"synthetic-b01-{uuid4()}@example.invalid"
    with migration.begin() as connection:
        connection.execute(
            text("""
                INSERT INTO public."user"
                    (id, name, email, "emailVerified", "createdAt", "updatedAt")
                VALUES (:id, 'Synthetic B-01 Owner', :email, true,
                        clock_timestamp(), clock_timestamp())
            """),
            {"id": native_user, "email": email},
        )
        connection.execute(
            text("""
                INSERT INTO public.staff_accounts
                    (id, organization_id, auth_user_id, verified_email,
                     role, enabled, verified)
                VALUES (:staff, :tenant, :user, :email, 'DOCTOR_OWNER', true, true)
            """),
            {"staff": staff_id, "tenant": active, "user": native_user, "email": email},
        )
        invitation_id = uuid4()
        connection.execute(
            text("""
                INSERT INTO public.invitations
                    (id, organization_id, purpose, target_email, proof_digest,
                     issued_by_operator_id, expires_at)
                VALUES (:id, :tenant, 'FIRST_OWNER', :email, :digest,
                        :operator, clock_timestamp() + interval '20 minutes')
            """),
            {
                "id": invitation_id,
                "tenant": active,
                "email": email,
                "digest": sha256(str(invitation_id).encode()).hexdigest(),
                "operator": operator_id,
            },
        )
        connection.execute(
            text("""
                UPDATE public.invitations
                SET state='ACCEPTED', accepted_by_user_id=:user WHERE id=:id
            """),
            {"user": native_user, "id": invitation_id},
        )
        connection.execute(
            text("UPDATE public.organizations SET state='ACTIVE' WHERE id=:id"),
            {"id": active},
        )
        connection.execute(
            text("""
                INSERT INTO public."session"
                    (id, "userId", token, "expiresAt", "createdAt", "updatedAt",
                     "securityRevision", "absoluteDeadline", "lastActivity")
                VALUES (:id, :user, :token,
                        clock_timestamp() + interval '1 hour',
                        clock_timestamp(), clock_timestamp(), 0,
                        clock_timestamp() + interval '1 hour', clock_timestamp())
            """),
            {"id": session_id, "user": native_user, "token": f"synthetic-{uuid4()}"},
        )
    config = ServiceConfig(
        environment="test",
        policy=policy,
        db_host="db",
        db_port=5432,
        db_name="carepulse",
        db_user="carepulse_runtime",
        db_password="synthetic-never-used",
        file_encryption_key="synthetic-never-used",
        service_key=_SECRET,
        signing_key_id="v1",
        mail_enabled=False,
        scan_enabled=False,
        mail=None,
        scan=None,
    )
    try:
        yield SimpleNamespace(
            tenant=active,
            other=other,
            staff=staff_id,
            session=session_id,
            native_user=native_user,
            migration=migration,
            runtime=runtime,
            config=config,
        )
    finally:
        runtime.dispose()
        migration.dispose()


def _signed(data, *, tenant=None, session=None, nonce=None, key=None, value="first"):
    body = json.dumps({"value": value}, separators=(",", ":")).encode()
    identity = CanonicalEnvelope(
        PROTOCOL_VERSION,
        "v1",
        "web",
        "POST",
        _PATH,
        "",
        raw_body_sha256(body),
        str(tenant or data.tenant),
        str(data.staff),
        session or data.session,
        "DOCTOR_OWNER",
        "0",
        str(int(datetime.now(UTC).timestamp() * 1000)),
        nonce or f"synthetic_{uuid4().hex}",
        _PURPOSE,
        key or f"synthetic-{uuid4()}",
    )
    field_names = (
        "protocol-version",
        "key-id",
        "service",
        "organization-id",
        "staff-id",
        "session-id",
        "role",
        "security-revision",
        "timestamp",
        "nonce",
        "purpose",
        "idempotency-key",
    )
    values = (
        identity.protocol_version,
        identity.key_id,
        identity.service,
        identity.organization_id,
        identity.staff_id,
        identity.session_id,
        identity.role,
        identity.security_revision,
        identity.timestamp,
        identity.nonce,
        identity.purpose,
        identity.idempotency_key,
    )
    headers = [
        (f"x-carepulse-{name}".encode(), value.encode())
        for name, value in zip(field_names, values)
    ]
    headers.extend(
        [
            (b"x-carepulse-service-key", _SECRET.encode()),
            (
                b"x-carepulse-signature",
                new(_SECRET.encode(), canonical_payload(identity), sha256)
                .hexdigest()
                .encode(),
            ),
        ]
    )
    return identity, body, headers


def _dispatch(data, payload, handle):
    identity, body, headers = payload
    messages = []
    scope = {
        "type": "http",
        "method": identity.method,
        "path": identity.path,
        "raw_path": identity.path.encode(),
        "query_string": b"",
        "headers": headers,
        "app": SimpleNamespace(
            state=SimpleNamespace(service_config=data.config, db_engine=data.runtime)
        ),
    }

    async def app(request_scope, receive, send):
        assert (await receive())["body"] == body
        try:
            result = handle(request_scope["carepulse.identity"], body)
            response = success_response(result, revision=0)
        except ContractError as denied:
            response = error_response(denied.status_code, code=denied.code)
        except ReceiptConflict:
            response = error_response(409, code="IDEMPOTENCY_CONFLICT")
        await response(request_scope, receive, send)

    async def receive():
        return {"type": "http.request", "body": body, "more_body": False}

    async def send(message):
        messages.append(message)

    asyncio.run(
        ClosedServiceBoundary(app, routes={("POST", _PATH): _PURPOSE})(
            scope, receive, send
        )
    )
    return messages[0]["status"], json.loads(messages[1]["body"])


def _read_own(data, identity, _body):
    with protected_transaction(
        data.runtime, identity, data.config.policy.session_idle_minutes
    ) as (connection, actor):
        assert actor.organization_id == data.tenant
        assert connection.scalar(text("SELECT current_setting('app.staff_id')")) == str(
            data.staff
        )
        return {
            "tenant": str(
                connection.scalar(text("SELECT current_setting('app.organization_id')"))
            )
        }


def test_valid_native_session_and_provisioning_or_forged_session_denied(boundary_data):
    data = boundary_data
    assert _dispatch(data, _signed(data), lambda i, b: _read_own(data, i, b))[0] == 200
    assert (
        _dispatch(
            data, _signed(data, tenant=data.other), lambda i, b: _read_own(data, i, b)
        )[0]
        == 403
    )
    assert (
        _dispatch(
            data,
            _signed(data, session="forged-session"),
            lambda i, b: _read_own(data, i, b),
        )[0]
        == 401
    )
    with transaction(data.runtime) as connection:
        assert (
            connection.scalar(text("SELECT count(*) FROM public.staff_accounts")) == 0
        )
    with data.runtime.connect() as connection:
        assert connection.scalar(
            text("SELECT current_setting('app.organization_id', true)")
        ) in ("", None)
        assert connection.scalar(
            text("SELECT current_setting('app.staff_id', true)")
        ) in ("", None)


def test_expired_persisted_session_is_denied(boundary_data):
    data = boundary_data
    with data.migration.begin() as connection:
        connection.execute(
            text("""
                UPDATE public."session"
                SET "expiresAt"=clock_timestamp() - interval '1 second'
                WHERE id=:session
            """),
            {"session": data.session},
        )
    assert _dispatch(data, _signed(data), lambda i, b: _read_own(data, i, b))[0] == 401


def test_stale_security_revision_is_denied(boundary_data):
    data = boundary_data
    with data.migration.begin() as connection:
        connection.execute(
            text("""
                UPDATE public.staff_accounts
                SET security_revision=security_revision+1
                WHERE id=:staff
            """),
            {"staff": data.staff},
        )
    assert _dispatch(data, _signed(data), lambda i, b: _read_own(data, i, b))[0] == 401


def test_deleted_native_session_is_denied(boundary_data):
    data = boundary_data
    with data.migration.begin() as connection:
        connection.execute(
            text('DELETE FROM public."session" WHERE id=:session'),
            {"session": data.session},
        )
    assert _dispatch(data, _signed(data), lambda i, b: _read_own(data, i, b))[0] == 401


def test_foreign_record_is_nonvisible_even_with_live_own_session(boundary_data):
    data = boundary_data

    def foreign_read(identity, _body):
        with protected_transaction(
            data.runtime, identity, data.config.policy.session_idle_minutes
        ) as (connection, actor):
            foreign = (
                connection.execute(
                    text(
                        "SELECT id, id AS organization_id FROM public.organizations WHERE id=:id"
                    ),
                    {"id": data.other},
                )
                .mappings()
                .one_or_none()
            )
            return project_visible_resource(
                foreign,
                actor.organization_id,
                actor.role,
                ResourceFields(
                    readable={"DOCTOR_OWNER": frozenset({"id"})}, writable={}
                ),
            )

    assert _dispatch(data, _signed(data), foreign_read)[0] == 404


def test_parallel_same_nonce_has_single_committed_winner(boundary_data):
    data = boundary_data
    payload = _signed(data)
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [
            pool.submit(_dispatch, data, payload, lambda i, b: _read_own(data, i, b))
            for _ in range(2)
        ]
        results = [future.result(timeout=15)[0] for future in futures]
    assert sorted(results) == [200, 401]


def test_nonce_replay_survives_rolled_back_business_transaction(boundary_data):
    data = boundary_data
    payload = _signed(data)

    def failed(identity, _body):
        with protected_transaction(
            data.runtime, identity, data.config.policy.session_idle_minutes
        ) as (connection, actor):
            append_outbox(
                connection,
                {
                    "organization_id": actor.organization_id,
                    "event_type": "SYNTHETIC_T023_ROLLBACK",
                    "aggregate_type": "synthetic",
                    "aggregate_id": str(uuid4()),
                    "payload": {},
                },
            )
            raise ContractError(409)

    assert _dispatch(data, payload, failed)[0] == 409
    assert _dispatch(data, payload, failed)[0] == 401
    with transaction(data.runtime, data.tenant) as connection:
        assert (
            connection.scalar(
                text("""
            SELECT count(*) FROM public.outbox_events
            WHERE organization_id=:tenant AND event_type='SYNTHETIC_T023_ROLLBACK'
        """),
                {"tenant": data.tenant},
            )
            == 0
        )


def test_receipt_retry_changed_body_and_revoked_cache(boundary_data):
    data = boundary_data
    key = f"synthetic-test-key-{uuid4()}"

    def mutate(identity, body):
        digest = normalized_request_digest(SyntheticRequest.model_validate_json(body))
        with protected_transaction(
            data.runtime, identity, data.config.policy.session_idle_minutes
        ) as (connection, actor):
            query = TenantReceiptQuery(
                actor=actor,
                operation=_PURPOSE,
                idempotency_key=identity.idempotency_key,
                request_digest=digest,
            )
            existing = resolve_tenant_receipt(connection, query)
            if existing:
                return existing[0]
            result = {"id": str(uuid4())}
            insert_tenant_receipt(
                connection,
                Receipt(
                    actor.staff_id,
                    _PURPOSE,
                    identity.idempotency_key,
                    digest,
                    result,
                    0,
                    organization_id=actor.organization_id,
                    staff_id=actor.staff_id,
                ),
            )
            return result

    first = _dispatch(data, _signed(data, key=key), mutate)
    second = _dispatch(data, _signed(data, key=key), mutate)
    assert first[0] == second[0] == 200
    assert first[1] == second[1]
    assert _dispatch(data, _signed(data, key=key, value="changed"), mutate)[0] == 409
    with data.migration.begin() as connection:
        connection.execute(
            text("UPDATE public.organizations SET state='SUSPENDED' WHERE id=:tenant"),
            {"tenant": data.tenant},
        )
    assert _dispatch(data, _signed(data, key=key), mutate)[0] == 403


def test_suspension_serializes_with_protected_mutation(boundary_data):
    data = boundary_data
    entered = Event()
    release = Event()
    attempting_suspension = Event()

    def operation():
        identity, _, _ = _signed(data)
        with protected_transaction(
            data.runtime, identity, data.config.policy.session_idle_minutes
        ):
            entered.set()
            assert release.wait(timeout=10)
        return "authorized_committed"

    def suspension():
        with data.migration.begin() as connection:
            attempting_suspension.set()
            connection.execute(
                text(
                    "UPDATE public.organizations SET state='SUSPENDED' WHERE id=:tenant"
                ),
                {"tenant": data.tenant},
            )
        return "suspended_committed"

    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(operation)
        assert entered.wait(timeout=10)
        second = pool.submit(suspension)
        assert attempting_suspension.wait(timeout=10)
        assert (
            not second.done()
        )  # The Organization row lock holds through the operation.
        release.set()
        assert first.result(timeout=10) == "authorized_committed"
        assert second.result(timeout=10) == "suspended_committed"
    identity, _, _ = _signed(data)
    with (
        pytest.raises(ContractError),
        protected_transaction(
            data.runtime, identity, data.config.policy.session_idle_minutes
        ),
    ):
        pytest.fail("Suspended tenant admitted")

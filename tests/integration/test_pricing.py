"""B-06 signed pricing API and frozen appointment acceptance (disposable CI PostgreSQL)."""

import asyncio
import json
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import UTC, datetime
from hashlib import sha256
from hmac import new
from time import monotonic, sleep
from urllib.parse import urlencode
from uuid import UUID, uuid4

import pytest
from carepulse.appointments.persistence import persist_appointment
from carepulse.db import transaction
from carepulse.identity.authorization import (
    can_manage_operational_settings,
    protected_transaction,
)
from carepulse.identity.signing import canonical_payload, canonical_query
from carepulse.main import app
from carepulse.policies.pricing import quote_price, validate_discount_reason
from carepulse.policies.pricing_contracts import PricingPatch
from carepulse.policies.pricing_service import update_pricing
from carepulse.policies.versions import append_version
from sqlalchemy import text
from test_appointment_persistence import (
    _draft,
    _patient,
    _query,
)
from test_appointment_persistence import (
    appointment_storage as appointment_storage,  # noqa: PLC0414 - fixture export
)
from test_followup_persistence import _v2_priced_followup
from test_patients import patient_backend as patient_backend  # noqa: PLC0414 - fixture export
from test_request_boundary import boundary_data as boundary_data  # noqa: PLC0414 - fixture export

PATH = "/v1/settings/pricing"


def pricing_request(
    clinic,
    actor,
    *,
    method="GET",
    body=None,
    key="",
    query=None,
    origin=None,
    raw_body=None,
):
    payload = (
        raw_body
        if raw_body is not None
        else (
            json.dumps(body, separators=(",", ":")).encode()
            if body is not None
            else b""
        )
    )
    envelope = replace(
        actor,
        method=method,
        path=PATH,
        query=canonical_query(urlencode(query or {}, doseq=True)),
        body_sha256=sha256(payload).hexdigest(),
        purpose="SETTINGS_PRICING",
        timestamp=str(int(datetime.now(UTC).timestamp() * 1000)),
        nonce=f"synthetic_{uuid4().hex}",
        idempotency_key=key,
    )
    signed = (
        "protocol_version",
        "key_id",
        "service",
        "organization_id",
        "staff_id",
        "session_id",
        "role",
        "security_revision",
        "timestamp",
        "nonce",
        "purpose",
        "idempotency_key",
    )
    headers = [
        (
            f"x-carepulse-{name.replace('_', '-')}".encode(),
            str(getattr(envelope, name)).encode(),
        )
        for name in signed
    ]
    headers += [
        (b"content-type", b"application/json"),
        (b"x-carepulse-service-key", clinic.config.service_key.encode()),
        (
            b"x-carepulse-signature",
            new(clinic.config.service_key.encode(), canonical_payload(envelope), sha256)
            .hexdigest()
            .encode(),
        ),
    ]
    if origin is not None:
        headers.append((b"origin", origin.encode()))
    scope = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "scheme": "http",
        "method": method,
        "path": PATH,
        "raw_path": PATH.encode(),
        "query_string": envelope.query.encode(),
        "root_path": "",
        "headers": headers,
        "client": ("synthetic", 1),
        "server": ("api", 8000),
    }
    output = []

    async def receive():
        return {"type": "http.request", "body": payload, "more_body": False}

    async def send(message):
        output.append(message)

    asyncio.run(app(scope, receive, send))
    return output[0]["status"], json.loads(
        b"".join(message.get("body", b"") for message in output[1:])
    )


def _patch(**changes):
    return {
        "expected_price_revision": 1,
        "reason": "Synthetic approved pricing change",
        **changes,
    }


@pytest.mark.parametrize("raw_body", [b"\xff", b'{"reason":' + b"9" * 4301 + b"}"])
def test_signed_pricing_malformed_json_denied_without_effects(
    patient_backend, raw_body
):
    clinic = patient_backend
    app.state.service_config, app.state.db_engine = clinic.config, clinic.runtime
    before = _pricing_evidence(clinic)
    status, denied = pricing_request(
        clinic, clinic.owner, method="PATCH", raw_body=raw_body, key=str(uuid4())
    )
    assert status == 422 and denied["error"]["code"] == "INVALID_INPUT"
    assert _pricing_evidence(clinic) == before


def _manager_change(connection, clinic, enabled, revision):
    connection.execute(
        text("""
            SELECT public.set_receptionist_operational_manager(
                :tenant, :owner, :staff, :enabled,
                :revision, :revision, :reason, :correlation
            )
        """),
        {
            "tenant": clinic.tenant,
            "owner": UUID(clinic.owner.staff_id),
            "staff": UUID(clinic.receptionist.staff_id),
            "enabled": enabled,
            "revision": revision,
            "reason": "Synthetic Owner operational Manager grant",
            "correlation": uuid4(),
        },
    )


def _owner_changes_manager(clinic, enabled, revision):
    with protected_transaction(
        clinic.runtime, clinic.owner, clinic.config.policy.session_idle_minutes
    ) as (connection, actor):
        assert actor.role == "DOCTOR_OWNER"
        _manager_change(connection, clinic, enabled, revision)


def _fresh_receptionist_session(clinic, revision):
    session = f"synthetic-m1-pricing-{uuid4()}"
    with clinic.migration.begin() as connection:
        user = connection.scalar(
            text("""
                SELECT auth_user_id FROM public.staff_accounts
                 WHERE organization_id = :tenant AND id = :staff
            """),
            {"tenant": clinic.tenant, "staff": UUID(clinic.receptionist.staff_id)},
        )
        assert user
        connection.execute(
            text("""
                INSERT INTO public."session"
                    (id, "userId", token, "expiresAt", "createdAt", "updatedAt",
                     "securityRevision", "absoluteDeadline", "lastActivity")
                VALUES (:session, :user, :token,
                    clock_timestamp() + interval '1 hour',
                    clock_timestamp(), clock_timestamp(), :revision,
                    clock_timestamp() + interval '1 hour', clock_timestamp())
            """),
            {
                "session": session,
                "user": user,
                "token": f"synthetic-{uuid4()}",
                "revision": revision,
            },
        )
    return replace(
        clinic.receptionist, session_id=session, security_revision=str(revision)
    )


def _pricing_evidence(clinic):
    """Inspect committed settings effects without role-filtered projections."""
    with clinic.migration.connect() as connection:
        heads = tuple(
            connection.execute(
                text("""
                    SELECT kind, current_version, revision
                    FROM public.clinic_policy_heads
                    WHERE organization_id=:tenant AND kind IN ('PRICE', 'FOLLOWUP')
                    ORDER BY kind
                """),
                {"tenant": clinic.tenant},
            ).all()
        )
        counts = {
            label: connection.scalar(
                text(
                    f"SELECT count(*) FROM public.{table} "
                    f"WHERE organization_id=:tenant {condition}"
                ),
                {"tenant": clinic.tenant},
            )
            for label, table, condition in (
                (
                    "versions",
                    "clinic_policy_versions",
                    "AND kind IN ('PRICE', 'FOLLOWUP')",
                ),
                ("audit", "sensitive_audit", "AND action='settings.pricing.update'"),
                ("outbox", "outbox_events", ""),
                (
                    "receipts",
                    "idempotency_receipts",
                    "AND operation='settings.pricing.update'",
                ),
            )
        }
    return heads, counts


def test_signed_pricing_surrogate_reason_is_422_before_receipt(patient_backend):
    clinic = patient_backend
    before = _pricing_evidence(clinic)
    key = str(uuid4())
    for surrogate in ("\ud800", "\udfff"):
        status, refused = pricing_request(
            clinic,
            clinic.owner,
            method="PATCH",
            body=_patch(standard_new_exam_egp="100.00", reason=surrogate),
            key=key,
        )
        assert status == 422 and refused["error"]["code"] == "INVALID_INPUT"
        assert _pricing_evidence(clinic) == before

    accepted = _patch(standard_new_exam_egp="100.00", reason="مراجعة معتمدة")
    status, result = pricing_request(
        clinic, clinic.owner, method="PATCH", body=accepted, key=key
    )
    assert status == 200, result
    assert pricing_request(
        clinic, clinic.owner, method="PATCH", body=accepted, key=key
    ) == (status, result)


def _blocked_by(clinic, holder):
    deadline = monotonic() + 12
    with clinic.runtime.connect() as observer:
        while monotonic() < deadline:
            observer.execute(text("SELECT pg_stat_clear_snapshot()"))
            blocked = observer.scalar(
                text("""
                    SELECT pid FROM pg_stat_activity
                     WHERE wait_event_type = 'Lock'
                       AND :holder = ANY(pg_blocking_pids(pid))
                     LIMIT 1
                """),
                {"holder": holder},
            )
            if blocked is not None:
                return blocked
            sleep(0.02)
    pytest.fail("Expected PostgreSQL blocking order was not observed")


def test_manager_grant_enables_pricing_but_not_legacy_bulk(patient_backend):
    clinic = patient_backend
    change = _patch(standard_new_exam_egp="100.00")
    assert (
        pricing_request(
            clinic, clinic.receptionist, method="PATCH", body=change, key=str(uuid4())
        )[0]
        == 403
    )

    # Historical true survives the upgrade but cannot confer operational authority.
    with clinic.migration.begin() as connection:
        connection.execute(
            text("""
                UPDATE public.staff_accounts
                   SET bulk_reschedule = true, security_revision = security_revision + 1
                 WHERE organization_id = :tenant AND id = :staff
            """),
            {"tenant": clinic.tenant, "staff": UUID(clinic.receptionist.staff_id)},
        )
    ordinary = _fresh_receptionist_session(clinic, 1)
    assert (
        pricing_request(
            clinic, ordinary, method="PATCH", body=change, key=str(uuid4())
        )[0]
        == 403
    )
    assert pricing_request(clinic, ordinary)[0] == 200
    assert "urgent_enabled" not in pricing_request(clinic, ordinary)[1]["data"]

    _owner_changes_manager(clinic, True, 1)
    manager = _fresh_receptionist_session(clinic, 2)
    assert pricing_request(clinic, ordinary)[0] == 401
    status, read = pricing_request(clinic, manager)
    assert status == 200, read
    assert read["data"]["urgent_enabled"] is False
    assert "legacy_price_remediation" not in read["data"]
    key = str(uuid4())
    status, applied = pricing_request(
        clinic, manager, method="PATCH", body=change, key=key
    )
    assert status == 200, applied
    assert applied["data"]["price_revision"] == 2
    assert "legacy_price_remediation" not in applied["data"]
    assert pricing_request(clinic, manager, method="PATCH", body=change, key=key) == (
        status,
        applied,
    )

    _owner_changes_manager(clinic, False, 2)
    assert (
        pricing_request(clinic, manager, method="PATCH", body=change, key=key)[0] == 401
    )
    assert (
        pricing_request(
            clinic,
            manager,
            method="PATCH",
            body={
                **change,
                "standard_new_exam_egp": "110.00",
            },
            key=str(uuid4()),
        )[0]
        == 401
    )
    assert pricing_request(clinic, manager)[0] == 401
    refreshed = _fresh_receptionist_session(clinic, 3)
    assert (
        pricing_request(
            clinic,
            refreshed,
            method="PATCH",
            body=change,
            key=key,
        )[0]
        == 403
    )
    assert pricing_request(clinic, refreshed)[0] == 200
    _owner_changes_manager(clinic, True, 3)
    regranted = _fresh_receptionist_session(clinic, 4)
    assert pricing_request(clinic, regranted)[0] == 200
    assert pricing_request(clinic, regranted, method="PATCH", body=change, key=key) == (
        status,
        applied,
    )
    with transaction(clinic.runtime, clinic.tenant) as connection:
        assert (
            connection.scalar(
                text("""
                SELECT revision FROM public.clinic_policy_heads
                 WHERE organization_id = :tenant AND kind = 'PRICE'
            """),
                {"tenant": clinic.tenant},
            )
            == 2
        )
        assert (
            connection.scalar(
                text("""
                SELECT count(*) FROM public.idempotency_receipts
                 WHERE organization_id=:tenant AND operation='settings.pricing.update'
            """),
                {"tenant": clinic.tenant},
            )
            == 1
        )
    with clinic.migration.connect() as connection:
        audit = connection.execute(
            text("""
                SELECT actor_staff_id, actor_role FROM public.sensitive_audit
                WHERE organization_id = :tenant AND action = 'settings.pricing.update'
            """),
            {"tenant": clinic.tenant},
        ).all()
        assert audit == [(UUID(manager.staff_id), "RECEPTIONIST")]


def test_manager_can_publish_pricing_and_followup_policy_in_one_transaction(
    patient_backend,
):
    clinic = patient_backend
    _owner_changes_manager(clinic, True, 0)
    manager = _fresh_receptionist_session(clinic, 1)
    change = {
        **_patch(
            standard_new_exam_egp="100.00",
            preferential_followup_egp="30.00",
            urgent_base_egp="150.00",
            followup_last_day_inclusive=10,
        ),
        "expected_followup_revision": 1,
    }
    key = str(uuid4())
    status, published = pricing_request(
        clinic, manager, method="PATCH", body=change, key=key
    )
    assert status == 200, published
    assert published["data"]["price_revision"] == 2
    assert published["data"]["followup_revision"] == 2
    assert published["data"]["followup_last_day_inclusive"] == 10
    assert published["data"]["urgent_base_egp"] == "150.00"
    assert "legacy_price_remediation" not in published["data"]
    assert pricing_request(clinic, manager, method="PATCH", body=change, key=key) == (
        status,
        published,
    )
    assert pricing_request(clinic, manager)[1]["data"] == published["data"]
    assert (
        pricing_request(
            clinic,
            manager,
            method="PATCH",
            body={**_patch(urgent_enabled=True), "expected_price_revision": 2},
            key=str(uuid4()),
        )[0]
        == 422
    ), "Urgent cannot be enabled without operating periods"
    with transaction(clinic.runtime, clinic.tenant) as connection:
        assert connection.execute(
            text("""
                SELECT kind, revision FROM public.clinic_policy_heads
                WHERE organization_id = :tenant AND kind IN ('PRICE', 'FOLLOWUP')
                ORDER BY kind
            """),
            {"tenant": clinic.tenant},
        ).all() == [("FOLLOWUP", 2), ("PRICE", 2)]
    with clinic.migration.connect() as connection:
        audits = connection.execute(
            text("""
                SELECT actor_staff_id, actor_role FROM public.sensitive_audit
                WHERE organization_id = :tenant AND action = 'settings.pricing.update'
            """),
            {"tenant": clinic.tenant},
        ).all()
        assert audits == [(UUID(manager.staff_id), "RECEPTIONIST")]


def test_owner_and_manager_followup_only_publications_enforce_both_revisions(
    patient_backend,
):
    clinic = patient_backend
    _owner_changes_manager(clinic, True, 0)
    manager = _fresh_receptionist_session(clinic, 1)
    initial, before = _pricing_evidence(clinic)
    assert initial == (("FOLLOWUP", 1, 1), ("PRICE", 1, 1))

    owner_body = {
        **_patch(followup_last_day_inclusive=0),
        "expected_followup_revision": 1,
    }
    status, owner_result = pricing_request(
        clinic, clinic.owner, method="PATCH", body=owner_body, key=str(uuid4())
    )
    assert status == 200, owner_result
    assert (
        owner_result["data"]["price_revision"],
        owner_result["data"]["followup_revision"],
        owner_result["data"]["followup_last_day_inclusive"],
    ) == (1, 2, 0)

    manager_body = {
        **_patch(followup_last_day_inclusive=3),
        "expected_followup_revision": 2,
    }
    key = str(uuid4())
    status, manager_result = pricing_request(
        clinic, manager, method="PATCH", body=manager_body, key=key
    )
    assert status == 200, manager_result
    assert manager_result["data"]["followup_revision"] == 3
    assert manager_result["data"]["followup_last_day_inclusive"] == 3
    assert pricing_request(
        clinic, manager, method="PATCH", body=manager_body, key=key
    ) == (status, manager_result)

    committed = _pricing_evidence(clinic)
    assert committed[0] == (("FOLLOWUP", 3, 3), ("PRICE", 1, 1))
    assert committed[1] == {
        **before,
        "versions": before["versions"] + 2,
        "audit": before["audit"] + 2,
        "receipts": before["receipts"] + 2,
    }
    for actor, payload in (
        (
            clinic.owner,
            {
                **manager_body,
                "expected_price_revision": 0,
                "expected_followup_revision": 3,
            },
        ),
        (manager, {**manager_body, "expected_followup_revision": 2}),
    ):
        denied, response = pricing_request(
            clinic, actor, method="PATCH", body=payload, key=str(uuid4())
        )
        assert denied == 409 and response["error"]["code"] == "STALE_REVISION"
    assert (
        pricing_request(
            clinic,
            manager,
            method="PATCH",
            body={**manager_body, "followup_last_day_inclusive": 4},
            key=key,
        )[0]
        == 409
    )
    assert _pricing_evidence(clinic) == committed
    with clinic.migration.connect() as connection:
        actors = connection.execute(
            text("""
                SELECT actor_staff_id, actor_role
                FROM public.sensitive_audit
                WHERE organization_id=:tenant AND action='settings.pricing.update'
                ORDER BY occurred_at, id
            """),
            {"tenant": clinic.tenant},
        ).all()
    assert set(actors) == {
        (UUID(clinic.owner.staff_id), "DOCTOR_OWNER"),
        (UUID(manager.staff_id), "RECEPTIONIST"),
    }
    assert len(actors) == 2


def test_disabled_manager_cannot_recover_prior_followup_receipt(patient_backend):
    clinic = patient_backend
    _owner_changes_manager(clinic, True, 0)
    manager = _fresh_receptionist_session(clinic, 1)
    body = {
        **_patch(followup_last_day_inclusive=5),
        "expected_followup_revision": 1,
    }
    key = str(uuid4())
    status, original = pricing_request(
        clinic, manager, method="PATCH", body=body, key=key
    )
    assert status == 200, original
    before = _pricing_evidence(clinic)
    with clinic.migration.begin() as connection:
        connection.execute(
            text("""
                UPDATE public.staff_accounts
                   SET enabled=false, security_revision=security_revision+1
                 WHERE organization_id=:tenant AND id=:staff
            """),
            {"tenant": clinic.tenant, "staff": UUID(manager.staff_id)},
        )
    for actor in (manager, _fresh_receptionist_session(clinic, 2)):
        assert pricing_request(clinic, actor)[0] in (401, 403)
        assert pricing_request(clinic, actor, method="PATCH", body=body, key=key)[
            0
        ] in (401, 403)
    assert _pricing_evidence(clinic) == before


def test_manager_cannot_remediate_noncanonical_legacy_price(patient_backend):
    clinic = patient_backend
    with transaction(clinic.runtime, clinic.tenant) as connection:
        append_version(
            connection,
            str(clinic.tenant),
            "PRICE",
            '{"standard_new_exam_egp":100.123,"preferential_followup_egp":30,'
            '"urgent_base_egp":null,"urgent_enabled":false}',
            1,
        )
    _owner_changes_manager(clinic, True, 0)
    manager = _fresh_receptionist_session(clinic, 1)
    owner_status, owner_view = pricing_request(clinic, clinic.owner)
    manager_status, manager_view = pricing_request(clinic, manager)
    assert owner_status == manager_status == 200
    assert owner_view["data"]["legacy_price_remediation"] == {
        "standard_new_exam_egp": "100.123"
    }
    assert "legacy_price_remediation" not in manager_view["data"]
    correction = {
        **_patch(standard_new_exam_egp="100.12"),
        "expected_price_revision": 2,
    }
    assert (
        pricing_request(
            clinic, manager, method="PATCH", body=correction, key=str(uuid4())
        )[0]
        == 403
    )
    status, repaired = pricing_request(
        clinic, clinic.owner, method="PATCH", body=correction, key=str(uuid4())
    )
    assert status == 200, repaired
    assert repaired["data"]["standard_new_exam_egp"] == "100.12"


@pytest.mark.parametrize("first", ["revoke", "receipt"])
def test_manager_receipt_recovery_serializes_with_revoke(patient_backend, first):
    clinic = patient_backend
    _owner_changes_manager(clinic, True, 0)
    manager = _fresh_receptionist_session(clinic, 1)
    body = {
        **_patch(followup_last_day_inclusive=6),
        "expected_followup_revision": 1,
    }
    key = str(uuid4())
    status, original = pricing_request(
        clinic, manager, method="PATCH", body=body, key=key
    )
    assert status == 200, original
    before = _pricing_evidence(clinic)

    with ThreadPoolExecutor(max_workers=2) as pool:
        if first == "revoke":
            with protected_transaction(
                clinic.runtime, clinic.owner, clinic.config.policy.session_idle_minutes
            ) as (connection, _):
                holder = connection.scalar(text("SELECT pg_backend_pid()"))
                _manager_change(connection, clinic, False, 1)
                future = pool.submit(
                    pricing_request,
                    clinic,
                    manager,
                    method="PATCH",
                    body=body,
                    key=key,
                )
                _blocked_by(clinic, holder)
            denied, response = future.result(timeout=30)
            assert denied == 401, response
        else:
            with protected_transaction(
                clinic.runtime, manager, clinic.config.policy.session_idle_minutes
            ) as (connection, live_actor):
                holder = connection.scalar(text("SELECT pg_backend_pid()"))
                future = pool.submit(_owner_changes_manager, clinic, False, 1)
                _blocked_by(clinic, holder)
                assert (
                    update_pricing(
                        connection, live_actor, PricingPatch.model_validate(body), key
                    )
                    == original["data"]
                )
            future.result(timeout=30)

    assert (
        pricing_request(clinic, manager, method="PATCH", body=body, key=key)[0] == 401
    )
    ordinary = _fresh_receptionist_session(clinic, 2)
    assert (
        pricing_request(clinic, ordinary, method="PATCH", body=body, key=key)[0] == 403
    )
    assert _pricing_evidence(clinic) == before


@pytest.mark.parametrize(
    "change, operation",
    [("grant", "quote"), ("grant", "write"), ("revoke", "quote")],
)
def test_manager_grant_revoke_serializes_signed_pricing_operations(
    patient_backend, change, operation
):
    clinic = patient_backend
    body = _patch(standard_new_exam_egp="100.00")
    assert (
        pricing_request(
            clinic, clinic.owner, method="PATCH", body=body, key=str(uuid4())
        )[0]
        == 200
    )
    if change == "revoke":
        _owner_changes_manager(clinic, True, 0)
        reader = _fresh_receptionist_session(clinic, 1)
    else:
        reader = clinic.receptionist
    before = _pricing_evidence(clinic)
    quote = {
        "service_type": "NEW_EXAMINATION",
        "discount_type": "NONE",
        "discount_value": "0",
    }
    with ThreadPoolExecutor(max_workers=2) as pool:
        with protected_transaction(
            clinic.runtime, clinic.owner, clinic.config.policy.session_idle_minutes
        ) as (connection, _):
            holder = connection.scalar(text("SELECT pg_backend_pid()"))
            _manager_change(
                connection, clinic, change == "grant", 0 if change == "grant" else 1
            )
            request = (
                {"query": quote}
                if operation == "quote"
                else {
                    "method": "PATCH",
                    "body": {
                        **_patch(followup_last_day_inclusive=5),
                        "expected_price_revision": 2,
                        "expected_followup_revision": 1,
                    },
                    "key": str(uuid4()),
                }
            )
            future = pool.submit(pricing_request, clinic, reader, **request)
            _blocked_by(clinic, holder)
        status, response = future.result(timeout=30)
    assert status == 401, response  # Signed session revision is stale after the change.
    assert _pricing_evidence(clinic) == before

    refreshed = _fresh_receptionist_session(clinic, 1 if change == "grant" else 2)
    status, response = pricing_request(clinic, refreshed, query=quote)
    assert status == 200, response
    assert response["data"]["price_quote"]["net_egp"] == "100.00"
    assert ("urgent_enabled" in response["data"]) is (change == "grant")


@pytest.mark.parametrize("winner", ["revoke", "authorize"])
def test_manager_revoke_and_live_authorization_serialize_both_orders(
    patient_backend, winner
):
    clinic = patient_backend
    _owner_changes_manager(clinic, True, 0)
    manager = _fresh_receptionist_session(clinic, 1)
    body = _patch(standard_new_exam_egp="100.00")
    with ThreadPoolExecutor(max_workers=2) as pool:
        if winner == "revoke":
            with protected_transaction(
                clinic.runtime, clinic.owner, clinic.config.policy.session_idle_minutes
            ) as (connection, _):
                holder = connection.scalar(text("SELECT pg_backend_pid()"))
                _manager_change(connection, clinic, False, 1)
                pricing_future = pool.submit(
                    pricing_request,
                    clinic,
                    manager,
                    method="PATCH",
                    body=body,
                    key=str(uuid4()),
                )
                _blocked_by(clinic, holder)
            status, response = pricing_future.result(timeout=30)
            assert status == 401, response
        else:
            with protected_transaction(
                clinic.runtime, manager, clinic.config.policy.session_idle_minutes
            ) as (connection, actor):
                assert actor.role == "RECEPTIONIST"
                assert can_manage_operational_settings(actor)
                holder = connection.scalar(text("SELECT pg_backend_pid()"))
                revoke_future = pool.submit(_owner_changes_manager, clinic, False, 1)
                _blocked_by(clinic, holder)
                # A checked mutation commits before a waiting revoke.
                changed = update_pricing(
                    connection, actor, PricingPatch.model_validate(body), str(uuid4())
                )
                assert changed["price_revision"] == 2
            revoke_future.result(timeout=30)

    assert pricing_request(clinic, manager)[0] == 401
    ordinary = _fresh_receptionist_session(clinic, 2)
    assert (
        pricing_request(clinic, ordinary, method="PATCH", body=body, key=str(uuid4()))[
            0
        ]
        == 403
    )
    with transaction(clinic.runtime, clinic.tenant) as connection:
        actual = connection.execute(
            text("""
                SELECT (SELECT revision FROM public.clinic_policy_heads
                         WHERE organization_id = :tenant AND kind = 'PRICE'),
                       (SELECT security_revision FROM public.staff_accounts
                         WHERE organization_id = :tenant AND id = :staff),
                       (SELECT count(*) FROM public.idempotency_receipts
                         WHERE organization_id = :tenant
                           AND operation = 'settings.pricing.update')
            """),
            {"tenant": clinic.tenant, "staff": UUID(manager.staff_id)},
        ).one()
    assert actual == (1 if winner == "revoke" else 2, 2, 0 if winner == "revoke" else 1)


def test_owner_patch_replays_receipt_without_leaking_revoked_session(patient_backend):
    clinic = patient_backend
    assert pricing_request(clinic, clinic.owner) == (
        200,
        {
            "data": {
                "standard_new_exam_egp": None,
                "preferential_followup_egp": None,
                "urgent_base_egp": None,
                "urgent_enabled": False,
                "completion_day_zero": True,
                "followup_last_day_inclusive": 7,
                "price_revision": 1,
                "followup_revision": 1,
            },
            "revision": 1,
        },
    )
    body = _patch(standard_new_exam_egp="100.00", preferential_followup_egp="30.00")
    key = str(uuid4())
    status, first = pricing_request(
        clinic, clinic.owner, method="PATCH", body=body, key=key
    )
    assert status == 200, first
    assert first["data"]["price_revision"] == 2
    assert pricing_request(
        clinic, clinic.owner, method="PATCH", body=body, key=key
    ) == (status, first)
    status, changed = pricing_request(
        clinic,
        clinic.owner,
        method="PATCH",
        body={**body, "standard_new_exam_egp": "110.00"},
        key=key,
    )
    assert status == 409 and changed["error"]["code"] == "IDEMPOTENCY_CONFLICT"
    with transaction(clinic.runtime, clinic.tenant) as conn:
        rows = (
            conn.execute(
                text("""
            SELECT payload::text FROM public.clinic_policy_versions
            WHERE kind = 'PRICE' ORDER BY version
        """)
            )
            .scalars()
            .all()
        )
    assert len(rows) == 2
    assert '"100.00"' in rows[-1] and '"30.00"' in rows[-1]
    with clinic.migration.begin() as conn:
        conn.execute(
            text('DELETE FROM public."session" WHERE id = :id'),
            {"id": clinic.owner.session_id},
        )
    assert pricing_request(clinic, clinic.owner, method="PATCH", body=body, key=key)[
        0
    ] in (401, 403)
    assert pricing_request(clinic, clinic.owner)[0] in (401, 403)


@pytest.mark.parametrize(
    "service,base,net",
    [
        ("NEW_EXAMINATION", "100.00", "90.00"),
        ("FOLLOW_UP", "30.00", "27.00"),
    ],
)
def test_both_roles_get_reason_free_quotes_with_correct_base(
    patient_backend, service, base, net
):
    clinic = patient_backend
    assert (
        pricing_request(
            clinic,
            clinic.owner,
            method="PATCH",
            body=_patch(
                standard_new_exam_egp="100.00", preferential_followup_egp="30.00"
            ),
            key=str(uuid4()),
        )[0]
        == 200
    )
    query = {
        "service_type": service,
        "discount_type": "PERCENT",
        "discount_value": "10",
    }
    for actor in (clinic.owner, clinic.receptionist):
        status, result = pricing_request(clinic, actor, query=query)
        assert status == 200, result
        quote = result["data"]["price_quote"]
        assert quote["service_type"] == service
        assert quote["base_egp"] == base and quote["net_egp"] == net
        assert quote["discount_value"] == "10"
        assert "discount_reason" not in quote
        fixed_status, fixed_result = pricing_request(
            clinic,
            actor,
            query={
                "service_type": service,
                "discount_type": "FIXED",
                "discount_value": "1",
            },
        )
        assert fixed_status == 200, fixed_result
        assert fixed_result["data"]["price_quote"]["discount_value"] == "1.00"
        if actor == clinic.receptionist:
            assert "urgent_base_egp" not in result["data"]
            assert "urgent_enabled" not in result["data"]


def test_unset_price_denies_quote_and_receptionist_cannot_patch(patient_backend):
    clinic = patient_backend
    for actor in (clinic.owner, clinic.receptionist):
        for service in ("NEW_EXAMINATION", "FOLLOW_UP"):
            assert (
                pricing_request(
                    clinic,
                    actor,
                    query={
                        "service_type": service,
                        "discount_type": "NONE",
                        "discount_value": "0",
                    },
                )[0]
                == 409
            )
    assert (
        pricing_request(
            clinic,
            clinic.receptionist,
            method="PATCH",
            body=_patch(standard_new_exam_egp="100.00"),
            key=str(uuid4()),
        )[0]
        == 403
    )


def test_operator_and_foreign_tenant_cannot_read_or_update_pricing(patient_backend):
    clinic = patient_backend
    _owner_changes_manager(clinic, True, 0)
    manager = _fresh_receptionist_session(clinic, 1)
    before = _pricing_evidence(clinic)
    for forged_actor in (
        replace(clinic.owner, role="OPERATOR"),
        replace(clinic.owner, organization_id=str(clinic.other)),
        replace(clinic.receptionist, organization_id=str(clinic.other)),
        replace(manager, organization_id=str(clinic.other)),
    ):
        for method, body in (
            ("GET", None),
            ("PATCH", _patch(standard_new_exam_egp="100.00")),
        ):
            assert pricing_request(
                clinic,
                forged_actor,
                method=method,
                body=body,
                key=str(uuid4()) if method == "PATCH" else "",
            )[0] in (401, 403)
    assert _pricing_evidence(clinic) == before
    with transaction(clinic.runtime, clinic.tenant) as conn:
        assert (
            conn.scalar(
                text(
                    "SELECT revision FROM public.clinic_policy_heads WHERE kind = 'PRICE'"
                )
            )
            == 1
        )


def test_legacy_numeric_null_and_mixed_string_versions_remain_readable(patient_backend):
    clinic = patient_backend
    assert (
        pricing_request(clinic, clinic.owner)[1]["data"]["standard_new_exam_egp"]
        is None
    )
    assert (
        pricing_request(clinic, clinic.receptionist)[1]["data"][
            "preferential_followup_egp"
        ]
        is None
    )
    with transaction(clinic.runtime, clinic.tenant) as conn:
        append_version(
            conn,
            str(clinic.tenant),
            "PRICE",
            {
                "standard_new_exam_egp": 100,
                "preferential_followup_egp": 30.5,
                "urgent_base_egp": None,
                "urgent_enabled": False,
            },
            1,
        )
    for actor in (clinic.owner, clinic.receptionist):
        for service, base, net in (
            ("NEW_EXAMINATION", "100.00", "90.00"),
            ("FOLLOW_UP", "30.50", "27.45"),
        ):
            status, response = pricing_request(
                clinic,
                actor,
                query={
                    "service_type": service,
                    "discount_type": "PERCENT",
                    "discount_value": "10",
                },
            )
            assert status == 200, response
            assert response["data"]["price_quote"]["base_egp"] == base
            assert response["data"]["price_quote"]["net_egp"] == net
            if actor == clinic.receptionist:
                assert "urgent_base_egp" not in response["data"]
    status, result = pricing_request(
        clinic,
        clinic.owner,
        method="PATCH",
        body={
            **_patch(standard_new_exam_egp="105.00"),
            "expected_price_revision": 2,
        },
        key=str(uuid4()),
    )
    assert status == 200, result
    assert result["data"]["price_revision"] == 3
    with transaction(clinic.runtime, clinic.tenant) as conn:
        types = conn.execute(
            text("""
            SELECT version, jsonb_typeof(payload->'standard_new_exam_egp')
            FROM public.clinic_policy_versions
            WHERE kind = 'PRICE' ORDER BY version
        """)
        ).all()
    assert types == [(1, "null"), (2, "number"), (3, "string")]
    assert (
        pricing_request(
            clinic,
            clinic.owner,
            query={
                "service_type": "NEW_EXAMINATION",
                "discount_type": "PERCENT",
                "discount_value": "10",
            },
        )[1]["data"]["price_quote"]["net_egp"]
        == "94.50"
    )


def test_noncanonical_legacy_price_requires_explicit_owner_remediation(patient_backend):
    clinic = patient_backend
    with transaction(clinic.runtime, clinic.tenant) as conn:
        saved = persist_appointment(
            conn,
            _draft(
                _patient(conn, clinic.tenant),
                UUID(clinic.owner.staff_id),
            ),
            _query(clinic.tenant, UUID(clinic.owner.staff_id)),
        )
    with transaction(clinic.runtime, clinic.tenant) as conn:
        append_version(
            conn,
            str(clinic.tenant),
            "PRICE",
            '{"standard_new_exam_egp":100.123,"preferential_followup_egp":30,'
            '"urgent_base_egp":null,"urgent_enabled":false}',
            1,
        )

    with transaction(clinic.runtime, clinic.tenant) as conn:
        snapshot_before = conn.scalar(
            text("""
                SELECT row_to_json(s)::text FROM public.appointment_price_snapshots s
                WHERE appointment_id = :id
            """),
            {"id": saved["id"]},
        )
    owner_status, owner_result = pricing_request(clinic, clinic.owner)
    assert owner_status == 200, owner_result
    assert owner_result["data"]["standard_new_exam_egp"] is None
    assert owner_result["data"]["preferential_followup_egp"] == "30.00"
    assert owner_result["data"]["legacy_price_remediation"] == {
        "standard_new_exam_egp": "100.123"
    }
    for actor in (clinic.owner, clinic.receptionist):
        status, result = pricing_request(clinic, actor)
        assert status == 200, result
        assert result["data"]["standard_new_exam_egp"] is None
        if actor == clinic.receptionist:
            assert "legacy_price_remediation" not in result["data"]
        for service_type, expected_status in (
            ("NEW_EXAMINATION", 409),
            ("FOLLOW_UP", 200),
        ):
            status, response = pricing_request(
                clinic,
                actor,
                query={
                    "service_type": service_type,
                    "discount_type": "PERCENT",
                    "discount_value": "10",
                },
            )
            assert status == expected_status, response
            if status == 200:
                assert response["data"]["price_quote"]["net_egp"] == "27.00"
                if actor == clinic.receptionist:
                    assert "legacy_price_remediation" not in response["data"]

    for actor in (
        clinic.receptionist,
        replace(clinic.owner, role="OPERATOR"),
        replace(clinic.owner, organization_id=str(clinic.other)),
    ):
        assert pricing_request(
            clinic,
            actor,
            method="PATCH",
            body={
                **_patch(standard_new_exam_egp="100.12"),
                "expected_price_revision": 2,
            },
            key=str(uuid4()),
        )[0] in (401, 403)
        get_status, get_result = pricing_request(clinic, actor)
        if actor == clinic.receptionist:
            assert get_status == 200
            assert "legacy_price_remediation" not in get_result["data"]
        else:
            assert get_status in (401, 403)

    # Publishing an unrelated canonical field cannot silently repair the legacy amount.
    unrelated = {
        **_patch(preferential_followup_egp="31.00"),
        "expected_price_revision": 2,
    }
    unrelated_key = str(uuid4())
    status, unresolved = pricing_request(
        clinic, clinic.owner, method="PATCH", body=unrelated, key=unrelated_key
    )
    assert status == 200, unresolved
    assert unresolved["data"]["standard_new_exam_egp"] is None
    assert unresolved["data"]["legacy_price_remediation"] == {
        "standard_new_exam_egp": "100.123"
    }
    assert pricing_request(
        clinic, clinic.owner, method="PATCH", body=unrelated, key=unrelated_key
    ) == (status, unresolved)
    correction = {
        **_patch(standard_new_exam_egp="100.12"),
        "expected_price_revision": 3,
    }
    key = str(uuid4())
    status, corrected = pricing_request(
        clinic, clinic.owner, method="PATCH", body=correction, key=key
    )
    assert status == 200, corrected
    assert corrected["data"]["standard_new_exam_egp"] == "100.12"
    assert corrected["data"]["price_revision"] == 4
    assert "legacy_price_remediation" not in corrected["data"]
    assert pricing_request(
        clinic, clinic.owner, method="PATCH", body=correction, key=key
    ) == (status, corrected)
    status, conflict = pricing_request(
        clinic,
        clinic.owner,
        method="PATCH",
        body={**correction, "standard_new_exam_egp": "100.13"},
        key=key,
    )
    assert status == 409 and conflict["error"]["code"] == "IDEMPOTENCY_CONFLICT"
    assert (
        pricing_request(
            clinic,
            clinic.receptionist,
            query={
                "service_type": "NEW_EXAMINATION",
                "discount_type": "NONE",
                "discount_value": "0",
            },
        )[1]["data"]["price_quote"]["base_egp"]
        == "100.12"
    )

    with transaction(clinic.runtime, clinic.tenant) as conn:
        versions = conn.execute(
            text("""
                SELECT version, payload->>'standard_new_exam_egp',
                       jsonb_typeof(payload->'standard_new_exam_egp')
                FROM public.clinic_policy_versions
                WHERE kind = 'PRICE' ORDER BY version
            """)
        ).all()
        assert versions == [
            (1, None, "null"),
            (2, "100.123", "number"),
            (3, "100.123", "number"),
            (4, "100.12", "string"),
        ]
        assert (
            conn.scalar(
                text("""
                SELECT row_to_json(s)::text FROM public.appointment_price_snapshots s
                WHERE appointment_id = :id
            """),
                {"id": saved["id"]},
            )
            == snapshot_before
        )
        assert (
            conn.scalar(
                text("""
            SELECT count(*) FROM public.idempotency_receipts
            WHERE operation = 'settings.pricing.update'
        """)
            )
            == 2
        )


def test_noncanonical_followup_legacy_blocks_only_followup_quotes(patient_backend):
    clinic = patient_backend
    with transaction(clinic.runtime, clinic.tenant) as conn:
        append_version(
            conn,
            str(clinic.tenant),
            "PRICE",
            '{"standard_new_exam_egp":100,"preferential_followup_egp":100.123,'
            '"urgent_base_egp":null,"urgent_enabled":false}',
            1,
        )
    for actor in (clinic.owner, clinic.receptionist):
        status, response = pricing_request(clinic, actor)
        assert status == 200, response
        assert response["data"]["standard_new_exam_egp"] == "100.00"
        assert response["data"]["preferential_followup_egp"] is None
        if actor == clinic.owner:
            assert response["data"]["legacy_price_remediation"] == {
                "preferential_followup_egp": "100.123"
            }
        else:
            assert "legacy_price_remediation" not in response["data"]
        for service, expected in (("NEW_EXAMINATION", 200), ("FOLLOW_UP", 409)):
            status, result = pricing_request(
                clinic,
                actor,
                query={
                    "service_type": service,
                    "discount_type": "NONE",
                    "discount_value": "0",
                },
            )
            assert status == expected, result
            if status == 200:
                assert result["data"]["price_quote"]["net_egp"] == "100.00"


def test_legacy_numeric_trailing_zero_preserves_scale_without_blocking_quotes(
    patient_backend,
):
    clinic = patient_backend
    with transaction(clinic.runtime, clinic.tenant) as conn:
        append_version(
            conn,
            str(clinic.tenant),
            "PRICE",
            '{"standard_new_exam_egp":100.120,"preferential_followup_egp":30.123,'
            '"urgent_base_egp":null,"urgent_enabled":false}',
            1,
        )

    for actor in (clinic.owner, clinic.receptionist):
        status, result = pricing_request(clinic, actor)
        assert status == 200, result
        assert result["data"]["standard_new_exam_egp"] == "100.12"
        assert result["data"]["preferential_followup_egp"] is None
        if actor == clinic.owner:
            assert result["data"]["legacy_price_remediation"] == {
                "preferential_followup_egp": "30.123"
            }
        else:
            assert "legacy_price_remediation" not in result["data"]
        for service, expected in (("NEW_EXAMINATION", 200), ("FOLLOW_UP", 409)):
            status, response = pricing_request(
                clinic,
                actor,
                query={
                    "service_type": service,
                    "discount_type": "NONE",
                    "discount_value": "0",
                },
            )
            assert status == expected, response
            if status == 200:
                assert response["data"]["price_quote"]["net_egp"] == "100.12"

    status, result = pricing_request(
        clinic,
        clinic.owner,
        method="PATCH",
        body={**_patch(urgent_base_egp="150.00"), "expected_price_revision": 2},
        key=str(uuid4()),
    )
    assert status == 200, result
    assert result["data"]["standard_new_exam_egp"] == "100.12"
    assert result["data"]["legacy_price_remediation"] == {
        "preferential_followup_egp": "30.123"
    }

    with transaction(clinic.runtime, clinic.tenant) as conn:
        retained = conn.execute(
            text("""
                SELECT version, payload->>'standard_new_exam_egp',
                       jsonb_typeof(payload->'standard_new_exam_egp'),
                       payload->>'preferential_followup_egp',
                       jsonb_typeof(payload->'preferential_followup_egp')
                FROM public.clinic_policy_versions
                WHERE kind = 'PRICE' AND version IN (2, 3) ORDER BY version
            """)
        ).all()
    assert retained == [
        (2, "100.120", "number", "30.123", "number"),
        (3, "100.120", "number", "30.123", "number"),
    ]


def test_two_noncanonical_legacy_fields_are_corrected_independently(patient_backend):
    clinic = patient_backend
    with transaction(clinic.runtime, clinic.tenant) as conn:
        append_version(
            conn,
            str(clinic.tenant),
            "PRICE",
            '{"standard_new_exam_egp":100.123,"preferential_followup_egp":30.123,'
            '"urgent_base_egp":null,"urgent_enabled":false}',
            1,
        )
    assert pricing_request(clinic, clinic.owner)[1]["data"][
        "legacy_price_remediation"
    ] == {
        "standard_new_exam_egp": "100.123",
        "preferential_followup_egp": "30.123",
    }

    first_command = {
        **_patch(standard_new_exam_egp="100.12"),
        "expected_price_revision": 2,
    }
    first_key = str(uuid4())
    status, first = pricing_request(
        clinic, clinic.owner, method="PATCH", body=first_command, key=first_key
    )
    assert status == 200, first
    assert first["data"]["price_revision"] == 3
    assert first["data"]["standard_new_exam_egp"] == "100.12"
    assert first["data"]["preferential_followup_egp"] is None
    assert first["data"]["legacy_price_remediation"] == {
        "preferential_followup_egp": "30.123"
    }
    assert pricing_request(
        clinic, clinic.owner, method="PATCH", body=first_command, key=first_key
    ) == (status, first)
    receptionist = pricing_request(clinic, clinic.receptionist)
    assert receptionist[0] == 200
    assert "legacy_price_remediation" not in receptionist[1]["data"]
    for service, expected in (("NEW_EXAMINATION", 200), ("FOLLOW_UP", 409)):
        status, response = pricing_request(
            clinic,
            clinic.receptionist,
            query={
                "service_type": service,
                "discount_type": "NONE",
                "discount_value": "0",
            },
        )
        assert status == expected, response

    second_command = {
        **_patch(preferential_followup_egp="30.12"),
        "expected_price_revision": 3,
    }
    second_key = str(uuid4())
    status, second = pricing_request(
        clinic, clinic.owner, method="PATCH", body=second_command, key=second_key
    )
    assert status == 200, second
    assert second["data"]["price_revision"] == 4
    assert second["data"]["preferential_followup_egp"] == "30.12"
    assert "legacy_price_remediation" not in second["data"]
    assert pricing_request(
        clinic, clinic.owner, method="PATCH", body=second_command, key=second_key
    ) == (status, second)
    assert (
        pricing_request(
            clinic,
            clinic.receptionist,
            query={
                "service_type": "FOLLOW_UP",
                "discount_type": "NONE",
                "discount_value": "0",
            },
        )[1]["data"]["price_quote"]["net_egp"]
        == "30.12"
    )
    with transaction(clinic.runtime, clinic.tenant) as conn:
        retained = conn.execute(
            text("""
                SELECT version, payload->>'standard_new_exam_egp',
                       payload->>'preferential_followup_egp',
                       jsonb_typeof(payload->'standard_new_exam_egp'),
                       jsonb_typeof(payload->'preferential_followup_egp')
                FROM public.clinic_policy_versions
                WHERE kind = 'PRICE' ORDER BY version
            """)
        ).all()
        assert retained == [
            (1, None, None, "null", "null"),
            (2, "100.123", "30.123", "number", "number"),
            (3, "100.12", "30.123", "string", "number"),
            (4, "100.12", "30.12", "string", "string"),
        ]
        assert (
            conn.scalar(
                text("""
            SELECT count(*) FROM public.idempotency_receipts
            WHERE operation = 'settings.pricing.update'
        """)
            )
            == 2
        )


def test_remediation_preserves_untouched_legacy_numeric_and_nested_precision(
    patient_backend,
):
    clinic = patient_backend
    with transaction(clinic.runtime, clinic.tenant) as conn:
        append_version(
            conn,
            str(clinic.tenant),
            "PRICE",
            '{"standard_new_exam_egp":100.123,'
            '"preferential_followup_egp":30.5,'
            '"urgent_base_egp":null,"urgent_enabled":false,'
            '"nested_extra":{"rate":7.123}}',
            1,
        )
    status, result = pricing_request(
        clinic,
        clinic.owner,
        method="PATCH",
        body={
            **_patch(standard_new_exam_egp="100.12"),
            "expected_price_revision": 2,
        },
        key=str(uuid4()),
    )
    assert status == 200, result
    assert result["data"]["standard_new_exam_egp"] == "100.12"
    assert result["data"]["preferential_followup_egp"] == "30.50"
    assert "legacy_price_remediation" not in result["data"]
    with transaction(clinic.runtime, clinic.tenant) as conn:
        retained = conn.execute(
            text("""
                SELECT version, payload->>'standard_new_exam_egp',
                       jsonb_typeof(payload->'standard_new_exam_egp'),
                       payload->>'preferential_followup_egp',
                       jsonb_typeof(payload->'preferential_followup_egp'),
                       payload->'nested_extra'->>'rate',
                       jsonb_typeof(payload->'nested_extra'->'rate')
                FROM public.clinic_policy_versions
                WHERE kind = 'PRICE' AND version IN (2, 3) ORDER BY version
            """)
        ).all()
    assert retained == [
        (2, "100.123", "number", "30.5", "number", "7.123", "number"),
        (3, "100.12", "string", "30.5", "number", "7.123", "number"),
    ]


@pytest.mark.parametrize(
    "bad",
    [
        {"organization_id": "other"},
        {"actor_id": "other"},
        {"net_egp": "0.00"},
        {"is_free": True},
        {"price_revision": 99},
        {"standard_new_exam_egp": 100},
        {"standard_new_exam_egp": "1e2"},
    ],
)
def test_server_owned_and_untrusted_fields_rejected(patient_backend, bad):
    clinic = patient_backend
    assert pricing_request(
        clinic,
        clinic.owner,
        method="PATCH",
        body={**_patch(standard_new_exam_egp="100.00"), **bad},
        key=str(uuid4()),
    )[0] in (403, 422)
    with transaction(clinic.runtime, clinic.tenant) as conn:
        assert (
            conn.scalar(
                text("""
            SELECT revision FROM public.clinic_policy_heads WHERE kind = 'PRICE'
        """)
            )
            == 1
        )


def test_invalid_query_and_stale_revision_rejected(patient_backend):
    clinic = patient_backend
    for query in (
        {"service_type": "FOLLOW_UP"},
        {
            "service_type": "NEW_EXAMINATION",
            "discount_type": "NONE",
            "discount_value": "0",
            "organization_id": "forged",
        },
        {
            "service_type": "NEW_EXAMINATION",
            "discount_type": "PERCENT",
            "discount_value": "101",
        },
    ):
        assert pricing_request(clinic, clinic.owner, query=query)[0] == 422
    assert (
        pricing_request(
            clinic,
            clinic.owner,
            method="PATCH",
            body=_patch(standard_new_exam_egp="100.00"),
            key=str(uuid4()),
        )[0]
        == 200
    )
    assert (
        pricing_request(
            clinic,
            clinic.owner,
            method="PATCH",
            body=_patch(standard_new_exam_egp="120.00"),
            key=str(uuid4()),
        )[0]
        == 409
    )


def test_urgent_requires_operating_periods_and_valid_premium(patient_backend):
    clinic = patient_backend
    assert (
        pricing_request(
            clinic,
            clinic.owner,
            method="PATCH",
            body=_patch(
                standard_new_exam_egp="100.00",
                urgent_base_egp="120.00",
                urgent_enabled=True,
            ),
            key=str(uuid4()),
        )[0]
        == 422
    )
    # Actual working period is published through the existing D-04 policy helper.
    from carepulse.policies.versions import append_version

    with transaction(clinic.runtime, clinic.tenant) as conn:
        assert (
            append_version(
                conn,
                str(clinic.tenant),
                "SCHEDULE",
                {
                    "booking_mode": "OPEN",
                    "duration_minutes": None,
                    "holiday_booking_enabled": False,
                    "routine_walk_in_enabled": False,
                    "allow_overlap": False,
                    "no_show_cutoff": {"enabled": False, "local_time": None},
                },
                1,
                intervals=[
                    {
                        "interval_kind": "WORK",
                        "weekday": 1,
                        "service_date": None,
                        "starts_at": "09:00",
                        "ends_at": "17:00",
                    }
                ],
            )
            == 2
        )
    assert (
        pricing_request(
            clinic,
            clinic.owner,
            method="PATCH",
            body=_patch(
                standard_new_exam_egp="100.00",
                urgent_base_egp="120.00",
                urgent_enabled=True,
            ),
            key=str(uuid4()),
        )[0]
        == 200
    )
    assert (
        pricing_request(
            clinic,
            clinic.owner,
            method="PATCH",
            body={
                **_patch(standard_new_exam_egp="120.00"),
                "expected_price_revision": 2,
            },
            key=str(uuid4()),
        )[0]
        == 422
    )
    assert (
        pricing_request(
            clinic,
            clinic.owner,
            method="PATCH",
            body={
                **_patch(urgent_enabled=False),
                "expected_price_revision": 2,
            },
            key=str(uuid4()),
        )[0]
        == 200
    )


def test_signed_pricing_after_lawful_schedule_hours_removal(patient_backend):
    from test_schedule import _config, _schedule_request
    from test_schedule import _patch as schedule_patch

    clinic = patient_backend
    app.state.service_config, app.state.db_engine = clinic.config, clinic.runtime

    def activate(config, revision):
        body = schedule_patch(config, revision=revision).model_dump(mode="json")
        status, staged = _schedule_request(
            clinic, clinic.owner, method="PATCH", body=body, key=str(uuid4())
        )
        assert status == 200, staged
        status, preview = _schedule_request(
            clinic,
            clinic.owner,
            method="POST",
            suffix="/preview",
            body=body,
            key=str(uuid4()),
        )
        assert status == 200 and preview["data"]["affected"] == [], preview
        status, result = _schedule_request(
            clinic,
            clinic.owner,
            method="POST",
            suffix="/activate",
            body={
                "preview_id": preview["data"]["preview_id"],
                "expected_schedule_revision": revision,
                "expected_capacity_revision": 1,
                "reason": "Authorized operating hours change",
            },
            key=str(uuid4()),
        )
        assert status == 200 and result["data"]["schedule_revision"] == revision + 1, (
            result
        )

    open_hours = _config(
        mode="OPEN",
        working_weekdays=[1],
        weekly_work_periods=[{"weekday": 1, "starts_at": "09:00", "ends_at": "17:00"}],
    )
    activate(open_hours, 1)
    initial = _patch(
        standard_new_exam_egp="100.00",
        preferential_followup_egp="30.00",
        urgent_base_egp="150.00",
        urgent_enabled=True,
    )
    status, enabled = pricing_request(
        clinic, clinic.owner, method="PATCH", body=initial, key=str(uuid4())
    )
    assert status == 200, enabled
    activate(_config(mode="OPEN", working_weekdays=[], weekly_work_periods=[]), 2)

    followup = {
        **_patch(followup_last_day_inclusive=6),
        "expected_price_revision": 2,
        "expected_followup_revision": 1,
    }
    key = str(uuid4())
    status, changed = pricing_request(
        clinic, clinic.owner, method="PATCH", body=followup, key=key
    )
    assert status == 200 and changed["data"]["followup_revision"] == 2, changed
    assert changed["data"]["price_revision"] == 2
    assert pricing_request(
        clinic, clinic.owner, method="PATCH", body=followup, key=key
    ) == (status, changed)

    before = _pricing_evidence(clinic)
    invalid = {
        **_patch(standard_new_exam_egp="150.00"),
        "expected_price_revision": 2,
    }
    status, denied = pricing_request(
        clinic, clinic.owner, method="PATCH", body=invalid, key=str(uuid4())
    )
    assert status == 422 and denied["error"]["code"] == "INVALID_INPUT", denied
    assert _pricing_evidence(clinic) == before

    valid = {
        **_patch(preferential_followup_egp="35.00"),
        "expected_price_revision": 2,
    }
    status, price_changed = pricing_request(
        clinic, clinic.owner, method="PATCH", body=valid, key=str(uuid4())
    )
    assert status == 200 and price_changed["data"]["price_revision"] == 3, price_changed
    assert price_changed["data"]["urgent_enabled"] is True
    assert price_changed["data"]["urgent_base_egp"] == "150.00"


def test_signed_enable_rejects_retained_noncanonical_urgent_premium(patient_backend):
    clinic = patient_backend
    with transaction(clinic.runtime, clinic.tenant) as conn:
        append_version(
            conn,
            str(clinic.tenant),
            "PRICE",
            '{"standard_new_exam_egp":"100.00",'
            '"preferential_followup_egp":"30.00",'
            '"urgent_base_egp":150.123,"urgent_enabled":false}',
            1,
        )
        append_version(
            conn,
            str(clinic.tenant),
            "SCHEDULE",
            {
                "booking_mode": "OPEN",
                "duration_minutes": None,
                "holiday_booking_enabled": False,
                "routine_walk_in_enabled": False,
                "allow_overlap": False,
                "no_show_cutoff": {"enabled": False, "local_time": None},
            },
            1,
            intervals=[
                {
                    "interval_kind": "WORK",
                    "weekday": 1,
                    "service_date": None,
                    "starts_at": "09:00",
                    "ends_at": "17:00",
                }
            ],
        )
    before = _pricing_evidence(clinic)
    body = {**_patch(urgent_enabled=True), "expected_price_revision": 2}
    status, denied = pricing_request(
        clinic, clinic.owner, method="PATCH", body=body, key=str(uuid4())
    )
    assert status == 422 and denied["error"]["code"] == "INVALID_INPUT", denied
    assert _pricing_evidence(clinic) == before
    repaired = {
        **_patch(urgent_enabled=True, urgent_base_egp="150.00"),
        "expected_price_revision": 2,
    }
    status, result = pricing_request(
        clinic, clinic.owner, method="PATCH", body=repaired, key=str(uuid4())
    )
    assert status == 200 and result["data"]["urgent_enabled"] is True, result
    assert result["data"]["urgent_base_egp"] == "150.00"


@pytest.mark.parametrize(
    "service,base,standard,kind,discount",
    [
        ("NEW_EXAMINATION", "100.00", "100.00", "PERCENT", "12.345"),
        (
            "NEW_EXAMINATION",
            "1234567890123456789012345678901.00",
            "1234567890123456789012345678901.00",
            "PERCENT",
            "0.000000000000000000000000000000000000000000000001",
        ),
        ("FOLLOW_UP", "30.00", "100.00", "PERCENT", "0.500000000000000000000000000001"),
    ],
)
def test_quote_confirm_persist_and_read_preserves_full_percentage_and_service(
    appointment_storage, service, base, standard, kind, discount
):
    runtime, _, (tenant, _), (doctor, _) = appointment_storage
    quote = quote_price(base, standard, service, "ROUTINE", kind, discount)
    reason = "Approved discount"
    validate_discount_reason(kind, reason)
    with transaction(runtime, tenant) as conn:
        snapshot = {
            "base_egp": base,
            "creation_standard_base_egp": standard,
            "discount_type": kind,
            "discount_value": discount,
            "discount_reason": reason,
            "net_egp": format(quote.net_egp, ".2f"),
            "is_free": quote.is_free,
            "pricing_version": 1,
            "entitlement_provenance": None,
        }
        if service == "FOLLOW_UP":
            saved = _v2_priced_followup(conn, tenant, doctor, snapshot)
        else:
            saved = persist_appointment(
                conn,
                _draft(
                    _patient(conn, tenant),
                    doctor,
                    service_type=service,
                    price_snapshot=snapshot,
                ),
                _query(tenant, doctor),
            )
        assert saved["net_egp"] == format(quote.net_egp, ".2f")
    with transaction(runtime, tenant) as conn:
        row = conn.execute(
            text("""
            SELECT a.service_type, p.discount_value, p.discount_percent_text, p.net_egp
            FROM public.appointments a
            JOIN public.appointment_price_snapshots p ON p.appointment_id = a.id
                AND p.organization_id = a.organization_id
            WHERE a.id = :appointment
        """),
            {"appointment": saved["id"]},
        ).one()
        assert row[0] == service
        assert row[1] is None
        assert row[2] == discount
        assert row[3] == quote.net_egp

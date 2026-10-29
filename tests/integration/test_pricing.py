"""B-06 signed pricing API and frozen appointment acceptance (disposable CI PostgreSQL)."""

import asyncio
import json
from dataclasses import replace
from datetime import UTC, datetime
from decimal import Decimal
from hashlib import sha256
from hmac import new
from urllib.parse import urlencode
from uuid import UUID, uuid4

import pytest
from carepulse.appointments.persistence import persist_appointment
from carepulse.db import transaction
from carepulse.identity.signing import canonical_payload, canonical_query
from carepulse.main import app
from carepulse.policies.pricing import quote_price, validate_discount_reason
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
from test_patients import patient_backend as patient_backend  # noqa: PLC0414 - fixture export
from test_request_boundary import boundary_data as boundary_data  # noqa: PLC0414 - fixture export

PATH = "/v1/settings/pricing"


def pricing_request(
    clinic, actor, *, method="GET", body=None, key="", query=None, origin=None
):
    payload = (
        json.dumps(body, separators=(",", ":")).encode() if body is not None else b""
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
    for forged_actor in (
        replace(clinic.owner, role="OPERATOR"),
        replace(clinic.owner, organization_id=str(clinic.other)),
        replace(clinic.receptionist, organization_id=str(clinic.other)),
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
        draft = _draft(
            _patient(conn, tenant),
            doctor,
            service_type=service,
            price_snapshot={
                "base_egp": base,
                "creation_standard_base_egp": standard,
                "discount_type": kind,
                "discount_value": discount,
                "discount_reason": reason,
                "net_egp": format(quote.net_egp, ".2f"),
                "is_free": quote.is_free,
                "entitlement_provenance": {"synthetic_prior_completed": True}
                if service == "FOLLOW_UP"
                else None,
            },
        )
        saved = persist_appointment(conn, draft, _query(tenant, doctor))
        assert saved["net_egp"] == format(quote.net_egp, ".2f")
    with transaction(runtime, tenant) as conn:
        row = conn.execute(
            text("""
            SELECT a.service_type, p.discount_value::text, p.net_egp
            FROM public.appointments a
            JOIN public.appointment_price_snapshots p ON p.appointment_id = a.id
                AND p.organization_id = a.organization_id
            WHERE a.id = :appointment
        """),
            {"appointment": saved["id"]},
        ).one()
        assert row[0] == service
        assert Decimal(row[1]) == Decimal(discount)
        assert row[2] == quote.net_egp

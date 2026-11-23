"""T004 regression coverage for target-aware receipts and B-10 compatibility."""

from dataclasses import replace
from datetime import UTC, datetime
from hashlib import sha256
from uuid import UUID, uuid4

import pytest
from carepulse.identity.authorization import AuthorizationDenied, protected_transaction
from carepulse.identity.contracts import ContractError, normalized_command_digest
from carepulse.identity.receipts import (
    Receipt,
    ReceiptConflict,
    TenantReceiptQuery,
    insert_tenant_receipt,
    lookup_tenant_receipt,
    require_safe_committed_tenant_receipt,
    resolve_safe_tenant_receipt,
)
from pydantic import BaseModel, ConfigDict
from runtime_identity_fixture import registered_test_request
from sqlalchemy import text
from test_booking import booking_clinic as booking_clinic  # noqa: PLC0414
from test_database import admin as admin  # noqa: PLC0414
from test_database import disposable_project as disposable_project  # noqa: PLC0414
from test_database import empty_database as empty_database  # noqa: PLC0414
from test_followup_decisions import _decision, _signed_visit_request, _source
from test_followups import followup_clinic as followup_clinic  # noqa: PLC0414
from test_patients import create_patient
from test_patients import patient_backend as patient_backend  # noqa: PLC0414
from test_request_boundary import boundary_data as boundary_data  # noqa: PLC0414


class _TargetCommand(BaseModel):
    model_config = ConfigDict(extra="forbid")

    physical_presence_confirmed: bool
    expected_appointment_revision: int


def _booking_envelope(actor, *, key: str, patient_id: UUID):
    body = (
        '{"patient_id":"'
        + str(patient_id)
        + '","service_date":"2026-11-24","service_type":"NEW_EXAMINATION",'
        '"expected_schedule_revision":1,"expected_price_revision":1,'
        '"expected_followup_revision":1}'
    ).encode()
    signed = replace(
        actor,
        method="POST",
        path="/v1/appointments",
        query="",
        body_sha256=sha256(body).hexdigest(),
        purpose="APPOINTMENT_BOOKING",
        timestamp=str(int(datetime.now(UTC).timestamp() * 1000)),
        nonce=f"synthetic_{uuid4().hex}",
        idempotency_key=key,
    )
    return signed, body


def test_target_aware_receipt_conflict_hidden_target_and_lost_response_recovery(
    booking_clinic,
) -> None:
    patient_a = UUID(create_patient(booking_clinic, booking_clinic.owner, name="Alpha"))
    patient_b = UUID(create_patient(booking_clinic, booking_clinic.owner, name="Beta"))
    foreign_target = uuid4()
    key = f"t004-target-{uuid4().hex}"
    body = _TargetCommand(
        physical_presence_confirmed=True, expected_appointment_revision=0
    )
    digest_a = normalized_command_digest(
        "appointments.book", body, target={"patient_id": patient_a}
    )
    digest_b = normalized_command_digest(
        "appointments.book", body, target={"patient_id": patient_b}
    )
    digest_foreign = normalized_command_digest(
        "appointments.book", body, target={"patient_id": foreign_target}
    )

    signed, payload = _booking_envelope(
        booking_clinic.owner, key=key, patient_id=patient_a
    )
    with (
        registered_test_request(booking_clinic, signed, payload),
        protected_transaction(
            booking_clinic.runtime,
            signed,
            booking_clinic.config.policy.session_idle_minutes,
        ) as (connection, actor),
    ):
        receipt_id = insert_tenant_receipt(
            connection,
            Receipt(
                actor_id=actor.staff_id,
                operation="appointments.book",
                idempotency_key=key,
                request_digest=digest_a,
                result_projection={"patient_id": str(patient_a), "status": "Booked"},
                result_revision=1,
                organization_id=actor.organization_id,
                staff_id=actor.staff_id,
            ),
        )
        stored = lookup_tenant_receipt(
            connection,
            actor.organization_id,
            actor.staff_id,
            "appointments.book",
            key,
        )
        assert stored is not None
        assert stored["id"] == receipt_id

    # 1) Committed/lost-response recovery on identical authorized target returns original result.
    signed_rec, payload_rec = _booking_envelope(
        booking_clinic.owner, key=key, patient_id=patient_a
    )
    with (
        registered_test_request(booking_clinic, signed_rec, payload_rec),
        protected_transaction(
            booking_clinic.runtime,
            signed_rec,
            booking_clinic.config.policy.session_idle_minutes,
            stage="RECEIPT_RECOVERY",
        ) as (connection, actor),
    ):
        query = TenantReceiptQuery(actor, "appointments.book", key, digest_a)
        projection, revision = require_safe_committed_tenant_receipt(
            connection,
            query,
            check_target_visible=lambda: None,
            validate_projection=lambda proj, rev: (
                proj.get("patient_id") == str(patient_a) and rev == 1
            ),
        )
        assert projection == {"patient_id": str(patient_a), "status": "Booked"}
        assert revision == 1

    # 2) Same actor/key/body on a second visible target conflicts (no cross-target replay).
    signed_b, payload_b = _booking_envelope(
        booking_clinic.owner, key=key, patient_id=patient_b
    )
    with (
        registered_test_request(booking_clinic, signed_b, payload_b),
        protected_transaction(
            booking_clinic.runtime,
            signed_b,
            booking_clinic.config.policy.session_idle_minutes,
            stage="RECEIPT_RECOVERY",
        ) as (connection, actor),
    ):
        query_b = TenantReceiptQuery(actor, "appointments.book", key, digest_b)
        with pytest.raises(ReceiptConflict):
            resolve_safe_tenant_receipt(
                connection,
                query_b,
                check_target_visible=lambda: None,
                validate_projection=lambda _p, _r: True,
            )

    # 3) Same actor/key/body on a hidden/foreign target returns 404 before conflict disclosure.
    signed_f, payload_f = _booking_envelope(
        booking_clinic.owner, key=key, patient_id=foreign_target
    )
    with (
        registered_test_request(booking_clinic, signed_f, payload_f),
        protected_transaction(
            booking_clinic.runtime,
            signed_f,
            booking_clinic.config.policy.session_idle_minutes,
            stage="RECEIPT_RECOVERY",
        ) as (connection, actor),
    ):
        query_f = TenantReceiptQuery(actor, "appointments.book", key, digest_foreign)

        def _missing_target():
            raise ContractError(404)

        with pytest.raises(ContractError) as denied:
            resolve_safe_tenant_receipt(
                connection,
                query_f,
                check_target_visible=_missing_target,
                validate_projection=lambda _p, _r: True,
            )
        assert denied.value.status_code == 404

    # 4) Post-revocation lookup denies under fresh authorization without returning the receipt.
    with booking_clinic.migration.begin() as migration:
        migration.execute(
            text("""
                UPDATE public.staff_accounts
                SET security_revision = security_revision + 1
                WHERE organization_id = :tenant AND id = :staff
            """),
            {
                "tenant": booking_clinic.tenant,
                "staff": UUID(booking_clinic.owner.staff_id),
            },
        )
    signed_revoked, payload_revoked = _booking_envelope(
        booking_clinic.owner, key=key, patient_id=patient_a
    )
    with (
        registered_test_request(booking_clinic, signed_revoked, payload_revoked),
        pytest.raises(AuthorizationDenied),
        protected_transaction(
            booking_clinic.runtime,
            signed_revoked,
            booking_clinic.config.policy.session_idle_minutes,
            stage="RECEIPT_RECOVERY",
        ),
    ):
        pass


def test_b10_existing_receipt_compatibility_preserved(followup_clinic) -> None:
    source = _source(followup_clinic)
    key = f"t004-b10-{uuid4().hex}"
    payload = _decision("OPTED_OUT", reason="Not indicated").model_dump(
        mode="json", exclude_unset=True
    )
    first_status, first_body = _signed_visit_request(
        followup_clinic,
        followup_clinic.owner,
        method="PATCH",
        path=f"/v1/visits/{source['visit']}",
        body=payload,
        key=key,
    )
    assert first_status == 200, first_body

    replay_status, replay_body = _signed_visit_request(
        followup_clinic,
        followup_clinic.owner,
        method="PATCH",
        path=f"/v1/visits/{source['visit']}",
        body=payload,
        key=key,
    )
    assert replay_status == 200, replay_body
    assert replay_body == first_body

    second_source = _source(followup_clinic)
    conflict_status, conflict_body = _signed_visit_request(
        followup_clinic,
        followup_clinic.owner,
        method="PATCH",
        path=f"/v1/visits/{second_source['visit']}",
        body=payload,
        key=key,
    )
    assert conflict_status == 409, conflict_body
    assert conflict_body["error"]["code"] == "IDEMPOTENCY_CONFLICT"

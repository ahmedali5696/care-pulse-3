"""T046: signed B-17 cancellation, new-ID replacement and receipt boundaries."""

import json
from dataclasses import replace
from datetime import UTC, datetime
from decimal import Decimal
from hashlib import sha256
from uuid import UUID, uuid4

import pytest
from carepulse.appointments.cancel import recover_cancel_replace, replace_urgent
from carepulse.appointments.cancel_contracts import ReplaceUrgent
from carepulse.identity.authorization import protected_transaction
from runtime_identity_fixture import registered_test_request, signed_test_transaction
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from test_booking import booking_body, booking_request, new_patient
from test_booking import booking_clinic as booking_clinic  # noqa: PLC0414
from test_followups import completed_source, followup_body
from test_patients import patient_backend as patient_backend  # noqa: PLC0414
from test_patients import private_request
from test_request_boundary import boundary_data as boundary_data  # noqa: PLC0414
from test_w3_admission import _admit, admit_body, waiting_case
from test_w3_checkin import queue_request
from test_w3_intake import (
    _policy_revision,
    _today,
    intake_body,
    intake_effects,
    intake_request,
    set_intake_schedule,
)
from test_w3_intake import intake_clinic as intake_clinic  # noqa: PLC0414
from test_w3_receipt_boundary import _foreign_appointment


def change_request(clinic, actor, appointment_id, operation, body, *, key=None):
    return queue_request(
        clinic,
        actor,
        method="POST",
        path=f"/v1/appointments/{appointment_id}/{operation}",
        purpose={
            "cancel": "APPOINTMENT_CANCEL",
            "replace-urgent": "APPOINTMENT_REPLACE_URGENT",
        }[operation],
        body=body,
        key=key or uuid4().hex,
    )


def _appointment(clinic, appointment_id):
    with clinic.migration.connect() as connection:
        return (
            connection.execute(
                text("""
            SELECT a.*, p.base_egp, p.net_egp, p.pricing_version, p.discount_reason
            FROM public.appointments a JOIN public.appointment_price_snapshots p
              ON p.organization_id=a.organization_id AND p.appointment_id=a.id
            WHERE a.organization_id=:tenant AND a.id=:id
        """),
                {"tenant": clinic.tenant, "id": appointment_id},
            )
            .mappings()
            .one()
        )


def cancel_body(clinic, appointment_id, **changes):
    with clinic.migration.connect() as connection:
        row = connection.execute(
            text("""
            SELECT a.revision, CASE WHEN a.status='Waiting' THEN q.revision END AS queue_revision,
                   c.revision AS claim_revision,
                   CASE WHEN c.id IS NOT NULL THEN COALESCE(s.source_revision,0) END AS source_revision,
                   CASE WHEN c.id IS NULL THEN NULL ELSE
                     (SELECT COALESCE(max(e.authorization_revision),0)
                      FROM public.followup_authorization_events e
                      WHERE e.organization_id=c.organization_id AND e.claim_id=c.id)
                   END AS authorization_revision
            FROM public.appointments a
            LEFT JOIN public.queue_days q ON q.organization_id=a.organization_id
                 AND q.service_date=a.service_date AND q.lane=a.lane
            LEFT JOIN public.followup_claims c ON c.organization_id=a.organization_id AND c.appointment_id=a.id
            LEFT JOIN public.followup_source_state s ON s.organization_id=c.organization_id AND s.visit_id=c.source_visit_id
            WHERE a.organization_id=:tenant AND a.id=:id
        """),
            {"tenant": clinic.tenant, "id": appointment_id},
        ).one()
    revisions = {
        "expected_appointment_revision": row.revision,
        "expected_queue_day_revision": row.queue_revision,
        "expected_claim_revision": row.claim_revision,
        "expected_source_revision": row.source_revision,
        "expected_authorization_revision": row.authorization_revision,
    }
    return {
        **{key: value for key, value in revisions.items() if value is not None},
        "reason": "Synthetic reasoned cancellation",
        **changes,
    }


def replace_body(clinic, appointment_id, **changes):
    premium = intake_body(
        clinic, _appointment(clinic, appointment_id)["patient_id"], "urgent"
    )
    del premium["patient_id"]
    return {
        **cancel_body(clinic, appointment_id),
        **premium,
        "confirmation": "REPLACE",
        **changes,
    }


def book_case(clinic, *, waiting=False, patient=None):
    patient = patient or new_patient(clinic)
    if waiting:
        status, reply = intake_request(
            clinic,
            clinic.receptionist,
            "routine",
            intake_body(clinic, patient, "routine"),
        )
    else:
        status, reply = booking_request(
            clinic,
            clinic.receptionist,
            body=booking_body(
                patient,
                day=_today(),
                expected_price_revision=_policy_revision(clinic, "PRICE"),
                expected_schedule_revision=_policy_revision(clinic, "SCHEDULE"),
            ),
            key=uuid4().hex,
        )
    assert status == 201, reply
    return reply["data"]["id"]


def _denied(clinic, actor, appointment_id, operation, body, expected, *, key=None):
    before = intake_effects(clinic)
    status, reply = change_request(
        clinic, actor, appointment_id, operation, body, key=key
    )
    assert status == expected, reply
    assert set(reply) == {"error"}
    assert intake_effects(clinic) == before
    return reply


def _assert_recovery_stage(clinic, target, body, key, reply):
    payload = json.dumps(body, separators=(",", ":")).encode()
    identity = replace(
        clinic.receptionist,
        method="POST",
        path=f"/v1/appointments/{target}/replace-urgent",
        purpose="APPOINTMENT_REPLACE_URGENT",
        query="",
        body_sha256=sha256(payload).hexdigest(),
        timestamp=str(int(datetime.now(UTC).timestamp() * 1000)),
        nonce=f"synthetic_{uuid4().hex}",
        idempotency_key=key,
    )
    idle = clinic.config.policy.session_idle_minutes
    with registered_test_request(clinic, identity, payload):
        with (
            pytest.raises(RuntimeError, match="synthetic rollback"),
            protected_transaction(clinic.runtime, identity, idle),
        ):
            raise RuntimeError("synthetic rollback")
        with protected_transaction(
            clinic.runtime, identity, idle, stage="RECEIPT_RECOVERY"
        ) as (connection, actor):
            assert connection.scalar(text("SELECT session_user")) == "carepulse_api"
            assert (
                connection.scalar(text("SELECT count(*) FROM public.appointments")) == 0
            )
            with pytest.raises(DBAPIError) as denied, connection.begin_nested():
                connection.scalar(text("SELECT public.f01_b17_cancel(NULL)"))
            assert denied.value.orig.sqlstate == "42501"
            assert recover_cancel_replace(
                connection, actor, UUID(target), ReplaceUrgent.model_validate(body), key
            ) == (reply["data"], reply["revision"])


def _terminal_evidence(clinic, appointment_id, actor, body, *, replacement=None):
    with clinic.migration.connect() as connection:
        args = {"tenant": clinic.tenant, "id": appointment_id}
        history = (
            connection.execute(
                text("""
            SELECT * FROM public.appointment_history WHERE organization_id=:tenant AND appointment_id=:id
            ORDER BY appointment_revision DESC LIMIT 1
        """),
                args,
            )
            .mappings()
            .one()
        )
        assert history["new_status"] == "Cancelled"
        assert (
            history["appointment_revision"] == body["expected_appointment_revision"] + 1
        )
        assert (
            str(history["actor_staff_id"]) == actor.staff_id
            and history["actor_role"] == actor.role
        )
        assert history["reason"] == body["reason"]
        assert history["old_number"] == history["new_number"]
        assert history["old_service_date"] == history["new_service_date"]
        assert (
            str(history["replacement_appointment_id"]) == replacement
            if replacement
            else history["replacement_appointment_id"] is None
        )
        entries = connection.execute(
            text("""
            SELECT disposition,closure_history_revision FROM public.queue_entries
            WHERE organization_id=:tenant AND appointment_id=:id
        """),
            args,
        ).all()
        if history["old_status"] == "Waiting":
            assert entries == [("CLOSED", history["appointment_revision"])]
        else:
            assert entries == []
        review = (
            connection.execute(
                text("""
            SELECT payload FROM public.outbox_events WHERE organization_id=:tenant
              AND aggregate_id=CAST(:id AS text) AND event_type='DOCTOR_CANCELLED_APPOINTMENT'
        """),
                args,
            )
            .scalars()
            .all()
        )
        assert len(review) == (1 if actor.role == "DOCTOR_OWNER" else 0)
        if review:
            assert review[0]["appointment_id"] == str(appointment_id)
        return history


@pytest.mark.parametrize(
    "actor_name,waiting", [("owner", True), ("receptionist", False)]
)
def test_reasoned_cancellation_closes_only_the_target_and_emits_actor_specific_review(
    intake_clinic, actor_name, waiting
):
    clinic = intake_clinic
    actor = getattr(clinic, actor_name)
    target = book_case(clinic, waiting=waiting)
    body = cancel_body(clinic, target)
    _denied(clinic, actor, target, "cancel", {**body, "reason": " "}, 422)
    _denied(
        clinic,
        actor,
        target,
        "cancel",
        {**body, "expected_appointment_revision": 9},
        409,
    )
    before = intake_effects(clinic)
    key = uuid4().hex
    status, reply = change_request(clinic, actor, target, "cancel", body, key=key)
    assert status == 200, reply
    assert reply["data"]["status"] == "Cancelled"
    assert _appointment(clinic, target)["status"] == "Cancelled"
    _terminal_evidence(clinic, target, actor, body)
    after = intake_effects(clinic)
    for table in (
        "appointment_day_counters",
        "appointment_number_assignments",
        "appointment_price_snapshots",
        "visits",
        "followup_claims",
    ):
        assert before[table] == after[table]
    assert len(after["appointment_history"]) == len(before["appointment_history"]) + 1
    assert len(after["sensitive_audit"]) == len(before["sensitive_audit"]) + 1
    assert len(after["idempotency_receipts"]) == len(before["idempotency_receipts"]) + 1
    assert change_request(clinic, actor, target, "cancel", body, key=key) == (
        200,
        reply,
    )
    assert intake_effects(clinic) == after
    _denied(clinic, actor, target, "cancel", cancel_body(clinic, target), 409)


def test_waiting_followup_cancellation_releases_frozen_claim(intake_clinic):
    clinic = intake_clinic
    source = completed_source(clinic)
    status, reply = intake_request(
        clinic,
        clinic.receptionist,
        "routine",
        followup_body(
            source,
            offset=1,
            expected_price_revision=_policy_revision(clinic, "PRICE"),
            expected_schedule_revision=_policy_revision(clinic, "SCHEDULE"),
            physical_presence_confirmed=True,
        ),
    )
    assert status == 201, reply
    target = reply["data"]["id"]
    body = cancel_body(clinic, target)
    assert body["expected_claim_revision"] >= 1
    _denied(
        clinic,
        clinic.receptionist,
        target,
        "cancel",
        {**body, "expected_claim_revision": body["expected_claim_revision"] + 1},
        409,
    )
    before = intake_effects(clinic)
    status, reply = change_request(clinic, clinic.receptionist, target, "cancel", body)
    assert status == 200, reply
    assert reply["data"]["claim_revision"] == body["expected_claim_revision"] + 1
    _terminal_evidence(clinic, target, clinic.receptionist, body)
    with clinic.migration.connect() as connection:
        claim = connection.execute(
            text("""
            SELECT state,revision FROM public.followup_claims WHERE organization_id=:tenant AND appointment_id=:id
        """),
            {"tenant": clinic.tenant, "id": target},
        ).one()
        assert claim == ("RELEASED", body["expected_claim_revision"] + 1)
    after = intake_effects(clinic)
    for table in (
        "appointment_price_snapshots",
        "appointment_number_assignments",
        "appointment_day_counters",
        "followup_authorization_events",
        "visits",
    ):
        assert before[table] == after[table]


def test_replacement_preserves_old_number_price_history_and_recovers_exact_result(
    intake_clinic,
):
    clinic = intake_clinic
    target = book_case(clinic, waiting=True)
    other = book_case(clinic)
    body = replace_body(
        clinic,
        target,
        discount={
            "discount_type": "FIXED",
            "discount_value": "49.99",
            "discount_reason": "Synthetic premium\nApproved onsite",
        },
        expected_net_egp="100.01",
        expected_capacity_revision=None,
    )
    old = _appointment(clinic, target)
    key = uuid4().hex
    before = intake_effects(clinic)
    with clinic.runtime.connect() as connection:
        with pytest.raises(DBAPIError) as denied, connection.begin_nested():
            connection.scalar(text("SELECT public.f01_b17_replay()"))
        assert denied.value.orig.sqlstate == "42501"
    with signed_test_transaction(
        clinic,
        clinic.receptionist,
        method="POST",
        path=f"/v1/appointments/{target}/replace-urgent",
        purpose="APPOINTMENT_REPLACE_URGENT",
        body=body,
        key=key,
    ) as (connection, _):
        assert connection.scalar(text("SELECT session_user")) == "carepulse_api"
        assert connection.execute(
            text("SELECT id FROM public.appointments")
        ).scalars().all() == [UUID(target)]
        assert connection.scalar(text("SELECT public.f01_b17_replay()")) is None
        for statement in (
            "SELECT public.f01_b17_cancel(NULL)",
            "SELECT public.f01_b17_finish('{}'::jsonb)",
            "UPDATE public.appointments SET status='Cancelled'",
            "SELECT public.f01_b17_start(gen_random_uuid())",
            "SELECT public.f01_b16_mutate()",
        ):
            with pytest.raises(DBAPIError) as denied, connection.begin_nested():
                connection.execute(text(statement))
            assert denied.value.orig.sqlstate == "42501"
    assert intake_effects(clinic) == before
    # Force deferred DB checks while still inside the signed request, then roll back.
    with (
        pytest.raises(RuntimeError, match="synthetic replacement rollback"),
        signed_test_transaction(
            clinic,
            clinic.receptionist,
            method="POST",
            path=f"/v1/appointments/{target}/replace-urgent",
            purpose="APPOINTMENT_REPLACE_URGENT",
            body=body,
            key=key,
        ) as (connection, actor),
    ):
        assert connection.scalar(text("SELECT session_user")) == "carepulse_api"
        projection, revision, created = replace_urgent(
            connection, actor, UUID(target), ReplaceUrgent.model_validate(body), key
        )
        assert created and revision == body["expected_appointment_revision"] + 1
        assert projection["new_status"] == "Waiting"
        connection.execute(text("SET CONSTRAINTS ALL IMMEDIATE"))
        raise RuntimeError("synthetic replacement rollback")
    assert intake_effects(clinic) == before
    status, reply = change_request(
        clinic, clinic.receptionist, target, "replace-urgent", body, key=key
    )
    assert status == 201, reply
    projection = reply["data"]
    new_id = projection["replacement_appointment_id"]
    assert new_id != target and projection["appointment_id"] == target
    original, new = _appointment(clinic, target), _appointment(clinic, new_id)
    assert (
        original["status"] == "Cancelled"
        and original["current_number"] == old["current_number"]
    )
    assert (
        original["base_egp"] == old["base_egp"]
        and original["net_egp"] == old["net_egp"]
    )
    assert (
        new["patient_id"] == old["patient_id"]
        and new["service_date"] == old["service_date"]
    )
    assert (new["status"], new["classification"], new["lane"], new["service_type"]) == (
        "Waiting",
        "URGENT",
        "MAIN",
        "NEW_EXAMINATION",
    )
    assert new["current_number"] > old["current_number"] and new["revision"] == 0
    assert (new["base_egp"], new["net_egp"]) == (Decimal("150.00"), Decimal("100.01"))
    assert new["discount_reason"] == body["discount"]["discount_reason"]
    _terminal_evidence(clinic, target, clinic.receptionist, body, replacement=new_id)
    after = intake_effects(clinic)
    for table in (
        "appointments",
        "appointment_number_assignments",
        "appointment_price_snapshots",
        "queue_entries",
        "idempotency_receipts",
    ):
        assert len(after[table]) == len(before[table]) + 1
    assert len(after["appointment_history"]) == len(before["appointment_history"]) + 2
    assert change_request(
        clinic, clinic.receptionist, target, "replace-urgent", body, key=key
    ) == (200, reply)
    _assert_recovery_stage(clinic, target, body, key, reply)
    assert intake_effects(clinic) == after
    omitted = {k: v for k, v in body.items() if k != "expected_capacity_revision"}
    conflict = _denied(
        clinic, clinic.receptionist, target, "replace-urgent", omitted, 409, key=key
    )
    assert conflict["error"]["code"] == "IDEMPOTENCY_CONFLICT"
    _denied(
        clinic,
        clinic.receptionist,
        target,
        "replace-urgent",
        {**body, "reason": "Changed reason"},
        409,
        key=key,
    )
    _denied(
        clinic,
        clinic.receptionist,
        other,
        "replace-urgent",
        replace_body(clinic, other),
        409,
        key=key,
    )
    foreign = _foreign_appointment(clinic)
    _denied(clinic, clinic.receptionist, foreign, "replace-urgent", body, 404, key=key)
    second_body, second_key = replace_body(clinic, other), uuid4().hex
    status, second = change_request(
        clinic,
        clinic.receptionist,
        other,
        "replace-urgent",
        second_body,
        key=second_key,
    )
    assert status == 201, second
    assert second["data"]["replacement_appointment_id"] not in (new_id, other, target)
    committed = intake_effects(clinic)
    assert change_request(
        clinic,
        clinic.receptionist,
        other,
        "replace-urgent",
        second_body,
        key=second_key,
    ) == (200, second)
    omitted_discount = {
        **second_body,
        "discount": {
            k: v for k, v in second_body["discount"].items() if k != "discount_reason"
        },
    }
    conflict = _denied(
        clinic,
        clinic.receptionist,
        other,
        "replace-urgent",
        omitted_discount,
        409,
        key=second_key,
    )
    assert conflict["error"]["code"] == "IDEMPOTENCY_CONFLICT"
    assert intake_effects(clinic) == committed


def test_replacement_rejects_invalid_actor_onsite_premium_holiday_and_archived_patient(
    intake_clinic,
):
    clinic = intake_clinic
    target = book_case(clinic)
    body = replace_body(clinic, target)
    _denied(clinic, clinic.owner, target, "replace-urgent", body, 403)
    for changes in (
        {"confirmation": "replace"},
        {"physical_presence_confirmed": False},
        {"physical_presence_confirmed": 1},
        {"reason": ""},
        {"patient_id": str(uuid4())},
    ):
        _denied(
            clinic,
            clinic.receptionist,
            target,
            "replace-urgent",
            {**body, **changes},
            422,
        )
    _denied(
        clinic,
        clinic.receptionist,
        target,
        "replace-urgent",
        {**body, "expected_price_revision": body["expected_price_revision"] - 1},
        409,
    )
    _denied(
        clinic,
        clinic.receptionist,
        target,
        "replace-urgent",
        {
            **body,
            "discount": {
                "discount_type": "FIXED",
                "discount_value": "50.00",
                "discount_reason": "Premium too low",
            },
            "expected_net_egp": "100.00",
        },
        422,
    )
    set_intake_schedule(clinic, holidays=(_today(),), holiday_booking_enabled=True)
    _denied(
        clinic,
        clinic.receptionist,
        target,
        "replace-urgent",
        replace_body(clinic, target),
        409,
    )
    set_intake_schedule(clinic)
    patient = _appointment(clinic, target)["patient_id"]
    status, reply = private_request(
        clinic,
        clinic.owner,
        f"/v1/patients/{patient}/archive",
        method="POST",
        body={"reason": "Synthetic archive", "expected_lifecycle_revision": 0},
    )
    assert status == 200, reply
    _denied(
        clinic,
        clinic.receptionist,
        target,
        "replace-urgent",
        replace_body(clinic, target),
        409,
    )
    status, reply = change_request(
        clinic, clinic.receptionist, target, "cancel", cancel_body(clinic, target)
    )
    assert status == 200, reply


def test_in_examination_cannot_use_ordinary_cancellation_or_replacement(intake_clinic):
    clinic = intake_clinic
    waiting = waiting_case(clinic)
    _admit(
        clinic,
        clinic.owner,
        admit_body(clinic, waiting, physical_presence_confirmed=True),
    )
    target = waiting["appointment_id"]
    _denied(clinic, clinic.owner, target, "cancel", cancel_body(clinic, target), 409)
    _denied(
        clinic,
        clinic.receptionist,
        target,
        "replace-urgent",
        replace_body(clinic, target),
        409,
    )

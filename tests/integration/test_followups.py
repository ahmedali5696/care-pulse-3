"""B-10 real-PostgreSQL acceptance of preferential booking and receipts."""

import json
from contextlib import nullcontext
from dataclasses import replace
from datetime import datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace
from uuid import UUID, uuid4
from zoneinfo import ZoneInfo

import pytest
from carepulse.appointments.book import book_appointment
from carepulse.appointments.booking_contracts import BookAppointment
from carepulse.appointments.persistence import persist_appointment
from carepulse.appointments.queries import appointment_detail
from carepulse.db import transaction
from carepulse.identity.contracts import normalized_request_digest
from carepulse.identity.receipts import Receipt, insert_tenant_receipt
from carepulse.main import app
from carepulse.notifications.evidence import append_outbox, append_sensitive_audit
from sqlalchemy import text
from test_appointment_persistence import _admit_saved, _draft, _query
from test_booking import (
    booking_body,
    booking_effects,
    booking_request,
    new_patient,
)
from test_booking import (
    booking_clinic as booking_clinic,  # noqa: PLC0414
)
from test_database import admin as admin  # noqa: PLC0414
from test_database import empty_database as empty_database  # noqa: PLC0414
from test_followup_persistence import _source, _v2_booking, _v2_command, _v2_source
from test_patients import patient_backend as patient_backend  # noqa: PLC0414
from test_pricing import _patch, pricing_request
from test_request_boundary import boundary_data as boundary_data  # noqa: PLC0414
from test_schedule import _actor, _config, _publish
from test_visit_migration import _prepare, _upgrade


@pytest.fixture
def followup_clinic(booking_clinic):
    clinic = booking_clinic
    status, response = pricing_request(
        clinic,
        clinic.owner,
        method="PATCH",
        body=_patch(expected_price_revision=2, preferential_followup_egp="30.00"),
        key=str(uuid4()),
    )
    assert status == 200, response
    with transaction(clinic.runtime, clinic.tenant) as connection:
        _publish(
            connection,
            _actor(clinic.tenant, UUID(clinic.owner.staff_id)),
            _config(mode="OPEN", working_weekdays=list(range(1, 8))),
            revision=2,
        )
    return clinic


def completed_source(clinic, *, classification="ROUTINE"):
    with transaction(clinic.runtime, clinic.tenant) as connection:
        doctor = UUID(clinic.owner.staff_id)
        source = (
            _v2_source(connection, clinic.tenant, doctor)
            if classification == "ROUTINE"
            else _source(
                connection, clinic.tenant, doctor, classification=classification
            )
        )
        assert source["day"] is not None
        assert (
            connection.scalar(
                text("""
            SELECT count(*) FROM public.completion_attestations
            WHERE organization_id=:tenant AND visit_id=:visit AND examination_occurred
        """),
                {"tenant": clinic.tenant, "visit": source["visit"]},
            )
            == 1
        )
        return source


def same_day_completed_source(clinic):
    doctor = UUID(clinic.owner.staff_id)
    patient = new_patient(clinic)
    today = datetime.now(ZoneInfo("Africa/Cairo")).date()
    with transaction(clinic.runtime, clinic.tenant) as connection:
        saved = persist_appointment(
            connection,
            _draft(patient, doctor, service_date=today),
            _query(clinic.tenant, doctor),
        )
        _admit_saved(connection, clinic.tenant, doctor, saved, complete=True)
        visit, completion_day = connection.execute(
            text("""
            SELECT c.visit_id, (c.completed_at AT TIME ZONE 'Africa/Cairo')::date
            FROM public.completion_attestations c
            WHERE c.organization_id=:tenant AND c.appointment_id=:appointment
        """),
            {"tenant": clinic.tenant, "appointment": UUID(saved["id"])},
        ).one()
    assert completion_day == today
    return {
        "patient": patient,
        "visit": visit,
        "day": completion_day,
        "appointment": UUID(saved["id"]),
    }


def followup_body(source, *, offset=0, base="30.00", source_revision=0, **changes):
    fields = {
        "requested_service_type": "FOLLOW_UP",
        "source_visit_id": str(source["visit"]),
        "expected_source_decision_revision": source_revision,
        "expected_visit_revision": 0,
        "expected_base_egp": base,
        "expected_net_egp": base,
        "expected_price_revision": 3,
        "expected_followup_revision": 1,
        "expected_schedule_revision": 3,
        **changes,
    }
    return booking_body(
        source["patient"], day=source["day"] + timedelta(days=offset), **fields
    )


def claim_record(clinic, appointment_id):
    with transaction(clinic.runtime, clinic.tenant) as connection:
        return (
            connection.execute(
                text("""
                SELECT c.state, c.source_visit_id, c.source_appointment_id,
                       c.completion_local_date, c.approved_deadline,
                       c.policy_snapshot, s.base_egp, s.net_egp,
                       s.entitlement_provenance
                  FROM public.followup_claims c
                  JOIN public.appointment_price_snapshots s
                    ON s.organization_id=c.organization_id
                   AND s.appointment_id=c.appointment_id
                 WHERE c.organization_id=:tenant AND c.appointment_id=:appointment
            """),
                {"tenant": clinic.tenant, "appointment": UUID(appointment_id)},
            )
            .mappings()
            .one_or_none()
        )


def doctor_decision(clinic, visit_id, decision):
    with transaction(clinic.runtime, clinic.tenant) as connection:
        connection.execute(
            text("SELECT set_config('app.staff_id', :actor, true)"),
            {"actor": clinic.owner.staff_id},
        )
        current = connection.execute(
            text("""
                SELECT COALESCE(s.source_revision,0), v.clinical_revision,
                       h.current_version FROM public.visits v
                JOIN public.clinic_policy_heads h ON h.organization_id=v.organization_id
                    AND h.kind='FOLLOWUP'
                LEFT JOIN public.followup_source_state s
                    ON s.organization_id=v.organization_id AND s.visit_id=v.id
                WHERE v.organization_id=:tenant AND v.id=:visit
            """),
            {"tenant": clinic.tenant, "visit": visit_id},
        ).one()
        return connection.scalar(
            text("""
                SELECT public.record_followup_source_decision(
                    :tenant, :visit, CAST(:expected AS jsonb), CAST(:decision AS jsonb))
            """),
            {
                "tenant": clinic.tenant,
                "visit": visit_id,
                "expected": json.dumps(
                    {
                        "source_revision": current[0],
                        "clinical_revision": current[1],
                        "policy_version": current[2],
                    }
                ),
                "decision": json.dumps(decision),
            },
        )


def terminal_claim(
    clinic, booked, *, status, migration_connection=None, pre_admitted=False
):
    """Record a real terminal Appointment history and its matched frozen claim state."""
    assert status in ("Cancelled", "Completed")
    appointment_id = UUID(booked["id"])
    doctor = UUID(clinic.owner.staff_id)
    if status == "Completed" and not pre_admitted:
        # Admission is real; B-17 has no terminal endpoint yet, so migration
        # writes the matched completion and claim evidence in one transaction.
        with transaction(clinic.runtime, clinic.tenant) as connection:
            _admit_saved(connection, clinic.tenant, doctor, booked, complete=False)
    with (
        nullcontext(migration_connection)
        if migration_connection is not None
        else clinic.migration.begin()
    ) as connection:
        if status == "Completed":
            revision = connection.scalar(
                text("""
                UPDATE public.appointments
                SET status='Completed', revision=revision+1
                WHERE organization_id=:tenant AND id=:appointment
                  AND status='In Examination' AND revision=2
                RETURNING revision
            """),
                {"tenant": clinic.tenant, "appointment": appointment_id},
            )
            assert revision == 3
            completed_at = connection.scalar(
                text("""
                INSERT INTO public.appointment_history
                    (id, organization_id, appointment_id, appointment_revision,
                     action, old_status, new_status, old_service_date, new_service_date,
                     old_number, new_number, actor_staff_id, actor_role,
                     reason, price_snapshot_appointment_id)
                SELECT :history, organization_id, id, :revision,
                       'STATUS', 'In Examination', 'Completed', service_date, service_date,
                       current_number, current_number, :doctor, 'DOCTOR_OWNER',
                       'Examination completed', id
                FROM public.appointments
                WHERE organization_id=:tenant AND id=:appointment
                RETURNING occurred_at
            """),
                {
                    "history": uuid4(),
                    "revision": revision,
                    "doctor": doctor,
                    "tenant": clinic.tenant,
                    "appointment": appointment_id,
                },
            )
            visit = connection.scalar(
                text("""
                SELECT id FROM public.visits
                WHERE organization_id=:tenant AND appointment_id=:appointment
            """),
                {"tenant": clinic.tenant, "appointment": appointment_id},
            )
            assert visit is not None and completed_at is not None
            attestation_id, audit_id = uuid4(), uuid4()
            connection.execute(
                text("""
                INSERT INTO public.sensitive_audit
                    (id, organization_id, actor_staff_id, actor_role,
                     action, reason, correlation_id, before_state, after_state, occurred_at)
                VALUES (:audit, :tenant, :doctor, 'DOCTOR_OWNER',
                        'VISIT_EXAMINATION_COMPLETED', 'Examination completion',
                        :correlation,
                        jsonb_build_object('appointment_id', CAST(:appointment AS uuid),
                            'visit_id', CAST(:visit AS uuid),
                            'appointment_revision', 2, 'status', 'In Examination'),
                        jsonb_build_object('appointment_id', CAST(:appointment AS uuid),
                            'visit_id', CAST(:visit AS uuid),
                            'appointment_revision', 3, 'status', 'Completed',
                            'examination_occurred', true), :at)
            """),
                {
                    "audit": audit_id,
                    "tenant": clinic.tenant,
                    "doctor": doctor,
                    "correlation": str(attestation_id),
                    "appointment": appointment_id,
                    "visit": visit,
                    "at": completed_at,
                },
            )
            connection.execute(
                text("""
                INSERT INTO public.completion_attestations
                    (id, organization_id, appointment_id, visit_id,
                     completion_history_revision, examination_occurred,
                     actor_staff_id, actor_role, completed_at, sensitive_audit_id)
                VALUES (:attestation, :tenant, :appointment, :visit, 3, true,
                        :doctor, 'DOCTOR_OWNER', :at, :audit)
            """),
                {
                    "attestation": attestation_id,
                    "tenant": clinic.tenant,
                    "appointment": appointment_id,
                    "visit": visit,
                    "doctor": doctor,
                    "at": completed_at,
                    "audit": audit_id,
                },
            )
        else:
            revision = connection.scalar(
                text("""
                UPDATE public.appointments
                SET status='Cancelled', revision=revision+1
                WHERE organization_id=:tenant AND id=:appointment AND status='Booked'
                RETURNING revision
            """),
                {"tenant": clinic.tenant, "appointment": appointment_id},
            )
            assert revision == 1
            connection.execute(
                text("""
                INSERT INTO public.appointment_history
                    (id, organization_id, appointment_id, appointment_revision,
                     action, old_status, new_status, old_service_date, new_service_date,
                     old_number, new_number, actor_staff_id, actor_role,
                     reason, price_snapshot_appointment_id)
                SELECT :history, organization_id, id, :revision,
                       'STATUS', 'Booked', 'Cancelled', service_date, service_date,
                       current_number, current_number, :actor, 'DOCTOR_OWNER',
                       'Synthetic B-10 cancellation', id
                FROM public.appointments
                WHERE organization_id=:tenant AND id=:appointment
            """),
                {
                    "history": uuid4(),
                    "revision": revision,
                    "actor": doctor,
                    "tenant": clinic.tenant,
                    "appointment": appointment_id,
                },
            )
        terminal = connection.execute(
            text("""
            SELECT a.revision, h.occurred_at, h.actor_staff_id, h.new_status
            FROM public.appointments a
            JOIN public.appointment_history h
              ON (h.organization_id,h.appointment_id,h.appointment_revision)
               = (a.organization_id,a.id,a.revision)
            WHERE a.organization_id=:tenant AND a.id=:appointment
              AND a.status=:status AND h.new_status=:status
        """),
            {
                "tenant": clinic.tenant,
                "appointment": appointment_id,
                "status": status,
            },
        ).one()
        assert terminal.actor_staff_id == doctor
        claim = connection.scalar(
            text("""
            UPDATE public.followup_claims
            SET state=:claim_state, revision=revision+1,
                terminal_history_revision=:revision, terminal_at=:occurred_at
            WHERE organization_id=:tenant AND appointment_id=:appointment
              AND state='ACTIVE'
            RETURNING id
        """),
            {
                "tenant": clinic.tenant,
                "appointment": appointment_id,
                "claim_state": "CONSUMED" if status == "Completed" else "RELEASED",
                "revision": terminal.revision,
                "occurred_at": terminal.occurred_at,
            },
        )
        assert claim is not None
        connection.execute(text("SET CONSTRAINTS ALL IMMEDIATE"))
    return claim


@pytest.mark.parametrize("classification", ["ROUTINE", "EMERGENCY", "URGENT"])
@pytest.mark.parametrize("offset", [0, 7])
def test_completed_source_books_preferential_on_day_zero_and_day_seven(
    followup_clinic, classification, offset
):
    clinic = followup_clinic
    source = completed_source(clinic, classification=classification)
    status, response = booking_request(
        clinic,
        clinic.receptionist,
        body=followup_body(source, offset=offset),
        key=str(uuid4()),
    )
    assert status == 201, response
    booked = response["data"]
    assert (booked["service_type"], booked["classification"], booked["net_egp"]) == (
        "FOLLOW_UP",
        "ROUTINE",
        "30.00",
    )
    claim = claim_record(clinic, booked["id"])
    assert claim is not None
    assert (
        claim["state"],
        claim["source_visit_id"],
        claim["source_appointment_id"],
    ) == ("ACTIVE", source["visit"], source["appointment"])
    assert (claim["completion_local_date"], claim["approved_deadline"]) == (
        source["day"],
        source["day"] + timedelta(days=7),
    )
    assert (claim["base_egp"], claim["net_egp"]) == (Decimal("30.00"), Decimal("30.00"))
    assert claim["policy_snapshot"] == claim["entitlement_provenance"]
    evidence = claim["policy_snapshot"]
    assert evidence["schema_version"] == 2
    assert evidence["source_visit_id"] == str(source["visit"])
    assert evidence["source_decision_revision"] == 0
    assert evidence["source_clinical_revision"] == 0
    assert evidence["doctor_staff_id"] == clinic.owner.staff_id
    assert evidence["actor_staff_id"] == clinic.receptionist.staff_id
    assert evidence["actor_role"] == "RECEPTIONIST"
    assert evidence["authorized"] is True
    assert evidence["policy_revision"] == 1


@pytest.mark.parametrize("offset", [8, 366, 3650])
def test_expired_source_requires_acknowledged_new_examination(followup_clinic, offset):
    clinic = followup_clinic
    source = completed_source(clinic)
    body = followup_body(
        source,
        offset=offset,
        base="100.00",
        fallback_acknowledgment={
            "service_type": "NEW_EXAMINATION",
            "discount_reaffirmed": True,
        },
    )
    status, response = booking_request(
        clinic, clinic.owner, body=body, key=str(uuid4())
    )
    assert status == 201, response
    booked = response["data"]
    assert (booked["service_type"], booked["base_egp"], booked["net_egp"]) == (
        "NEW_EXAMINATION",
        "100.00",
        "100.00",
    )
    assert claim_record(clinic, booked["id"]) is None


def test_original_completed_same_day_still_occupies_counted_patient_day(
    followup_clinic,
):
    clinic = followup_clinic
    source = same_day_completed_source(clinic)
    before = booking_effects(clinic)
    status, response = booking_request(
        clinic, clinic.owner, body=followup_body(source), key=str(uuid4())
    )
    assert status == 409, response
    assert booking_effects(clinic) == before


@pytest.mark.parametrize("offset", [8, 366, 3650])
def test_justified_doctor_deadline_has_no_maximum_horizon(followup_clinic, offset):
    clinic = followup_clinic
    source = completed_source(clinic)
    doctor_decision(
        clinic,
        source["visit"],
        {
            "kind": "DEADLINE",
            "reason": "Specific clinical need",
            "deadline_date": (source["day"] + timedelta(days=offset)).isoformat(),
        },
    )
    status, response = booking_request(
        clinic,
        clinic.owner,
        body=followup_body(source, offset=offset, source_revision=1),
        key=str(uuid4()),
    )
    assert status == 201, response
    claim = claim_record(clinic, response["data"]["id"])
    assert claim["approved_deadline"] == source["day"] + timedelta(days=offset)
    assert claim["policy_snapshot"]["source_decision_revision"] == 1


@pytest.mark.parametrize(
    ("staff_kind", "reason"),
    [("receptionist", None), ("owner", "Reviewed necessity after default deadline")],
)
def test_signed_new_date_adds_exact_authorization_evidence(
    followup_clinic, staff_kind, reason
):
    clinic = followup_clinic
    source = completed_source(clinic)
    actor = getattr(clinic, staff_kind)
    beyond = followup_body(source, offset=8)
    before = booking_effects(clinic)
    denied, refused = booking_request(clinic, actor, body=beyond, key=str(uuid4()))
    assert denied == 409, refused
    assert booking_effects(clinic) == before
    declaration = {"selected_service_date": beyond["service_date"]}
    if reason is not None:
        declaration["reason"] = reason
    status, response = booking_request(
        clinic,
        actor,
        body={**beyond, "new_booking_date_exception": declaration},
        key=str(uuid4()),
    )
    assert status == 201, response
    assert response["data"]["service_type"] == "FOLLOW_UP"
    claim = claim_record(clinic, response["data"]["id"])
    assert claim["state"] == "ACTIVE"
    assert claim["approved_deadline"] == source["day"] + timedelta(days=7)
    evidence = claim["policy_snapshot"]
    assert evidence["schema_version"] == 2
    assert evidence["new_booking_date_exception"] == declaration
    assert evidence["source_decision_revision"] == 0
    with clinic.migration.connect() as connection:
        event = connection.execute(
            text("""
            SELECT e.id, e.kind, e.authorization_revision, e.prior_deadline,
                   e.new_deadline, e.selected_date, e.actor_staff_id, e.actor_role,
                   e.reason, a.correlation_id, a.after_state
            FROM public.followup_authorization_events e
            JOIN public.followup_claims c ON (c.organization_id,c.id)
                                         = (e.organization_id,e.claim_id)
            JOIN public.sensitive_audit a ON (a.organization_id,a.id)
                                         = (e.organization_id,e.sensitive_audit_id)
            WHERE c.organization_id=:tenant AND c.appointment_id=:appointment
        """),
            {
                "tenant": clinic.tenant,
                "appointment": UUID(response["data"]["id"]),
            },
        ).one()
    assert (
        event.kind,
        event.authorization_revision,
        event.prior_deadline,
        event.new_deadline,
        event.selected_date,
    ) == (
        "NEW_DATE",
        1,
        source["day"] + timedelta(days=7),
        source["day"] + timedelta(days=8),
        source["day"] + timedelta(days=8),
    )
    assert event.actor_staff_id == UUID(actor.staff_id)
    assert event.actor_role == (
        "DOCTOR_OWNER" if staff_kind == "owner" else "RECEPTIONIST"
    )
    assert event.reason == reason
    assert evidence["new_date_event_id"] == str(event.id)
    assert event.correlation_id == str(event.id)
    assert event.after_state["id"] == str(event.id)


@pytest.mark.parametrize(
    ("case", "expected"),
    [
        ("doctor_without_reason", 422),
        ("unnecessary_in_window", 422),
        ("doctor_opt_out", 409),
    ],
)
def test_signed_new_date_rejects_missing_reason_unneeded_or_restricted_exception(
    followup_clinic, case, expected
):
    clinic = followup_clinic
    source = completed_source(clinic)
    if case == "doctor_opt_out":
        doctor_decision(
            clinic,
            source["visit"],
            {"kind": "OPTED_OUT", "reason": "Doctor forbids repeat"},
        )
    offset = 2 if case == "unnecessary_in_window" else 8
    body = followup_body(
        source,
        offset=offset,
        source_revision=1 if case == "doctor_opt_out" else 0,
        new_booking_date_exception={
            "selected_service_date": (
                source["day"] + timedelta(days=offset)
            ).isoformat(),
            **({"reason": "Reviewed need"} if case != "doctor_without_reason" else {}),
        },
    )
    before = booking_effects(clinic)
    denied, refusal = booking_request(clinic, clinic.owner, body=body, key=str(uuid4()))
    assert denied == expected, refusal
    assert booking_effects(clinic) == before
    with transaction(clinic.runtime, clinic.tenant) as connection:
        assert (
            connection.scalar(
                text("""
            SELECT count(*) FROM public.followup_claims
            WHERE organization_id=:tenant AND source_visit_id=:visit
        """),
                {"tenant": clinic.tenant, "visit": source["visit"]},
            )
            == 0
        )


@pytest.mark.parametrize("window", [0, 4, 10])
def test_current_nonseven_day_followup_window_rebooks_released_claim(
    followup_clinic, window
):
    clinic = followup_clinic
    source = (
        same_day_completed_source(clinic) if window == 0 else completed_source(clinic)
    )
    status, response = pricing_request(
        clinic,
        clinic.owner,
        method="PATCH",
        body={
            **_patch(expected_price_revision=3, followup_last_day_inclusive=window),
            "expected_followup_revision": 1,
        },
        key=str(uuid4()),
    )
    assert status == 200, response
    assert response["data"]["followup_revision"] == 2
    assert response["data"]["followup_last_day_inclusive"] == window
    inside = followup_body(source, offset=window, expected_followup_revision=2)
    outside = followup_body(source, offset=window + 1, expected_followup_revision=2)
    before = booking_effects(clinic)
    denied, refused = booking_request(
        clinic, clinic.owner, body=outside, key=str(uuid4())
    )
    assert denied == 409, refused
    assert booking_effects(clinic) == before
    if window == 0:
        status, detail = booking_request(
            clinic,
            clinic.owner,
            method="GET",
            path=f"/v1/appointments/{source['appointment']}",
            query={"intended_service_date": source["day"].isoformat()},
        )
        assert status == 200, detail
        qualification = detail["data"]["followup_qualification"]
        assert qualification["followup_revision"] == 2
        assert qualification["actual_service_type"] == "FOLLOW_UP"
        status, denial = booking_request(
            clinic, clinic.owner, body=inside, key=str(uuid4())
        )
        assert status == 409, denial
        assert booking_effects(clinic) == before
        return
    first_status, first = booking_request(
        clinic, clinic.owner, body=inside, key=str(uuid4())
    )
    assert first_status == 201, first
    first_claim = claim_record(clinic, first["data"]["id"])
    assert first_claim["approved_deadline"] == source["day"] + timedelta(days=window)
    assert first_claim["policy_snapshot"]["policy_revision"] == 2
    terminal_claim(clinic, first["data"], status="Cancelled")
    second_status, second = booking_request(
        clinic, clinic.owner, body=inside, key=str(uuid4())
    )
    assert second_status == 201, second
    assert claim_record(clinic, first["data"]["id"])["state"] == "RELEASED"
    second_claim = claim_record(clinic, second["data"]["id"])
    assert second_claim["state"] == "ACTIVE"
    assert second_claim["approved_deadline"] == source["day"] + timedelta(days=window)
    assert second_claim["policy_snapshot"]["policy_revision"] == 2


def test_released_claim_requalifies_at_current_price(followup_clinic):
    clinic = followup_clinic
    source = completed_source(clinic)
    first_status, first = booking_request(
        clinic, clinic.owner, body=followup_body(source, offset=1), key=str(uuid4())
    )
    assert first_status == 201, first
    first_id = first["data"]["id"]
    terminal_claim(clinic, first["data"], status="Cancelled")
    status, response = pricing_request(
        clinic,
        clinic.owner,
        method="PATCH",
        body=_patch(expected_price_revision=3, preferential_followup_egp="55.00"),
        key=str(uuid4()),
    )
    assert status == 200, response
    second_body = followup_body(
        source, offset=2, base="55.00", expected_price_revision=4
    )
    second_status, second = booking_request(
        clinic, clinic.owner, body=second_body, key=str(uuid4())
    )
    assert second_status == 201, second
    assert claim_record(clinic, first_id)["state"] == "RELEASED"
    assert claim_record(clinic, first_id)["net_egp"] == Decimal("30.00")
    assert claim_record(clinic, second["data"]["id"])["state"] == "ACTIVE"
    assert claim_record(clinic, second["data"]["id"])["net_egp"] == Decimal("55.00")


def test_confirmed_claim_is_frozen_after_price_policy_and_decision_changes(
    followup_clinic,
):
    clinic = followup_clinic
    source = completed_source(clinic)
    status, response = booking_request(
        clinic, clinic.owner, body=followup_body(source, offset=7), key=str(uuid4())
    )
    assert status == 201, response
    booked_id = response["data"]["id"]
    frozen = dict(claim_record(clinic, booked_id))
    status, response = pricing_request(
        clinic,
        clinic.owner,
        method="PATCH",
        body=_patch(
            expected_price_revision=3,
            expected_followup_revision=1,
            preferential_followup_egp="55.00",
            followup_last_day_inclusive=0,
        ),
        key=str(uuid4()),
    )
    assert status == 200, response
    doctor_decision(
        clinic,
        source["visit"],
        {"kind": "OPTED_OUT", "reason": "New clinical assessment"},
    )
    assert dict(claim_record(clinic, booked_id)) == frozen


def test_preferential_receipt_retries_recover_after_changes_and_deny_revocation(
    followup_clinic,
):
    clinic = followup_clinic
    source = completed_source(clinic)
    body, key = followup_body(source, offset=1), str(uuid4())
    status, first = booking_request(clinic, clinic.owner, body=body, key=key)
    assert status == 201, first
    status, pricing = pricing_request(
        clinic,
        clinic.owner,
        method="PATCH",
        body=_patch(expected_price_revision=3, preferential_followup_egp="55.00"),
        key=str(uuid4()),
    )
    assert status == 200, pricing
    doctor_decision(
        clinic,
        source["visit"],
        {"kind": "OPTED_OUT", "reason": "Fresh medical judgment"},
    )
    before = booking_effects(clinic)
    assert booking_request(clinic, clinic.owner, body=body, key=key) == (201, first)
    status, changed = booking_request(
        clinic, clinic.owner, body={**body, "expected_net_egp": "29.00"}, key=key
    )
    assert status == 409 and changed["error"]["code"] == "IDEMPOTENCY_CONFLICT"
    assert booking_request(clinic, clinic.owner, body=body, key=str(uuid4()))[0] == 409
    assert booking_effects(clinic) == before
    with clinic.migration.begin() as connection:
        connection.execute(
            text('DELETE FROM public."session" WHERE id=:session'),
            {"session": clinic.owner.session_id},
        )
    assert booking_request(clinic, clinic.owner, body=body, key=key)[0] in (401, 403)
    assert booking_effects(clinic) == before


def test_preferential_lost_response_recovered_by_original_key(followup_clinic):
    clinic = followup_clinic
    source = completed_source(clinic)
    body, key = followup_body(source, offset=2), str(uuid4())
    with transaction(clinic.runtime, clinic.tenant) as connection:
        connection.execute(
            text("SELECT set_config('app.staff_id', :actor, true)"),
            {"actor": clinic.owner.staff_id},
        )
        first = book_appointment(
            connection,
            _actor(clinic.tenant, UUID(clinic.owner.staff_id)),
            BookAppointment.model_validate(body),
            key,
        )
    before = booking_effects(clinic)
    status, resolved = booking_request(clinic, clinic.owner, body=body, key=key)
    assert status == 201, resolved
    assert resolved["data"]["id"] == first["id"]
    assert booking_effects(clinic) == before


def _seed_historical_booking_receipt(clinic, appointment, source, *, unsafe=False):
    """Add an independent committed v1 fixture without altering any saved v2 receipt."""
    body = followup_body(source, offset=2)
    body["expected_source_revision"] = body.pop("expected_visit_revision")
    body.pop("expected_source_decision_revision")
    key = str(uuid4())
    actor = _actor(clinic.tenant, UUID(clinic.owner.staff_id))
    with transaction(clinic.runtime, clinic.tenant) as connection:
        connection.execute(
            text("SELECT set_config('app.staff_id', :actor, true)"),
            {"actor": clinic.owner.staff_id},
        )
        projection = appointment_detail(connection, actor, UUID(appointment))
        assert len(projection["history"]) == 1
        assert projection["history"][0]["action"] == "CREATED"
        assert projection["history"][0]["reason"] is None
        assert projection["service_type"] == "FOLLOW_UP"
        if unsafe:
            projection["history"][0]["reason"] = "DOCTOR_PRIVATE_REASON"
        insert_tenant_receipt(
            connection,
            Receipt(
                actor_id=actor.staff_id,
                operation="appointments.book",
                idempotency_key=key,
                request_digest=normalized_request_digest(
                    BookAppointment.model_validate(body)
                ),
                result_projection=projection,
                result_revision=0,
                organization_id=actor.organization_id,
                staff_id=actor.staff_id,
            ),
        )
    return body, key, projection


def test_pre0029_preferential_v1_receipt_survives_upgrade_and_signed_replay(
    patient_backend, admin, empty_database
):
    migration, runtime, tenant, doctor = _prepare(admin, empty_database)
    try:
        _upgrade(migration, "0028_d08_followup_provenance")
        session = f"synthetic-b10-legacy-{uuid4()}"
        with migration.begin() as connection:
            connection.execute(
                text("""
                    INSERT INTO public."session"
                        (id, "userId", token, "expiresAt", "createdAt", "updatedAt",
                         "securityRevision", "absoluteDeadline", "lastActivity")
                    SELECT :session, s.auth_user_id, :token,
                           now() + interval '1 hour', now(), now(), 0,
                           now() + interval '1 hour', now()
                    FROM public.staff_accounts s
                    WHERE s.organization_id=:tenant AND s.id=:doctor
                """),
                {
                    "session": session,
                    "token": str(uuid4()),
                    "tenant": tenant,
                    "doctor": doctor,
                },
            )
        actor = replace(
            patient_backend.owner,
            organization_id=str(tenant),
            staff_id=str(doctor),
            session_id=session,
        )
        clinic = SimpleNamespace(
            tenant=tenant,
            runtime=runtime,
            migration=migration,
            owner=actor,
            config=patient_backend.config,
        )
        with transaction(runtime, tenant) as connection:
            source = _v2_source(connection, tenant, doctor)
            claim = _v2_command(
                connection, "create_followup_claim", source, **_v2_booking(source, 2)
            )
            appointment = connection.scalar(
                text(
                    "SELECT appointment_id FROM public.followup_claims WHERE id=:claim"
                ),
                {"claim": claim},
            )
            body = booking_body(
                source["patient"],
                day=source["day"] + timedelta(days=2),
                requested_service_type="FOLLOW_UP",
                source_visit_id=str(source["visit"]),
                expected_source_revision=0,
                expected_base_egp="100.00",
                expected_net_egp="100.00",
                expected_price_revision=1,
                expected_schedule_revision=1,
                expected_followup_revision=1,
            )
            command = BookAppointment.model_validate(body)
            assert command.source_family == "v1"
            saved = appointment_detail(connection, _actor(tenant, doctor), appointment)
            assert saved["service_type"] == "FOLLOW_UP"
            assert saved["history"][0]["reason"] is None
            key = str(uuid4())
            # The predecessor SQL writer persists the claim; the v1 API also wrote
            # its booking audit, outbox and D-09 receipt in this transaction.
            append_sensitive_audit(
                connection,
                {
                    "organization_id": tenant,
                    "actor_staff_id": doctor,
                    "actor_role": "DOCTOR_OWNER",
                    "action": "appointments.book",
                    "correlation_id": str(appointment),
                    "after_state": {"appointment_id": str(appointment), "revision": 0},
                },
            )
            append_outbox(
                connection,
                {
                    "organization_id": tenant,
                    "event_type": "APPOINTMENT_BOOKED",
                    "aggregate_type": "APPOINTMENT",
                    "aggregate_id": str(appointment),
                    "payload": {"appointment_id": str(appointment)},
                },
            )
            insert_tenant_receipt(
                connection,
                Receipt(
                    actor_id=doctor,
                    operation="appointments.book",
                    idempotency_key=key,
                    request_digest=normalized_request_digest(command),
                    result_projection=saved,
                    result_revision=0,
                    organization_id=tenant,
                    staff_id=doctor,
                ),
            )
        receipt_query = text("""
            SELECT request_digest, result_projection, result_revision
            FROM public.idempotency_receipts
            WHERE organization_id=:tenant AND operation='appointments.book'
              AND idempotency_key=:key
        """)
        receipt_params = {"tenant": tenant, "key": key}
        with migration.connect() as connection:
            original_receipt = connection.execute(receipt_query, receipt_params).one()
        assert original_receipt == (normalized_request_digest(command), saved, 0)
        _upgrade(migration, "0029_b10_followup_writers")
        with migration.connect() as connection:
            assert (
                connection.execute(receipt_query, receipt_params).one()
                == original_receipt
            )
        app.state.db_engine = runtime
        assert booking_request(clinic, actor, body=body, key=key) == (
            201,
            {"data": saved, "revision": 0},
        )
        conflict_status, conflict = booking_request(
            clinic, actor, body={**body, "expected_net_egp": "99.00"}, key=key
        )
        assert conflict_status == 409
        assert conflict["error"]["code"] == "IDEMPOTENCY_CONFLICT"
        assert booking_request(clinic, actor, body=body, key=str(uuid4()))[0] == 409
        with migration.begin() as connection:
            connection.execute(
                text('DELETE FROM public."session" WHERE id=:session'),
                {"session": session},
            )
        for candidate in (body, {**body, "expected_net_egp": "99.00"}):
            status, refusal = booking_request(clinic, actor, body=candidate, key=key)
            assert status in (401, 403), refusal
            assert "data" not in refusal
        with migration.connect() as connection:
            assert (
                connection.execute(receipt_query, receipt_params).one()
                == original_receipt
            )
            assert (
                connection.scalar(
                    text("""
                    SELECT count(*) FROM public.followup_claims
                    WHERE organization_id=:tenant AND source_visit_id=:visit
                """),
                    {"tenant": tenant, "visit": source["visit"]},
                )
                == 1
            )
    finally:
        app.state.db_engine = patient_backend.runtime
        runtime.dispose()
        migration.dispose()


@pytest.mark.parametrize("unsafe", [False, True])
def test_signed_v1_committed_booking_recovery_authorizes_full_history_before_digest(
    followup_clinic, unsafe
):
    clinic = followup_clinic
    source = completed_source(clinic)
    status, created = booking_request(
        clinic,
        clinic.owner,
        body=followup_body(source, offset=2),
        key=str(uuid4()),
    )
    assert status == 201, created
    legacy_body, key, persisted = _seed_historical_booking_receipt(
        clinic, created["data"]["id"], source, unsafe=unsafe
    )
    before = booking_effects(clinic)
    matching = booking_request(clinic, clinic.owner, body=legacy_body, key=key)
    conflicting = booking_request(
        clinic,
        clinic.owner,
        body={**legacy_body, "expected_net_egp": "29.00"},
        key=key,
    )
    if unsafe:
        assert matching[0] == conflicting[0] == 403
    else:
        assert matching == (201, {"data": persisted, "revision": 0})
        assert conflicting[0] == 409
        assert conflicting[1]["error"]["code"] == "IDEMPOTENCY_CONFLICT"
    assert booking_effects(clinic) == before
    with clinic.migration.connect() as connection:
        stored = connection.scalar(
            text("""
                SELECT result_projection FROM public.idempotency_receipts
                WHERE organization_id=:tenant AND idempotency_key=:key
                  AND operation='appointments.book'
            """),
            {"tenant": clinic.tenant, "key": key},
        )
        assert stored == persisted


def test_signed_duplicate_json_keys_and_fresh_v1_source_writer_are_rejected(
    followup_clinic,
):
    clinic = followup_clinic
    source = completed_source(clinic)
    valid = followup_body(source, offset=2)
    original = json.dumps(valid, separators=(",", ":"))
    duplicate = (
        original[:-1] + ',"service_date":"' + valid["service_date"] + '"}'
    ).encode()
    before = booking_effects(clinic)
    assert (
        booking_request(
            clinic,
            clinic.receptionist,
            raw_body=duplicate,
            key=str(uuid4()),
        )[0]
        == 422
    )
    original_v1 = {
        **valid,
        "expected_source_revision": valid["expected_visit_revision"],
    }
    del original_v1["expected_source_decision_revision"]
    del original_v1["expected_visit_revision"]
    status, _ = booking_request(
        clinic, clinic.receptionist, body=original_v1, key=str(uuid4())
    )
    assert status == 409
    assert booking_effects(clinic) == before

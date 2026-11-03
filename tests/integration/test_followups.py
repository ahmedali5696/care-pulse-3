"""B-10 real-PostgreSQL acceptance of preferential booking and receipts."""

import json
from datetime import datetime, timedelta
from decimal import Decimal
from uuid import UUID, uuid4
from zoneinfo import ZoneInfo

import pytest
from carepulse.appointments.book import book_appointment
from carepulse.appointments.booking_contracts import BookAppointment
from carepulse.appointments.persistence import persist_appointment
from carepulse.db import transaction
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
from test_followup_persistence import _outcome, _source
from test_patients import patient_backend as patient_backend  # noqa: PLC0414
from test_pricing import _patch, pricing_request
from test_request_boundary import boundary_data as boundary_data  # noqa: PLC0414
from test_schedule import _actor, _config, _publish


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
        return _source(
            connection,
            clinic.tenant,
            UUID(clinic.owner.staff_id),
            classification=classification,
        )


def followup_body(source, *, offset=0, base="30.00", source_revision=1, **changes):
    fields = {
        "requested_service_type": "FOLLOW_UP",
        "source_visit_id": str(source["visit"]),
        "expected_source_revision": source_revision,
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
        connection.execute(
            text(
                "SELECT set_config('app.clinical_change_kind', 'DECISION_EXCEPTION', true)"
            )
        )
        connection.execute(
            text(
                "SELECT set_config('app.clinical_reason', 'Synthetic Doctor review', true)"
            )
        )
        connection.execute(
            text("""
                UPDATE public.visits SET followup_decision=CAST(:decision AS jsonb)
                WHERE organization_id=:tenant AND id=:visit
            """),
            {
                "tenant": clinic.tenant,
                "visit": visit_id,
                "decision": json.dumps(decision),
            },
        )


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
    assert claim["policy_snapshot"] == {
        "source_visit_id": str(source["visit"]),
        "source_revision": 1,
        "doctor_staff_id": clinic.owner.staff_id,
        "actor_staff_id": clinic.receptionist.staff_id,
        "authorized": True,
        "policy_revision": 1,
    }


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
        visit_id = connection.scalar(
            text("SELECT id FROM public.visits WHERE appointment_id=:appointment"),
            {"appointment": UUID(saved["id"])},
        )
        connection.execute(
            text(
                "SELECT set_config('app.clinical_change_kind', 'DECISION_DEFAULT', true)"
            )
        )
        connection.execute(text("SELECT set_config('app.clinical_reason', '', true)"))
        connection.execute(
            text("""
                UPDATE public.visits SET followup_decision='{"kind":"DEFAULT"}'::jsonb
                WHERE organization_id=:tenant AND id=:visit
            """),
            {"tenant": clinic.tenant, "visit": visit_id},
        )
    source = {"patient": patient, "visit": visit_id, "day": today}
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
        body=followup_body(source, offset=offset, source_revision=2),
        key=str(uuid4()),
    )
    assert status == 201, response
    claim = claim_record(clinic, response["data"]["id"])
    assert claim["approved_deadline"] == source["day"] + timedelta(days=offset)
    assert claim["policy_snapshot"]["source_revision"] == 2


def test_released_claim_requalifies_at_current_price(followup_clinic):
    clinic = followup_clinic
    source = completed_source(clinic)
    first_status, first = booking_request(
        clinic, clinic.owner, body=followup_body(source, offset=1), key=str(uuid4())
    )
    assert first_status == 201, first
    first_id = first["data"]["id"]
    with transaction(clinic.runtime, clinic.tenant) as connection:
        claim_id = connection.scalar(
            text("""
                SELECT id FROM public.followup_claims
                WHERE organization_id=:tenant AND appointment_id=:appointment
            """),
            {"tenant": clinic.tenant, "appointment": UUID(first_id)},
        )
        _outcome(
            connection,
            clinic.tenant,
            UUID(clinic.owner.staff_id),
            {"id": UUID(first_id), "claim": claim_id, "saved": first["data"]},
            status="Cancelled",
        )
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

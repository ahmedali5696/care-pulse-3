"""B-10 refusal and privacy contracts on the signed live PostgreSQL boundary."""

import json
from datetime import timedelta
from uuid import UUID, uuid4

import pytest
from carepulse.db import transaction
from sqlalchemy import text
from test_booking import booking_body, booking_effects, booking_request, new_patient
from test_booking import booking_clinic as booking_clinic  # noqa: PLC0414
from test_followup_persistence import _source
from test_followups import (
    _seed_historical_booking_receipt,
    completed_source,
    doctor_decision,
    followup_body,
    terminal_claim,
)
from test_followups import followup_clinic as followup_clinic  # noqa: PLC0414
from test_patients import patient_backend as patient_backend  # noqa: PLC0414
from test_request_boundary import boundary_data as boundary_data  # noqa: PLC0414
from test_schedule import SERVICE_DAY


def _completed_source(clinic):
    return completed_source(clinic)


def _reserve(clinic):
    source = _completed_source(clinic)
    status, result = booking_request(
        clinic, clinic.owner, body=followup_body(source, offset=1), key=str(uuid4())
    )
    assert status == 201, result
    assert result["data"]["service_type"] == "FOLLOW_UP"
    return source, result["data"]


def _followup_body(source, **changes):
    request = booking_body(
        source["patient"],
        requested_service_type="FOLLOW_UP",
        source_visit_id=str(source["visit"]),
        expected_source_decision_revision=0,
        expected_visit_revision=0,
        fallback_acknowledgment={
            "service_type": "NEW_EXAMINATION",
            "discount_reaffirmed": True,
        },
    )
    return {**request, **changes}


def test_no_reviewed_source_requires_explicit_ordinary_type_and_discount(
    booking_clinic,
):
    clinic = booking_clinic
    patient = new_patient(clinic)
    body = booking_body(
        patient,
        requested_service_type="FOLLOW_UP",
    )
    before = booking_effects(clinic)
    assert (
        booking_request(clinic, clinic.receptionist, body=body, key=str(uuid4()))[0]
        == 409
    )
    assert booking_effects(clinic) == before

    body["fallback_acknowledgment"] = {
        "service_type": "NEW_EXAMINATION",
        "discount_reaffirmed": True,
    }
    status, result = booking_request(
        clinic, clinic.receptionist, body=body, key=str(uuid4())
    )
    assert status == 201, result
    assert result["data"]["service_type"] == "NEW_EXAMINATION"
    assert result["data"]["net_egp"] == "100.00"


@pytest.mark.parametrize(
    "extra,expected",
    [
        ({"source_visit_id": None, "expected_source_revision": 1}, 422),
        ({"expected_source_revision": None}, 422),
        ({"fallback_acknowledgment": None}, 409),
        (
            {
                "fallback_acknowledgment": {
                    "service_type": "FOLLOW_UP",
                    "discount_reaffirmed": True,
                }
            },
            422,
        ),
        (
            {
                "fallback_acknowledgment": {
                    "service_type": "NEW_EXAMINATION",
                    "discount_reaffirmed": False,
                }
            },
            422,
        ),
        (
            {
                "fallback_acknowledgment": {
                    "service_type": "NEW_EXAMINATION",
                    "discount_reaffirmed": 1,
                }
            },
            422,
        ),
        (
            {
                "fallback_acknowledgment": {
                    "service_type": "NEW_EXAMINATION",
                    "discount_reaffirmed": 1.0,
                }
            },
            422,
        ),
        ({"expected_price_revision": 0}, 409),
        ({"expected_schedule_revision": 0}, 409),
    ],
)
def test_nonqualifying_followup_invalid_confirmation_leaves_no_booking(
    booking_clinic, extra, expected
):
    clinic = booking_clinic
    source = _completed_source(clinic)
    before = booking_effects(clinic)
    status, _ = booking_request(
        clinic,
        clinic.receptionist,
        body=_followup_body(source, **extra),
        key=str(uuid4()),
    )
    assert status == expected
    assert booking_effects(clinic) == before


def test_null_foreign_family_keys_deny_before_committed_receipt_replay(followup_clinic):
    clinic = followup_clinic
    source = completed_source(clinic)
    v2_body = followup_body(source, offset=2)
    v2_key = str(uuid4())
    status, created = booking_request(clinic, clinic.owner, body=v2_body, key=v2_key)
    assert status == 201, created
    legacy_body, legacy_key, projection = _seed_historical_booking_receipt(
        clinic, created["data"]["id"], source
    )
    before = booking_effects(clinic)
    for foreign_key in (
        "expected_source_decision_revision",
        "expected_visit_revision",
        "new_booking_date_exception",
    ):
        denied, response = booking_request(
            clinic,
            clinic.owner,
            body={**legacy_body, foreign_key: None},
            key=legacy_key,
        )
        assert denied == 422, response
        assert booking_effects(clinic) == before
    denied, response = booking_request(
        clinic,
        clinic.owner,
        body={**v2_body, "expected_source_revision": None},
        key=v2_key,
    )
    assert denied == 422, response
    assert booking_effects(clinic) == before
    assert booking_request(clinic, clinic.owner, body=legacy_body, key=legacy_key) == (
        201,
        {"data": projection, "revision": 0},
    )
    assert booking_effects(clinic) == before


def test_referenced_source_revision_is_checked_even_on_acknowledged_fallback(
    booking_clinic,
):
    clinic = booking_clinic
    source = _completed_source(clinic)
    doctor_decision(
        clinic, source["visit"], {"kind": "OPTED_OUT", "reason": "Changed eligibility"}
    )
    before = booking_effects(clinic)
    status, response = booking_request(
        clinic, clinic.receptionist, body=_followup_body(source), key=str(uuid4())
    )
    assert status == 409
    assert response["error"]["code"] == "STALE_REVISION"
    assert booking_effects(clinic) == before


def test_explicit_missing_or_foreign_source_cannot_be_recast_as_fallback(
    booking_clinic,
):
    clinic = booking_clinic
    source = _completed_source(clinic)
    before = booking_effects(clinic)
    for missing in [uuid4(), source["visit"]]:
        patient = (
            new_patient(clinic) if missing == source["visit"] else source["patient"]
        )
        body = _followup_body(source)
        body["patient_id"] = str(patient)
        body["source_visit_id"] = str(missing)
        status, _ = booking_request(
            clinic, clinic.receptionist, body=body, key=str(uuid4())
        )
        assert status == 404
    assert booking_effects(clinic) == before


def test_active_source_never_falls_back_while_reserved(followup_clinic):
    clinic = followup_clinic
    source, _ = _reserve(clinic)
    before = booking_effects(clinic)
    status, _ = booking_request(
        clinic,
        clinic.receptionist,
        body=followup_body(
            source,
            offset=2,
            base="100.00",
            fallback_acknowledgment={
                "service_type": "NEW_EXAMINATION",
                "discount_reaffirmed": True,
            },
        ),
        key=str(uuid4()),
    )
    assert status == 409
    assert booking_effects(clinic) == before


def test_active_claim_stays_busy_after_doctor_changes_source_decision(followup_clinic):
    clinic = followup_clinic
    source, _ = _reserve(clinic)
    doctor_decision(
        clinic,
        source["visit"],
        {"kind": "OPTED_OUT", "reason": "Changed recommendation"},
    )
    before = booking_effects(clinic)
    status, _ = booking_request(
        clinic,
        clinic.receptionist,
        method="GET",
        path=f"/v1/appointments/{source['appointment']}",
        query={"intended_service_date": SERVICE_DAY.isoformat()},
    )
    assert status == 409
    assert booking_effects(clinic) == before


def test_receptionist_get_qualification_never_serializes_doctor_decision(
    booking_clinic,
):
    clinic = booking_clinic
    source = _completed_source(clinic)
    doctor_decision(
        clinic,
        source["visit"],
        {
            "kind": "DEADLINE",
            "deadline_date": (source["day"] + timedelta(days=3)).isoformat(),
            "reason": "DOCTOR_PRIVATE_REASON",
        },
    )
    status, detail = booking_request(
        clinic,
        clinic.receptionist,
        method="GET",
        path=f"/v1/appointments/{source['appointment']}",
        query={"intended_service_date": SERVICE_DAY.isoformat()},
    )
    assert status == 200, detail
    qualification = detail["data"]["followup_qualification"]
    assert qualification["requested_service_type"] == "FOLLOW_UP"
    assert qualification["actual_service_type"] == "NEW_EXAMINATION"
    assert qualification["source_visit_id"] == str(source["visit"])
    assert qualification["source_revision"] == 0
    assert qualification["source_decision_revision"] == 1
    assert qualification["decision_control"]["kind"] == "DEADLINE"
    assert qualification["decision_control"]["actor_role"] == "DOCTOR_OWNER"
    assert qualification["decision_control"]["can_revise"] is False
    assert qualification["price_quote"]["base_egp"] == "100.00"
    serialized = json.dumps(detail, ensure_ascii=False)
    for forbidden in (
        "DOCTOR_PRIVATE_REASON",
        "followup_decision",
        "clinical_revisions",
        "entitlement_provenance",
    ):
        assert forbidden not in serialized


def test_archived_patient_cannot_use_acknowledged_fallback(booking_clinic):
    clinic = booking_clinic
    patient = new_patient(clinic)
    with transaction(clinic.runtime, clinic.tenant) as connection:
        connection.execute(
            text(
                "UPDATE public.patients SET archived=true, lifecycle_revision=lifecycle_revision+1 WHERE id=:patient"
            ),
            {"patient": patient},
        )
    before = booking_effects(clinic)
    status, _ = booking_request(
        clinic,
        clinic.owner,
        body=booking_body(
            patient,
            requested_service_type="FOLLOW_UP",
            fallback_acknowledgment={
                "service_type": "NEW_EXAMINATION",
                "discount_reaffirmed": True,
            },
        ),
        key=str(uuid4()),
    )
    assert status == 409
    assert booking_effects(clinic) == before


def _doctor_extend_to_booking_day(clinic, visit_id):
    """Explicit Doctor operational deadline, distinct from Visit clinical content."""
    return doctor_decision(
        clinic,
        visit_id,
        {
            "kind": "DEADLINE",
            "deadline_date": SERVICE_DAY.isoformat(),
            "reason": "Reviewed future visit",
        },
    )


def test_consumed_source_cannot_book_second_preferential_followup(followup_clinic):
    clinic = followup_clinic
    source, first = _reserve(clinic)
    terminal_claim(clinic, first, status="Completed")
    revision = _doctor_extend_to_booking_day(clinic, source["visit"])
    assert revision == 1
    body = _followup_body(
        source,
        expected_source_decision_revision=revision,
        expected_price_revision=3,
        expected_schedule_revision=3,
    )
    before = booking_effects(clinic)
    denied, response = booking_request(
        clinic,
        clinic.receptionist,
        body={**body, "fallback_acknowledgment": None},
        key=str(uuid4()),
    )
    assert denied == 409, response
    assert booking_effects(clinic) == before
    status, response = booking_request(
        clinic, clinic.receptionist, body=body, key=str(uuid4())
    )
    assert status == 201, response
    assert response["data"]["service_type"] == "NEW_EXAMINATION"
    assert response["data"]["net_egp"] == "100.00"
    with transaction(clinic.runtime, clinic.tenant) as connection:
        assert connection.execute(
            text("""
                SELECT state, count(*) FROM public.followup_claims
                WHERE organization_id=:tenant AND source_visit_id=:visit
                GROUP BY state
            """),
            {"tenant": clinic.tenant, "visit": source["visit"]},
        ).all() == [("CONSUMED", 1)]


def test_completed_followup_visit_cannot_be_recursive_preferential_source(
    followup_clinic,
):
    clinic = followup_clinic
    original, first = _reserve(clinic)
    terminal_claim(clinic, first, status="Completed")
    with transaction(clinic.runtime, clinic.tenant) as connection:
        recursive_visit = connection.scalar(
            text("""
                SELECT id FROM public.visits
                WHERE organization_id=:tenant AND appointment_id=:appointment
            """),
            {"tenant": clinic.tenant, "appointment": UUID(first["id"])},
        )
        assert recursive_visit is not None
    candidate = {**original, "visit": recursive_visit}
    body = _followup_body(
        candidate, expected_price_revision=3, expected_schedule_revision=3
    )
    before = booking_effects(clinic)
    denied, response = booking_request(
        clinic,
        clinic.receptionist,
        body={**body, "fallback_acknowledgment": None},
        key=str(uuid4()),
    )
    assert denied == 409, response
    assert booking_effects(clinic) == before
    status, response = booking_request(
        clinic, clinic.receptionist, body=body, key=str(uuid4())
    )
    assert status == 201, response
    assert response["data"]["service_type"] == "NEW_EXAMINATION"
    with transaction(clinic.runtime, clinic.tenant) as connection:
        assert (
            connection.scalar(
                text("""
                SELECT count(*) FROM public.followup_claims
                WHERE organization_id=:tenant AND source_visit_id=:visit
            """),
                {"tenant": clinic.tenant, "visit": recursive_visit},
            )
            == 0
        )


@pytest.mark.parametrize(
    ("discount_type", "discount_value", "net"),
    [("FIXED", "12.50", "87.50"), ("PERCENT", "12.345", "87.66")],
)
def test_acknowledged_ordinary_fallback_reaffirms_discretionary_discount(
    booking_clinic, discount_type, discount_value, net
):
    clinic = booking_clinic
    patient = new_patient(clinic)
    body = booking_body(
        patient,
        requested_service_type="FOLLOW_UP",
        discount={
            "discount_type": discount_type,
            "discount_value": discount_value,
            "discount_reason": "Synthetic authorized discretionary discount",
        },
        expected_net_egp=net,
        fallback_acknowledgment={
            "service_type": "NEW_EXAMINATION",
            "discount_reaffirmed": True,
        },
    )
    before = booking_effects(clinic)
    denied, response = booking_request(
        clinic,
        clinic.receptionist,
        body={**body, "fallback_acknowledgment": None},
        key=str(uuid4()),
    )
    assert denied == 409, response
    assert booking_effects(clinic) == before
    status, response = booking_request(
        clinic, clinic.receptionist, body=body, key=str(uuid4())
    )
    assert status == 201, response
    booked = response["data"]
    assert (
        booked["service_type"],
        booked["base_egp"],
        booked["net_egp"],
        booked["discount_type"],
        booked["discount_value"],
    ) == ("NEW_EXAMINATION", "100.00", net, discount_type, discount_value)
    with transaction(clinic.runtime, clinic.tenant) as connection:
        assert (
            connection.scalar(
                text("""
                SELECT count(*) FROM public.followup_claims
                WHERE organization_id=:tenant AND appointment_id=:appointment
            """),
                {"tenant": clinic.tenant, "appointment": UUID(booked["id"])},
            )
            == 0
        )


def test_in_examination_source_requires_acknowledgment_to_book_ordinary(
    booking_clinic,
):
    clinic = booking_clinic
    with transaction(clinic.runtime, clinic.tenant) as connection:
        source = _source(
            connection, clinic.tenant, UUID(clinic.owner.staff_id), complete=False
        )
        assert (
            connection.scalar(
                text("""
                SELECT status FROM public.appointments
                WHERE organization_id=:tenant AND id=:appointment
            """),
                {"tenant": clinic.tenant, "appointment": source["appointment"]},
            )
            == "In Examination"
        )
    body = _followup_body(source)
    before = booking_effects(clinic)
    denied, response = booking_request(
        clinic,
        clinic.receptionist,
        body={**body, "fallback_acknowledgment": None},
        key=str(uuid4()),
    )
    assert denied == 409, response
    assert booking_effects(clinic) == before

    status, response = booking_request(
        clinic, clinic.receptionist, body=body, key=str(uuid4())
    )
    assert status == 201, response
    assert response["data"]["service_type"] == "NEW_EXAMINATION"
    assert response["data"]["net_egp"] == "100.00"
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


def test_acknowledged_fallback_still_denied_on_closed_day_and_counted_day(
    booking_clinic,
):
    clinic = booking_clinic
    patient = new_patient(clinic)
    fallback = booking_body(
        patient,
        requested_service_type="FOLLOW_UP",
        fallback_acknowledgment={
            "service_type": "NEW_EXAMINATION",
            "discount_reaffirmed": True,
        },
    )
    # booking_clinic publishes OPEN mode only for SERVICE_DAY's weekday.
    closed_day = SERVICE_DAY + timedelta(days=1)
    assert closed_day.isoweekday() != SERVICE_DAY.isoweekday()
    before = booking_effects(clinic)
    status, response = booking_request(
        clinic,
        clinic.receptionist,
        body={**fallback, "service_date": closed_day.isoformat()},
        key=str(uuid4()),
    )
    assert status == 409, response
    assert booking_effects(clinic) == before

    status, response = booking_request(
        clinic,
        clinic.receptionist,
        body=booking_body(patient),
        key=str(uuid4()),
    )
    assert status == 201, response
    before = booking_effects(clinic)
    status, response = booking_request(
        clinic, clinic.receptionist, body=fallback, key=str(uuid4())
    )
    assert status == 409, response
    assert booking_effects(clinic) == before

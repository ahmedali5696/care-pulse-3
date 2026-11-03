"""B-10 refusal and privacy contracts on the signed live PostgreSQL boundary."""

import json
from datetime import timedelta
from uuid import UUID, uuid4

import pytest
from carepulse.db import transaction
from sqlalchemy import text
from test_booking import booking_body, booking_effects, booking_request, new_patient
from test_booking import booking_clinic as booking_clinic  # noqa: PLC0414
from test_followup_persistence import _claim, _source
from test_patients import patient_backend as patient_backend  # noqa: PLC0414
from test_request_boundary import boundary_data as boundary_data  # noqa: PLC0414
from test_schedule import SERVICE_DAY


def _completed_source(clinic):
    with transaction(clinic.runtime, clinic.tenant) as connection:
        return _source(connection, clinic.tenant, UUID(clinic.owner.staff_id))


def _followup_body(source, **changes):
    request = booking_body(
        source["patient"],
        requested_service_type="FOLLOW_UP",
        source_visit_id=str(source["visit"]),
        expected_source_revision=1,
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


def test_referenced_source_revision_is_checked_even_on_acknowledged_fallback(
    booking_clinic,
):
    clinic = booking_clinic
    source = _completed_source(clinic)
    with transaction(clinic.runtime, clinic.tenant) as connection:
        connection.execute(
            text(
                "SELECT set_config('app.staff_id', :doctor, true), "
                "set_config('app.clinical_change_kind', 'DECISION_EXCEPTION', true), "
                "set_config('app.clinical_reason', 'Changed eligibility', true)"
            ),
            {"doctor": clinic.owner.staff_id},
        )
        connection.execute(
            text("""
                UPDATE public.visits
                SET followup_decision=CAST(:decision AS jsonb)
                WHERE organization_id=:tenant AND id=:visit
            """),
            {
                "tenant": clinic.tenant,
                "visit": source["visit"],
                "decision": json.dumps(
                    {"kind": "OPTED_OUT", "reason": "Changed eligibility"}
                ),
            },
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


def test_active_source_never_falls_back_while_reserved(booking_clinic):
    clinic = booking_clinic
    with transaction(clinic.runtime, clinic.tenant) as connection:
        source = _source(connection, clinic.tenant, UUID(clinic.owner.staff_id))
        _claim(connection, clinic.tenant, source)
    before = booking_effects(clinic)
    status, _ = booking_request(
        clinic, clinic.receptionist, body=_followup_body(source), key=str(uuid4())
    )
    assert status == 409
    assert booking_effects(clinic) == before


def test_active_claim_stays_busy_after_doctor_changes_source_decision(booking_clinic):
    clinic = booking_clinic
    with transaction(clinic.runtime, clinic.tenant) as connection:
        source = _source(connection, clinic.tenant, UUID(clinic.owner.staff_id))
        _claim(connection, clinic.tenant, source)
    with transaction(clinic.runtime, clinic.tenant) as connection:
        connection.execute(
            text("""
                SELECT set_config('app.staff_id', :doctor, true),
                       set_config('app.clinical_change_kind', 'DECISION_EXCEPTION', true),
                       set_config('app.clinical_reason', 'Changed source recommendation', true)
            """),
            {"doctor": clinic.owner.staff_id},
        )
        connection.execute(
            text("""
                UPDATE public.visits
                SET followup_decision='{"kind":"OPTED_OUT","reason":"Changed recommendation"}'::jsonb
                WHERE organization_id=:tenant AND id=:visit
            """),
            {"tenant": clinic.tenant, "visit": source["visit"]},
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
    with transaction(clinic.runtime, clinic.tenant) as connection:
        connection.execute(
            text(
                "SELECT set_config('app.staff_id', :doctor, true), "
                "set_config('app.clinical_change_kind', 'DECISION_EXCEPTION', true), "
                "set_config('app.clinical_reason', 'DOCTOR_PRIVATE_REASON', true)"
            ),
            {"doctor": clinic.owner.staff_id},
        )
        connection.execute(
            text("""
                UPDATE public.visits SET followup_decision=CAST(:decision AS jsonb)
                WHERE organization_id=:tenant AND id=:visit
            """),
            {
                "tenant": clinic.tenant,
                "visit": source["visit"],
                "decision": json.dumps(
                    {
                        "kind": "DEADLINE",
                        "deadline_date": (
                            source["day"] + timedelta(days=3)
                        ).isoformat(),
                        "reason": "DOCTOR_PRIVATE_REASON",
                    }
                ),
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
    assert qualification["source_revision"] == 2
    assert qualification["price_quote"]["base_egp"] == "100.00"
    serialized = json.dumps(detail, ensure_ascii=False)
    for forbidden in (
        "DOCTOR_PRIVATE_REASON",
        "followup_decision",
        "deadline_date",
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

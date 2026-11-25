"""T016: signed immediate intake serialization and immutable evidence."""

from contextlib import contextmanager
from dataclasses import replace
from uuid import UUID, uuid4

import pytest
from carepulse.appointments.intake import intake_appointment
from carepulse.appointments.intake_contracts import (
    EmergencyIntake,
    RoutineIntake,
    UrgentIntake,
)
from carepulse.policies.pricing_contracts import PricingPatch
from carepulse.policies.pricing_service import update_pricing
from runtime_identity_fixture import signed_test_transaction
from sqlalchemy import text
from test_booking import booking_clinic as booking_clinic  # noqa: PLC0414
from test_database import admin as admin  # noqa: PLC0414
from test_database import disposable_project as disposable_project  # noqa: PLC0414
from test_database import empty_database as empty_database  # noqa: PLC0414
from test_patients import patient_backend as patient_backend  # noqa: PLC0414
from test_request_boundary import boundary_data as boundary_data  # noqa: PLC0414
from test_schedule import _activate, _config, _patch, _schedule_request
from test_w3_checkin import _new_patient, queue_request
from test_w3_checkin_races import _archive, _ordered_race
from test_w3_intake import intake_body, intake_request
from test_w3_intake import intake_clinic as intake_clinic  # noqa: PLC0414


@contextmanager
def _intake(clinic, actor, kind, body, key):
    schema = {
        "routine": RoutineIntake,
        "emergency": EmergencyIntake,
        "urgent": UrgentIntake,
    }[kind]
    command = schema.model_validate(body)
    with signed_test_transaction(
        clinic,
        actor,
        method="POST",
        path=f"/v1/intake/{kind}",
        purpose=f"INTAKE_{kind.upper()}",
        body=body,
        key=key,
    ) as (connection, authorized):
        yield (
            connection,
            lambda: intake_appointment(connection, authorized, command, key),
        )


def _counts(clinic):
    with clinic.migration.connect() as connection:
        counts = {}
        for table, condition in (
            ("appointments", ""),
            ("appointment_day_counters", ""),
            ("appointment_number_assignments", ""),
            ("appointment_price_snapshots", ""),
            ("appointment_history", ""),
            ("queue_entries", ""),
            ("queue_days", ""),
            ("visits", ""),
            ("sensitive_audit", "AND action LIKE 'appointments.intake_%'"),
            ("outbox_events", "AND event_type='APPOINTMENT_INTAKE'"),
            ("idempotency_receipts", "AND operation LIKE 'appointments.intake_%'"),
        ):
            counts[table] = connection.scalar(
                text(
                    f"SELECT count(*) FROM public.{table} WHERE organization_id=:tenant {condition}"
                ),
                {"tenant": clinic.tenant},
            )
        return counts


def _assert_commits(clinic, count, lanes):
    expected = {
        table: count
        for table in (
            "appointments",
            "appointment_number_assignments",
            "appointment_price_snapshots",
            "appointment_history",
            "queue_entries",
            "sensitive_audit",
            "outbox_events",
            "idempotency_receipts",
        )
    }
    expected.update(appointment_day_counters=int(count > 0), queue_days=lanes, visits=0)
    assert _counts(clinic) == expected
    with clinic.migration.connect() as connection:
        params = {"tenant": clinic.tenant}
        assert connection.scalars(
            text(
                "SELECT current_number FROM public.appointments WHERE organization_id=:tenant ORDER BY current_number"
            ),
            params,
        ).all() == list(range(1, count + 1))
        assert connection.scalars(
            text(
                "SELECT last_number FROM public.appointment_day_counters WHERE organization_id=:tenant"
            ),
            params,
        ).all() == ([count] if count else [])
        assert (
            connection.scalars(
                text(
                    "SELECT status FROM public.appointments WHERE organization_id=:tenant"
                ),
                params,
            ).all()
            == ["Waiting"] * count
        )


@pytest.mark.parametrize("archive_first", [True, False])
def test_archive_and_new_intake_serialize_both_orders(intake_clinic, archive_first):
    clinic = intake_clinic
    patient = UUID(_new_patient(clinic, clinic.owner, "Intake Archive"))
    body = intake_body(clinic, patient, "emergency")
    key = str(uuid4())
    intake = lambda: _intake(clinic, clinic.receptionist, "emergency", body, key)
    archive = lambda: _archive(clinic, patient)
    if archive_first:
        _, result = _ordered_race(clinic, archive, intake, denial="CONFLICT")
        assert result is None
    else:
        result, _ = _ordered_race(clinic, intake, archive)
        assert result[0]["status"] == "Waiting"
    _assert_commits(clinic, 0 if archive_first else 1, 0 if archive_first else 1)
    with clinic.migration.connect() as connection:
        assert (
            connection.scalar(
                text(
                    "SELECT archived FROM public.patients WHERE organization_id=:tenant AND id=:patient"
                ),
                {"tenant": clinic.tenant, "patient": patient},
            )
            is True
        )


@pytest.mark.parametrize("same_patient", [True, False])
@pytest.mark.parametrize("emergency_first", [True, False])
def test_concurrent_main_and_emergency_share_number_ledger(
    intake_clinic, same_patient, emergency_first
):
    clinic = intake_clinic
    first = _new_patient(clinic, clinic.owner, "Intake Main")
    second = (
        first
        if same_patient
        else _new_patient(clinic, clinic.owner, "Intake Emergency")
    )
    routine = intake_body(clinic, first, "routine")
    emergency = intake_body(clinic, second, "emergency")
    main_key, emergency_key = str(uuid4()), str(uuid4())
    main = lambda: _intake(clinic, clinic.owner, "routine", routine, main_key)
    other = lambda: _intake(
        clinic, clinic.receptionist, "emergency", emergency, emergency_key
    )
    winner, loser = _ordered_race(
        clinic,
        other if emergency_first else main,
        main if emergency_first else other,
        denial="CONFLICT" if same_patient else None,
    )
    assert winner[0]["number"] == 1
    if same_patient:
        assert loser is None
    else:
        assert loser[0]["number"] == 2
        assert winner[0]["arrival_sequence"] == loser[0]["arrival_sequence"] == 1
    _assert_commits(clinic, 1 if same_patient else 2, 1 if same_patient else 2)


@pytest.mark.parametrize("price_first", [True, False])
def test_price_publication_and_intake_freeze_only_valid_revision(
    intake_clinic, price_first
):
    clinic = intake_clinic
    patient = _new_patient(clinic, clinic.owner, "Intake Price")
    body = intake_body(clinic, patient, "urgent")
    key, price_key = str(uuid4()), str(uuid4())
    price_body = {
        "expected_price_revision": body["expected_price_revision"],
        "reason": "Synthetic premium revision",
        "urgent_base_egp": "175.00",
    }

    @contextmanager
    def pricing():
        with signed_test_transaction(
            clinic,
            clinic.owner,
            method="PATCH",
            path="/v1/settings/pricing",
            purpose="SETTINGS_PRICING",
            body=price_body,
            key=price_key,
        ) as (connection, actor):
            yield (
                connection,
                lambda: update_pricing(
                    connection,
                    actor,
                    PricingPatch.model_validate(price_body),
                    price_key,
                ),
            )

    intake = lambda: _intake(clinic, clinic.receptionist, "urgent", body, key)
    first, second = _ordered_race(
        clinic,
        pricing if price_first else intake,
        intake if price_first else pricing,
        denial="STALE_REVISION" if price_first else None,
    )
    if price_first:
        assert second is None
    else:
        assert first[0]["base_egp"] == first[0]["net_egp"] == "150.00"
    _assert_commits(clinic, 0 if price_first else 1, 0 if price_first else 1)


@pytest.mark.parametrize("schedule_first", [True, False])
def test_schedule_publication_and_intake_serialize_both_orders(
    intake_clinic, schedule_first
):
    clinic = intake_clinic
    patient = _new_patient(clinic, clinic.owner, "Intake Schedule")
    body = intake_body(clinic, patient, "routine")
    key, schedule_key = str(uuid4()), str(uuid4())
    status, response = _schedule_request(
        clinic,
        clinic.owner,
        method="POST",
        suffix="/preview",
        body=_patch(
            _config(mode="OPEN", working_weekdays=[]),
            revision=body["expected_schedule_revision"],
        ).model_dump(mode="json"),
        key=str(uuid4()),
    )
    assert status == 200, response
    preview = response["data"]
    activation_body = {
        "preview_id": preview["preview_id"],
        "expected_schedule_revision": preview["revisions"]["schedule"],
        "expected_capacity_revision": preview["revisions"]["capacity"],
        "reason": "Synthetic reviewed schedule publication",
    }

    @contextmanager
    def schedule():
        with signed_test_transaction(
            clinic,
            clinic.owner,
            method="POST",
            path="/v1/settings/schedule/activate",
            purpose="SETTINGS_SCHEDULE",
            body=activation_body,
            key=schedule_key,
        ) as (connection, actor):
            yield (
                connection,
                lambda: _activate(connection, actor, preview, schedule_key),
            )

    intake = lambda: _intake(clinic, clinic.receptionist, "routine", body, key)
    _, second = _ordered_race(
        clinic,
        schedule if schedule_first else intake,
        intake if schedule_first else schedule,
        denial="CONFLICT",
    )
    assert second is None
    _assert_commits(clinic, 0 if schedule_first else 1, 0 if schedule_first else 1)


def test_same_key_recovery_and_wrong_signed_scope(intake_clinic):
    clinic = intake_clinic
    patient = _new_patient(clinic, clinic.owner, "Intake Replay")
    body = intake_body(clinic, patient, "emergency")
    key = str(uuid4())
    intake = lambda: _intake(clinic, clinic.receptionist, "emergency", body, key)
    first, second = _ordered_race(clinic, intake, intake)
    assert first[0:2] == second[0:2]
    assert first[2] is True and second[2] is False
    status, response = intake_request(
        clinic, clinic.receptionist, "emergency", body, key=key
    )
    assert status == 200 and response["data"] == first[0]
    status, response = queue_request(
        clinic,
        clinic.receptionist,
        method="POST",
        path="/v1/intake/emergency",
        purpose="INTAKE_ROUTINE",
        body=body,
        key=key,
    )
    assert status == 403, response
    forged = replace(clinic.receptionist, role="DOCTOR_OWNER")
    status, response = intake_request(clinic, forged, "emergency", body, key=key)
    assert status == 403, response
    foreign = replace(clinic.receptionist, organization_id=str(uuid4()))
    status, response = intake_request(clinic, foreign, "emergency", body, key=key)
    assert status in (401, 403, 404), response
    _assert_commits(clinic, 1, 1)

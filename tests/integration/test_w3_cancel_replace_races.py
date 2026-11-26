"""B-17 rollback and ordered contention at the real signed PostgreSQL boundary."""

import json
from contextlib import contextmanager
from datetime import timedelta
from uuid import UUID, uuid4

import pytest
from carepulse.appointments import cancel, intake
from carepulse.appointments.cancel_contracts import CancelAppointment, ReplaceUrgent
from carepulse.policies.pricing_contracts import PricingPatch
from carepulse.policies.pricing_service import update_pricing
from carepulse.queue.contracts import CheckInAppointment
from runtime_identity_fixture import signed_test_transaction
from test_booking import booking_body, booking_request
from test_booking import booking_clinic as booking_clinic  # noqa: PLC0414
from test_database import admin as admin  # noqa: PLC0414
from test_database import disposable_project as disposable_project  # noqa: PLC0414
from test_database import empty_database as empty_database  # noqa: PLC0414
from test_followups import completed_source, followup_body
from test_followups import followup_clinic as followup_clinic  # noqa: PLC0414
from test_patients import patient_backend as patient_backend  # noqa: PLC0414
from test_request_boundary import boundary_data as boundary_data  # noqa: PLC0414
from test_schedule import _activate, _config, _patch, _schedule_request
from test_w3_admission import admit_body
from test_w3_admission_races import _admission
from test_w3_cancel_replace import cancel_body as _cancel_body
from test_w3_cancel_replace import replace_body
from test_w3_checkin import _new_patient
from test_w3_checkin_races import _archive, _checkin, _ordered_race
from test_w3_intake import _policy_revision, _today, intake_effects
from test_w3_intake import intake_clinic as intake_clinic  # noqa: PLC0414


def _state(clinic):
    return {
        table: [json.loads(row) for row in rows]
        for table, rows in intake_effects(clinic).items()
    }


def _rows(state, table, appointment):
    return [
        row for row in state[table] if row.get("appointment_id") == str(appointment)
    ]


def _appointment(state, appointment):
    return next(row for row in state["appointments"] if row["id"] == str(appointment))


def _added(before, after, table, count):
    old, new = before[table], after[table]
    assert all(row in new for row in old), table
    added = [row for row in new if row not in old]
    assert len(added) == count, table
    return added


def _checkpoint(clinic, before):
    def observe(_connection):
        assert _state(clinic) == before

    return observe


def _book_today(clinic):
    patient = _new_patient(clinic, clinic.owner, "B17 contention")
    body = booking_body(
        patient,
        day=_today(),
        expected_price_revision=_policy_revision(clinic, "PRICE"),
        expected_followup_revision=_policy_revision(clinic, "FOLLOWUP"),
        expected_schedule_revision=_policy_revision(clinic, "SCHEDULE"),
    )
    status, reply = booking_request(clinic, clinic.owner, body=body, key=str(uuid4()))
    assert status == 201, reply
    return UUID(reply["data"]["id"]), UUID(patient)


@contextmanager
def _command(clinic, actor, appointment, body, key, *, replacement=False):
    suffix, purpose, schema, execute = (
        (
            "replace-urgent",
            "APPOINTMENT_REPLACE_URGENT",
            ReplaceUrgent,
            cancel.replace_urgent,
        )
        if replacement
        else (
            "cancel",
            "APPOINTMENT_CANCEL",
            CancelAppointment,
            cancel.cancel_appointment,
        )
    )
    with signed_test_transaction(
        clinic,
        actor,
        method="POST",
        path=f"/v1/appointments/{appointment}/{suffix}",
        purpose=purpose,
        body=body,
        key=key,
    ) as (connection, authorized):
        yield (
            connection,
            lambda: execute(
                connection, authorized, appointment, schema.model_validate(body), key
            ),
        )


def _receipt(state, key):
    return [
        row for row in state["idempotency_receipts"] if row["idempotency_key"] == key
    ]


def _assert_unchanged_allocations(before, after):
    for table in (
        "appointment_day_counters",
        "appointment_number_assignments",
        "appointment_price_snapshots",
    ):
        assert before[table] == after[table], table


def _assert_replacement(before, after, original, result, key):
    projection, revision, created = result
    assert created is True
    new_id = projection["replacement_appointment_id"]
    assert new_id != str(original)
    old = _appointment(before, original)
    terminal = _appointment(after, original)
    assert terminal["status"] == "Cancelled" and terminal["revision"] == revision
    assert {
        k: v
        for k, v in terminal.items()
        if k not in {"status", "revision", "updated_at"}
    } == {k: v for k, v in old.items() if k not in {"status", "revision", "updated_at"}}
    new = _appointment(after, new_id)
    assert (
        new["status"],
        new["classification"],
        new["lane"],
        new["patient_id"],
        new["service_date"],
    ) == ("Waiting", "URGENT", "MAIN", old["patient_id"], old["service_date"])
    assert len(after["appointments"]) == len(before["appointments"]) + 1
    (assignment,) = _added(before, after, "appointment_number_assignments", 1)
    assert assignment["appointment_id"] == new_id
    (price,) = _added(before, after, "appointment_price_snapshots", 1)
    assert price["appointment_id"] == new_id
    assert (price["base_egp"], price["net_egp"]) == (150, 150)
    history = _added(before, after, "appointment_history", 2)
    assert any(
        row["appointment_id"] == str(original)
        and row["replacement_appointment_id"] == new_id
        for row in history
    )
    (entry,) = _rows(after, "queue_entries", new_id)
    assert entry["disposition"] == "ACTIVE"
    assert after["visits"] == before["visits"]
    assert len(_receipt(after, key)) == 1
    assert _receipt(after, key)[0]["result_projection"] == projection
    assert not any(
        row["event_type"] == "DOCTOR_CANCELLED_APPOINTMENT"
        for row in after["outbox_events"]
        if row not in before["outbox_events"]
    )


@pytest.mark.parametrize("stage", ["allocation", "cancellation"])
def test_replacement_rolls_back_real_partial_writes(intake_clinic, monkeypatch, stage):
    clinic = intake_clinic
    appointment, _ = _book_today(clinic)
    with _checkin(
        clinic,
        clinic.owner,
        appointment,
        CheckInAppointment(
            physical_presence_confirmed=True, expected_appointment_revision=0
        ),
        str(uuid4()),
    ) as (_, execute):
        execute()
    body = replace_body(clinic, appointment)
    before = _state(clinic)
    observed = []

    class InjectedFailure(Exception):
        pass

    if stage == "allocation":
        original = intake.allocate_day_number

        def fail(connection, *args, **kwargs):
            number = original(connection, *args, **kwargs)
            observed.append(number)
            assert number == _appointment(before, appointment)["current_number"] + 1
            raise InjectedFailure

        monkeypatch.setattr(intake, "allocate_day_number", fail)
    else:
        original = cancel._cancel

        def fail(connection, replacement_id):
            result = original(connection, replacement_id)
            observed.append(result)
            assert result["status"] == "Cancelled"
            assert result["replacement_appointment_id"] == str(replacement_id)
            raise InjectedFailure

        monkeypatch.setattr(cancel, "_cancel", fail)
    with (
        pytest.raises(InjectedFailure),
        _command(
            clinic,
            clinic.receptionist,
            appointment,
            body,
            str(uuid4()),
            replacement=True,
        ) as (_, execute),
    ):
        execute()
    assert len(observed) == 1
    assert _state(clinic) == before


@pytest.mark.parametrize("replacement_first", [True, False])
@pytest.mark.parametrize(
    "competitor", ["cancel", "admit", "archive", "price", "schedule"]
)
def test_replacement_serializes_with_supported_competitors(
    intake_clinic, replacement_first, competitor
):
    clinic = intake_clinic
    appointment, patient = _book_today(clinic)
    if competitor == "admit":
        with _checkin(
            clinic,
            clinic.owner,
            appointment,
            CheckInAppointment(
                physical_presence_confirmed=True, expected_appointment_revision=0
            ),
            str(uuid4()),
        ) as (_, execute):
            waiting = execute()[0]
    body = replace_body(clinic, appointment)
    key, other_key = str(uuid4()), str(uuid4())

    def replacement():
        return _command(
            clinic, clinic.receptionist, appointment, body, key, replacement=True
        )

    if competitor == "cancel":
        cancel_body = _cancel_body(clinic, appointment)

        def competing():
            return _command(clinic, clinic.owner, appointment, cancel_body, other_key)
    elif competitor == "admit":
        admission = admit_body(clinic, waiting, physical_presence_confirmed=True)

        def competing():
            return _admission(clinic, clinic.owner, admission, other_key)
    elif competitor == "archive":

        def competing():
            return _archive(clinic, patient)
    elif competitor == "price":
        price = {
            "expected_price_revision": body["expected_price_revision"],
            "reason": "Synthetic changed premium",
            "urgent_base_egp": "175.00",
        }

        @contextmanager
        def competing():
            with signed_test_transaction(
                clinic,
                clinic.owner,
                method="PATCH",
                path="/v1/settings/pricing",
                purpose="SETTINGS_PRICING",
                body=price,
                key=other_key,
            ) as (conn, actor):
                yield (
                    conn,
                    lambda: update_pricing(
                        conn, actor, PricingPatch.model_validate(price), other_key
                    ),
                )
    else:
        config = _config(
            mode="OPEN",
            working_weekdays=list(range(1, 8)),
            weekly_work_periods=[
                {"weekday": day, "starts_at": "00:00", "ends_at": "23:59"}
                for day in range(1, 8)
            ],
            holidays=[(_today() + timedelta(days=10)).isoformat()],
        )
        status, reply = _schedule_request(
            clinic,
            clinic.owner,
            method="POST",
            suffix="/preview",
            body=_patch(config, revision=body["expected_schedule_revision"]).model_dump(
                mode="json"
            ),
            key=str(uuid4()),
        )
        assert status == 200, reply
        preview = reply["data"]
        payload = {
            "preview_id": preview["preview_id"],
            "expected_schedule_revision": preview["revisions"]["schedule"],
            "expected_capacity_revision": preview["revisions"]["capacity"],
            "reason": "Synthetic reviewed schedule publication",
        }

        @contextmanager
        def competing():
            with signed_test_transaction(
                clinic,
                clinic.owner,
                method="POST",
                path="/v1/settings/schedule/activate",
                purpose="SETTINGS_SCHEDULE",
                body=payload,
                key=other_key,
            ) as (conn, actor):
                yield conn, lambda: _activate(conn, actor, preview, other_key)

    before = _state(clinic)
    first, second = _ordered_race(
        clinic,
        replacement if replacement_first else competing,
        competing if replacement_first else replacement,
        denial=None
        if replacement_first and competitor in ("archive", "price", "schedule")
        else "CONFLICT",
        checkpoint=_checkpoint(clinic, before),
    )
    after = _state(clinic)
    if replacement_first:
        _assert_replacement(before, after, appointment, first, key)
        if competitor in ("cancel", "admit"):
            assert second is None
            assert not _receipt(after, other_key)
        else:
            assert second is not None
    else:
        assert second is None
        assert not _receipt(after, key)
        assert len(after["appointments"]) == len(before["appointments"])
        _assert_unchanged_allocations(before, after)
        expected = {"cancel": "Cancelled", "admit": "In Examination"}.get(
            competitor, "Booked"
        )
        assert _appointment(after, appointment)["status"] == expected
        for table in (
            "appointment_history",
            "outbox_events",
            "sensitive_audit",
            "idempotency_receipts",
        ):
            assert all(row in after[table] for row in before[table])
        if competitor in ("archive", "price", "schedule"):
            for table in (
                "appointments",
                "queue_entries",
                "queue_days",
                "appointment_history",
                "outbox_events",
                "visits",
                "followup_claims",
            ):
                assert after[table] == before[table], table


@pytest.mark.parametrize("checkin_first", [True, False])
def test_booked_claim_checkin_and_cancellation_in_both_orders(
    followup_clinic, checkin_first
):
    clinic = followup_clinic
    source = completed_source(clinic)
    status, reply = booking_request(
        clinic, clinic.owner, body=followup_body(source, offset=1), key=str(uuid4())
    )
    assert status == 201, reply
    appointment = UUID(reply["data"]["id"])
    body = _cancel_body(clinic, appointment)
    cancel_key, checkin_key = str(uuid4()), str(uuid4())
    checkin_body = CheckInAppointment(
        physical_presence_confirmed=True, expected_appointment_revision=0
    )

    def checking():
        return _checkin(
            clinic, clinic.receptionist, appointment, checkin_body, checkin_key
        )

    def cancelling():
        return _command(clinic, clinic.owner, appointment, body, cancel_key)

    before = _state(clinic)
    first, second = _ordered_race(
        clinic,
        checking if checkin_first else cancelling,
        cancelling if checkin_first else checking,
        denial="CONFLICT",
        checkpoint=_checkpoint(clinic, before),
    )
    assert second is None
    after = _state(clinic)
    _assert_unchanged_allocations(before, after)
    assert after["visits"] == before["visits"]
    assert _appointment(after, appointment)["status"] == (
        "Waiting" if checkin_first else "Cancelled"
    )
    assert _appointment(after, appointment)["revision"] == 1
    (claim,) = _rows(after, "followup_claims", appointment)
    assert claim["state"] == ("ACTIVE" if checkin_first else "RELEASED")
    entries = _rows(after, "queue_entries", appointment)
    assert [entry["disposition"] for entry in entries] == (
        ["ACTIVE"] if checkin_first else []
    )
    (history,) = _added(before, after, "appointment_history", 1)
    assert history["appointment_id"] == str(appointment)
    assert history["actor_staff_id"] == (
        clinic.receptionist.staff_id if checkin_first else clinic.owner.staff_id
    )
    assert not _receipt(after, cancel_key if checkin_first else checkin_key)
    (receipt,) = _added(before, after, "idempotency_receipts", 1)
    assert receipt["idempotency_key"] == (checkin_key if checkin_first else cancel_key)
    assert receipt["result_projection"] == first[0]
    events = [
        row for row in after["outbox_events"] if row not in before["outbox_events"]
    ]
    assert sorted(row["event_type"] for row in events) == (
        ["PATIENT_CHECKED_IN"]
        if checkin_first
        else ["APPOINTMENT_CANCELLED", "DOCTOR_CANCELLED_APPOINTMENT"]
    )
    assert all(row in after["sensitive_audit"] for row in before["sensitive_audit"])
    if checkin_first:
        with _command(
            clinic,
            clinic.owner,
            appointment,
            _cancel_body(clinic, appointment),
            str(uuid4()),
        ) as (_, execute):
            assert execute()[0]["status"] == "Cancelled"
        final = _state(clinic)
        assert _rows(final, "queue_entries", appointment)[0]["disposition"] == "CLOSED"
        assert _rows(final, "followup_claims", appointment)[0]["state"] == "RELEASED"

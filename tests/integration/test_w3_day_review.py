"""T069: signed day review and close against physical attendance."""

from contextlib import contextmanager
from datetime import datetime, timedelta
from uuid import UUID, uuid4

import pytest
from carepulse.appointments.day_review import CloseCommand, record_review
from carepulse.queue.contracts import CheckInAppointment
from runtime_identity_fixture import signed_test_transaction
from sqlalchemy import text
from test_booking import booking_clinic as booking_clinic  # noqa: PLC0414
from test_database import admin as admin  # noqa: PLC0414
from test_database import disposable_project as disposable_project  # noqa: PLC0414
from test_database import empty_database as empty_database  # noqa: PLC0414
from test_patients import patient_backend as patient_backend  # noqa: PLC0414
from test_pricing import _fresh_receptionist_session, _owner_changes_manager
from test_request_boundary import boundary_data as boundary_data  # noqa: PLC0414
from test_schedule import CAIRO
from test_w3_admission import _admit, admit_body, waiting_case
from test_w3_cancel_replace_races import _checkpoint, _observe_waiter
from test_w3_checkin import _new_patient, _seed_booked, queue_request
from test_w3_checkin_races import _checkin, _ordered_race
from test_w3_visits import effects


def _close_body():
    return {
        "expected_review_revision": 0,
        "expected_main_queue_day_revision": 0,
        "expected_emergency_queue_day_revision": 0,
    }


def _request(clinic, actor, day, operation, body, key):
    return queue_request(
        clinic,
        actor,
        method="POST",
        path=f"/v1/days/{day}/{operation}",
        purpose=f"DAY_{operation.upper()}",
        body=body,
        key=key,
    )


def _reviews(clinic):
    with clinic.migration.connect() as connection:
        return {
            table: list(
                connection.scalars(
                    text(
                        f"SELECT to_jsonb(r) FROM public.{table} r WHERE organization_id=:tenant ORDER BY to_jsonb(r)::text"
                    ),
                    {"tenant": clinic.tenant},
                )
            )
            for table in ("day_reviews", "day_review_history")
        }


@contextmanager
def _close(clinic, day, body, key):
    with signed_test_transaction(
        clinic,
        clinic.owner,
        method="POST",
        path=f"/v1/days/{day}/close",
        purpose="DAY_CLOSE",
        body=body,
        key=key,
    ) as (connection, _actor):
        yield (
            connection,
            lambda: record_review(connection, day, CloseCommand.model_validate(body)),
        )


def test_initial_review_replay_and_owner_only_amendment(booking_clinic):
    clinic = booking_clinic
    day = datetime.now(CAIRO).date() - timedelta(days=1)
    _owner_changes_manager(clinic, True, 0)
    manager = _fresh_receptionist_session(clinic, 1)
    body = {"expected_review_revision": 0, "notes": "Initial review"}
    key = str(uuid4())
    status, reply = _request(clinic, manager, day, "review", body, key)
    assert status == 200, reply
    before = effects(clinic), _reviews(clinic)
    assert _request(clinic, manager, day, "review", body, key) == (status, reply)
    assert (
        _request(clinic, manager, day, "review", {**body, "notes": "Changed"}, key)[0]
        == 409
    )
    amend = {
        "expected_review_revision": 1,
        "notes": "Reopened",
        "reason": "Synthetic amendment",
    }
    assert _request(clinic, manager, day, "review", amend, str(uuid4()))[0] in (
        403,
        409,
    )
    assert (effects(clinic), _reviews(clinic)) == before
    status, amended = _request(clinic, clinic.owner, day, "review", amend, str(uuid4()))
    assert status == 200, amended
    assert amended["data"]["revision"] == 2
    assert len(_reviews(clinic)["day_review_history"]) == 2


@pytest.mark.parametrize("checkin_first", [True, False])
def test_close_and_first_checkin_in_both_orders(booking_clinic, checkin_first):
    clinic = booking_clinic
    day = datetime.now(CAIRO).date() - timedelta(days=1)
    patient = UUID(_new_patient(clinic, clinic.owner, "B20 close race"))
    target = _seed_booked(clinic, patient, day)
    appointment = UUID(target["id"])
    close_key, checkin_key = str(uuid4()), str(uuid4())
    command = CheckInAppointment(
        physical_presence_confirmed=True, expected_appointment_revision=0
    )

    def closing():
        return _close(
            clinic,
            day,
            {**_close_body(), "expected_main_queue_day_revision": int(checkin_first)},
            close_key,
        )

    def checking():
        return _checkin(clinic, clinic.receptionist, appointment, command, checkin_key)

    before = effects(clinic)
    committed = []
    _, second = _ordered_race(
        clinic,
        checking if checkin_first else closing,
        lambda: _observe_waiter(
            clinic, closing if checkin_first else checking, committed
        ),
        denial="CONFLICT" if checkin_first else None,
        checkpoint=_checkpoint(clinic, before),
    )
    after = effects(clinic)
    if checkin_first:
        assert second is None and committed == [after]
        assert _reviews(clinic)["day_reviews"] == []
        assert not any(
            row["idempotency_key"] == close_key for row in after["idempotency_receipts"]
        )
    else:
        assert second is not None
        reviews = _reviews(clinic)
        (review,) = reviews["day_reviews"]
        assert (review["revision"], review["disposition"]) == (1, "FINAL_CLOSED")
        assert len(reviews["day_review_history"]) == 1
    (appointment_row,) = [
        row for row in after["appointments"] if row["id"] == target["id"]
    ]
    assert (appointment_row["status"], appointment_row["service_date"]) == (
        "Waiting",
        day.isoformat(),
    )
    assert after["visits"] == before["visits"]
    assert (
        after["appointment_number_assignments"]
        == before["appointment_number_assignments"]
    )
    assert (
        len(
            [
                row
                for row in after["queue_entries"]
                if row["appointment_id"] == target["id"]
            ]
        )
        == 1
    )


def test_close_preserves_booked_and_reopen_requires_owner(booking_clinic):
    clinic = booking_clinic
    day = datetime.now(CAIRO).date() - timedelta(days=1)
    target = _seed_booked(
        clinic, UUID(_new_patient(clinic, clinic.owner, "B20 retained")), day
    )
    before = effects(clinic)
    status, reply = _request(
        clinic, clinic.receptionist, day, "close", _close_body(), str(uuid4())
    )
    assert status == 200, reply
    closed = effects(clinic)
    assert closed["appointments"] == before["appointments"]
    assert closed["visits"] == before["visits"]
    assert (
        next(row for row in closed["appointments"] if row["id"] == target["id"])[
            "status"
        ]
        == "Booked"
    )
    reopen = {"expected_review_revision": 1, "reason": "Synthetic reopen"}
    snapshot = effects(clinic), _reviews(clinic)
    assert _request(clinic, clinic.receptionist, day, "review", reopen, str(uuid4()))[
        0
    ] in (403, 409)
    assert (effects(clinic), _reviews(clinic)) == snapshot
    status, reply = _request(clinic, clinic.owner, day, "review", reopen, str(uuid4()))
    assert status == 200, reply
    assert (reply["data"]["disposition"], reply["data"]["revision"]) == ("REVIEWED", 2)


def test_close_retains_ongoing_visit_on_original_day(booking_clinic):
    clinic = booking_clinic
    day = datetime.now(CAIRO).date() - timedelta(days=1)
    waiting = waiting_case(clinic, day=day)
    admitted = _admit(
        clinic,
        clinic.owner,
        admit_body(clinic, waiting, physical_presence_confirmed=True),
    )
    status, view = queue_request(
        clinic,
        clinic.owner,
        method="GET",
        path="/v1/reconciliation",
        purpose="RECONCILIATION_READ",
        query={"date": day.isoformat()},
    )
    assert status == 200, view
    body = {
        "expected_review_revision": view["data"]["review_revision"],
        "expected_main_queue_day_revision": view["data"]["main_queue_day_revision"],
        "expected_emergency_queue_day_revision": view["data"][
            "emergency_queue_day_revision"
        ],
    }
    before = effects(clinic)
    status, reply = _request(
        clinic, clinic.receptionist, day, "close", body, str(uuid4())
    )
    assert status == 200, reply
    assert reply["data"]["disposition"] == "FINAL_CLOSED"
    after = effects(clinic)
    assert after["appointments"] == before["appointments"]
    assert after["visits"] == before["visits"]
    assert any(row["id"] == admitted["visit_id"] for row in after["visits"])
    assert any(
        row["id"] == admitted["appointment_id"]
        and row["status"] == "In Examination"
        and row["service_date"] == day.isoformat()
        for row in after["appointments"]
    )


def test_close_transaction_rollback_leaves_no_review(booking_clinic):
    clinic = booking_clinic
    day = datetime.now(CAIRO).date() - timedelta(days=1)
    before = effects(clinic), _reviews(clinic)
    with (
        pytest.raises(RuntimeError, match="synthetic rollback"),
        _close(clinic, day, _close_body(), str(uuid4())) as (_, execute),
    ):
        assert execute()[0]["disposition"] == "FINAL_CLOSED"
        raise RuntimeError("synthetic rollback")
    assert (effects(clinic), _reviews(clinic)) == before

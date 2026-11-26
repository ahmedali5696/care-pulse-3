"""T034: observed PostgreSQL ordering for placement and future queue policy."""

from contextlib import contextmanager
from datetime import timedelta
from functools import partial
from uuid import UUID, uuid4

import pytest
from carepulse.policies.queue import QueuePolicyCommand, set_queue_policy
from carepulse.queue.contracts import CheckInAppointment
from carepulse.queue.placement import PlacementCommand, place_routine
from runtime_identity_fixture import signed_test_transaction
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from test_booking import booking_clinic as booking_clinic  # noqa: PLC0414
from test_patients import patient_backend as patient_backend  # noqa: PLC0414
from test_request_boundary import boundary_data as boundary_data  # noqa: PLC0414
from test_w3_admission import admission_effects, admit_body, today, waiting_case
from test_w3_admission_races import (
    _admission,
    _admission_delta,
    _assert_admission,
    _assert_delta,
    _assert_waiting_unchanged,
    _case_row,
)
from test_w3_checkin import SERVICE_DAY, _book, _new_patient
from test_w3_checkin_races import _assert_checkin_effects, _checkin, _ordered_race
from test_w3_queue_policy import _policy_body, _queue, placement_body, policy_request


@contextmanager
def _placement(clinic, actor, body, key):
    with signed_test_transaction(
        clinic,
        actor,
        method="POST",
        path="/v1/queue/placement",
        purpose="QUEUE_PLACEMENT",
        body=body,
        key=key,
    ) as (connection, authorized):
        yield (
            connection,
            lambda: place_routine(
                connection, authorized, PlacementCommand.model_validate(body), key
            ),
        )


@contextmanager
def _publication(clinic, actor, body, key):
    with signed_test_transaction(
        clinic,
        actor,
        method="PATCH",
        path="/v1/settings/queue",
        purpose="SETTINGS_QUEUE_UPDATE",
        body=body,
        key=key,
    ) as (connection, authorized):
        yield (
            connection,
            lambda: set_queue_policy(
                connection, authorized, QueuePolicyCommand.model_validate(body), key
            ),
        )


def _locked_checkpoint(clinic, before, statement, parameters):
    def observe(observer):
        assert admission_effects(clinic) == before
        with pytest.raises(DBAPIError) as blocked, observer.begin_nested():
            observer.execute(text(statement), parameters)
        assert blocked.value.orig.sqlstate == "55P03"

    return observe


def _assert_b13_evidence(after, actor, key, outcome, operation):
    projection, revision, replayed = outcome
    assert replayed is False
    (receipt,) = [
        row for row in after["idempotency_receipts"] if row["idempotency_key"] == key
    ]
    assert (receipt["operation"], receipt["actor_id"], receipt["staff_id"]) == (
        operation,
        actor.staff_id,
        actor.staff_id,
    )
    assert (receipt["result_projection"], receipt["result_revision"]) == (
        projection,
        revision,
    )
    (audit,) = [row for row in after["sensitive_audit"] if row["action"] == operation]
    assert (audit["actor_staff_id"], audit["actor_role"], audit["after_state"]) == (
        actor.staff_id,
        actor.role,
        projection,
    )
    return audit


@pytest.mark.parametrize("placement_first", [False, True])
def test_placement_and_old_head_admission_serialize(booking_clinic, placement_first):
    clinic = booking_clinic
    day = today()
    old_head, second = [waiting_case(clinic, day=day) for _ in range(2)]
    queue = _queue(clinic, day)
    assert queue["entries"][0]["appointment_id"] == old_head["appointment_id"]
    body = placement_body(queue)
    admission_body = admit_body(clinic, old_head, physical_presence_confirmed=True)
    place_key, admit_key = str(uuid4()), str(uuid4())
    place = partial(_placement, clinic, clinic.receptionist, body, place_key)
    admit = partial(_admission, clinic, clinic.owner, admission_body, admit_key)
    before = admission_effects(clinic)
    checkpoint = _locked_checkpoint(
        clinic,
        before,
        "SELECT id FROM public.queue_days WHERE id=:day FOR UPDATE NOWAIT",
        {"day": UUID(queue["queue_day_id"])},
    )
    winner, loser = _ordered_race(
        clinic,
        place if placement_first else admit,
        admit if placement_first else place,
        denial="STALE_REVISION",
        checkpoint=checkpoint,
    )
    assert loser is None
    after = admission_effects(clinic)
    current = _queue(clinic, day)
    assert current["queue_day_revision"] == queue["queue_day_revision"] + 1
    assert current["arrival_counter"] == queue["arrival_counter"]
    assert current["queue_policy_version"] == queue["queue_policy_version"]
    assert current["entries"][0]["appointment_id"] == second["appointment_id"]
    if placement_first:
        _assert_placement_winner(
            clinic, before, after, current, queue, body, place_key, winner
        )
    else:
        _assert_admission(clinic, clinic.owner, admission_body, admit_key, winner)
        _admission_delta(before, after)
        _assert_waiting_unchanged(before, after, second)
        assert current["routine_cursor"] == old_head["booking_number"]
        assert current["total_waiting"] == 1
        assert len(after["visits"]) == 1


def _assert_placement_winner(
    clinic, before, after, current, old_queue, body, key, winner
):
    _assert_delta(
        before,
        after,
        changed=("queue_days", "queue_entries", "queue_entries"),
        appended={"sensitive_audit": 1, "idempotency_receipts": 1},
    )
    audit = _assert_b13_evidence(
        after, clinic.receptionist, key, winner, "queue.placement"
    )
    assert audit["reason"] == body["reason"]
    assert audit["before_state"]["entries"] == [
        {
            "queue_entry_id": row["queue_entry_id"],
            "routine_rank": row["routine_rank"],
            "queue_entry_revision": row["queue_entry_revision"],
        }
        for row in old_queue["entries"]
    ]
    assert current["routine_cursor"] == old_queue["routine_cursor"]
    assert current["total_waiting"] == 2
    assert [row["queue_entry_id"] for row in current["entries"]] == [
        row["queue_entry_id"] for row in body["entries"]
    ]
    assert [row["routine_rank"] for row in current["entries"]] == [1, 2]
    for old in old_queue["entries"]:
        updated = _case_row(after, "queue_entries", old["queue_entry_id"])
        assert updated["revision"] == old["queue_entry_revision"] + 1


@pytest.mark.parametrize("publication_first", [False, True])
def test_first_next_operating_day_checkin_and_publication_snapshot_once(
    booking_clinic, publication_first
):
    clinic = booking_clinic
    day = today()
    next_day = day + timedelta(
        days=(SERVICE_DAY.isoweekday() - day.isoweekday()) % 7 or 7
    )
    status, initial = policy_request(clinic, clinic.owner)
    assert status == 200, initial
    appointment = _book(
        clinic,
        clinic.owner,
        _new_patient(clinic, clinic.owner, "Publication race"),
        service_date=next_day,
    )
    assert _queue(clinic, next_day)["queue_day_id"] is None
    command = CheckInAppointment(
        physical_presence_confirmed=True,
        expected_appointment_revision=0,
        expected_queue_day_revision=0,
    )
    body = _policy_body(initial["revision"], "ARRIVAL_FIFO", 0)
    publication_key, checkin_key = str(uuid4()), str(uuid4())
    publication = partial(_publication, clinic, clinic.owner, body, publication_key)
    checkin = partial(
        _checkin,
        clinic,
        clinic.receptionist,
        UUID(appointment["id"]),
        command,
        checkin_key,
    )
    before = admission_effects(clinic)
    checkpoint = _locked_checkpoint(
        clinic,
        before,
        "SELECT kind FROM public.clinic_policy_heads WHERE organization_id=:tenant AND kind='QUEUE' FOR UPDATE NOWAIT",
        {"tenant": clinic.tenant},
    )
    if publication_first:
        published, checked_in = _ordered_race(
            clinic, publication, checkin, checkpoint=checkpoint
        )
    else:
        checked_in, published = _ordered_race(
            clinic, checkin, publication, checkpoint=checkpoint
        )
    after = admission_effects(clinic)
    _assert_publication_and_checkin(
        clinic,
        before,
        after,
        appointment,
        initial,
        published,
        checked_in,
        publication_key,
        checkin_key,
        next_day,
        publication_first,
    )


def _assert_publication_and_checkin(
    clinic,
    before,
    after,
    appointment,
    initial,
    published,
    checked_in,
    publication_key,
    checkin_key,
    next_day,
    publication_first,
):
    policy = published[0]
    assert policy["effective_on"] == next_day.isoformat()
    assert policy["policy_revision"] == initial["revision"] + 1
    assert policy["routine_order"] == "ARRIVAL_FIFO" and policy["late_depth"] == 0
    audit = _assert_b13_evidence(
        after, clinic.owner, publication_key, published, "settings.queue.update"
    )
    assert audit["before_state"] == initial["data"]
    projection, revision = checked_in
    assert revision == 1
    _assert_checkin_effects(
        clinic, appointment, clinic.receptionist, checkin_key, projection
    )
    current = _queue(clinic, next_day)
    expected = policy if publication_first else initial["data"]
    assert (
        current["queue_policy_version"]
        == projection["queue_policy_version"]
        == expected["policy_version"]
    )
    assert current["routine_order"] == expected["routine_order"]
    assert current["late_depth"] == expected["late_depth"]
    assert current["queue_day_revision"] == current["arrival_counter"] == 1
    assert current["routine_cursor"] == 0 and current["total_waiting"] == 1
    _assert_delta(
        before,
        after,
        changed=("clinic_policy_heads", "appointments"),
        appended={
            "clinic_policy_versions": 1,
            "queue_days": 1,
            "queue_entries": 1,
            "appointment_history": 1,
            "sensitive_audit": 2,
            "outbox_events": 1,
            "idempotency_receipts": 2,
        },
    )

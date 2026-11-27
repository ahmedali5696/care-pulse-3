"""T033: signed B-13 policy publication, placement, and denied effects."""

from dataclasses import replace
from datetime import datetime, timedelta
from uuid import uuid4

import pytest
from runtime_identity_fixture import signed_test_transaction
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from test_booking import booking_clinic as booking_clinic  # noqa: PLC0414
from test_patients import patient_backend as patient_backend  # noqa: PLC0414
from test_pricing import _fresh_receptionist_session, _owner_changes_manager
from test_request_boundary import boundary_data as boundary_data  # noqa: PLC0414
from test_w3_admission import admission_effects, admit_body, admit_request, waiting_case
from test_w3_admission_controls import seed_waiting
from test_w3_checkin import (
    CAIRO,
    SERVICE_DAY,
    queue_request,
)


def policy_request(clinic, actor, body=None, key=None):
    return queue_request(
        clinic,
        actor,
        method="GET" if body is None else "PATCH",
        path="/v1/settings/queue",
        purpose="SETTINGS_QUEUE_READ" if body is None else "SETTINGS_QUEUE_UPDATE",
        body=body,
        key="" if body is None else key or str(uuid4()),
    )


def placement_request(clinic, actor, body, key=None):
    return queue_request(
        clinic,
        actor,
        method="POST",
        path="/v1/queue/placement",
        purpose="QUEUE_PLACEMENT",
        body=body,
        key=key or str(uuid4()),
    )


def _queue(clinic, day, lane="MAIN"):
    status, response = queue_request(
        clinic,
        clinic.owner,
        method="GET",
        path="/v1/queue",
        purpose="QUEUE_READ",
        query={"date": day.isoformat(), "lane": lane},
    )
    assert status == 200, response
    return response["data"]


def _policy_body(revision, order="ARRIVAL_FIFO", depth=0):
    return {
        "routine_order": order,
        "late_depth": depth,
        "expected_policy_revision": revision,
    }


def placement_body(queue):
    return {
        "date": queue["service_date"],
        "expected_queue_day_revision": queue["queue_day_revision"],
        "entries": [
            {
                "queue_entry_id": entry["queue_entry_id"],
                "expected_queue_entry_revision": entry["queue_entry_revision"],
            }
            for entry in reversed(queue["entries"])
            if entry["classification"] == "ROUTINE"
        ],
        "reason": "Synthetic justified Routine placement",
    }


def _denied(clinic, request, actor, status, *args):
    before = admission_effects(clinic)
    code, response = request(clinic, actor, *args)
    assert code in ((status,) if isinstance(status, int) else status), response
    assert "data" not in response
    assert admission_effects(clinic) == before


def _evidence(clinic, before, actor, reply, key, *, reason=None):
    after = admission_effects(clinic)
    audit = [
        row for row in after["sensitive_audit"] if row not in before["sensitive_audit"]
    ]
    receipts = [
        row
        for row in after["idempotency_receipts"]
        if row not in before["idempotency_receipts"]
    ]
    assert len(audit) == len(receipts) == 1
    assert audit[0]["actor_staff_id"] == actor.staff_id
    assert audit[0]["actor_role"] == actor.role
    operation = "queue.placement" if reason is not None else "settings.queue.update"
    assert audit[0]["action"] == operation
    assert audit[0]["after_state"] == reply["data"]
    assert audit[0]["reason"] == reason
    assert receipts[0]["idempotency_key"] == key
    assert receipts[0]["actor_id"] == actor.staff_id
    assert receipts[0]["staff_id"] == actor.staff_id
    assert receipts[0]["operation"] == operation
    assert receipts[0]["result_projection"] == reply["data"]
    assert receipts[0]["result_revision"] == reply["revision"]
    return audit[0]


def _publish(clinic, actor, body):
    before, key = admission_effects(clinic), str(uuid4())
    status, reply = policy_request(clinic, actor, body, key)
    assert status == 200, reply
    assert reply["data"]["routine_order"] == body["routine_order"]
    assert reply["data"]["late_depth"] == body["late_depth"]
    assert reply["revision"] == body["expected_policy_revision"] + 1
    audit = _evidence(clinic, before, actor, reply, key)
    assert audit["before_state"]["policy_revision"] == body["expected_policy_revision"]
    after = admission_effects(clinic)
    assert {table for table in before if before[table] != after[table]} <= {
        "clinic_policy_heads",
        "clinic_policy_versions",
        "sensitive_audit",
        "idempotency_receipts",
    }
    assert (
        len(after["clinic_policy_versions"])
        == len(before["clinic_policy_versions"]) + 1
    )
    assert all(
        row in after["clinic_policy_versions"]
        for row in before["clinic_policy_versions"]
    )
    assert policy_request(clinic, actor, body, key) == (200, reply)
    assert admission_effects(clinic) == after
    return reply, key


def test_owner_and_live_manager_publish_only_future_policy(booking_clinic):
    clinic = booking_clinic
    today = datetime.now(CAIRO).date()
    operating_day = today + timedelta(
        days=(SERVICE_DAY.isoweekday() - today.isoweekday()) % 7 or 7
    )
    seed_waiting(clinic, day=today)
    old_queue = _queue(clinic, today)
    status, initial = policy_request(clinic, clinic.owner)
    assert status == 200, initial
    assert initial["data"]["routine_order"] == "BOOKED_NUMBER"
    assert initial["data"]["late_depth"] == 5
    body = _policy_body(initial["revision"])
    _denied(clinic, policy_request, clinic.receptionist, 403)
    _denied(clinic, policy_request, clinic.receptionist, 403, body)
    operator = replace(clinic.owner, role="OPERATOR")
    _denied(clinic, policy_request, operator, (401, 403))
    _denied(clinic, policy_request, operator, (401, 403), body)
    owner_reply, _ = _publish(clinic, clinic.owner, body)
    assert owner_reply["data"]["effective_on"] == operating_day.isoformat()
    assert owner_reply["data"]["pending_effective"] is False
    _denied(clinic, policy_request, clinic.owner, 409, body)
    _owner_changes_manager(clinic, True, 0)
    manager = _fresh_receptionist_session(clinic, 1)
    manager_body = _policy_body(owner_reply["revision"], "BOOKED_NUMBER", 2147483647)
    manager_reply, key = _publish(clinic, manager, manager_body)
    assert policy_request(clinic, manager) == (200, manager_reply)
    assert manager_reply["data"]["effective_on"] == operating_day.isoformat()
    _denied(
        clinic, policy_request, manager, 409, {**manager_body, "late_depth": 2}, key
    )
    _owner_changes_manager(clinic, False, 1)
    _denied(clinic, policy_request, manager, 401, manager_body, key)
    former_manager = _fresh_receptionist_session(clinic, 2)
    _denied(clinic, policy_request, former_manager, 403)
    _denied(
        clinic,
        policy_request,
        former_manager,
        403,
        _policy_body(manager_reply["revision"]),
    )
    assert _queue(clinic, today) == old_queue
    seed_waiting(clinic, day=operating_day)
    next_queue = _queue(clinic, operating_day)
    assert next_queue["queue_policy_version"] == manager_reply["data"]["policy_version"]
    assert next_queue["routine_order"] == "BOOKED_NUMBER"
    assert next_queue["late_depth"] == 2147483647


@pytest.mark.parametrize(
    "changes",
    [
        {"late_depth": -1},
        {"late_depth": 2147483648},
        {"late_depth": True},
        {"late_depth": 1.5},
        {"late_depth": "5"},
        {"routine_order": "FIFO"},
        {"expected_policy_revision": True},
        {"effective_on": "2030-01-01"},
    ],
)
def test_invalid_policy_values_leave_no_effects(booking_clinic, changes):
    clinic = booking_clinic
    _denied(clinic, policy_request, clinic.owner, 422, {**_policy_body(1), **changes})


def _place(clinic, actor, body, queue):
    before, key = admission_effects(clinic), str(uuid4())
    status, reply = placement_request(clinic, actor, body, key)
    assert status == 200, reply
    audit = _evidence(clinic, before, actor, reply, key, reason=body["reason"])
    assert audit["before_state"]["queue_day_revision"] == queue["queue_day_revision"]
    assert [
        (entry["queue_entry_id"], entry["routine_rank"])
        for entry in audit["before_state"]["entries"]
    ] == [
        (entry["queue_entry_id"], entry["routine_rank"])
        for entry in queue["entries"]
        if entry["classification"] == "ROUTINE"
    ]
    assert reply["revision"] == queue["queue_day_revision"] + 1
    assert [entry["queue_entry_id"] for entry in reply["data"]["entries"]] == [
        entry["queue_entry_id"] for entry in body["entries"]
    ]
    after = admission_effects(clinic)
    assert {table for table in before if before[table] != after[table]} <= {
        "queue_days",
        "queue_entries",
        "sensitive_audit",
        "idempotency_receipts",
    }
    assert placement_request(clinic, actor, body, key) == (200, reply)
    assert admission_effects(clinic) == after
    return reply, key


def test_only_receptionists_place_routine_without_touching_other_lanes(booking_clinic):
    clinic = booking_clinic
    day = datetime.now(CAIRO).date()
    prior_day = day - timedelta(days=1)
    admitted = seed_waiting(clinic, day=day)
    status, reply = admit_request(
        clinic,
        clinic.owner,
        admit_body(clinic, admitted, physical_presence_confirmed=True),
    )
    assert status == 200, reply
    seed_waiting(clinic, day=day)
    seed_waiting(clinic, day=day)
    waiting_case(clinic, day=day, classification="URGENT")
    seed_waiting(clinic, day=day, lane="EMERGENCY")
    seed_waiting(clinic, day=prior_day)
    queue = _queue(clinic, day)
    assert queue["routine_cursor"] > 0
    emergency = _queue(clinic, day, "EMERGENCY")
    prior = _queue(clinic, prior_day)
    body = placement_body(queue)
    _denied(clinic, placement_request, clinic.owner, 403, body)
    _denied(
        clinic,
        placement_request,
        replace(clinic.owner, role="OPERATOR"),
        (401, 403),
        body,
    )
    _denied(
        clinic, placement_request, clinic.receptionist, 422, {**body, "reason": "  "}
    )
    _denied(
        clinic,
        placement_request,
        clinic.receptionist,
        422,
        {**body, "entries": body["entries"] * 2},
    )
    _denied(
        clinic,
        placement_request,
        clinic.receptionist,
        409,
        {**body, "date": prior_day.isoformat()},
    )
    _denied(
        clinic,
        placement_request,
        clinic.receptionist,
        409,
        {**body, "entries": body["entries"][:1]},
    )
    for wrong in (
        queue["entries"][0],
        emergency["entries"][0],
        prior["entries"][0],
        admitted,
    ):
        wrong_entry = {
            "queue_entry_id": wrong["queue_entry_id"],
            "expected_queue_entry_revision": wrong["queue_entry_revision"],
        }
        _denied(
            clinic,
            placement_request,
            clinic.receptionist,
            409,
            {**body, "entries": [wrong_entry, body["entries"][0]]},
        )
    stale_entries = [dict(entry) for entry in body["entries"]]
    stale_entries[0]["expected_queue_entry_revision"] += 1
    _denied(
        clinic,
        placement_request,
        clinic.receptionist,
        409,
        {**body, "entries": stale_entries},
    )
    _, key = _place(clinic, clinic.receptionist, body, queue)
    current = _queue(clinic, day)
    assert current["entries"][0] == queue["entries"][0]
    assert current["routine_cursor"] == queue["routine_cursor"]
    assert [entry["queue_entry_id"] for entry in current["entries"][1:]] == [
        entry["queue_entry_id"] for entry in body["entries"]
    ]
    _denied(clinic, placement_request, clinic.receptionist, 409, body)
    _denied(
        clinic,
        placement_request,
        clinic.receptionist,
        409,
        {**body, "entries": list(reversed(body["entries"]))},
        key,
    )
    _owner_changes_manager(clinic, True, 0)
    manager = _fresh_receptionist_session(clinic, 1)
    manager_body = placement_body(current)
    _, key = _place(clinic, manager, manager_body, current)
    _owner_changes_manager(clinic, False, 1)
    _denied(clinic, placement_request, manager, 401, manager_body, key)
    former_manager = _fresh_receptionist_session(clinic, 2)
    current = _queue(clinic, day)
    _place(clinic, former_manager, placement_body(current), current)
    assert _queue(clinic, day, "EMERGENCY") == emergency
    assert _queue(clinic, prior_day) == prior


def test_signed_policy_read_cannot_use_b13_mutation_functions(booking_clinic):
    clinic = booking_clinic
    seed_waiting(clinic)
    before = admission_effects(clinic)
    with signed_test_transaction(
        clinic,
        clinic.owner,
        method="GET",
        path="/v1/settings/queue",
        purpose="SETTINGS_QUEUE_READ",
    ) as (conn, _actor):
        assert conn.scalar(text("SELECT session_user")) == "carepulse_api"
        assert (
            conn.scalar(text("SELECT public.f01_b13_settings()"))["projection"][
                "late_depth"
            ]
            == 5
        )
        for statement in (
            "SELECT public.f01_b13_publish()",
            "SELECT public.f01_b13_place()",
            "SELECT public.f01_b13_replay()",
        ):
            with pytest.raises(DBAPIError) as denied, conn.begin_nested():
                conn.execute(text(statement))
            assert denied.value.orig.sqlstate == "42501"
        assert conn.scalars(text("SELECT id FROM public.queue_entries")).all() == []
        assert (
            conn.scalars(text("SELECT kind FROM public.clinic_policy_heads")).all()
            == []
        )
    with clinic.runtime.connect() as conn:
        for statement in (
            "SELECT public.f01_b13_settings()",
            "SELECT public.f01_b13_publish()",
            "SELECT public.f01_b13_place()",
        ):
            with pytest.raises(DBAPIError) as denied, conn.begin_nested():
                conn.execute(text(statement))
            assert denied.value.orig.sqlstate == "42501"
    assert admission_effects(clinic) == before

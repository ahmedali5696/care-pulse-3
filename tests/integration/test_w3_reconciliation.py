"""T067: signed staff reconciliation and manual absence."""

from contextlib import contextmanager
from datetime import datetime, time, timedelta
from uuid import UUID, uuid4

import pytest
from carepulse.appointments.persistence import persist_appointment
from carepulse.appointments.reconciliation import ManualNoShow, manual_no_show
from carepulse.db import transaction
from carepulse.queue.contracts import CheckInAppointment
from runtime_identity_fixture import signed_test_transaction
from sqlalchemy import text
from test_appointment_persistence import _draft, _query
from test_booking import booking_clinic as booking_clinic  # noqa: PLC0414
from test_database import admin as admin  # noqa: PLC0414
from test_database import disposable_project as disposable_project  # noqa: PLC0414
from test_database import empty_database as empty_database  # noqa: PLC0414
from test_followup_persistence import (
    _current_v2_command,
    _signed_v2_transaction,
    _v2_booking,
    _v2_source,
)
from test_followups import followup_clinic as followup_clinic  # noqa: PLC0414
from test_patients import patient_backend as patient_backend  # noqa: PLC0414
from test_pricing import _fresh_receptionist_session, _owner_changes_manager
from test_report_query_plans import _plan_nodes
from test_request_boundary import boundary_data as boundary_data  # noqa: PLC0414
from test_schedule import CAIRO
from test_w3_cancel_replace_races import _checkpoint, _observe_waiter
from test_w3_checkin import _new_patient, _seed_booked, queue_request
from test_w3_checkin_races import _checkin, _ordered_race
from test_w3_visits import effects


def _seed(clinic, day):
    return _seed_booked(clinic, UUID(_new_patient(clinic, clinic.owner, "B20")), day)


def _body():
    return {
        "verified_absence": True,
        "reason": "Verified synthetic absence",
        "expected_appointment_revision": 0,
    }


def historical_claim(clinic):
    """Seed authentic historical source/claim evidence with only the fixture clock moved."""
    asof = datetime.now(CAIRO) - timedelta(days=3)
    replacements = (
        ("public.guard_appointment_history_insert()", "clock_timestamp());"),
        (
            "public.b10_schedule_without_offset_guard(uuid,jsonb)",
            "clock_timestamp() AT TIME ZONE 'Africa/Cairo'",
        ),
    )
    originals = []
    with clinic.migration.begin() as connection:
        for signature, marker in replacements:
            original = connection.scalar(
                text("SELECT pg_get_functiondef(CAST(:signature AS regprocedure))"),
                {"signature": signature},
            )
            assert original.count(marker) == 1
            originals.append(original)
            connection.exec_driver_sql(
                original.replace(
                    marker,
                    marker.replace(
                        "clock_timestamp()",
                        "COALESCE(NULLIF(current_setting('cp.b20_asof', true), '')::timestamptz, clock_timestamp())",
                    ),
                )
            )
    try:
        with transaction(clinic.migration, clinic.tenant) as connection:
            connection.execute(
                text("SELECT set_config('cp.b20_asof', :asof, true)"),
                {"asof": asof.isoformat()},
            )
            source = _v2_source(connection, clinic.tenant, UUID(clinic.owner.staff_id))
            assert source["day"] == asof.date()
        with _signed_v2_transaction(
            clinic, "create_followup_claim", source
        ) as connection:
            connection.execute(
                text("SELECT set_config('cp.b20_asof', :asof, true)"),
                {"asof": asof.isoformat()},
            )
            claim = _current_v2_command(
                connection, "create_followup_claim", source, **_v2_booking(source)
            )
    finally:
        with clinic.migration.begin() as connection:
            for original in originals:
                connection.exec_driver_sql(original)
    with clinic.migration.connect() as connection:
        target, day, state, revision = connection.execute(
            text("""SELECT a.id,a.service_date,c.state,c.revision
            FROM public.followup_claims c JOIN public.appointments a
            ON (a.organization_id,a.id)=(c.organization_id,c.appointment_id)
            WHERE c.organization_id=:tenant AND c.id=:claim"""),
            {"tenant": clinic.tenant, "claim": claim},
        ).one()
    assert (day, state, revision) == (asof.date() + timedelta(days=1), "ACTIVE", 1)
    return target, day


def _request(clinic, actor, target, body, key):
    return queue_request(
        clinic,
        actor,
        method="POST",
        path=f"/v1/appointments/{target}/manual-no-show",
        purpose="APPOINTMENT_MANUAL_NO_SHOW",
        body=body,
        key=key,
    )


@contextmanager
def _absence(clinic, actor, target, body, key):
    with signed_test_transaction(
        clinic,
        actor,
        method="POST",
        path=f"/v1/appointments/{target}/manual-no-show",
        purpose="APPOINTMENT_MANUAL_NO_SHOW",
        body=body,
        key=key,
    ) as (connection, authorized):
        yield (
            connection,
            lambda: manual_no_show(
                connection, authorized, target, ManualNoShow.model_validate(body)
            ),
        )


@pytest.mark.parametrize("role", ["owner", "receptionist", "manager"])
def test_prior_day_manual_absence_replays_and_conflicts(booking_clinic, role):
    clinic = booking_clinic
    target = _seed(clinic, datetime.now(CAIRO).date() - timedelta(days=1))
    actor = getattr(clinic, role, None)
    if role == "manager":
        _owner_changes_manager(clinic, True, 0)
        actor = _fresh_receptionist_session(clinic, 1)
    key = str(uuid4())
    before = effects(clinic)
    status, reply = _request(clinic, actor, target["id"], _body(), key)
    assert status == 200, reply
    assert reply["data"]["status"] == "No-show"
    after = effects(clinic)
    (history,) = [
        row
        for row in after["appointment_history"]
        if row not in before["appointment_history"]
    ]
    (audit,) = [
        row for row in after["sensitive_audit"] if row not in before["sensitive_audit"]
    ]
    assert (history["actor_staff_id"], history["actor_role"], history["reason"]) == (
        actor.staff_id,
        actor.role,
        _body()["reason"],
    )
    assert (
        audit["actor_staff_id"],
        audit["actor_role"],
        audit["action"],
        audit["reason"],
    ) == (actor.staff_id, actor.role, "APPOINTMENT_MARKED_NO_SHOW", _body()["reason"])
    assert _request(clinic, actor, target["id"], _body(), key) == (status, reply)
    assert (
        _request(
            clinic,
            actor,
            target["id"],
            {**_body(), "reason": "Changed synthetic reason"},
            key,
        )[0]
        == 409
    )
    assert effects(clinic) == after
    assert len(after["appointment_history"]) == len(before["appointment_history"]) + 1
    assert len(after["sensitive_audit"]) == len(before["sensitive_audit"]) + 1
    assert len(after["idempotency_receipts"]) == len(before["idempotency_receipts"]) + 1
    events = [
        row for row in after["outbox_events"] if row not in before["outbox_events"]
    ]
    assert [row["event_type"] for row in events] == ["APPOINTMENT_MARKED_NO_SHOW"]
    for table in (
        "queue_days",
        "queue_entries",
        "visits",
        "appointment_day_counters",
        "appointment_number_assignments",
        "followup_claims",
    ):
        assert after[table] == before[table]


def test_selected_date_and_invalid_absence_leave_state_unchanged(booking_clinic):
    clinic = booking_clinic
    today = datetime.now(CAIRO).date()
    current = _seed(clinic, today)
    prior = _seed(clinic, today - timedelta(days=1))
    before = effects(clinic)
    for target, body in (
        (current, _body()),
        (prior, {**_body(), "verified_absence": False}),
        (prior, {**_body(), "reason": ""}),
    ):
        assert _request(clinic, clinic.owner, target["id"], body, str(uuid4()))[0] in (
            409,
            422,
        )
        assert effects(clinic) == before
    for day, target, overdue in (
        (today, current, False),
        (today - timedelta(days=1), prior, True),
    ):
        status, reply = queue_request(
            clinic,
            clinic.owner,
            method="GET",
            path="/v1/reconciliation",
            purpose="RECONCILIATION_READ",
            query={"date": day.isoformat(), "lane": "MAIN", "limit": 1, "offset": 0},
        )
        assert status == 200, reply
        assert reply["data"]["total"] == 1
        (entry,) = reply["data"]["entries"]
        assert (
            entry["appointment_id"],
            entry["overdue"],
            entry["requires_review"],
        ) == (target["id"], overdue, overdue)
    assert effects(clinic) == before
    # A timed slot on its selected Cairo day is not overdue, even after its end.
    # Seed a separate historical timed interval and move only the read clock.
    doctor = UUID(clinic.owner.staff_id)
    patient = UUID(_new_patient(clinic, clinic.owner, "B20 timed"))
    slot_day = today - timedelta(days=3)
    start = datetime.combine(slot_day, time(10), CAIRO)
    with transaction(clinic.migration, clinic.tenant) as connection:
        timed = persist_appointment(
            connection,
            _draft(
                patient,
                doctor,
                service_date=slot_day,
                booking_mode="EXACT_TIME",
                starts_at=start,
                ends_at=start + timedelta(minutes=20),
            ),
            _query(clinic.tenant, doctor),
        )
    before = effects(clinic)
    signature = "public.f01_b20_reconciliation()"
    marker = "today:=(clock_timestamp() AT TIME ZONE 'Africa/Cairo')::date;"
    with clinic.migration.begin() as connection:
        original = connection.scalar(
            text("SELECT pg_get_functiondef(CAST(:signature AS regprocedure))"),
            {"signature": signature},
        )
        assert original.count(marker) == 1
        connection.exec_driver_sql(
            original.replace(
                marker,
                marker.replace(
                    "clock_timestamp()",
                    "COALESCE(NULLIF(current_setting('cp.b20_read_asof', true), '')::timestamptz, clock_timestamp())",
                ),
            ),
            execution_options={"no_parameters": True},
        )
    try:
        midnight = datetime.combine(slot_day + timedelta(days=1), time.min, CAIRO)
        for instant, overdue in (
            (midnight - timedelta(seconds=1), False),
            (midnight, True),
        ):
            with signed_test_transaction(
                clinic,
                clinic.owner,
                method="GET",
                path="/v1/reconciliation",
                purpose="RECONCILIATION_READ",
                query=f"date={slot_day}",
            ) as (connection, _):
                connection.execute(
                    text("SELECT set_config('cp.b20_read_asof', :asof, true)"),
                    {"asof": instant.isoformat()},
                )
                page = connection.scalar(text("SELECT public.f01_b20_reconciliation()"))
                (entry,) = page["entries"]
                assert (
                    entry["appointment_id"],
                    entry["overdue"],
                    entry["requires_review"],
                    entry["status"],
                ) == (timed["id"], overdue, overdue, "Booked")
    finally:
        with clinic.migration.begin() as connection:
            connection.exec_driver_sql(
                original, execution_options={"no_parameters": True}
            )
    assert effects(clinic) == before


@pytest.mark.parametrize("checkin_first", [True, False])
def test_manual_absence_and_checkin_serialize_without_loser_effects(
    booking_clinic, checkin_first
):
    clinic = booking_clinic
    target = _seed(clinic, datetime.now(CAIRO).date() - timedelta(days=1))
    appointment = UUID(target["id"])
    absence_key, checkin_key = str(uuid4()), str(uuid4())
    command = CheckInAppointment(
        physical_presence_confirmed=True, expected_appointment_revision=0
    )

    def absence():
        return _absence(clinic, clinic.owner, appointment, _body(), absence_key)

    def checkin():
        return _checkin(clinic, clinic.receptionist, appointment, command, checkin_key)

    before = effects(clinic)
    committed = []
    _, loser = _ordered_race(
        clinic,
        checkin if checkin_first else absence,
        lambda: _observe_waiter(
            clinic, absence if checkin_first else checkin, committed
        ),
        denial="CONFLICT",
        checkpoint=_checkpoint(clinic, before),
    )
    after = effects(clinic)
    assert loser is None and committed == [after]
    (row,) = [row for row in after["appointments"] if row["id"] == target["id"]]
    assert (row["status"], row["revision"], row["service_date"]) == (
        "Waiting" if checkin_first else "No-show",
        1,
        target["service_date"],
    )
    assert len(after["appointment_history"]) == len(before["appointment_history"]) + 1
    assert len(after["idempotency_receipts"]) == len(before["idempotency_receipts"]) + 1
    assert not any(
        row["idempotency_key"] == (absence_key if checkin_first else checkin_key)
        for row in after["idempotency_receipts"]
    )
    for table in (
        "appointment_day_counters",
        "appointment_number_assignments",
        "appointment_price_snapshots",
        "visits",
        "followup_claims",
    ):
        assert after[table] == before[table]


def test_reconciliation_query_plan_bounds_page(booking_clinic, record_property):
    clinic = booking_clinic
    day = datetime.now(CAIRO).date() - timedelta(days=1)
    for _ in range(3):
        _seed(clinic, day)
    for offset in range(1, 7):
        _seed(clinic, day - timedelta(days=offset))
    with clinic.migration.connect() as connection:
        plan = connection.scalar(
            text("""EXPLAIN (ANALYZE, FORMAT JSON)
            SELECT x.id AS appointment_id,x.patient_id,
                array_to_string(p.name_parts,' ') AS patient_name,
                x.service_date,x.status,x.classification,x.lane,x.service_type,x.booking_mode,
                x.starts_at,x.ends_at,x.current_number AS booking_number,
                x.revision AS appointment_revision,cl.revision AS claim_revision,
                v.id AS visit_id,x.service_date<:today AS overdue,
                x.service_date<:today AND x.status IN ('Booked','Waiting') AS requires_review
            FROM public.appointments x
            JOIN public.patients p ON p.organization_id=x.organization_id AND p.id=x.patient_id
            LEFT JOIN public.followup_claims cl ON cl.organization_id=x.organization_id AND cl.appointment_id=x.id
            LEFT JOIN public.visits v ON v.organization_id=x.organization_id AND v.appointment_id=x.id
            WHERE x.organization_id=:tenant AND x.service_date=:day
            AND x.status IN ('Booked','Waiting','In Examination') AND x.lane='MAIN'
            ORDER BY x.current_number,x.id LIMIT 2 OFFSET 0"""),
            {"tenant": clinic.tenant, "day": day, "today": day + timedelta(days=1)},
        )
    observed = [
        {
            key: node[key]
            for key in (
                "Node Type",
                "Relation Name",
                "Index Name",
                "Actual Rows",
                "Actual Loops",
            )
            if key in node
        }
        for node in _plan_nodes(plan[0]["Plan"])
    ]
    record_property("reconciliation_plan", str(observed))
    print(f"B-20 reconciliation EXPLAIN (sanitized): {observed}")
    assert plan[0]["Plan"]["Node Type"] == "Limit"
    assert plan[0]["Plan"]["Actual Rows"] == 2


def test_manual_absence_transaction_rollback_leaves_no_effects(followup_clinic):
    clinic = followup_clinic
    target, day = historical_claim(clinic)
    body = {**_body(), "expected_claim_revision": 1}
    key = str(uuid4())
    before = effects(clinic)
    with (
        pytest.raises(RuntimeError, match="synthetic rollback"),
        _absence(clinic, clinic.owner, target, body, key) as (
            _,
            execute,
        ),
    ):
        assert execute()[0]["claim_revision"] == 2
        raise RuntimeError("synthetic rollback")
    assert effects(clinic) == before
    status, reply = _request(clinic, clinic.owner, target, body, key)
    assert status == 200, reply
    assert reply["data"]["claim_revision"] == 2
    after = effects(clinic)
    (claim,) = [
        row for row in after["followup_claims"] if row["appointment_id"] == str(target)
    ]
    (history,) = [
        row
        for row in after["appointment_history"]
        if row not in before["appointment_history"]
    ]
    assert (
        claim["state"],
        claim["revision"],
        claim["terminal_history_revision"],
        claim["terminal_at"],
    ) == ("RELEASED", 2, 1, history["occurred_at"])
    assert history["new_service_date"] == day.isoformat()
    assert _request(clinic, clinic.owner, target, body, key) == (status, reply)
    assert effects(clinic) == after

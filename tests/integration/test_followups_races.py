"""B-10 booking, source-revision and claim races on distinct PostgreSQL sessions."""

from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from threading import Event
from uuid import UUID, uuid4

import pytest
from carepulse.appointments import persistence as appointment_persistence
from carepulse.appointments.book import book_appointment
from carepulse.appointments.booking_contracts import BookAppointment
from carepulse.db import transaction
from carepulse.identity.contracts import ContractError
from carepulse.policies.schedule_service import load_current_schedule
from carepulse.policies.slots import candidates_for_date
from carepulse.policies.versions import append_version
from carepulse.visits.eligibility import extend_followup, record_followup_decision
from carepulse.visits.eligibility_contracts import (
    ExtendFollowupV2,
    FollowupDecisionPatchV2,
)
from sqlalchemy import text
from test_appointment_persistence import _admit_saved
from test_booking import booking_body, booking_effects
from test_booking import booking_clinic as booking_clinic  # noqa: PLC0414
from test_booking_races import _concurrent, _wait_for_postgres_lock
from test_followup_persistence import _v2_source
from test_followups import terminal_claim
from test_patients import patient_backend as patient_backend  # noqa: PLC0414
from test_pricing import _patch, pricing_request
from test_request_boundary import boundary_data as boundary_data  # noqa: PLC0414
from test_schedule import _actor, _config, _publish


@pytest.fixture
def followup_clinic(booking_clinic):
    clinic = booking_clinic
    status, reply = pricing_request(
        clinic,
        clinic.owner,
        method="PATCH",
        body=_patch(expected_price_revision=2, preferential_followup_egp="40.00"),
        key=str(uuid4()),
    )
    assert status == 200, reply
    with transaction(clinic.runtime, clinic.tenant) as connection:
        _publish(
            connection,
            _actor(clinic.tenant, UUID(clinic.owner.staff_id)),
            _config(mode="OPEN", working_weekdays=list(range(1, 8))),
            revision=2,
        )
    return clinic


def _source_for(clinic):
    with transaction(clinic.runtime, clinic.tenant) as connection:
        return _v2_source(connection, clinic.tenant, UUID(clinic.owner.staff_id))


def _command(source, day_offset=2, **changes):
    fields = {
        "requested_service_type": "FOLLOW_UP",
        "source_visit_id": str(source["visit"]),
        "expected_source_decision_revision": 0,
        "expected_visit_revision": 0,
        "expected_followup_revision": 1,
        "expected_base_egp": "40.00",
        "expected_net_egp": "40.00",
        "expected_price_revision": 3,
        "expected_schedule_revision": 3,
        **changes,
    }
    return BookAppointment.model_validate(
        booking_body(
            source["patient"],
            day=source["day"] + timedelta(days=day_offset),
            **fields,
        )
    )


def _attempt(clinic, command):
    actor = _actor(clinic.tenant, UUID(clinic.owner.staff_id))
    with transaction(clinic.runtime, clinic.tenant) as connection:
        _doctor_context(connection, actor.staff_id)
        return book_appointment(connection, actor, command, str(uuid4()))


def _doctor_context(connection, staff):
    connection.execute(text("SET LOCAL lock_timeout = '30s'"))
    connection.execute(text("SET LOCAL statement_timeout = '45s'"))
    connection.execute(
        text("SELECT set_config('app.staff_id', :actor, true)"),
        {"actor": str(staff)},
    )


def _decision(kind, revision, *, reason=None):
    decision = {"kind": kind}
    if reason:
        decision["reason"] = reason
    return FollowupDecisionPatchV2.model_validate(
        {
            "followup_decision": decision,
            "expected_source_decision_revision": revision,
            "expected_visit_revision": 0,
            "expected_followup_revision": 1,
        }
    )


def _claim_count(clinic, source):
    with transaction(clinic.runtime, clinic.tenant) as connection:
        return connection.scalar(
            text("""
                SELECT count(*) FROM public.followup_claims
                WHERE organization_id=:tenant AND source_visit_id=:visit
                  AND state='ACTIVE'
            """),
            {"tenant": clinic.tenant, "visit": source["visit"]},
        )


def test_two_different_day_claims_cannot_reserve_one_source(followup_clinic):
    clinic = followup_clinic
    source = _source_for(clinic)
    before = booking_effects(clinic)
    outcomes = _concurrent(
        lambda: _attempt(clinic, _command(source, 2)),
        lambda: _attempt(clinic, _command(source, 3)),
    )
    winners = [candidate for candidate in outcomes if isinstance(candidate, dict)]
    denied = [
        candidate for candidate in outcomes if isinstance(candidate, ContractError)
    ]
    assert len(winners) == len(denied) == 1, outcomes
    assert winners[0]["service_type"] == "FOLLOW_UP"
    assert denied[0].status_code == 409
    assert _claim_count(clinic, source) == 1
    assert booking_effects(clinic) == tuple(earlier + 1 for earlier in before)


@pytest.mark.parametrize("acknowledged_fallback", [False, True])
def test_doctor_edit_first_makes_reviewed_source_stale_without_booking_writes(
    followup_clinic, acknowledged_fallback
):
    clinic = followup_clinic
    source = _source_for(clinic)
    requested = _command(source)
    next_decision = {"kind": "OPTED_OUT", "reason": "Corrected recommendation"}
    expected_revision = 0
    if acknowledged_fallback:
        with transaction(clinic.runtime, clinic.tenant) as connection:
            _doctor_context(connection, UUID(clinic.owner.staff_id))
            record_followup_decision(
                connection,
                _actor(clinic.tenant, UUID(clinic.owner.staff_id)),
                source["visit"],
                _decision("OPTED_OUT", 0, reason="Prior opt-out"),
                str(uuid4()),
            )
        requested = _command(
            source,
            expected_source_decision_revision=1,
            expected_base_egp="100.00",
            expected_net_egp="100.00",
            fallback_acknowledgment={
                "service_type": "NEW_EXAMINATION",
                "discount_reaffirmed": True,
            },
        )
        next_decision = {"kind": "DEFAULT"}
        expected_revision = 1
    changed = Event()
    permit_commit = Event()
    booking_started = Event()
    edit_pid = []
    booking_pid = []
    actor = _actor(clinic.tenant, UUID(clinic.owner.staff_id))

    def edit():
        with transaction(clinic.runtime, clinic.tenant) as connection:
            edit_pid.append(connection.scalar(text("SELECT pg_backend_pid()")))
            _doctor_context(connection, actor.staff_id)
            record_followup_decision(
                connection,
                actor,
                source["visit"],
                _decision(
                    next_decision["kind"],
                    expected_revision,
                    reason=next_decision.get("reason"),
                ),
                str(uuid4()),
            )
            changed.set()
            assert permit_commit.wait(timeout=20)

    def book():
        with transaction(clinic.runtime, clinic.tenant) as connection:
            booking_pid.append(connection.scalar(text("SELECT pg_backend_pid()")))
            booking_started.set()
            _doctor_context(connection, actor.staff_id)
            return book_appointment(connection, actor, requested, str(uuid4()))

    before = booking_effects(clinic)
    with ThreadPoolExecutor(max_workers=2) as pool:
        editor = pool.submit(edit)
        assert changed.wait(timeout=15)
        booking = pool.submit(book)
        try:
            assert booking_started.wait(timeout=15)
            with transaction(clinic.runtime, clinic.tenant) as observer:
                _wait_for_postgres_lock(observer, booking_pid[0], edit_pid[0])
        finally:
            permit_commit.set()
        editor.result(timeout=30)
        with pytest.raises(ContractError) as denied:
            booking.result(timeout=30)
    assert denied.value.status_code == 409
    assert denied.value.code == "STALE_REVISION"
    assert booking_effects(clinic) == before
    assert _claim_count(clinic, source) == 0


def test_confirmation_first_freezes_claim_before_doctor_opt_out(followup_clinic):
    clinic = followup_clinic
    source = _source_for(clinic)
    booked = Event()
    commit_booking = Event()
    confirmation_pid = []
    edit_pid = []
    edit_started = Event()
    actor = _actor(clinic.tenant, UUID(clinic.owner.staff_id))

    def confirm():
        with transaction(clinic.runtime, clinic.tenant) as connection:
            confirmation_pid.append(connection.scalar(text("SELECT pg_backend_pid()")))
            _doctor_context(connection, actor.staff_id)
            result = book_appointment(connection, actor, _command(source), str(uuid4()))
            booked.set()
            assert commit_booking.wait(timeout=20)
            return result

    def update_decision():
        with transaction(clinic.runtime, clinic.tenant) as connection:
            edit_pid.append(connection.scalar(text("SELECT pg_backend_pid()")))
            edit_started.set()
            _doctor_context(connection, actor.staff_id)
            return record_followup_decision(
                connection,
                actor,
                source["visit"],
                _decision("OPTED_OUT", 0, reason="Later assessment"),
                str(uuid4()),
            )

    with ThreadPoolExecutor(max_workers=2) as pool:
        confirmed = pool.submit(confirm)
        assert booked.wait(timeout=15)
        edited = pool.submit(update_decision)
        try:
            assert edit_started.wait(timeout=15)
            with transaction(clinic.runtime, clinic.tenant) as observer:
                _wait_for_postgres_lock(observer, edit_pid[0], confirmation_pid[0])
        finally:
            commit_booking.set()
        saved = confirmed.result(timeout=30)
        assert edited.result(timeout=30)[1] == 1

    assert saved["service_type"] == "FOLLOW_UP"
    assert _claim_count(clinic, source) == 1
    with transaction(clinic.runtime, clinic.tenant) as connection:
        assert connection.scalar(
            text("""
                SELECT approved_deadline FROM public.followup_claims
                WHERE organization_id=:tenant AND appointment_id=:appointment
            """),
            {"tenant": clinic.tenant, "appointment": UUID(saved["id"])},
        ) == source["day"] + timedelta(days=7)


@pytest.mark.parametrize("terminal", ["Cancelled", "Completed"])
def test_release_or_consume_races_fresh_claim_without_double_use(
    followup_clinic, terminal
):
    clinic = followup_clinic
    source = _source_for(clinic)
    first = _attempt(clinic, _command(source, 2))
    changed = Event()
    finish = Event()
    replacement_started = Event()
    terminal_pid = []
    replacement_pid = []

    if terminal == "Completed":
        with transaction(clinic.runtime, clinic.tenant) as connection:
            _doctor_context(connection, UUID(clinic.owner.staff_id))
            _admit_saved(
                connection,
                clinic.tenant,
                UUID(clinic.owner.staff_id),
                first,
                complete=False,
            )

    def terminate():
        with clinic.migration.begin() as connection:
            terminal_pid.append(connection.scalar(text("SELECT pg_backend_pid()")))
            connection.execute(
                text("""
                    SELECT id FROM public.patients
                    WHERE organization_id=:tenant AND id=:patient FOR UPDATE
                """),
                {"tenant": clinic.tenant, "patient": source["patient"]},
            ).one()
            terminal_claim(
                clinic,
                first,
                status=terminal,
                migration_connection=connection,
                pre_admitted=terminal == "Completed",
            )
            changed.set()
            assert finish.wait(timeout=20)

    def book_replacement():
        actor = _actor(clinic.tenant, UUID(clinic.owner.staff_id))
        with transaction(clinic.runtime, clinic.tenant) as connection:
            replacement_pid.append(connection.scalar(text("SELECT pg_backend_pid()")))
            replacement_started.set()
            _doctor_context(connection, actor.staff_id)
            return book_appointment(
                connection,
                actor,
                _command(source, 3),
                str(uuid4()),
            )

    with ThreadPoolExecutor(max_workers=2) as pool:
        terminal_future = pool.submit(terminate)
        assert changed.wait(timeout=15)
        replacement = pool.submit(book_replacement)
        try:
            assert replacement_started.wait(timeout=15)
            with transaction(clinic.runtime, clinic.tenant) as observer:
                _wait_for_postgres_lock(
                    observer,
                    replacement_pid[0],
                    terminal_pid[0],
                )
        finally:
            finish.set()
        terminal_future.result(timeout=30)
        if terminal == "Cancelled":
            assert replacement.result(timeout=30)["service_type"] == "FOLLOW_UP"
            assert _claim_count(clinic, source) == 1
        else:
            with pytest.raises(ContractError) as denied:
                replacement.result(timeout=30)
            assert denied.value.status_code == 409
            assert _claim_count(clinic, source) == 0


def test_claim_confirmation_first_blocks_terminal_until_booking_commits(
    followup_clinic,
):
    clinic = followup_clinic
    source = _source_for(clinic)
    before = booking_effects(clinic)
    confirmed = Event()
    release_booking = Event()
    terminal_started = Event()
    booking_pid = []
    terminal_pid = []
    pending_booking = []
    actor = _actor(clinic.tenant, UUID(clinic.owner.staff_id))

    def book():
        with transaction(clinic.runtime, clinic.tenant) as connection:
            booking_pid.append(connection.scalar(text("SELECT pg_backend_pid()")))
            _doctor_context(connection, actor.staff_id)
            saved = book_appointment(connection, actor, _command(source), str(uuid4()))
            pending_booking.append(saved)
            confirmed.set()
            assert release_booking.wait(timeout=20)
            return saved

    def cancel():
        with clinic.migration.begin() as connection:
            connection.execute(text("SET LOCAL lock_timeout = '30s'"))
            connection.execute(text("SET LOCAL statement_timeout = '45s'"))
            terminal_pid.append(connection.scalar(text("SELECT pg_backend_pid()")))
            terminal_started.set()
            connection.execute(
                text("""
                    SELECT id FROM public.patients
                    WHERE organization_id=:tenant AND id=:patient FOR UPDATE
                """),
                {"tenant": clinic.tenant, "patient": source["patient"]},
            ).one()
            return terminal_claim(
                clinic,
                pending_booking[0],
                status="Cancelled",
                migration_connection=connection,
            )

    with ThreadPoolExecutor(max_workers=2) as pool:
        booking = pool.submit(book)
        assert confirmed.wait(timeout=15)
        terminal = pool.submit(cancel)
        try:
            assert terminal_started.wait(timeout=15)
            # pg_stat_activity masks another role's wait details; observe the
            # migration waiter using a separate migration-role connection.
            with clinic.migration.connect() as observer:
                _wait_for_postgres_lock(observer, terminal_pid[0], booking_pid[0])
            assert booking_effects(clinic) == before
            assert _claim_count(clinic, source) == 0
        finally:
            release_booking.set()
        saved = booking.result(timeout=30)
        released = terminal.result(timeout=30)

    assert saved["service_type"] == "FOLLOW_UP"
    assert released is not None
    assert _claim_count(clinic, source) == 0
    assert booking_effects(clinic) == tuple(
        count + delta for count, delta in zip(before, (1, 1, 1, 1, 2, 1, 1, 1))
    )
    with clinic.migration.connect() as connection:
        state = connection.execute(
            text("""
                SELECT c.id, c.state, c.terminal_history_revision,
                       a.status, a.revision, h.action, h.new_status, h.actor_staff_id
                FROM public.followup_claims c
                JOIN public.appointments a ON (a.organization_id,a.id)
                    = (c.organization_id,c.appointment_id)
                JOIN public.appointment_history h ON (h.organization_id,h.appointment_id,
                    h.appointment_revision) =
                    (a.organization_id,a.id,c.terminal_history_revision)
                WHERE c.organization_id=:tenant AND a.id=:appointment
            """),
            {"tenant": clinic.tenant, "appointment": UUID(saved["id"])},
        ).one()
        assert state == (
            released,
            "RELEASED",
            1,
            "Cancelled",
            1,
            "STATUS",
            "Cancelled",
            actor.staff_id,
        )


def test_doctor_extension_and_new_claim_compete_for_same_source(followup_clinic):
    clinic = followup_clinic
    source = _source_for(clinic)
    first = _attempt(clinic, _command(source, 2))
    extension = ExtendFollowupV2.model_validate(
        {
            "appointment_id": first["id"],
            "extended_deadline": (source["day"] + timedelta(days=9)).isoformat(),
            "reason": "Synthetic reviewed extension",
            "expected_source_decision_revision": 0,
            "expected_visit_revision": 0,
            "expected_followup_revision": 1,
            "expected_claim_revision": 1,
            "expected_authorization_revision": 0,
            "expected_appointment_revision": 0,
        }
    )
    extension_written = Event()
    release_extension = Event()
    booking_started = Event()
    extension_pid = []
    booking_pid = []

    def doctor_extend():
        with transaction(clinic.runtime, clinic.tenant) as connection:
            extension_pid.append(connection.scalar(text("SELECT pg_backend_pid()")))
            _doctor_context(connection, UUID(clinic.owner.staff_id))
            result = extend_followup(
                connection,
                _actor(clinic.tenant, UUID(clinic.owner.staff_id)),
                source["visit"],
                extension,
                str(uuid4()),
            )
            extension_written.set()
            assert release_extension.wait(timeout=20)
            return result

    def booking_attempt():
        with transaction(clinic.runtime, clinic.tenant) as connection:
            booking_pid.append(connection.scalar(text("SELECT pg_backend_pid()")))
            booking_started.set()
            _doctor_context(connection, UUID(clinic.owner.staff_id))
            return book_appointment(
                connection,
                _actor(clinic.tenant, UUID(clinic.owner.staff_id)),
                _command(source, 3),
                str(uuid4()),
            )

    with ThreadPoolExecutor(max_workers=2) as pool:
        extending = pool.submit(doctor_extend)
        assert extension_written.wait(timeout=15)
        competing = pool.submit(booking_attempt)
        try:
            assert booking_started.wait(timeout=15)
            with transaction(clinic.runtime, clinic.tenant) as observer:
                _wait_for_postgres_lock(observer, booking_pid[0], extension_pid[0])
        finally:
            release_extension.set()
        extended = extending.result(timeout=30)
        assert (
            extended[0]["extended_deadline"] == extension.extended_deadline.isoformat()
        )
        with pytest.raises(ContractError) as denied:
            competing.result(timeout=30)
        assert denied.value.status_code == 409
        assert denied.value.code is None
    assert _claim_count(clinic, source) == 1


@pytest.mark.parametrize(
    "source_first", [True, False], ids=["source-lower", "target-lower"]
)
def test_extension_locks_source_and_target_by_uuid_before_claim(
    followup_clinic, monkeypatch, source_first
):
    clinic = followup_clinic
    chosen_id = UUID(
        "00000000-0000-4000-8000-000000000001"
        if source_first
        else "ffffffff-ffff-4fff-bfff-ffffffffffff"
    )
    controlled_ids = iter([chosen_id])
    with monkeypatch.context() as scoped:
        scoped.setattr(
            appointment_persistence,
            "uuid4",
            lambda: next(controlled_ids, None) or uuid4(),
        )
        source = _source_for(clinic)
    first = _attempt(clinic, _command(source))
    source_id, target_id = source["appointment"], UUID(first["id"])
    assert source_id == chosen_id
    assert (source_id < target_id) is source_first
    before = booking_effects(clinic)
    extension = ExtendFollowupV2.model_validate(
        {
            "appointment_id": first["id"],
            "extended_deadline": (source["day"] + timedelta(days=9)).isoformat(),
            "reason": "Ordered pair lock witness",
            "expected_source_decision_revision": 0,
            "expected_visit_revision": 0,
            "expected_followup_revision": 1,
            "expected_claim_revision": 1,
            "expected_authorization_revision": 0,
            "expected_appointment_revision": 0,
        }
    )
    started = Event()
    extension_pid = []

    def extend():
        with transaction(clinic.runtime, clinic.tenant) as connection:
            extension_pid.append(connection.scalar(text("SELECT pg_backend_pid()")))
            started.set()
            _doctor_context(connection, UUID(clinic.owner.staff_id))
            return extend_followup(
                connection,
                _actor(clinic.tenant, UUID(clinic.owner.staff_id)),
                source["visit"],
                extension,
                str(uuid4()),
            )

    lock_row = text("""
        SELECT id FROM public.appointments
        WHERE organization_id=:tenant AND id=:appointment FOR UPDATE
    """)
    first_id, second_id = sorted((source_id, target_id))
    with ThreadPoolExecutor(max_workers=1) as pool:
        with clinic.migration.begin() as second_holder:
            assert (
                second_holder.execute(
                    lock_row, {"tenant": clinic.tenant, "appointment": second_id}
                ).scalar_one()
                == second_id
            )
            second_pid = second_holder.scalar(text("SELECT pg_backend_pid()"))
            with clinic.migration.begin() as first_holder:
                assert (
                    first_holder.execute(
                        lock_row, {"tenant": clinic.tenant, "appointment": first_id}
                    ).scalar_one()
                    == first_id
                )
                first_pid = first_holder.scalar(text("SELECT pg_backend_pid()"))
                competing = pool.submit(extend)
                assert started.wait(timeout=15)
                with clinic.migration.connect() as observer:
                    _wait_for_postgres_lock(observer, extension_pid[0], first_pid)
            with clinic.migration.connect() as observer:
                _wait_for_postgres_lock(observer, extension_pid[0], second_pid)
                assert (
                    observer.scalar(
                        text("""
                        SELECT count(*) FROM public.followup_authorization_events
                        WHERE organization_id=:tenant AND appointment_id=:appointment
                    """),
                        {"tenant": clinic.tenant, "appointment": target_id},
                    )
                    == 0
                )
        extended, revision = competing.result(timeout=30)

    assert revision == 1
    assert extended["extended_deadline"] == extension.extended_deadline.isoformat()
    assert booking_effects(clinic) == before
    assert _claim_count(clinic, source) == 1
    with clinic.migration.connect() as connection:
        assert connection.execute(
            text("""
                SELECT kind, source_visit_id, appointment_id, actor_staff_id,
                       actor_role, authorization_revision, new_deadline, reason
                FROM public.followup_authorization_events
                WHERE organization_id=:tenant AND id=:event
            """),
            {"tenant": clinic.tenant, "event": UUID(extended["event_id"])},
        ).one() == (
            "EXTENSION",
            source["visit"],
            target_id,
            UUID(clinic.owner.staff_id),
            "DOCTOR_OWNER",
            1,
            extension.extended_deadline,
            extension.reason,
        )


def test_source_lock_is_acquired_after_day_counter_on_confirmation(followup_clinic):
    clinic = followup_clinic
    source = _source_for(clinic)
    before = booking_effects(clinic)
    command = _command(source)
    actor = _actor(clinic.tenant, UUID(clinic.owner.staff_id))
    observed = Event()
    pid = []

    def book():
        with transaction(clinic.runtime, clinic.tenant) as connection:
            pid.append(connection.scalar(text("SELECT pg_backend_pid()")))
            observed.set()
            _doctor_context(connection, actor.staff_id)
            return book_appointment(connection, actor, command, str(uuid4()))

    with ThreadPoolExecutor(max_workers=1) as pool:
        with transaction(clinic.runtime, clinic.tenant) as holder:
            held = holder.scalar(
                text("""
                    SELECT id FROM public.appointments
                    WHERE organization_id=:tenant AND id=:source FOR UPDATE
                """),
                {"tenant": clinic.tenant, "source": source["appointment"]},
            )
            assert held == source["appointment"]
            holder_pid = holder.scalar(text("SELECT pg_backend_pid()"))
            future = pool.submit(book)
            assert observed.wait(timeout=15)
            if future.done():
                future.result(timeout=0)
            with transaction(clinic.runtime, clinic.tenant) as observer:
                _wait_for_postgres_lock(observer, pid[0], holder_pid)
            # A blocked booking has not made any committed number/claim/receipt.
            assert booking_effects(clinic) == before
        saved = future.result(timeout=30)
    assert saved["service_type"] == "FOLLOW_UP"
    assert _claim_count(clinic, source) == 1


def test_timed_followup_rescans_capacity_after_waiting_for_day_allocator(
    followup_clinic,
):
    clinic = followup_clinic
    sources = [_source_for(clinic), _source_for(clinic)]
    day = max(source["day"] for source in sources) + timedelta(days=2)
    weekday = day.isoweekday()
    actor = _actor(clinic.tenant, UUID(clinic.owner.staff_id))
    with transaction(clinic.runtime, clinic.tenant) as connection:
        _publish(
            connection,
            actor,
            _config(
                mode="EXACT_TIME",
                working_weekdays=[weekday],
                weekly_work_periods=[
                    {"weekday": weekday, "starts_at": "09:00", "ends_at": "12:00"}
                ],
            ),
            revision=3,
        )
        slot = candidates_for_date(
            load_current_schedule(connection, clinic.tenant), day
        )[0]
    commands = [
        _command(
            source,
            (day - source["day"]).days,
            starts_at=slot.starts_at.isoformat(),
            ends_at=slot.ends_at.isoformat(),
            expected_schedule_revision=4,
        )
        for source in sources
    ]
    before = booking_effects(clinic)
    first_confirmed = Event()
    allow_first_commit = Event()
    second_started = Event()
    first_pid = []
    second_pid = []

    def first_booking():
        with transaction(clinic.runtime, clinic.tenant) as connection:
            first_pid.append(connection.scalar(text("SELECT pg_backend_pid()")))
            _doctor_context(connection, actor.staff_id)
            saved = book_appointment(connection, actor, commands[0], str(uuid4()))
            first_confirmed.set()
            assert allow_first_commit.wait(timeout=20)
            return saved

    def competing_booking():
        with transaction(clinic.runtime, clinic.tenant) as connection:
            second_pid.append(connection.scalar(text("SELECT pg_backend_pid()")))
            second_started.set()
            _doctor_context(connection, actor.staff_id)
            return book_appointment(connection, actor, commands[1], str(uuid4()))

    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(first_booking)
        assert first_confirmed.wait(timeout=15)
        second = pool.submit(competing_booking)
        try:
            assert second_started.wait(timeout=15)
            with transaction(clinic.runtime, clinic.tenant) as observer:
                _wait_for_postgres_lock(observer, second_pid[0], first_pid[0])
            assert booking_effects(clinic) == before
        finally:
            allow_first_commit.set()
        booked = first.result(timeout=30)
        with pytest.raises(ContractError) as denied:
            second.result(timeout=30)

    assert booked["service_type"] == "FOLLOW_UP"
    assert denied.value.status_code == 409
    assert booking_effects(clinic) == tuple(count + 1 for count in before)
    assert [_claim_count(clinic, source) for source in sources] == [1, 0]
    with clinic.migration.connect() as connection:
        assert (
            connection.scalar(
                text("""
                SELECT last_number FROM public.appointment_day_counters
                WHERE organization_id=:tenant AND service_date=:day
            """),
                {"tenant": clinic.tenant, "day": day},
            )
            == 1
        )


@pytest.mark.parametrize(
    "policy_first", [True, False], ids=["policy-first", "booking-first"]
)
def test_followup_policy_publication_serializes_with_preferential_booking(
    followup_clinic, policy_first
):
    clinic = followup_clinic
    source = _source_for(clinic)
    command = _command(source)
    actor = _actor(clinic.tenant, UUID(clinic.owner.staff_id))
    before = booking_effects(clinic)
    holder_ready = Event()
    waiter_started = Event()
    release_holder = Event()
    publication_pid = []
    booking_pid = []
    frozen_query = text("""
        SELECT c.state, c.approved_deadline, c.policy_snapshot,
               p.entitlement_provenance, p.net_egp
        FROM public.followup_claims c
        JOIN public.appointment_price_snapshots p
          ON (p.organization_id, p.appointment_id) =
             (c.organization_id, c.appointment_id)
        WHERE c.organization_id=:tenant AND c.appointment_id=:appointment
    """)

    def publish(hold):
        with transaction(clinic.runtime, clinic.tenant) as connection:
            _doctor_context(connection, actor.staff_id)
            publication_pid.append(connection.scalar(text("SELECT pg_backend_pid()")))
            if not hold:
                waiter_started.set()
            revision = append_version(
                connection,
                clinic.tenant,
                "FOLLOWUP",
                {"completion_day_zero": True, "last_day_inclusive": 10},
                1,
                actor_staff_id=str(actor.staff_id),
            )
            if hold:
                holder_ready.set()
                assert release_holder.wait(timeout=20)
            return revision

    def book(hold):
        with transaction(clinic.runtime, clinic.tenant) as connection:
            _doctor_context(connection, actor.staff_id)
            booking_pid.append(connection.scalar(text("SELECT pg_backend_pid()")))
            if not hold:
                waiter_started.set()
            saved = book_appointment(connection, actor, command, str(uuid4()))
            frozen = connection.execute(
                frozen_query,
                {"tenant": clinic.tenant, "appointment": UUID(saved["id"])},
            ).one()
            if hold:
                holder_ready.set()
                assert release_holder.wait(timeout=20)
            return saved, frozen

    with ThreadPoolExecutor(max_workers=2) as pool:
        holder = pool.submit(publish if policy_first else book, True)
        try:
            assert holder_ready.wait(timeout=15)
            waiter = pool.submit(book if policy_first else publish, False)
            assert waiter_started.wait(timeout=15)
            with transaction(clinic.runtime, clinic.tenant) as observer:
                _wait_for_postgres_lock(
                    observer,
                    booking_pid[0] if policy_first else publication_pid[0],
                    publication_pid[0] if policy_first else booking_pid[0],
                )
        finally:
            release_holder.set()
        if policy_first:
            assert holder.result(timeout=30) == 2
            with pytest.raises(ContractError) as denied:
                waiter.result(timeout=30)
            assert denied.value.status_code == 409
            assert denied.value.code == "STALE_REVISION"
        else:
            saved, frozen = holder.result(timeout=30)
            assert waiter.result(timeout=30) == 2
            assert saved["service_type"] == "FOLLOW_UP"
            assert frozen.state == "ACTIVE"
            assert frozen.approved_deadline == source["day"] + timedelta(days=7)
            assert frozen.policy_snapshot["policy_revision"] == 1

    with transaction(clinic.runtime, clinic.tenant) as connection:
        assert connection.execute(
            text("""
                SELECT current_version, revision FROM public.clinic_policy_heads
                WHERE organization_id=:tenant AND kind='FOLLOWUP'
            """),
            {"tenant": clinic.tenant},
        ).one() == (2, 2)
        if not policy_first:
            assert (
                connection.execute(
                    frozen_query,
                    {"tenant": clinic.tenant, "appointment": UUID(saved["id"])},
                ).one()
                == frozen
            )
    assert _claim_count(clinic, source) == (0 if policy_first else 1)
    assert booking_effects(clinic) == (
        before if policy_first else tuple(count + 1 for count in before)
    )

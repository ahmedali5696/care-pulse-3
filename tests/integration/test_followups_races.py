"""B-10 booking, source-revision and claim races on distinct PostgreSQL sessions."""

from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from threading import Event
from uuid import UUID, uuid4

import pytest
from carepulse.appointments.book import book_appointment
from carepulse.appointments.booking_contracts import BookAppointment
from carepulse.db import transaction
from carepulse.identity.contracts import ContractError
from carepulse.visits.eligibility import extend_followup, record_followup_decision
from carepulse.visits.eligibility_contracts import (
    ExtendFollowupV2,
    FollowupDecisionPatchV2,
)
from sqlalchemy import text
from test_booking import booking_body, booking_effects
from test_booking import booking_clinic as booking_clinic  # noqa: PLC0414
from test_booking_races import _concurrent, _wait_for_postgres_lock
from test_followup_persistence import _outcome, _v2_source
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
    with transaction(clinic.runtime, clinic.tenant) as connection:
        claim_id = connection.scalar(
            text("""
                SELECT id FROM public.followup_claims
                WHERE organization_id=:tenant AND appointment_id=:appointment
            """),
            {"tenant": clinic.tenant, "appointment": UUID(first["id"])},
        )
    changed = Event()
    finish = Event()
    replacement_started = Event()
    terminal_pid = []
    replacement_pid = []

    def terminate():
        with transaction(clinic.runtime, clinic.tenant) as connection:
            terminal_pid.append(connection.scalar(text("SELECT pg_backend_pid()")))
            _doctor_context(connection, UUID(clinic.owner.staff_id))
            _outcome(
                connection,
                clinic.tenant,
                UUID(clinic.owner.staff_id),
                {"id": UUID(first["id"]), "claim": claim_id, "saved": first},
                status=terminal,
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

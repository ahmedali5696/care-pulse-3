"""D-08 real PostgreSQL writers contend for one preferential source."""

import json
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from threading import Barrier, Event
from time import monotonic, sleep
from uuid import UUID, uuid4

import pytest
from carepulse.appointments.persistence import persist_appointment
from carepulse.db import transaction
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from test_appointment_persistence import _admit_saved, _draft, _query
from test_appointment_persistence import appointment_storage as appointment_storage  # noqa: PLC0414
from test_database import admin as admin  # noqa: PLC0414
from test_database import disposable_project as disposable_project  # noqa: PLC0414
from test_database import empty_database as empty_database  # noqa: PLC0414
from test_followup_persistence import (
    _claim,
    _insert_claim,
    _outcome,
    _provenance,
    _source,
    _v2_booking,
    _v2_command,
    _v2_record,
    _v2_source,
)
from test_followup_persistence import legacy_followup_storage as legacy_followup_storage  # noqa: PLC0414
from test_followup_persistence import (
    provenance_followup_storage as provenance_followup_storage,  # noqa: PLC0414
)


def _wait_for_pg_blocker(observer, pid):
    until = monotonic() + 3
    while monotonic() < until:
        blockers = observer.scalar(text("SELECT pg_blocking_pids(:pid)"), {"pid": pid})
        if blockers:
            return blockers
        sleep(0.01)
    raise AssertionError("Contender did not reach a PostgreSQL lock wait")


@pytest.mark.parametrize(
    "first", ["decision", "extension", "restriction", "release", "consume"]
)
def test_v2_waiting_writer_refreshes_source_claim_and_empty_authorization_ledger(
    request, first
):
    storage = request.getfixturevalue(
        "provenance_followup_storage"
        if first in ("release", "consume")
        else "appointment_storage"
    )
    runtime, migration, (tenant, _), (doctor, _) = storage
    with transaction(runtime, tenant) as connection:
        source = _v2_source(connection, tenant, doctor)
        claim = _v2_command(
            connection, "create_followup_claim", source, **_v2_booking(source)
        )
        record = _v2_record(connection, claim)
    held, release, started = Event(), Event(), Event()
    waiting_pid = []
    expected = {
        "source_revision": 0,
        "clinical_revision": 0,
        "policy_version": 1,
        "authorization_revision": 0,
        "claim_revision": 1,
        "appointment_revision": 0,
    }

    def extension(connection):
        return _v2_command(
            connection,
            "extend_followup_claim",
            source,
            reference=claim,
            expected=expected,
            deadline_date=(source["day"] + timedelta(days=9)).isoformat(),
            reason="Synthetic concurrent extension",
        )

    def hold_first():
        with transaction(runtime, tenant) as connection:
            if first in ("decision", "restriction"):
                _v2_command(
                    connection,
                    "record_followup_source_decision",
                    source,
                    **(
                        {"kind": "DEFAULT"}
                        if first == "decision"
                        else {"kind": "OPTED_OUT", "reason": "Doctor restriction"}
                    ),
                )
            elif first == "extension":
                extension(connection)
            else:
                _outcome(
                    connection,
                    tenant,
                    doctor,
                    record,
                    status="Completed" if first == "consume" else "Cancelled",
                )
            held.set()
            assert release.wait(timeout=15)

    def wait_second():
        assert held.wait(timeout=10)
        with transaction(runtime, tenant) as connection:
            waiting_pid.append(connection.scalar(text("SELECT pg_backend_pid()")))
            started.set()
            if first == "decision":
                return _v2_command(
                    connection,
                    "record_followup_source_decision",
                    source,
                    kind="DEFAULT",
                )
            return extension(connection)

    with ThreadPoolExecutor(max_workers=2) as pool:
        holder = pool.submit(hold_first)
        waiter = pool.submit(wait_second)
        try:
            assert started.wait(timeout=10)
            with migration.connect() as observer:
                assert _wait_for_pg_blocker(observer, waiting_pid[0])
        finally:
            release.set()
        holder.result(timeout=20)
        with pytest.raises(DBAPIError) as stale:
            waiter.result(timeout=20)
        assert stale.value.orig.sqlstate == "40001"
    with transaction(runtime, tenant) as connection:
        assert connection.scalar(
            text(
                "SELECT count(*) FROM public.followup_authorization_events WHERE claim_id=:claim"
            ),
            {"claim": claim},
        ) == (1 if first == "extension" else 0)
        assert (
            connection.scalar(
                text(
                    "SELECT count(*) FROM public.clinical_revisions WHERE visit_id=:visit"
                ),
                {"visit": source["visit"]},
            )
            == 0
        )


@pytest.mark.parametrize("later", ["restriction", "release", "consume"])
def test_v2_extension_first_remains_historical_after_later_restriction_or_terminal(
    request, later
):
    storage = request.getfixturevalue(
        "provenance_followup_storage"
        if later in ("release", "consume")
        else "appointment_storage"
    )
    runtime, migration, (tenant, _), (doctor, _) = storage
    with transaction(runtime, tenant) as connection:
        source = _v2_source(connection, tenant, doctor)
        claim = _v2_command(
            connection, "create_followup_claim", source, **_v2_booking(source)
        )
        record = _v2_record(connection, claim)
    held, release, started = Event(), Event(), Event()
    waiting_pid = []

    def extend_first():
        with transaction(runtime, tenant) as connection:
            _v2_command(
                connection,
                "extend_followup_claim",
                source,
                reference=claim,
                expected={
                    "source_revision": 0,
                    "clinical_revision": 0,
                    "policy_version": 1,
                    "authorization_revision": 0,
                    "claim_revision": 1,
                    "appointment_revision": 0,
                },
                deadline_date=(source["day"] + timedelta(days=9)).isoformat(),
                reason="Synthetic extension first",
            )
            held.set()
            assert release.wait(timeout=15)

    def follow_later():
        assert held.wait(timeout=10)
        with transaction(runtime, tenant) as connection:
            connection.execute(text("SET LOCAL lock_timeout='5s'"))
            connection.execute(text("SET LOCAL statement_timeout='15s'"))
            waiting_pid.append(connection.scalar(text("SELECT pg_backend_pid()")))
            started.set()
            if later == "restriction":
                _v2_command(
                    connection,
                    "record_followup_source_decision",
                    source,
                    kind="OPTED_OUT",
                    reason="Later restriction",
                )
            else:
                _outcome(
                    connection,
                    tenant,
                    doctor,
                    record,
                    status="Completed" if later == "consume" else "Cancelled",
                )

    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(extend_first)
        second = pool.submit(follow_later)
        try:
            assert started.wait(timeout=10)
            with migration.connect() as observer:
                assert _wait_for_pg_blocker(observer, waiting_pid[0])
        finally:
            release.set()
        first.result(timeout=20)
        second.result(timeout=20)
    with transaction(runtime, tenant) as connection:
        assert connection.execute(
            text("SELECT state, revision FROM public.followup_claims WHERE id=:claim"),
            {"claim": claim},
        ).one() == (
            {"restriction": "ACTIVE", "release": "RELEASED", "consume": "CONSUMED"}[
                later
            ],
            1 if later == "restriction" else 2,
        )
        assert (
            connection.scalar(
                text(
                    "SELECT count(*) FROM public.followup_authorization_events WHERE claim_id=:claim"
                ),
                {"claim": claim},
            )
            == 1
        )


@pytest.mark.parametrize("status", ["Cancelled", "Completed"])
def test_v2_claim_waits_for_release_and_cannot_reuse_consumed_source(
    provenance_followup_storage, status
):
    runtime, migration, (tenant, _), (doctor, _) = provenance_followup_storage
    with transaction(runtime, tenant) as connection:
        source = _v2_source(connection, tenant, doctor)
        claim = _v2_command(
            connection, "create_followup_claim", source, **_v2_booking(source)
        )
        record = _v2_record(connection, claim)
    held, release, started = Event(), Event(), Event()
    waiting_pid = []

    def finish():
        with transaction(runtime, tenant) as connection:
            _outcome(connection, tenant, doctor, record, status=status)
            held.set()
            assert release.wait(timeout=15)

    def rebook():
        assert held.wait(timeout=10)
        with transaction(runtime, tenant) as connection:
            waiting_pid.append(connection.scalar(text("SELECT pg_backend_pid()")))
            started.set()
            return _v2_command(
                connection, "create_followup_claim", source, **_v2_booking(source, 2)
            )

    with ThreadPoolExecutor(max_workers=2) as pool:
        terminal = pool.submit(finish)
        contender = pool.submit(rebook)
        try:
            assert started.wait(timeout=10)
            with migration.connect() as observer:
                assert _wait_for_pg_blocker(observer, waiting_pid[0])
        finally:
            release.set()
        terminal.result(timeout=20)
        if status == "Completed":
            with pytest.raises(DBAPIError) as consumed:
                contender.result(timeout=20)
            assert consumed.value.orig.sqlstate == "23505"
        else:
            assert contender.result(timeout=20) != claim
    with transaction(runtime, tenant) as connection:
        rows = (
            connection.execute(
                text("""
            SELECT c.state FROM public.followup_claims c WHERE c.source_visit_id=:visit ORDER BY c.state
        """),
                {"visit": source["visit"]},
            )
            .scalars()
            .all()
        )
        assert rows == (
            ["CONSUMED"] if status == "Completed" else ["ACTIVE", "RELEASED"]
        )
        assert connection.scalar(
            text("""
            SELECT count(*) FROM public.appointments WHERE organization_id=:tenant
              AND patient_id=(SELECT patient_id FROM public.visits WHERE id=:visit)
              AND service_type='FOLLOW_UP'
        """),
            {"tenant": tenant, "visit": source["visit"]},
        ) == len(rows)


def _seed(runtime, tenant, doctor):
    with transaction(runtime, tenant) as connection:
        return _source(connection, tenant, doctor)


def _claims(runtime, tenant, source):
    with transaction(runtime, tenant) as connection:
        rows = connection.execute(
            text("""
                SELECT c.id, c.appointment_id, c.state, c.revision,
                       c.terminal_history_revision, c.terminal_at,
                       a.status, a.service_date, h.new_status AS history_status,
                       h.occurred_at AS history_at
                FROM public.followup_claims c
                JOIN public.appointments a
                  ON a.organization_id=c.organization_id AND a.id=c.appointment_id
                LEFT JOIN public.appointment_history h
                  ON h.organization_id=c.organization_id AND h.appointment_id=c.appointment_id
                 AND h.appointment_revision=c.terminal_history_revision
                WHERE c.organization_id=:tenant AND c.source_visit_id=:visit
            """),
            {"tenant": tenant, "visit": source["visit"]},
        ).all()
        assert sum(row.state == "ACTIVE" for row in rows) <= 1
        assert sum(row.state == "CONSUMED" for row in rows) <= 1
        assert not (
            any(row.state == "ACTIVE" for row in rows)
            and any(row.state == "CONSUMED" for row in rows)
        )
        for row in rows:
            if row.state == "ACTIVE":
                assert row.status in ("Booked", "Waiting", "In Examination")
                assert row.revision == 1
                assert row.terminal_history_revision is row.terminal_at is None
            else:
                assert row.revision == 2
                assert row.status == row.history_status
                assert row.terminal_at == row.history_at
                assert (row.state == "CONSUMED") == (row.status == "Completed")
        assert connection.scalar(
            text("""
                SELECT count(*) FROM public.appointments
                WHERE organization_id=:tenant AND patient_id=:patient
                  AND service_type='FOLLOW_UP'
            """),
            {"tenant": tenant, "patient": source["patient"]},
        ) == len(rows)
        return rows


def test_two_simultaneous_claims_leave_one_active_and_no_orphan_appointment(
    legacy_followup_storage,
):
    runtime, _, (tenant, _), (doctor, _) = legacy_followup_storage
    source = _seed(runtime, tenant, doctor)
    barrier = Barrier(2)

    def attempt(day_offset):
        with transaction(runtime, tenant) as connection:
            barrier.wait(timeout=15)
            return _claim(connection, tenant, source, day_offset=day_offset)

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(attempt, offset) for offset in (1, 2)]
        outcomes = [future.exception() or future.result() for future in futures]
    assert sum(isinstance(value, dict) for value in outcomes) == 1, outcomes
    errors = [value for value in outcomes if isinstance(value, DBAPIError)]
    assert len(errors) == 1 and errors[0].orig.sqlstate == "23505"
    rows = _claims(runtime, tenant, source)
    assert len(rows) == 1 and rows[0].state == "ACTIVE"


@pytest.mark.parametrize("status", ["Cancelled", "No-show", "Completed"])
def test_terminal_first_holds_claim_until_release_or_permanent_consumption(
    legacy_followup_storage, status
):
    runtime, _, (tenant, _), (doctor, _) = legacy_followup_storage
    source = _seed(runtime, tenant, doctor)
    with transaction(runtime, tenant) as connection:
        existing = _claim(connection, tenant, source)
    written, commit = Event(), Event()

    def finish():
        with transaction(runtime, tenant) as connection:
            _outcome(connection, tenant, doctor, existing, status=status)
            written.set()
            assert commit.wait(timeout=20)

    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(finish)
        try:
            assert written.wait(timeout=15)
            with (
                pytest.raises(DBAPIError) as blocked,
                transaction(runtime, tenant) as connection,
            ):
                connection.execute(text("SET LOCAL lock_timeout = '1s'"))
                _claim(connection, tenant, source, day_offset=2)
            assert blocked.value.orig.sqlstate == "55P03"
        finally:
            commit.set()
        future.result(timeout=30)

    if status == "Completed":
        with (
            pytest.raises(DBAPIError) as denied,
            transaction(runtime, tenant) as connection,
        ):
            _claim(connection, tenant, source, day_offset=2)
        assert denied.value.orig.sqlstate == "23505"
    else:
        with transaction(runtime, tenant) as connection:
            _claim(connection, tenant, source, day_offset=2)
    rows = _claims(runtime, tenant, source)
    expected = ["CONSUMED"] if status == "Completed" else ["ACTIVE", "RELEASED"]
    assert sorted(row.state for row in rows) == expected


@pytest.mark.parametrize("status", ["Cancelled", "Completed"])
def test_claim_attempt_first_respects_later_terminal_outcome(
    legacy_followup_storage, status
):
    runtime, _, (tenant, _), (doctor, _) = legacy_followup_storage
    source = _seed(runtime, tenant, doctor)
    with transaction(runtime, tenant) as connection:
        existing = _claim(connection, tenant, source)
    attempted, terminal_committed = Event(), Event()

    def compete():
        with transaction(runtime, tenant) as connection:
            with pytest.raises(DBAPIError) as denied, connection.begin_nested():
                _claim(connection, tenant, source, day_offset=2)
            assert denied.value.orig.sqlstate == "23505"
            attempted.set()
            assert terminal_committed.wait(timeout=20)
            if status == "Completed":
                with (
                    pytest.raises(DBAPIError) as denied_again,
                    connection.begin_nested(),
                ):
                    _claim(connection, tenant, source, day_offset=2)
                assert denied_again.value.orig.sqlstate == "23505"
                return None
            return _claim(connection, tenant, source, day_offset=2)

    def finish():
        assert attempted.wait(timeout=15)
        with transaction(runtime, tenant) as connection:
            _outcome(connection, tenant, doctor, existing, status=status)
        terminal_committed.set()

    with ThreadPoolExecutor(max_workers=2) as pool:
        contender = pool.submit(compete)
        terminal = pool.submit(finish)
        try:
            terminal.result(timeout=30)
            fresh = contender.result(timeout=30)
        finally:
            terminal_committed.set()

    with (
        pytest.raises(DBAPIError) as immutable_appointment,
        transaction(runtime, tenant) as connection,
    ):
        connection.execute(
            text("""
                UPDATE public.appointments SET status='Booked', revision=revision+1
                WHERE organization_id=:tenant AND id=:appointment
            """),
            {"tenant": tenant, "appointment": existing["id"]},
        )
    assert immutable_appointment.value.orig.sqlstate == "23514"
    with (
        pytest.raises(DBAPIError) as immutable_claim,
        transaction(runtime, tenant) as connection,
    ):
        connection.execute(
            text("""
                UPDATE public.followup_claims SET state='ACTIVE', revision=revision+1
                WHERE organization_id=:tenant AND id=:claim
            """),
            {"tenant": tenant, "claim": existing["claim"]},
        )
    assert immutable_claim.value.orig.sqlstate == "23514"
    rows = _claims(runtime, tenant, source)
    assert len(rows) == (1 if status == "Completed" else 2)
    terminal_row = next(row for row in rows if row.appointment_id == existing["id"])
    assert terminal_row.status == status
    assert terminal_row.state == ("CONSUMED" if status == "Completed" else "RELEASED")
    if status == "Completed":
        assert fresh is None
    else:
        assert fresh is not None
        assert any(
            row.appointment_id == fresh["id"] and row.state == "ACTIVE" for row in rows
        )


def _tentative_booking_evidence(connection, tenant, appointment, receipt_key):
    return tuple(
        connection.execute(
            text("""
                SELECT
                    (SELECT count(*) FROM public.appointments
                     WHERE organization_id=:tenant AND id=:appointment),
                    (SELECT count(*) FROM public.appointment_number_assignments
                     WHERE organization_id=:tenant AND appointment_id=:appointment),
                    (SELECT count(*) FROM public.appointment_price_snapshots
                     WHERE organization_id=:tenant AND appointment_id=:appointment),
                    (SELECT count(*) FROM public.appointment_history
                     WHERE organization_id=:tenant AND appointment_id=:appointment),
                    (SELECT count(*) FROM public.sensitive_audit
                     WHERE organization_id=:tenant AND correlation_id=:correlation),
                    (SELECT count(*) FROM public.outbox_events
                     WHERE organization_id=:tenant AND aggregate_id=:correlation),
                    (SELECT count(*) FROM public.idempotency_receipts
                     WHERE organization_id=:tenant AND idempotency_key=:receipt_key),
                    (SELECT count(*) FROM public.followup_claims
                     WHERE organization_id=:tenant AND appointment_id=:appointment)
            """),
            {
                "tenant": tenant,
                "appointment": appointment,
                "correlation": str(appointment),
                "receipt_key": receipt_key,
            },
        ).one()
    )


@pytest.mark.parametrize("status", ["Cancelled", "Completed"])
def test_claim_first_staged_booking_contends_with_terminal_before_commit(
    legacy_followup_storage, status
):
    runtime, _, (tenant, _), (doctor, _) = legacy_followup_storage
    source = _seed(runtime, tenant, doctor)
    with transaction(runtime, tenant) as connection:
        existing = _claim(connection, tenant, source)
        if status == "Completed":
            # The terminal writer must not need to insert a Visit while A holds
            # the patient's natural new-booking lock.
            _admit_saved(connection, tenant, doctor, existing["saved"])

    target_day = source["day"] + timedelta(days=2)
    with transaction(runtime, tenant) as connection:
        counter_before = connection.scalar(
            text("""
                SELECT last_number FROM public.appointment_day_counters
                WHERE organization_id=:tenant AND service_date=:day
            """),
            {"tenant": tenant, "day": target_day},
        )

    staged = {}
    booking_written, terminal_written = Event(), Event()
    release_terminal, terminal_committed = Event(), Event()

    def compete():
        with transaction(runtime, tenant) as connection:
            connection.execute(
                text("SELECT set_config('app.staff_id', :doctor, true)"),
                {"doctor": str(doctor)},
            )
            provenance = _provenance(source)
            query = _query(tenant, doctor)
            saved = persist_appointment(
                connection,
                _draft(
                    source["patient"],
                    doctor,
                    service_date=target_day,
                    service_type="FOLLOW_UP",
                    price_snapshot={
                        "base_egp": "100.00",
                        "net_egp": "100.00",
                        "entitlement_provenance": provenance,
                    },
                ),
                query,
            )
            appointment = UUID(saved["id"])
            staged.update(appointment=appointment, receipt_key=query.idempotency_key)
            expected_pending = (1, 1, 1, 1, 1, 1, 1, 0)
            assert (
                _tentative_booking_evidence(
                    connection, tenant, appointment, query.idempotency_key
                )
                == expected_pending
            )
            booking_written.set()
            try:
                assert terminal_written.wait(timeout=20)
                connection.execute(text("SET LOCAL lock_timeout = '1s'"))
                with pytest.raises(DBAPIError) as blocked, connection.begin_nested():
                    _insert_claim(connection, tenant, source, appointment, provenance)
                assert blocked.value.orig.sqlstate == "55P03"
                assert not release_terminal.is_set()
                # Rolling back the claim INSERT savepoint must retain A's
                # uncommitted booking, snapshot, history, audit and receipt.
                assert (
                    _tentative_booking_evidence(
                        connection, tenant, appointment, query.idempotency_key
                    )
                    == expected_pending
                )
            finally:
                release_terminal.set()

            assert terminal_committed.wait(timeout=20)
            if status == "Cancelled":
                _insert_claim(connection, tenant, source, appointment, provenance)
                return appointment
            with pytest.raises(DBAPIError) as denied, connection.begin_nested():
                _insert_claim(connection, tenant, source, appointment, provenance)
            assert denied.value.orig.sqlstate == "23505"
            # Commit must reject the still-unpaired tentative Follow-up,
            # rolling back all D-05 evidence rather than leaving an orphan.

    def finish():
        assert booking_written.wait(timeout=20)
        with transaction(runtime, tenant) as connection:
            _outcome(
                connection,
                tenant,
                doctor,
                existing,
                status=status,
                pre_admitted=status == "Completed",
            )
            terminal_written.set()
            assert release_terminal.wait(timeout=20)
        terminal_committed.set()

    with ThreadPoolExecutor(max_workers=2) as pool:
        contender = pool.submit(compete)
        terminal = pool.submit(finish)
        try:
            try:
                terminal.result(timeout=35)
            except AssertionError:
                if not booking_written.is_set():
                    contender.result(timeout=35)
                raise
            if status == "Completed":
                failure = contender.exception(timeout=35)
                if failure is not None and not isinstance(failure, DBAPIError):
                    raise failure
                assert isinstance(failure, DBAPIError)
                assert failure.orig.sqlstate == "23514"
            else:
                assert contender.result(timeout=35) == staged["appointment"]
        finally:
            release_terminal.set()

    rows = _claims(runtime, tenant, source)
    assert sorted(row.state for row in rows) == (
        ["CONSUMED"] if status == "Completed" else ["ACTIVE", "RELEASED"]
    )
    assert (
        next(row for row in rows if row.appointment_id == existing["id"]).status
        == status
    )
    with transaction(runtime, tenant) as connection:
        connection.execute(
            text("SELECT set_config('app.staff_id', :doctor, true)"),
            {"doctor": str(doctor)},
        )
        assert _tentative_booking_evidence(
            connection, tenant, staged["appointment"], staged["receipt_key"]
        ) == ((0,) * 8 if status == "Completed" else (1,) * 8)
        if status == "Completed":
            assert (
                connection.scalar(
                    text("""
                    SELECT last_number FROM public.appointment_day_counters
                    WHERE organization_id=:tenant AND service_date=:day
                """),
                    {"tenant": tenant, "day": target_day},
                )
                == counter_before
            )


def _extend(
    connection, tenant, source, record, *, audit_correlation=None, deadline_offset=8
):
    connection.execute(
        text("SELECT set_config('app.staff_id', :doctor, true)"),
        {"doctor": str(source["doctor"])},
    )
    connection.execute(
        text("SELECT set_config('app.clinical_change_kind', 'EXTENSION', true)")
    )
    connection.execute(
        text("SELECT set_config('app.clinical_reason', 'Synthetic extension', true)")
    )
    connection.execute(
        text("""
            UPDATE public.visits SET followup_decision=CAST(:decision AS jsonb)
            WHERE organization_id=:tenant AND id=:visit
        """),
        {
            "decision": json.dumps(
                {
                    "kind": "DEFAULT",
                    "eligible": True,
                    "extension_claim_id": str(record["claim"]),
                    "extension_evidence_id": str(uuid4()),
                    "extension_deadline": (
                        source["day"] + timedelta(days=deadline_offset)
                    ).isoformat(),
                }
            ),
            "tenant": tenant,
            "visit": source["visit"],
        },
    )
    revision = connection.execute(
        text("""
        SELECT id, revision, occurred_at FROM public.clinical_revisions
            WHERE organization_id=:tenant AND visit_id=:visit
            ORDER BY revision DESC LIMIT 1
        """),
        {"tenant": tenant, "visit": source["visit"]},
    ).one()
    audit_id = uuid4()
    connection.execute(
        text("""
            INSERT INTO public.clinical_audit
                (id, organization_id, doctor_staff_id, clinical_revision, correlation_id)
            VALUES (:id, :tenant, :doctor, :revision, :correlation)
        """),
        {
            "id": audit_id,
            "tenant": tenant,
            "doctor": source["doctor"],
            "revision": revision.revision,
            "correlation": audit_correlation or str(revision.id),
        },
    )
    connection.execute(
        text("""
            INSERT INTO public.followup_extensions (
                id, organization_id, source_visit_id, claim_id, appointment_id,
                prior_deadline, extended_deadline, issuing_doctor_staff_id,
                reason, issued_at, expected_source_revision, expected_claim_revision,
                clinical_revision_id, clinical_audit_id
            ) VALUES (
                :id, :tenant, :visit, :claim, :appointment, :prior, :extended,
                :doctor, 'Synthetic extension', :issued_at,
                :source_revision, 1, :clinical, :audit
            )
        """),
        {
            "id": uuid4(),
            "tenant": tenant,
            "visit": source["visit"],
            "claim": record["claim"],
            "appointment": record["id"],
            "prior": source["day"] + timedelta(days=7),
            "extended": source["day"] + timedelta(days=deadline_offset),
            "doctor": source["doctor"],
            "source_revision": revision.revision - 1,
            "clinical": revision.id,
            "audit": audit_id,
            "issued_at": revision.occurred_at,
        },
    )


def test_extension_and_competing_claim_serialize_without_new_reservation(
    legacy_followup_storage,
):
    runtime, _, (tenant, _), (doctor, _) = legacy_followup_storage
    source = _seed(runtime, tenant, doctor)
    with transaction(runtime, tenant) as connection:
        existing = _claim(connection, tenant, source)
    barrier = Barrier(2)

    def extend():
        with transaction(runtime, tenant) as connection:
            barrier.wait(timeout=15)
            _extend(connection, tenant, source, existing)
        return "extended"

    def compete():
        with transaction(runtime, tenant) as connection:
            barrier.wait(timeout=15)
            return _claim(connection, tenant, source, day_offset=2)

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(extend), pool.submit(compete)]
        outcomes = [future.exception() or future.result() for future in futures]
    assert "extended" in outcomes, outcomes
    errors = [value for value in outcomes if isinstance(value, DBAPIError)]
    # The contender may observe the source revision before or after extension.
    assert len(errors) == 1 and errors[0].orig.sqlstate in ("23505", "23514")
    rows = _claims(runtime, tenant, source)
    assert len(rows) == 1 and rows[0].state == "ACTIVE"
    with transaction(runtime, tenant) as connection:
        assert connection.scalar(
            text("""
                SELECT max(extended_deadline) FROM public.followup_extensions
                WHERE organization_id=:tenant AND claim_id=:claim
            """),
            {"tenant": tenant, "claim": existing["claim"]},
        ) == source["day"] + timedelta(days=8)
        assert (
            connection.scalar(
                text("""
                SELECT count(*) FROM public.followup_extensions
                WHERE organization_id=:tenant AND claim_id=:claim
            """),
                {"tenant": tenant, "claim": existing["claim"]},
            )
            == 1
        )


def test_extension_rejects_unrelated_clinical_audit_evidence(legacy_followup_storage):
    runtime, _, (tenant, _), (doctor, _) = legacy_followup_storage
    source = _seed(runtime, tenant, doctor)
    with transaction(runtime, tenant) as connection:
        record = _claim(connection, tenant, source)
        with pytest.raises(DBAPIError) as denied, connection.begin_nested():
            _extend(connection, tenant, source, record, audit_correlation=str(uuid4()))
        assert denied.value.orig.sqlstate == "23514"
        assert (
            connection.scalar(
                text(
                    "SELECT count(*) FROM public.followup_extensions WHERE claim_id=:id"
                ),
                {"id": record["claim"]},
            )
            == 0
        )


def test_deadline_day_is_inclusive_but_next_day_loses_parallel_race(
    legacy_followup_storage,
):
    runtime, _, (tenant, _), (doctor, _) = legacy_followup_storage
    source = _seed(runtime, tenant, doctor)
    barrier = Barrier(2)

    def attempt(offset):
        with transaction(runtime, tenant) as connection:
            barrier.wait(timeout=15)
            return _claim(connection, tenant, source, day_offset=offset)

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(attempt, offset) for offset in (7, 8)]
        outcomes = [future.exception() or future.result() for future in futures]
    assert isinstance(outcomes[0], dict), outcomes
    assert isinstance(outcomes[1], DBAPIError)
    assert outcomes[1].orig.sqlstate == "23514"
    rows = _claims(runtime, tenant, source)
    assert len(rows) == 1
    assert rows[0].state == "ACTIVE"
    assert rows[0].service_date == source["day"] + timedelta(days=7)

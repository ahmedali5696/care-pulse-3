"""T010: real PostgreSQL check-in races and operation-scope denials."""

from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from datetime import date
from threading import Event
from uuid import UUID, uuid4

import pytest
from carepulse.identity.contracts import ContractError
from carepulse.patients.contracts import ArchivePatient
from carepulse.patients.service import PatientTarget, archive_patient
from carepulse.queue.checkin import check_in_appointment
from carepulse.queue.contracts import CheckInAppointment
from postgres_locks import wait_for_postgres_lock
from runtime_identity_fixture import signed_test_transaction
from sqlalchemy import event, text
from sqlalchemy.exc import DBAPIError
from test_booking import booking_clinic as booking_clinic  # noqa: PLC0414
from test_booking import booking_request
from test_database import admin as admin  # noqa: PLC0414
from test_database import disposable_project as disposable_project  # noqa: PLC0414
from test_database import empty_database as empty_database  # noqa: PLC0414
from test_followups import completed_source, followup_body
from test_followups import followup_clinic as followup_clinic  # noqa: PLC0414
from test_patients import patient_backend as patient_backend  # noqa: PLC0414
from test_request_boundary import boundary_data as boundary_data  # noqa: PLC0414
from test_schedule import SERVICE_DAY
from test_w3_checkin import _book, _new_patient


@contextmanager
def _checkin(clinic, actor, appointment_id, command, key):
    with signed_test_transaction(
        clinic,
        actor,
        method="POST",
        path=f"/v1/appointments/{appointment_id}/check-in",
        purpose="QUEUE_CHECK_IN",
        body=command.model_dump(mode="json", exclude_unset=True),
        key=key,
    ) as (connection, authorized):
        yield (
            connection,
            lambda: check_in_appointment(
                connection, authorized, appointment_id, command, key
            ),
        )


@contextmanager
def _archive(clinic, patient_id):
    command = ArchivePatient(
        reason="CON-41 synthetic archive", expected_lifecycle_revision=0
    )
    with signed_test_transaction(
        clinic,
        clinic.owner,
        method="POST",
        path=f"/v1/patients/{patient_id}/archive",
        purpose="PATIENT_REGISTRY",
        body=command.model_dump(mode="json"),
    ) as (connection, actor):
        yield (
            connection,
            lambda: archive_patient(
                connection, PatientTarget(actor, patient_id), command
            ),
        )


def _ordered_race(
    clinic, holder_transaction, waiter_transaction, denial=None, *, checkpoint=None
):
    """Hold the first command until PostgreSQL proves the second waits at F-01."""
    ready = Event()
    waiter_pid = []

    def observe(connection, _cursor, statement, _params, _context, _many):
        # F-01 acquires Organization FOR UPDATE before the domain command runs.
        if "SELECT public.f01_activate()" in statement:
            waiter_pid.append(connection.connection.driver_connection.info.backend_pid)
            ready.set()

    def compete():
        with waiter_transaction() as (connection, execute):
            assert connection.scalar(text("SELECT session_user")) == "carepulse_api"
            return execute()

    with ThreadPoolExecutor(max_workers=1) as pool:
        with holder_transaction() as (holder, execute):
            assert holder.scalar(text("SELECT session_user")) == "carepulse_api"
            holder_pid = holder.scalar(text("SELECT pg_backend_pid()"))
            first = execute()
            event.listen(clinic.runtime, "before_cursor_execute", observe)
            try:
                future = pool.submit(compete)
                assert ready.wait(timeout=15), "Waiter did not reach F-01 activation"
                assert waiter_pid[0] != holder_pid
                with clinic.migration.connect() as observer:
                    wait_for_postgres_lock(observer, waiter_pid[0], holder_pid)
                    if checkpoint is not None:
                        checkpoint(observer)
            finally:
                event.remove(clinic.runtime, "before_cursor_execute", observe)
        if denial is not None:
            with pytest.raises(ContractError) as denied:
                future.result(timeout=15)
            assert denied.value.status_code == 409
            if denial == "STALE_REVISION":
                assert denied.value.code == denial
            return first, None
        return first, future.result(timeout=15)


def _assert_checkin_effects(clinic, appointment, winner, key, projection):
    appointment_id = UUID(appointment["id"])
    params = {"t": clinic.tenant, "a": appointment_id}
    actor_id = UUID(winner.staff_id)
    with clinic.migration.connect() as conn:
        assert conn.execute(
            text("""
                SELECT status, service_date, current_number, revision
                FROM public.appointments WHERE organization_id=:t AND id=:a
            """),
            params,
        ).one() == (
            "Waiting",
            date.fromisoformat(appointment["service_date"]),
            appointment["number"],
            1,
        )
        assert conn.execute(
            text("""
                SELECT action, appointment_revision, actor_staff_id, actor_role
                FROM public.appointment_history
                WHERE organization_id=:t AND appointment_id=:a
                ORDER BY appointment_revision
            """),
            params,
        ).all() == [
            ("CREATED", 0, UUID(clinic.owner.staff_id), "DOCTOR_OWNER"),
            ("CHECKED_IN", 1, actor_id, winner.role),
        ]
        assert conn.execute(
            text("""
                SELECT e.id, e.disposition, e.service_date, e.lane,
                       e.arrival_sequence, e.routine_rank, e.revision,
                       e.attested_by_staff_id, d.revision, d.arrival_counter, d.routine_cursor
                FROM public.queue_entries e
                JOIN public.queue_days d ON d.organization_id=e.organization_id
                                        AND d.id=e.queue_day_id
                WHERE e.organization_id=:t AND e.appointment_id=:a
            """),
            params,
        ).one() == (
            UUID(projection["queue_entry_id"]),
            "ACTIVE",
            date.fromisoformat(appointment["service_date"]),
            "MAIN",
            1,
            1,
            1,
            actor_id,
            1,
            1,
            0,
        )
        assert conn.execute(
            text("""
                SELECT actor_staff_id, actor_role, after_state->>'queue_entry_id'
                FROM public.sensitive_audit
                WHERE organization_id=:t AND action='appointments.check_in'
            """),
            params,
        ).all() == [(actor_id, winner.role, projection["queue_entry_id"])]
        assert conn.execute(
            text("""
                SELECT aggregate_id, payload->>'queue_entry_id', payload->>'arrival_sequence'
                FROM public.outbox_events
                WHERE organization_id=:t AND event_type='PATIENT_CHECKED_IN'
            """),
            params,
        ).all() == [(str(appointment_id), projection["queue_entry_id"], "1")]
        assert conn.execute(
            text("""
                SELECT staff_id, idempotency_key, result_revision, result_projection
                FROM public.idempotency_receipts
                WHERE organization_id=:t AND operation='appointments.check_in'
            """),
            params,
        ).all() == [(actor_id, key, 1, projection)]
        assert (
            conn.scalar(
                text(
                    "SELECT count(*) FROM public.visits WHERE organization_id=:t AND appointment_id=:a"
                ),
                params,
            )
            == 0
        )


@pytest.mark.parametrize(
    "conflict",
    [
        "duplicate",
        "duplicate-different-actor-same-key",
        "replay-same-actor-same-key",
        "stale-queue-revision",
        "stale-appointment-revision",
    ],
)
def test_two_connection_checkin_race_commits_once(booking_clinic, conflict):
    clinic = booking_clinic
    appointment = _book(
        clinic, clinic.owner, _new_patient(clinic, clinic.owner, "Race Winner")
    )
    other = (
        _book(clinic, clinic.owner, _new_patient(clinic, clinic.owner, "Race Loser"))
        if conflict.startswith("stale-")
        else appointment
    )
    appointment_id, other_id = UUID(appointment["id"]), UUID(other["id"])
    command = CheckInAppointment(
        physical_presence_confirmed=True,
        expected_appointment_revision=0,
        expected_queue_day_revision=0 if conflict == "stale-queue-revision" else None,
    )
    competing_command = (
        CheckInAppointment(
            physical_presence_confirmed=True, expected_appointment_revision=1
        )
        if conflict == "stale-appointment-revision"
        else command
    )
    first_key = str(uuid4())
    second_key = first_key if conflict.endswith("same-key") else str(uuid4())
    replay = conflict == "replay-same-actor-same-key"
    result, competing_result = _ordered_race(
        clinic,
        lambda: _checkin(clinic, clinic.owner, appointment_id, command, first_key),
        lambda: _checkin(
            clinic,
            clinic.owner if replay else clinic.receptionist,
            other_id,
            competing_command,
            second_key,
        ),
        denial=(
            None
            if replay
            else "STALE_REVISION"
            if conflict.startswith("stale-")
            else "CONFLICT"
        ),
    )
    if replay:
        assert competing_result == result
    assert result[0]["status"] == "Waiting" and result[1] == 1
    _assert_checkin_effects(clinic, appointment, clinic.owner, first_key, result[0])

    with clinic.migration.connect() as conn:
        assert (
            conn.scalar(
                text(
                    "SELECT count(*) FROM public.queue_entries WHERE organization_id=:t"
                ),
                {"t": clinic.tenant},
            )
            == 1
        )
        assert (
            conn.scalar(
                text("SELECT count(*) FROM public.queue_days WHERE organization_id=:t"),
                {"t": clinic.tenant},
            )
            == 1
        )
        assert (
            conn.scalar(
                text("SELECT count(*) FROM public.visits WHERE organization_id=:t"),
                {"t": clinic.tenant},
            )
            == 0
        )
        if conflict.startswith("stale-"):
            params = {"t": clinic.tenant, "a": other_id}
            assert conn.execute(
                text(
                    "SELECT status, revision, current_number FROM public.appointments WHERE organization_id=:t AND id=:a"
                ),
                params,
            ).one() == ("Booked", 0, other["number"])
            assert conn.scalars(
                text(
                    "SELECT action FROM public.appointment_history WHERE organization_id=:t AND appointment_id=:a"
                ),
                params,
            ).all() == ["CREATED"]


@pytest.mark.parametrize("archive_first", [True, False])
def test_con41_archive_vs_existing_id_checkin_both_orders(
    followup_clinic, archive_first
):
    clinic = followup_clinic
    source = completed_source(clinic)
    status, reply = booking_request(
        clinic, clinic.owner, body=followup_body(source, offset=1), key=str(uuid4())
    )
    assert status == 201, reply
    appointment = reply["data"]
    appointment_id, patient_id = UUID(appointment["id"]), source["patient"]
    params = {
        "t": clinic.tenant,
        "a": appointment_id,
        "p": patient_id,
        "patient": str(patient_id),
    }
    claim_sql = text("""
        SELECT to_jsonb(c) FROM public.followup_claims c
        WHERE organization_id=:t AND appointment_id=:a
    """)

    # Snapshot the committed domain/evidence rows, including the completed source.
    # The observer must see the same state while the winner is still uncommitted.
    def state(conn):
        return {
            table: conn.scalar(
                text(f"""
                    SELECT jsonb_agg(to_jsonb(r) ORDER BY to_jsonb(r))
                    FROM public.{table} r WHERE organization_id=:t
                """),
                params,
            )
            for table in (
                "patients",
                "appointments",
                "appointment_day_counters",
                "appointment_number_assignments",
                "appointment_price_snapshots",
                "appointment_history",
                "queue_days",
                "queue_entries",
                "visits",
                "followup_claims",
                "sensitive_audit",
                "outbox_events",
                "idempotency_receipts",
            )
        }

    with clinic.migration.connect() as conn:
        claim_before = conn.execute(claim_sql, params).scalar_one()
        assert claim_before["state"] == "ACTIVE"
        before = state(conn)

    command = CheckInAppointment(
        physical_presence_confirmed=True, expected_appointment_revision=0
    )
    key = str(uuid4())

    def checkin():
        return _checkin(clinic, clinic.receptionist, appointment_id, command, key)

    def archive():
        return _archive(clinic, patient_id)

    def checkpoint(observer):
        assert state(observer) == before

    if archive_first:
        archived, checked = _ordered_race(
            clinic, archive, checkin, checkpoint=checkpoint
        )
    else:
        checked, archived = _ordered_race(
            clinic, checkin, archive, checkpoint=checkpoint
        )

    assert archived["id"] == str(patient_id)
    assert archived["archived"] is True
    assert archived["lifecycle_revision"] == 1
    assert checked[0]["status"] == "Waiting" and checked[1] == 1
    assert checked[0]["appointment_id"] == str(appointment_id)
    _assert_checkin_effects(clinic, appointment, clinic.receptionist, key, checked[0])
    with clinic.migration.connect() as conn:
        assert conn.execute(
            text(
                "SELECT archived, lifecycle_revision FROM public.patients WHERE organization_id=:t AND id=:p"
            ),
            params,
        ).one() == (True, 1)
        assert conn.execute(claim_sql, params).scalar_one() == claim_before
        assert conn.execute(
            text("""
                SELECT actor_staff_id, actor_role, before_state->>'patient_id',
                       before_state->>'archived', before_state->>'lifecycle_revision',
                       after_state->>'archived', after_state->>'lifecycle_revision',
                       reason, occurred_at IS NOT NULL
                FROM public.sensitive_audit
                WHERE organization_id=:t AND action='patients.archive'
                      AND after_state->>'patient_id'=:patient
            """),
            params,
        ).all() == [
            (
                UUID(clinic.owner.staff_id),
                "DOCTOR_OWNER",
                str(patient_id),
                "false",
                "0",
                "true",
                "1",
                "CON-41 synthetic archive",
                True,
            )
        ]
        # B-05 archive is revision-guarded and audited; it has no receipt contract.
        assert (
            conn.scalar(
                text(
                    "SELECT count(*) FROM public.idempotency_receipts WHERE organization_id=:t AND operation='patients.archive'"
                ),
                params,
            )
            == 0
        )
        committed = state(conn)

    for table in (
        "appointment_day_counters",
        "appointment_number_assignments",
        "appointment_price_snapshots",
        "followup_claims",
        "visits",
    ):
        assert committed[table] == before[table]
    for table, added in (
        ("sensitive_audit", 2),
        ("outbox_events", 1),
        ("idempotency_receipts", 1),
        ("queue_entries", 1),
        ("appointment_history", 1),
    ):
        previous = before[table] or []
        current = committed[table] or []
        assert len(current) == len(previous) + added
        assert all(row in current for row in previous)

    # Denied fresh commands must leave both winners' immutable evidence intact.
    with pytest.raises(ContractError) as denied, archive() as (_conn, execute):
        execute()
    assert denied.value.status_code == 409
    assert denied.value.code == "STALE_REVISION"
    with (
        pytest.raises(ContractError) as denied,
        _checkin(
            clinic, clinic.receptionist, appointment_id, command, str(uuid4())
        ) as (_conn, execute),
    ):
        execute()
    assert denied.value.status_code == 409

    with clinic.migration.connect() as conn:
        assert state(conn) == committed


def test_operation_scope_db_denials_for_queue_tables(booking_clinic) -> None:
    clinic = booking_clinic
    patient_id = _new_patient(clinic, clinic.owner, "Scope Denial")
    appt = _book(clinic, clinic.owner, patient_id)

    with (
        pytest.raises(DBAPIError) as denied,
        signed_test_transaction(
            clinic,
            clinic.owner,
            method="GET",
            path=f"/v1/appointments/{appt['id']}",
            purpose="APPOINTMENT_BOOKING",
        ) as (conn, _actor),
    ):
        conn.execute(
            text("""
                INSERT INTO public.queue_days
                    (id, organization_id, doctor_staff_id, service_date, lane,
                     revision, queue_policy_kind, queue_policy_version, arrival_counter, routine_cursor)
                VALUES (:id, :t, :d, :day, 'MAIN', 1, 'QUEUE', 1, 0, 0)
            """),
            {
                "id": uuid4(),
                "t": clinic.tenant,
                "d": UUID(clinic.owner.staff_id),
                "day": SERVICE_DAY,
            },
        )
    assert denied.value.orig.sqlstate == "42501"

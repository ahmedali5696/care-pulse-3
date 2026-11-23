"""T010: B-12 duplicate check-in, stale-revision, CON-41 archive/check-in races, and scope denials."""

from concurrent.futures import ThreadPoolExecutor
from threading import Event
from uuid import UUID, uuid4

import pytest
from carepulse.identity.contracts import ContractError
from carepulse.patients.contracts import LifecycleAction
from carepulse.patients.service import set_archived
from carepulse.queue.checkin import check_in_appointment
from carepulse.queue.contracts import CheckInAppointment
from postgres_locks import wait_for_postgres_lock
from runtime_identity_fixture import signed_test_transaction
from sqlalchemy import event, text
from sqlalchemy.exc import DBAPIError
from test_booking import booking_clinic as booking_clinic  # noqa: PLC0414
from test_database import admin as admin  # noqa: PLC0414
from test_database import disposable_project as disposable_project  # noqa: PLC0414
from test_database import empty_database as empty_database  # noqa: PLC0414
from test_patients import create_patient
from test_patients import patient_backend as patient_backend  # noqa: PLC0414
from test_request_boundary import boundary_data as boundary_data  # noqa: PLC0414
from test_schedule import SERVICE_DAY
from test_w3_checkin import _book


def _pid(connection) -> int:
    return int(connection.scalar(text("SELECT pg_backend_pid()")))


def test_two_connection_duplicate_checkin_race_commits_once(booking_clinic) -> None:
    clinic = booking_clinic
    patient_id = create_patient(clinic, clinic.owner, name="Dup Race Patient")
    appt = _book(clinic, clinic.owner, patient_id)
    appt_id = UUID(appt["id"])

    holder_ready = Event()
    release_holder = Event()
    waiter_started = Event()
    pids: dict[str, int] = {}

    def first_checkin():
        with signed_test_transaction(
            clinic,
            clinic.owner,
            method="POST",
            path=f"/v1/appointments/{appt_id}/check-in",
            purpose="QUEUE_CHECK_IN",
            body={
                "physical_presence_confirmed": True,
                "expected_appointment_revision": 0,
            },
            key=f"dup-a-{uuid4().hex}",
        ) as (conn, actor):
            pids["holder"] = _pid(conn)

            def _after_execute(_conn, _clause, _multiparams, _params, _ctx, _res):
                if (
                    not holder_ready.is_set()
                    and "FOR UPDATE" in str(_clause)
                    and "patients" in str(_clause)
                ):
                    holder_ready.set()
                    release_holder.wait(timeout=15)

            event.listen(conn, "after_cursor_execute", _after_execute)
            try:
                return check_in_appointment(
                    conn,
                    actor,
                    appt_id,
                    CheckInAppointment(
                        physical_presence_confirmed=True,
                        expected_appointment_revision=0,
                    ),
                    f"dup-a-{uuid4().hex}",
                )
            finally:
                event.remove(conn, "after_cursor_execute", _after_execute)

    def second_checkin():
        assert holder_ready.wait(timeout=15)
        with signed_test_transaction(
            clinic,
            clinic.receptionist,
            method="POST",
            path=f"/v1/appointments/{appt_id}/check-in",
            purpose="QUEUE_CHECK_IN",
            body={
                "physical_presence_confirmed": True,
                "expected_appointment_revision": 0,
            },
            key=f"dup-b-{uuid4().hex}",
        ) as (conn, actor):
            pids["waiter"] = _pid(conn)
            waiter_started.set()
            return check_in_appointment(
                conn,
                actor,
                appt_id,
                CheckInAppointment(
                    physical_presence_confirmed=True,
                    expected_appointment_revision=0,
                ),
                f"dup-b-{uuid4().hex}",
            )

    with ThreadPoolExecutor(max_workers=2) as pool:
        f1 = pool.submit(first_checkin)
        f2 = pool.submit(second_checkin)
        assert holder_ready.wait(timeout=15)
        assert waiter_started.wait(timeout=15)
        with clinic.migration.connect() as observer:
            wait_for_postgres_lock(observer, pids["waiter"], pids["holder"])
        release_holder.set()
        res1 = f1.result(timeout=15)
        with pytest.raises(ContractError) as denied:
            f2.result(timeout=15)

    assert res1[0]["status"] == "Waiting"
    assert denied.value.status_code == 409

    with clinic.migration.begin() as conn:
        assert (
            conn.scalar(
                text(
                    "SELECT count(*) FROM public.queue_entries WHERE organization_id=:t AND appointment_id=:a AND disposition='ACTIVE'"
                ),
                {"t": clinic.tenant, "a": appt_id},
            )
            == 1
        )
        assert (
            conn.scalar(
                text(
                    "SELECT count(*) FROM public.outbox_events WHERE organization_id=:t AND aggregate_id=:a AND event_type='PATIENT_CHECKED_IN'"
                ),
                {"t": clinic.tenant, "a": str(appt_id)},
            )
            == 1
        )
        assert (
            conn.scalar(
                text(
                    "SELECT count(*) FROM public.visits WHERE organization_id=:t AND appointment_id=:a"
                ),
                {"t": clinic.tenant, "a": appt_id},
            )
            == 0
        )


@pytest.mark.parametrize("first_actor", ["archive_first", "checkin_first"])
def test_con41_archive_vs_existing_id_checkin_both_orders(
    booking_clinic, first_actor: str
) -> None:
    """CON-41: Race B-05 ArchivePatient with B-12 CheckIn in both PostgreSQL lock orders."""
    clinic = booking_clinic
    patient_id_str = create_patient(clinic, clinic.owner, name=f"CON41 {first_actor}")
    patient_id = UUID(patient_id_str)
    appt = _book(clinic, clinic.owner, patient_id_str)
    appt_id = UUID(appt["id"])

    holder_ready = Event()
    release_holder = Event()
    waiter_started = Event()
    pids: dict[str, int] = {}

    checkin_key = f"con41-checkin-{uuid4().hex}"
    archive_cmd = LifecycleAction(
        reason="CON-41 synthetic archive", expected_lifecycle_revision=0
    )
    checkin_cmd = CheckInAppointment(
        physical_presence_confirmed=True, expected_appointment_revision=0
    )

    def run_archive(*, is_holder: bool):
        if not is_holder:
            assert holder_ready.wait(timeout=15)
        with signed_test_transaction(
            clinic,
            clinic.owner,
            method="POST",
            path=f"/v1/patients/{patient_id}/archive",
            purpose="PATIENT_REGISTRY",
            body=archive_cmd.model_dump(mode="json"),
        ) as (conn, actor):
            pids["holder" if is_holder else "waiter"] = _pid(conn)
            if not is_holder:
                waiter_started.set()
            else:

                def _hold(_c, clause, _mp, _p, _ctx, _res):
                    if (
                        not holder_ready.is_set()
                        and "patients" in str(clause)
                        and "FOR UPDATE" in str(clause)
                    ):
                        holder_ready.set()
                        release_holder.wait(timeout=15)

                event.listen(conn, "after_cursor_execute", _hold)
            try:
                return set_archived(conn, actor, patient_id, archive_cmd, True)
            finally:
                if is_holder:
                    event.remove(conn, "after_cursor_execute", _hold)

    def run_checkin(*, is_holder: bool):
        if not is_holder:
            assert holder_ready.wait(timeout=15)
        with signed_test_transaction(
            clinic,
            clinic.receptionist,
            method="POST",
            path=f"/v1/appointments/{appt_id}/check-in",
            purpose="QUEUE_CHECK_IN",
            body=checkin_cmd.model_dump(mode="json", exclude_unset=True),
            key=checkin_key,
        ) as (conn, actor):
            pids["holder" if is_holder else "waiter"] = _pid(conn)
            if not is_holder:
                waiter_started.set()
            else:

                def _hold(_c, clause, _mp, _p, _ctx, _res):
                    if (
                        not holder_ready.is_set()
                        and "patients" in str(clause)
                        and "FOR UPDATE" in str(clause)
                    ):
                        holder_ready.set()
                        release_holder.wait(timeout=15)

                event.listen(conn, "after_cursor_execute", _hold)
            try:
                return check_in_appointment(
                    conn, actor, appt_id, checkin_cmd, checkin_key
                )
            finally:
                if is_holder:
                    event.remove(conn, "after_cursor_execute", _hold)

    with ThreadPoolExecutor(max_workers=2) as pool:
        if first_actor == "archive_first":
            f_arch = pool.submit(run_archive, is_holder=True)
            f_chk = pool.submit(run_checkin, is_holder=False)
        else:
            f_chk = pool.submit(run_checkin, is_holder=True)
            f_arch = pool.submit(run_archive, is_holder=False)

        assert holder_ready.wait(timeout=15)
        assert waiter_started.wait(timeout=15)
        with clinic.migration.connect() as observer:
            wait_for_postgres_lock(observer, pids["waiter"], pids["holder"])
        release_holder.set()

        arch_result = f_arch.result(timeout=15)
        chk_result = f_chk.result(timeout=15)

    assert arch_result["archived"] is True
    assert chk_result[0]["status"] == "Waiting"
    assert chk_result[0]["appointment_id"] == str(appt_id)

    # Assert complete post-commit state in both orders.
    with clinic.migration.begin() as conn:
        patient_row = conn.execute(
            text(
                "SELECT archived, lifecycle_revision FROM public.patients WHERE organization_id=:t AND id=:p"
            ),
            {"t": clinic.tenant, "p": patient_id},
        ).one()
        assert (patient_row.archived, int(patient_row.lifecycle_revision)) == (True, 1)

        appt_row = conn.execute(
            text("""
                SELECT status, service_date, current_number, revision
                FROM public.appointments WHERE organization_id=:t AND id=:a
            """),
            {"t": clinic.tenant, "a": appt_id},
        ).one()
        assert appt_row.status == "Waiting"
        assert appt_row.service_date == SERVICE_DAY
        assert int(appt_row.current_number) == appt["number"]
        assert int(appt_row.revision) == 1

        history_actions = conn.scalars(
            text("""
                SELECT action FROM public.appointment_history
                WHERE organization_id=:t AND appointment_id=:a
                ORDER BY appointment_revision ASC
            """),
            {"t": clinic.tenant, "a": appt_id},
        ).all()
        assert history_actions == ["CREATED", "CHECKED_IN"]

        assert (
            conn.scalar(
                text(
                    "SELECT count(*) FROM public.queue_entries WHERE organization_id=:t AND appointment_id=:a AND disposition='ACTIVE'"
                ),
                {"t": clinic.tenant, "a": appt_id},
            )
            == 1
        )
        assert (
            conn.scalar(
                text(
                    "SELECT count(*) FROM public.sensitive_audit WHERE organization_id=:t AND action='patients.archive' AND correlation_id=:p"
                ),
                {"t": clinic.tenant, "p": str(patient_id)},
            )
            == 1
        )
        assert (
            conn.scalar(
                text(
                    "SELECT count(*) FROM public.sensitive_audit WHERE organization_id=:t AND action='appointments.check_in' AND correlation_id=:a"
                ),
                {"t": clinic.tenant, "a": str(appt_id)},
            )
            == 1
        )
        assert (
            conn.scalar(
                text(
                    "SELECT count(*) FROM public.outbox_events WHERE organization_id=:t AND event_type='PATIENT_CHECKED_IN' AND aggregate_id=:a"
                ),
                {"t": clinic.tenant, "a": str(appt_id)},
            )
            == 1
        )
        assert (
            conn.scalar(
                text(
                    "SELECT count(*) FROM public.idempotency_receipts WHERE organization_id=:t AND operation='appointments.check_in' AND idempotency_key=:k"
                ),
                {"t": clinic.tenant, "k": checkin_key},
            )
            == 1
        )


def test_operation_scope_db_denials_for_queue_tables(booking_clinic) -> None:
    clinic = booking_clinic
    patient_id = create_patient(clinic, clinic.owner, name="Scope Denial")
    appt = _book(clinic, clinic.owner, patient_id)

    # APPOINTMENT_BOOKING purpose cannot directly update appointment to Waiting or write queue_days.
    with (
        signed_test_transaction(
            clinic,
            clinic.owner,
            method="GET",
            path=f"/v1/appointments/{appt['id']}",
            purpose="APPOINTMENT_BOOKING",
        ) as (conn, _actor),
        pytest.raises(DBAPIError),
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

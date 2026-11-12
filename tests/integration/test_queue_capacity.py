"""D-06 DoctorControl and approval SQL simulation; no Visit or admission API."""

import json
from datetime import timedelta
from uuid import UUID, uuid4

import pytest
from carepulse.db import transaction
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from test_appointment_persistence import appointment_storage as appointment_storage  # noqa: PLC0414
from test_queue_persistence import DAY, _appointment, _arrive


def _policy_capacity(connection, tenant, *, capacity=2, paused=False):
    """Synthetic authorized policy upgrade before DoctorControl lock."""
    connection.execute(
        text("""
            INSERT INTO public.clinic_policy_versions
                (organization_id, kind, version, payload)
            VALUES (:tenant, 'CAPACITY', 2,
                jsonb_build_object('capacity', CAST(:capacity_json AS jsonb),
                                   'paused', :paused))
        """),
        {"tenant": tenant, "capacity_json": json.dumps(capacity), "paused": paused},
    )
    connection.execute(
        text("""
            UPDATE public.clinic_policy_heads
            SET current_version = 2, revision = revision + 1
            WHERE organization_id = :tenant AND kind = 'CAPACITY'
        """),
        {"tenant": tenant},
    )
    connection.execute(
        text("""
            UPDATE public.doctor_controls
            SET capacity_policy_version = 2, revision = revision + 1
            WHERE organization_id = :tenant
        """),
        {"tenant": tenant},
    )


def _lock(connection, tenant, doctor, day=DAY, lane="MAIN"):
    return connection.execute(
        text("""
            SELECT * FROM public.lock_doctor_admission_capacity(
                :tenant, :doctor, :day, :lane)
        """),
        {"tenant": tenant, "doctor": doctor, "day": day, "lane": lane},
    ).one()


def _doctor_context(connection, doctor):
    connection.execute(
        text("SELECT set_config('app.staff_id', :doctor, true)"),
        {"doctor": str(doctor)},
    )


def _simulated_exam(connection, tenant, doctor, appointment):
    """D-06 SQL-only state/history/closure simulation, without a Visit."""
    appointment_id = UUID(appointment["id"])
    connection.execute(
        text("""
            UPDATE public.appointments SET status = 'In Examination',
                revision = revision + 1
            WHERE organization_id = :tenant AND id = :appointment
        """),
        {"tenant": tenant, "appointment": appointment_id},
    )
    connection.execute(
        text("""
            INSERT INTO public.appointment_history
                (id, organization_id, appointment_id, appointment_revision,
                 action, old_status, new_status, old_service_date, new_service_date,
                 old_number, new_number, actor_staff_id, actor_role,
                 price_snapshot_appointment_id)
            SELECT :history, a.organization_id, a.id, a.revision, 'STATUS',
                   'Waiting', 'In Examination', a.service_date, a.service_date,
                   a.current_number, a.current_number, :doctor, 'DOCTOR_OWNER', a.id
            FROM public.appointments a
            WHERE a.organization_id = :tenant AND a.id = :appointment
        """),
        {
            "history": uuid4(),
            "tenant": tenant,
            "appointment": appointment_id,
            "doctor": doctor,
        },
    )
    connection.execute(
        text("""
            UPDATE public.queue_entries
            SET disposition='CLOSED', closure_history_revision=1,
                revision=revision+1
            WHERE organization_id=:tenant AND appointment_id=:appointment
              AND disposition='ACTIVE'
        """),
        {"tenant": tenant, "appointment": appointment_id},
    )
    connection.execute(
        text("""
            UPDATE public.queue_days SET revision=revision+1
            WHERE organization_id=:tenant AND id=(
                SELECT queue_day_id FROM public.queue_entries
                WHERE organization_id=:tenant AND appointment_id=:appointment
            )
        """),
        {"tenant": tenant, "appointment": appointment_id},
    )


def _consume(connection, tenant, confirmation, appointment):
    return connection.execute(
        text("""
            UPDATE public.doctor_admission_confirmations
            SET state='CONSUMED', consuming_admission_revision=1
            WHERE organization_id=:tenant AND id=:confirmation
              AND appointment_id=:appointment AND state='ISSUED'
        """),
        {
            "tenant": tenant,
            "confirmation": confirmation,
            "appointment": UUID(appointment["id"]),
        },
    ).rowcount


@pytest.mark.parametrize("saved_capacity", [2, 2.0])
def test_scoped_confirmation_consumes_once_in_rolled_back_sql_simulation(
    appointment_storage,
    saved_capacity,
):
    runtime, _, (tenant, _), (doctor, _) = appointment_storage
    with transaction(runtime, tenant) as connection:
        first = _appointment(connection, tenant, doctor)
        _arrive(connection, tenant, doctor, first)
        second = _appointment(connection, tenant, doctor)
        _arrive(connection, tenant, doctor, second)
        _policy_capacity(connection, tenant, capacity=saved_capacity)
        assert connection.scalar(
            text("""
                SELECT payload->>'capacity' FROM public.clinic_policy_versions
                WHERE organization_id=:tenant AND kind='CAPACITY' AND version=2
            """),
            {"tenant": tenant},
        ) == str(saved_capacity)
    # D-07 requires a matching Visit on COMMIT. Exercise real D-06 SQL
    # transitions and one-use guards inside a transaction that MUST roll back.
    with (
        pytest.raises(RuntimeError, match="rollback simulation"),
        transaction(runtime, tenant) as connection,
    ):
        _doctor_context(connection, doctor)
        assert _lock(connection, tenant, doctor).active_examinations == 0
        _simulated_exam(connection, tenant, doctor, first)
        assert _lock(connection, tenant, doctor).active_examinations == 1
        approval = _issue(connection, tenant, doctor, second, "MAIN", DAY)
        _simulated_exam(connection, tenant, doctor, second)
        assert _consume(connection, tenant, approval, second) == 1
        assert _consume(connection, tenant, approval, second) == 0
        assert connection.execute(
            text("""
                SELECT state, consuming_admission_revision, consumed_at IS NOT NULL
                FROM public.doctor_admission_confirmations
                WHERE organization_id=:tenant AND id=:approval
            """),
            {"tenant": tenant, "approval": approval},
        ).one() == ("CONSUMED", 1, True)
        assert (
            connection.scalar(
                text("""
                SELECT count(*) FROM public.queue_entries
                WHERE organization_id=:tenant AND appointment_id=:appointment
                  AND disposition='CLOSED' AND closure_history_revision=1
            """),
                {"tenant": tenant, "appointment": UUID(second["id"])},
            )
            == 1
        )
        assert (
            connection.scalar(
                text("""
                SELECT count(*) FROM public.appointment_history
                WHERE organization_id=:tenant AND appointment_id=:appointment
                  AND old_status='Waiting' AND new_status='In Examination'
            """),
                {"tenant": tenant, "appointment": UUID(second["id"])},
            )
            == 1
        )
        with pytest.raises(DBAPIError) as reused, connection.begin_nested():
            connection.execute(
                text("""
                    UPDATE public.doctor_admission_confirmations SET state='ISSUED'
                    WHERE organization_id=:tenant AND id=:approval
                """),
                {"tenant": tenant, "approval": approval},
            )
        assert reused.value.orig.sqlstate == "23514"
        with pytest.raises(DBAPIError) as repeated, connection.begin_nested():
            connection.execute(
                text("""
                    UPDATE public.doctor_admission_confirmations SET state='CONSUMED'
                    WHERE organization_id=:tenant AND id=:approval
                """),
                {"tenant": tenant, "approval": approval},
            )
        assert repeated.value.orig.sqlstate == "23514"
        raise RuntimeError("rollback simulation")

    # Observe committed state from a *new* connection: no partial admission.
    with transaction(runtime, tenant) as connection:
        assert (
            connection.scalar(
                text("""
                SELECT count(*) FROM public.doctor_admission_confirmations
                WHERE organization_id=:tenant
            """),
                {"tenant": tenant},
            )
            == 0
        )
        assert (
            connection.scalar(
                text("""
                SELECT count(*) FROM public.appointments
                WHERE organization_id=:tenant AND status='In Examination'
            """),
                {"tenant": tenant},
            )
            == 0
        )
        assert (
            connection.scalar(
                text("""
                SELECT count(*) FROM public.queue_entries
                WHERE organization_id=:tenant AND disposition='ACTIVE'
            """),
                {"tenant": tenant},
            )
            == 2
        )
        assert (
            connection.scalar(
                text("""
                SELECT count(*) FROM public.appointment_history
                WHERE organization_id=:tenant
                  AND old_status='Waiting' AND new_status='In Examination'
            """),
                {"tenant": tenant},
            )
            == 0
        )


def _issue(
    connection,
    tenant,
    doctor,
    appointment,
    lane,
    day,
    *,
    changes=None,
    expiry_minutes=10,
):
    row = connection.execute(
        text("""
            SELECT a.patient_id, a.revision AS appointment_revision,
                   q.revision AS queue_revision, c.revision AS control_revision,
                   ch.revision AS capacity_revision, ah.revision AS admission_revision
            FROM public.appointments a
            JOIN public.queue_days q ON q.organization_id = a.organization_id
                AND q.service_date = :day AND q.lane = :lane
            JOIN public.doctor_controls c ON c.organization_id = a.organization_id
                AND c.doctor_staff_id = :doctor
            JOIN public.clinic_policy_heads ch ON ch.organization_id = a.organization_id
                AND ch.kind = 'CAPACITY'
            JOIN public.clinic_policy_heads ah ON ah.organization_id = a.organization_id
                AND ah.kind = 'ADMISSION'
            WHERE a.organization_id = :tenant AND a.id = :appointment
        """),
        {
            "tenant": tenant,
            "doctor": doctor,
            "appointment": UUID(appointment["id"]),
            "day": day,
            "lane": lane,
        },
    ).one()
    payload = {
        "id": uuid4(),
        "tenant": tenant,
        "doctor": doctor,
        "patient": row.patient_id,
        "appointment": UUID(appointment["id"]),
        "day": day,
        "lane": lane,
        "appointment_revision": row.appointment_revision,
        "queue_revision": row.queue_revision,
        "control_revision": row.control_revision,
        "capacity_revision": row.capacity_revision,
        "admission_revision": row.admission_revision,
        "expiry_minutes": expiry_minutes,
        **(changes or {}),
    }
    connection.execute(
        text("""
            INSERT INTO public.doctor_admission_confirmations
                (id, organization_id, issuing_doctor_staff_id,
                 doctor_staff_id, patient_id, appointment_id, service_date,
                 lane, issued_at, expires_at, appointment_revision,
                 queue_day_revision, control_revision, capacity_revision,
                 admission_revision, state)
            VALUES (:id, :tenant, :doctor, :doctor, :patient, :appointment,
                    :day, :lane, clock_timestamp(),
                    clock_timestamp() + make_interval(mins => :expiry_minutes),
                    :appointment_revision, :queue_revision, :control_revision,
                    :capacity_revision, :admission_revision, 'ISSUED')
        """),
        payload,
    )
    return payload["id"]


def test_one_shared_doctor_control_across_lane_and_original_date(appointment_storage):
    runtime, _, (tenant, _), (doctor, _) = appointment_storage
    with transaction(runtime, tenant) as connection:
        _arrive(connection, tenant, doctor, _appointment(connection, tenant, doctor))
        tomorrow = DAY + timedelta(days=1)
        _arrive(
            connection,
            tenant,
            doctor,
            _appointment(
                connection,
                tenant,
                doctor,
                service_date=tomorrow,
                classification="EMERGENCY",
                lane="EMERGENCY",
            ),
        )
        main = _lock(connection, tenant, doctor)
        emergency = _lock(connection, tenant, doctor, tomorrow, "EMERGENCY")
        assert main.active_examinations == emergency.active_examinations == 0
        assert main.configured_capacity == emergency.configured_capacity == 1
        assert main.paused is emergency.paused is False
        assert (
            connection.scalar(
                text(
                    "SELECT count(*) FROM public.doctor_controls WHERE organization_id=:id"
                ),
                {"id": tenant},
            )
            == 1
        )


def test_doctor_scoped_confirmation_validates_revision_and_one_use_transition(
    appointment_storage,
):
    runtime, _, (tenant, _), (doctor, _) = appointment_storage
    # Without an admission command/Visit this test simulates and rolls back
    # all provisional status changes; it never publishes an invalid encounter.
    with (
        pytest.raises(RuntimeError, match="rollback simulation"),
        transaction(runtime, tenant) as connection,
    ):
        first = _appointment(connection, tenant, doctor)
        _arrive(connection, tenant, doctor, first)
        second = _appointment(connection, tenant, doctor)
        _arrive(connection, tenant, doctor, second)
        _policy_capacity(connection, tenant)
        _doctor_context(connection, doctor)
        assert _lock(connection, tenant, doctor).configured_capacity == 2
        _simulated_exam(connection, tenant, doctor, first)
        assert _lock(connection, tenant, doctor).active_examinations == 1
        approved = _issue(connection, tenant, doctor, second, "MAIN", DAY)
        assert (
            connection.scalar(
                text("""
                    SELECT state FROM public.doctor_admission_confirmations
                    WHERE organization_id=:tenant AND id=:id
                """),
                {"tenant": tenant, "id": approved},
            )
            == "ISSUED"
        )
        with pytest.raises(DBAPIError) as stale, connection.begin_nested():
            _issue(
                connection,
                tenant,
                doctor,
                second,
                "MAIN",
                DAY,
                changes={"queue_revision": 123456},
            )
        assert stale.value.orig.sqlstate == "23514"
        with pytest.raises(DBAPIError) as expired, connection.begin_nested():
            _issue(connection, tenant, doctor, second, "MAIN", DAY, expiry_minutes=0)
        assert expired.value.orig.sqlstate == "23514"
        with pytest.raises(DBAPIError) as forged, connection.begin_nested():
            connection.execute(
                text("""
                        UPDATE public.doctor_admission_confirmations
                        SET state='CONSUMED', consuming_admission_revision=1
                        WHERE organization_id=:tenant AND id=:id
                    """),
                {"tenant": tenant, "id": approved},
            )
        assert forged.value.orig.sqlstate in ("23514", "42501")
        connection.execute(
            text("""
                    UPDATE public.doctor_admission_confirmations
                    SET state='REVOKED', revoked_by_staff_id=:doctor,
                        revoked_at=clock_timestamp(), revoke_reason='Test revoke'
                    WHERE organization_id=:tenant AND id=:id
                """),
            {"tenant": tenant, "id": approved, "doctor": doctor},
        )
        with pytest.raises(DBAPIError) as reused, connection.begin_nested():
            connection.execute(
                text("""
                        UPDATE public.doctor_admission_confirmations SET state='ISSUED'
                        WHERE organization_id=:tenant AND id=:id
                    """),
                {"tenant": tenant, "id": approved},
            )
        assert reused.value.orig.sqlstate == "23514"
        with (
            pytest.raises(DBAPIError) as consumed_after_revoke,
            connection.begin_nested(),
        ):
            connection.execute(
                text("""
                    UPDATE public.doctor_admission_confirmations
                    SET state='CONSUMED', consuming_admission_revision=1
                    WHERE organization_id=:tenant AND id=:id
                """),
                {"tenant": tenant, "id": approved},
            )
        assert consumed_after_revoke.value.orig.sqlstate == "23514"
        raise RuntimeError("rollback simulation")


def test_non_doctor_cannot_revoke_scoped_approval(appointment_storage):
    runtime, _, (tenant, _), (doctor, _) = appointment_storage
    with (
        pytest.raises(RuntimeError, match="rollback simulation"),
        transaction(runtime, tenant) as connection,
    ):
        first, second = (
            _appointment(connection, tenant, doctor),
            _appointment(connection, tenant, doctor),
        )
        _arrive(connection, tenant, doctor, first)
        _arrive(connection, tenant, doctor, second)
        _policy_capacity(connection, tenant)
        _doctor_context(connection, doctor)
        _lock(connection, tenant, doctor)
        _simulated_exam(connection, tenant, doctor, first)
        approval = _issue(connection, tenant, doctor, second, "MAIN", DAY)
        _doctor_context(connection, uuid4())
        with pytest.raises(DBAPIError) as denied, connection.begin_nested():
            connection.execute(
                text("""
                        UPDATE public.doctor_admission_confirmations
                        SET state='REVOKED', revoked_by_staff_id=:doctor,
                            revoked_at=clock_timestamp(), revoke_reason='Forged revoke'
                        WHERE organization_id=:tenant AND id=:approval
                    """),
                {"tenant": tenant, "doctor": doctor, "approval": approval},
            )
        assert denied.value.orig.sqlstate == "42501"
        raise RuntimeError("rollback simulation")

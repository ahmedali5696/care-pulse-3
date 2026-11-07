"""D-06 dated queue persistence on disposable PostgreSQL, independent of Visits."""

import json
from datetime import UTC, date, datetime, timedelta
from uuid import UUID, uuid4
from zoneinfo import ZoneInfo

import pytest
from carepulse.appointments.persistence import persist_appointment
from carepulse.db import transaction
from carepulse.policies.versions import append_version
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from test_appointment_persistence import _draft, _patient, _query
from test_appointment_persistence import appointment_storage as appointment_storage  # noqa: PLC0414

DAY = date(2026, 10, 27)
CAIRO = ZoneInfo("Africa/Cairo")


def _appointment(connection, tenant, doctor, *, status="Waiting", **changes):
    return persist_appointment(
        connection,
        _draft(_patient(connection, tenant), doctor, status=status, **changes),
        _query(tenant, doctor),
    )


def _arrive(
    connection,
    tenant,
    doctor,
    appointment,
    *,
    service_date=None,
    lane=None,
    patient=None,
):
    """Insert attendance evidence only in tests; all scope is row-derived."""
    appointment_id = UUID(appointment["id"])
    row = connection.execute(
        text("""
            SELECT patient_id, service_date, lane, classification
            FROM public.appointments
            WHERE organization_id = :tenant AND id = :appointment
        """),
        {"tenant": tenant, "appointment": appointment_id},
    ).one()
    actual_day = service_date or row.service_date
    actual_lane = lane or row.lane
    day_id, sequence = connection.execute(
        text("""
            INSERT INTO public.queue_days
                (id, organization_id, doctor_staff_id, service_date, lane,
                 queue_policy_version, arrival_counter)
            SELECT :day_id, :tenant, :doctor, :day, :lane, v.version, 1
            FROM public.clinic_policy_versions v
            JOIN public.clinic_policy_heads h
              ON h.organization_id=v.organization_id AND h.kind=v.kind
             AND v.version <= h.current_version
            WHERE v.organization_id = :tenant AND v.kind = 'QUEUE'
              AND NOT v.pending_effective
              AND (v.version = 1 OR
                   (v.effective_on IS NOT NULL AND v.effective_on <= :day))
            ORDER BY v.version DESC LIMIT 1
            ON CONFLICT (organization_id, service_date, lane)
            DO UPDATE SET arrival_counter = public.queue_days.arrival_counter + 1,
                          revision = public.queue_days.revision + 1
            RETURNING id, arrival_counter
        """),
        {
            "day_id": uuid4(),
            "tenant": tenant,
            "doctor": doctor,
            "day": actual_day,
            "lane": actual_lane,
        },
    ).one()
    rank = None
    if actual_lane == "MAIN" and row.classification == "ROUTINE":
        rank = connection.scalar(
            text("""
                SELECT coalesce(max(routine_rank), 0) + 1
                FROM public.queue_entries
                WHERE organization_id = :tenant AND queue_day_id = :day_id
                  AND disposition = 'ACTIVE'
            """),
            {"tenant": tenant, "day_id": day_id},
        )
    entry_id = uuid4()
    connection.execute(
        text("""
            INSERT INTO public.queue_entries
                (id, organization_id, queue_day_id, appointment_id,
                 patient_id, doctor_staff_id, service_date, lane,
                 routine_rank, disposition)
            VALUES (:entry, :tenant, :day_id, :appointment, :patient, :doctor,
                    :day, :lane, :rank, 'ACTIVE')
        """),
        {
            "entry": entry_id,
            "tenant": tenant,
            "day_id": day_id,
            "appointment": appointment_id,
            "patient": patient or row.patient_id,
            "doctor": doctor,
            "day": actual_day,
            "lane": actual_lane,
            "rank": rank,
        },
    )
    return day_id, entry_id, sequence


def test_queue_day_uses_effective_original_date_policy_not_latest_head(
    appointment_storage,
):
    runtime, _, (tenant, _), (doctor, _) = appointment_storage
    with transaction(runtime, tenant) as connection:
        append_version(
            connection,
            str(tenant),
            "SCHEDULE",
            {
                "booking_mode": "OPEN",
                "duration_minutes": None,
                "holiday_booking_enabled": False,
                "routine_walk_in_enabled": False,
                "allow_overlap": False,
                "no_show_cutoff": {"enabled": False, "local_time": None},
                "working_weekdays": [DAY.isoweekday()],
            },
            expected_revision=1,
        )
        assert (
            append_version(
                connection,
                str(tenant),
                "QUEUE",
                {"routine_order": "ARRIVAL_FIFO", "late_depth": 3},
                expected_revision=1,
                today=DAY - timedelta(days=1),
            )
            == 2
        )
        connection.execute(
            text("""
                SELECT 1 FROM public.clinic_policy_versions
                WHERE organization_id=:tenant AND kind='QUEUE'
                  AND version=2 AND effective_on=:day AND NOT pending_effective
            """),
            {"tenant": tenant, "day": DAY},
        ).one()
        append_version(
            connection,
            str(tenant),
            "SCHEDULE",
            {
                "booking_mode": "OPEN",
                "duration_minutes": None,
                "holiday_booking_enabled": False,
                "routine_walk_in_enabled": False,
                "allow_overlap": False,
                "no_show_cutoff": {"enabled": False, "local_time": None},
            },
            expected_revision=2,
        )
        assert (
            append_version(
                connection,
                str(tenant),
                "QUEUE",
                {"routine_order": "BOOKED_NUMBER", "late_depth": 9},
                expected_revision=2,
                today=DAY,
            )
            == 3
        )
        assert (
            connection.scalar(
                text("""
            SELECT pending_effective FROM public.clinic_policy_versions
            WHERE organization_id=:tenant AND kind='QUEUE' AND version=3
        """),
                {"tenant": tenant},
            )
            is True
        )
        old = _appointment(
            connection, tenant, doctor, service_date=DAY - timedelta(days=1)
        )
        old_day, _, _ = _arrive(connection, tenant, doctor, old)
        current = _appointment(connection, tenant, doctor)
        current_day, _, _ = _arrive(connection, tenant, doctor, current)
        future = _appointment(
            connection, tenant, doctor, service_date=DAY + timedelta(days=2)
        )
        future_day, _, _ = _arrive(connection, tenant, doctor, future)
        assert connection.execute(
            text("""
            SELECT service_date, queue_policy_version FROM public.queue_days
            WHERE organization_id=:tenant ORDER BY service_date
        """),
            {"tenant": tenant},
        ).all() == [
            (DAY - timedelta(days=1), 1),
            (DAY, 2),
            (DAY + timedelta(days=2), 2),
        ]
        for wrong_version in (1, 3):
            with pytest.raises(DBAPIError) as denied, connection.begin_nested():
                connection.execute(
                    text("""
                    INSERT INTO public.queue_days
                        (id, organization_id, doctor_staff_id, service_date,
                         lane, queue_policy_version)
                    VALUES (:id, :tenant, :doctor, :day, 'EMERGENCY', :version)
                """),
                    {
                        "id": uuid4(),
                        "tenant": tenant,
                        "doctor": doctor,
                        "day": DAY + timedelta(days=2),
                        "version": wrong_version,
                    },
                )
            assert denied.value.orig.sqlstate == "23514"
        with pytest.raises(DBAPIError) as historical, connection.begin_nested():
            connection.execute(
                text("""
                    INSERT INTO public.queue_days
                        (id, organization_id, doctor_staff_id, service_date,
                         lane, queue_policy_version)
                    VALUES (:id, :tenant, :doctor, :old_day, 'EMERGENCY', 2)
                """),
                {
                    "id": uuid4(),
                    "tenant": tenant,
                    "doctor": doctor,
                    "old_day": DAY - timedelta(days=1),
                },
            )
        assert historical.value.orig.sqlstate == "23514"
        # Unpublished but otherwise valid policy rows must not become a
        # caller-selected snapshot merely because they exist.
        with pytest.raises(DBAPIError) as unpublished, connection.begin_nested():
            connection.execute(
                text("""
                    INSERT INTO public.clinic_policy_versions
                        (organization_id, kind, version, payload, effective_on)
                    VALUES (:tenant, 'QUEUE', 4,
                            '{"routine_order":"ARRIVAL_FIFO","late_depth":8}'::jsonb,
                            :day)
                """),
                {"tenant": tenant, "day": DAY},
            )
            connection.execute(
                text("""
                    INSERT INTO public.queue_days
                        (id, organization_id, doctor_staff_id, service_date,
                         lane, queue_policy_version)
                    VALUES (:id, :tenant, :doctor, :day, 'EMERGENCY', 4)
                """),
                {
                    "id": uuid4(),
                    "tenant": tenant,
                    "doctor": doctor,
                    "day": DAY + timedelta(days=2),
                },
            )
        assert unpublished.value.orig.sqlstate == "23514"
        with pytest.raises(DBAPIError) as immutable, connection.begin_nested():
            connection.execute(
                text("""
                UPDATE public.queue_days
                SET queue_policy_version=1, revision=revision+1
                WHERE organization_id=:tenant AND id=:id
            """),
                {"tenant": tenant, "id": current_day},
            )
        assert immutable.value.orig.sqlstate == "23514"
        assert len({old_day, current_day, future_day}) == 3


def test_rank_change_must_invalidate_queue_revision_at_commit(appointment_storage):
    runtime, _, (tenant, _), (doctor, _) = appointment_storage
    with transaction(runtime, tenant) as connection:
        first = _appointment(connection, tenant, doctor)
        _arrive(connection, tenant, doctor, first)
        second = _appointment(connection, tenant, doctor)
        day_id, entry_id, _ = _arrive(connection, tenant, doctor, second)
        revision = connection.scalar(
            text("SELECT revision FROM public.queue_days WHERE id=:day"),
            {"day": day_id},
        )

    with (
        pytest.raises(DBAPIError) as stale,
        transaction(runtime, tenant) as connection,
    ):
        connection.execute(
            text("""
            UPDATE public.queue_entries
            SET routine_rank=3, revision=revision+1
            WHERE organization_id=:tenant AND id=:entry
        """),
            {"tenant": tenant, "entry": entry_id},
        )
    assert stale.value.orig.sqlstate == "23514"
    with transaction(runtime, tenant) as connection:
        assert (
            connection.scalar(
                text("SELECT revision FROM public.queue_days WHERE id=:day"),
                {"day": day_id},
            )
            == revision
        )
        connection.execute(
            text("""
            UPDATE public.queue_days SET revision=revision+1
            WHERE organization_id=:tenant AND id=:day
        """),
            {"tenant": tenant, "day": day_id},
        )
        connection.execute(
            text("""
            UPDATE public.queue_entries SET routine_rank=3, revision=revision+1
            WHERE organization_id=:tenant AND id=:entry
        """),
            {"tenant": tenant, "entry": entry_id},
        )
    with transaction(runtime, tenant) as connection:
        assert connection.execute(
            text("""
            SELECT q.revision, e.routine_rank FROM public.queue_days q
            JOIN public.queue_entries e
              ON e.organization_id=q.organization_id AND e.queue_day_id=q.id
            WHERE q.organization_id=:tenant AND e.id=:entry
        """),
            {"tenant": tenant, "entry": entry_id},
        ).one() == (revision + 1, 3)


def test_direct_sql_cannot_supply_attestation_identity_time_or_arrival(
    appointment_storage,
):
    runtime, _, (tenant, _), (doctor, _) = appointment_storage
    with transaction(runtime, tenant) as connection:
        fake_actor = uuid4()
        saved = _appointment(connection, tenant, doctor)
        connection.execute(
            text("SELECT set_config('app.staff_id', :fake, true)"),
            {"fake": str(fake_actor)},
        )
        before = datetime.now(UTC)
        _, entry_id, actual_sequence = _arrive(connection, tenant, doctor, saved)
        actor, sequence, instant = connection.execute(
            text("""
            SELECT attested_by_staff_id, arrival_sequence, attested_at
            FROM public.queue_entries
            WHERE organization_id=:tenant AND id=:entry
        """),
            {"tenant": tenant, "entry": entry_id},
        ).one()
        assert actor == doctor
        assert sequence == actual_sequence == 1
        assert before <= instant <= datetime.now(UTC)
        for column, forged in (
            ("arrival_sequence", 900),
            ("attested_by_staff_id", fake_actor),
            ("attested_at", datetime(2001, 1, 1, tzinfo=UTC)),
        ):
            with pytest.raises(DBAPIError) as denied, connection.begin_nested():
                connection.execute(
                    text(
                        f"INSERT INTO public.queue_entries ({column}) VALUES (:forged)"
                    ),
                    {"forged": forged},
                )
            assert denied.value.orig.sqlstate == "42501"
        for column, forged in (
            ("arrival_sequence", 900),
            ("attested_by_staff_id", fake_actor),
            ("attested_at", datetime(2001, 1, 1, tzinfo=UTC)),
        ):
            with pytest.raises(DBAPIError) as denied, connection.begin_nested():
                connection.execute(
                    text(
                        f"UPDATE public.queue_entries SET {column}=:forged WHERE id=:id"
                    ),
                    {"forged": forged, "id": entry_id},
                )
            assert denied.value.orig.sqlstate == "42501"


def test_one_queue_day_per_clinic_date_lane_with_independent_arrival_order(
    appointment_storage,
):
    runtime, _, (tenant, other), (doctor, other_doctor) = appointment_storage
    with transaction(runtime, tenant) as connection:
        main = [
            _arrive(
                connection, tenant, doctor, _appointment(connection, tenant, doctor)
            )
            for _ in range(2)
        ]
        emergency = [
            _arrive(
                connection,
                tenant,
                doctor,
                _appointment(
                    connection,
                    tenant,
                    doctor,
                    classification="EMERGENCY",
                    lane="EMERGENCY",
                ),
            )
            for _ in range(2)
        ]
        assert main[0][0] == main[1][0]
        assert emergency[0][0] == emergency[1][0]
        assert main[0][0] != emergency[0][0]
        assert [item[2] for item in main] == [1, 2]
        assert [item[2] for item in emergency] == [1, 2]
        assert connection.execute(
            text("""
                SELECT q.lane, q.arrival_counter, count(e.id),
                       min(e.routine_rank), max(e.routine_rank)
                FROM public.queue_days q
                JOIN public.queue_entries e ON e.organization_id = q.organization_id
                    AND e.queue_day_id = q.id
                WHERE q.organization_id = :tenant AND q.service_date = :day
                GROUP BY q.lane, q.arrival_counter ORDER BY q.lane
            """),
            {"tenant": tenant, "day": DAY},
        ).all() == [
            ("EMERGENCY", 2, 2, None, None),
            ("MAIN", 2, 2, 1, 2),
        ]
        with pytest.raises(DBAPIError) as duplicate, connection.begin_nested():
            connection.execute(
                text("""
                    INSERT INTO public.queue_days
                        (id, organization_id, doctor_staff_id, service_date,
                         lane, queue_policy_version)
                    VALUES (:id, :tenant, :doctor, :day, 'MAIN', 1)
                """),
                {"id": uuid4(), "tenant": tenant, "doctor": doctor, "day": DAY},
            )
        assert duplicate.value.orig.sqlstate == "23505"
    with transaction(runtime, other) as connection:
        other_entry = _arrive(
            connection,
            other,
            other_doctor,
            _appointment(connection, other, other_doctor),
        )
        assert other_entry[2] == 1
        assert connection.scalar(text("SELECT count(*) FROM public.queue_days")) == 1


@pytest.mark.parametrize("invalid", ["missing", "ghost", "patient", "date", "lane"])
def test_waiting_active_entry_equivalence_rejects_invalid_final_state(
    appointment_storage, invalid
):
    runtime, _, (tenant, _), (doctor, _) = appointment_storage
    with (
        pytest.raises(DBAPIError) as denied,
        transaction(runtime, tenant) as connection,
    ):
        appointment = _appointment(
            connection,
            tenant,
            doctor,
            status="Booked" if invalid == "ghost" else "Waiting",
        )
        if invalid != "missing":
            _arrive(
                connection,
                tenant,
                doctor,
                appointment,
                patient=_patient(connection, tenant) if invalid == "patient" else None,
                service_date=DAY + timedelta(days=1) if invalid == "date" else None,
                lane="EMERGENCY" if invalid == "lane" else None,
            )
    assert denied.value.orig.sqlstate == "23514"


def test_duplicate_active_entry_and_wrong_doctor_are_rejected(appointment_storage):
    runtime, _, (tenant, _), (doctor, foreign_doctor) = appointment_storage
    with transaction(runtime, tenant) as connection:
        appointment = _appointment(connection, tenant, doctor)
        _arrive(connection, tenant, doctor, appointment)
        with pytest.raises(DBAPIError) as duplicate, connection.begin_nested():
            _arrive(connection, tenant, doctor, appointment)
        assert duplicate.value.orig.sqlstate == "23505"
        assert (
            connection.scalar(
                text("""
                SELECT count(*) FROM public.queue_entries
                WHERE organization_id = :tenant AND disposition = 'ACTIVE'
            """),
                {"tenant": tenant},
            )
            == 1
        )
        # The schema permits just one DOCTOR_OWNER per tenant, so a foreign
        # Doctor violates the composite staff/tenant FK before commit.
        with pytest.raises(DBAPIError) as wrong_doctor, connection.begin_nested():
            _arrive(
                connection,
                tenant,
                foreign_doctor,
                _appointment(connection, tenant, doctor),
                service_date=DAY + timedelta(days=1),
            )
        assert wrong_doctor.value.orig.sqlstate == "23503"


def test_cancelled_waiting_entry_closes_with_immutable_history(appointment_storage):
    runtime, _, (tenant, _), (doctor, _) = appointment_storage
    with transaction(runtime, tenant) as connection:
        appointment = _appointment(connection, tenant, doctor)
        day_id, entry_id, _ = _arrive(connection, tenant, doctor, appointment)
        connection.execute(
            text("""
                UPDATE public.appointments
                SET status = 'Cancelled', revision = revision + 1
                WHERE organization_id = :tenant AND id = :appointment
            """),
            {"tenant": tenant, "appointment": UUID(appointment["id"])},
        )
        connection.execute(
            text("""
                INSERT INTO public.appointment_history
                    (id, organization_id, appointment_id, appointment_revision,
                     action, old_status, new_status, old_service_date,
                     new_service_date, old_number, new_number,
                     actor_staff_id, actor_role, reason, price_snapshot_appointment_id)
                SELECT :history, a.organization_id, a.id, a.revision,
                       'STATUS', 'Waiting', 'Cancelled', a.service_date,
                       a.service_date, a.current_number, a.current_number,
                       :doctor, 'DOCTOR_OWNER', 'Synthetic cancellation', a.id
                FROM public.appointments a
                WHERE a.organization_id = :tenant AND a.id = :appointment
            """),
            {
                "history": uuid4(),
                "tenant": tenant,
                "appointment": UUID(appointment["id"]),
                "doctor": doctor,
            },
        )
        connection.execute(
            text("""
                UPDATE public.queue_entries
                SET disposition = 'CLOSED', closure_history_revision = 1,
                    revision = revision + 1
                WHERE organization_id = :tenant AND id = :entry
            """),
            {"tenant": tenant, "entry": entry_id},
        )
        connection.execute(
            text("""
                UPDATE public.queue_days SET revision = revision + 1
                WHERE organization_id = :tenant AND id = :day
            """),
            {"tenant": tenant, "day": day_id},
        )
        assert connection.execute(
            text("""
                SELECT disposition, closure_history_revision, closure_queue_revision,
                       ordinary_head_admitted
                FROM public.queue_entries WHERE id = :entry
            """),
            {"entry": entry_id},
        ).one() == ("CLOSED", 1, 1, False)
        assert (
            connection.scalar(
                text("""
                SELECT count(*) FROM public.queue_entries
                WHERE queue_day_id = :day AND disposition = 'ACTIVE'
            """),
                {"day": day_id},
            )
            == 0
        )
    with transaction(runtime, tenant) as connection:
        assert connection.execute(
            text("""
                SELECT e.disposition, h.old_status, h.new_status
                FROM public.queue_entries e
                JOIN public.appointment_history h
                  ON h.organization_id = e.organization_id
                 AND h.appointment_id = e.appointment_id
                 AND h.appointment_revision = e.closure_history_revision
                WHERE e.id = :entry
            """),
            {"entry": entry_id},
        ).one() == ("CLOSED", "Waiting", "Cancelled")


def test_waiting_entry_remains_on_original_day_after_synthetic_cairo_midnight(
    appointment_storage,
):
    runtime, _, (tenant, _), (doctor, _) = appointment_storage
    before = datetime(2026, 10, 27, 18, tzinfo=UTC)
    after = datetime(2026, 10, 28, 2, tzinfo=UTC)
    assert before.astimezone(CAIRO).date() == DAY
    assert after.astimezone(CAIRO).date() == DAY + timedelta(days=1)
    with transaction(runtime, tenant) as connection:
        appointment = _appointment(connection, tenant, doctor)
        day_id, entry_id, _ = _arrive(connection, tenant, doctor, appointment)
    with transaction(runtime, tenant) as connection:
        stored = connection.execute(
            text("""
                SELECT a.status, q.service_date, e.service_date, e.attested_at
                FROM public.appointments a
                JOIN public.queue_entries e ON e.organization_id = a.organization_id
                    AND e.appointment_id = a.id
                JOIN public.queue_days q ON q.organization_id = e.organization_id
                    AND q.id = e.queue_day_id
                WHERE a.id = :appointment AND e.id = :entry
            """),
            {"appointment": UUID(appointment["id"]), "entry": entry_id},
        ).one()
        assert stored[:3] == ("Waiting", DAY, DAY)
        assert stored.attested_at > before
        assert stored.attested_at <= datetime.now(UTC)
        assert (
            connection.scalar(
                text("""
                SELECT count(*) FROM public.queue_days
                WHERE organization_id = :tenant AND service_date = :tomorrow
            """),
                {"tenant": tenant, "tomorrow": after.astimezone(CAIRO).date()},
            )
            == 0
        )
        assert (
            connection.scalar(
                text("SELECT id FROM public.queue_days WHERE id = :day"),
                {"day": day_id},
            )
            == day_id
        )


def test_cursor_rejects_unsupported_advancement_and_accepts_real_head_evidence(
    appointment_storage,
):
    runtime, _, (tenant, _), (doctor, _) = appointment_storage
    with transaction(runtime, tenant) as connection:
        appointment = _appointment(connection, tenant, doctor)
        day_id, entry_id, _ = _arrive(connection, tenant, doctor, appointment)
        with pytest.raises(DBAPIError) as unsupported, connection.begin_nested():
            connection.execute(
                text("""
                    UPDATE public.queue_days
                    SET routine_cursor = 1, revision = revision + 1
                    WHERE organization_id = :tenant AND id = :day
                """),
                {"tenant": tenant, "day": day_id},
            )
        assert unsupported.value.orig.sqlstate == "23514"
    # D-06 must have no Visit dependency: exercise admissible evidence within
    # the transaction, then roll it back before D-07's clinical commit checks.
    with (
        pytest.raises(RuntimeError, match="synthetic queue rollback"),
        transaction(runtime, tenant) as connection,
    ):
        connection.execute(
            text("""
                    SELECT * FROM public.lock_doctor_admission_capacity(
                        :tenant, :doctor, :service_date, 'MAIN')
                """),
            {"tenant": tenant, "doctor": doctor, "service_date": DAY},
        ).one()
        connection.execute(
            text("""
                    UPDATE public.appointments
                    SET status = 'In Examination', revision = revision + 1
                    WHERE organization_id = :tenant AND id = :appointment
                """),
            {"tenant": tenant, "appointment": UUID(appointment["id"])},
        )
        connection.execute(
            text("""
                    INSERT INTO public.appointment_history
                        (id, organization_id, appointment_id, appointment_revision,
                         action, old_status, new_status, old_service_date,
                         new_service_date, old_number, new_number,
                         actor_staff_id, actor_role, price_snapshot_appointment_id)
                    SELECT :history, a.organization_id, a.id, a.revision,
                           'STATUS', 'Waiting', 'In Examination', a.service_date,
                           a.service_date, a.current_number, a.current_number,
                           :doctor, 'DOCTOR_OWNER', a.id
                    FROM public.appointments a
                    WHERE a.organization_id = :tenant AND a.id = :appointment
                """),
            {
                "history": uuid4(),
                "tenant": tenant,
                "appointment": UUID(appointment["id"]),
                "doctor": doctor,
            },
        )
        connection.execute(
            text("""
                    UPDATE public.queue_entries
                    SET disposition = 'CLOSED', closure_history_revision = 1,
                        revision = revision + 1
                    WHERE organization_id = :tenant AND id = :entry
                """),
            {"tenant": tenant, "entry": entry_id},
        )
        assert (
            connection.scalar(
                text("""
                    SELECT ordinary_head_admitted FROM public.queue_entries
                    WHERE organization_id = :tenant AND id = :entry
                """),
                {"tenant": tenant, "entry": entry_id},
            )
            is True
        )
        connection.execute(
            text("""
                    UPDATE public.queue_days
                    SET routine_cursor = 1, revision = revision + 1
                    WHERE organization_id = :tenant AND id = :day
                """),
            {"tenant": tenant, "day": day_id},
        )
        assert (
            connection.scalar(
                text("SELECT routine_cursor FROM public.queue_days WHERE id = :day"),
                {"day": day_id},
            )
            == 1
        )
        raise RuntimeError("synthetic queue rollback")


def test_queue_rls_tenant_isolation_no_context_and_operator_grants(
    appointment_storage,
    role_engine,
):
    runtime, _, (tenant, other), (doctor, other_doctor) = appointment_storage
    for organization, staff in ((tenant, doctor), (other, other_doctor)):
        with transaction(runtime, organization) as connection:
            _arrive(
                connection,
                organization,
                staff,
                _appointment(connection, organization, staff),
            )
    for organization in (tenant, other):
        with transaction(runtime, organization) as connection:
            for table in ("queue_days", "queue_entries", "doctor_controls"):
                assert (
                    connection.scalar(text(f"SELECT count(*) FROM public.{table}")) == 1
                )
        with transaction(runtime) as connection:
            for table in ("queue_days", "queue_entries", "doctor_controls"):
                assert (
                    connection.scalar(text(f"SELECT count(*) FROM public.{table}")) == 0
                )
    operator = role_engine("carepulse_operator")
    try:
        with pytest.raises(DBAPIError) as denied, operator.connect() as connection:
            connection.execute(text("SELECT * FROM public.queue_entries"))
        assert denied.value.orig.sqlstate == "42501"
    finally:
        operator.dispose()


def test_tenant_first_dated_queue_lookups_have_explainable_plans(appointment_storage):
    runtime, _, (tenant, _), (doctor, _) = appointment_storage
    with transaction(runtime, tenant) as connection:
        appointment = _appointment(connection, tenant, doctor)
        _arrive(connection, tenant, doctor, appointment)
        for statement in (
            """SELECT id FROM public.queue_days
               WHERE organization_id = :tenant AND service_date = :day AND lane = 'MAIN'""",
            """SELECT e.id FROM public.queue_entries e
               JOIN public.queue_days q
                 ON q.organization_id = e.organization_id AND q.id = e.queue_day_id
               WHERE e.organization_id = :tenant AND q.service_date = :day
                 AND q.lane = 'MAIN' AND e.disposition = 'ACTIVE'
               ORDER BY e.arrival_sequence""",
        ):
            plan = connection.scalar(
                text(f"EXPLAIN (FORMAT JSON, COSTS FALSE) {statement}"),
                {"tenant": tenant, "day": DAY},
            )
            assert plan and "Plan" in plan[0]
            print(
                f"D-06 T102 PostgreSQL query plan: {json.dumps(plan[0], sort_keys=True)}"
            )

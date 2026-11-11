"""D-11 real PostgreSQL query shapes and service-day/event-day attribution."""

import json
from datetime import UTC, datetime, time, timedelta
from decimal import Decimal
from uuid import UUID, uuid4
from zoneinfo import ZoneInfo

from carepulse.appointments.persistence import allocate_day_number, persist_appointment
from carepulse.db import transaction
from sqlalchemy import text
from test_appointment_persistence import (
    _admit_saved,
    _draft,
    _patient,
    _persist_with_status,
    _query,
)
from test_appointment_persistence import appointment_storage as appointment_storage  # noqa: PLC0414
from test_queue_persistence import _appointment, _arrive


def _reschedule_booked(connection, tenant, doctor, appointment, next_day):
    """Retain the old number assignment and append the genuine revised-day evidence."""
    appointment_id = UUID(appointment["id"])
    old = connection.execute(
        text("""
            SELECT service_date, current_assignment_id, current_number
            FROM public.appointments WHERE organization_id=:tenant AND id=:appointment
        """),
        {"tenant": tenant, "appointment": appointment_id},
    ).one()
    number = allocate_day_number(connection, tenant, next_day)
    assignment = uuid4()
    connection.execute(
        text("""
            INSERT INTO public.appointment_number_assignments (
                id, organization_id, appointment_id, service_date, number,
                prior_assignment_id)
            VALUES (:assignment, :tenant, :appointment, :day, :number, :prior)
        """),
        {
            "assignment": assignment,
            "tenant": tenant,
            "appointment": appointment_id,
            "day": next_day,
            "number": number,
            "prior": old.current_assignment_id,
        },
    )
    connection.execute(
        text("""
            UPDATE public.appointments
            SET service_date=:day, current_assignment_id=:assignment,
                current_number=:number, revision=1
            WHERE organization_id=:tenant AND id=:appointment AND revision=0
        """),
        {
            "tenant": tenant,
            "appointment": appointment_id,
            "day": next_day,
            "assignment": assignment,
            "number": number,
        },
    )
    connection.execute(
        text("""
            INSERT INTO public.appointment_history (
                id, organization_id, appointment_id, appointment_revision,
                action, old_status, new_status, old_service_date, new_service_date,
                old_number, new_number, actor_staff_id, actor_role,
                price_snapshot_appointment_id)
            VALUES (:history, :tenant, :appointment, 1, 'RESCHEDULE',
                    'Booked', 'Booked', :old_day, :day,
                    :old_number, :number, :doctor, 'DOCTOR_OWNER', :appointment)
        """),
        {
            "history": uuid4(),
            "tenant": tenant,
            "appointment": appointment_id,
            "old_day": old.service_date,
            "day": next_day,
            "old_number": old.current_number,
            "number": number,
            "doctor": doctor,
        },
    )
    return appointment_id


def _index_path(plan, relation, indexed_columns):
    """Find an actual indexed predicate for this relation in PostgreSQL's plan."""

    def search(node):
        if node.get("Relation Name") == relation:
            condition = " ".join(
                str(node.get(key, "")) for key in ("Index Cond", "Recheck Cond")
            )
            if node.get("Node Type") in (
                "Index Scan",
                "Index Only Scan",
                "Bitmap Heap Scan",
            ) and all(column in condition for column in indexed_columns):
                return True
        return any(search(child) for child in node.get("Plans", []))

    return search(plan["Plan"])


def _plan_nodes(node):
    yield node
    for child in node.get("Plans", []):
        yield from _plan_nodes(child)


def _dated_appointment_pk_probe(node):
    return (
        node.get("Relation Name") == "appointments"
        and node.get("Node Type") in ("Index Scan", "Index Only Scan")
        and "id = s.appointment_id" in node.get("Index Cond", "")
        and all(
            field in node.get("Filter", "")
            for field in ("organization_id", "service_date", "status", "Completed")
        )
    )


def _bounded_completed_value_plan(plan):
    """Accept either a dated Appointment index or a tenant-indexed snapshot join."""
    nodes = tuple(_plan_nodes(plan["Plan"]))
    if any(
        node.get("Node Type") in ("Seq Scan", "Parallel Seq Scan") for node in nodes
    ):
        return False
    if _index_path(plan, "appointments", ("organization_id", "service_date")):
        return True
    return any(
        node.get("Node Type") == "Nested Loop"
        and _index_path(
            {"Plan": node}, "appointment_price_snapshots", ("organization_id",)
        )
        and any(_dated_appointment_pk_probe(child) for child in _plan_nodes(node))
        for node in nodes
    )


def test_tenant_bounded_day_lane_phone_and_event_query_plans(appointment_storage):
    runtime, migration, (tenant, other), (doctor, other_doctor) = appointment_storage
    # Give PostgreSQL representative dated data and current statistics: without
    # them, a tiny fixture can favor a tenant-only nested-loop cross product.
    with transaction(runtime, tenant) as connection:
        day = connection.scalar(
            text("SELECT (clock_timestamp() AT TIME ZONE 'Africa/Cairo')::date")
        )
        _persist_with_status(
            connection,
            tenant,
            _patient(connection, tenant),
            doctor,
            "Completed",
            service_date=day,
        )
        for offset in range(1, 65):
            _appointment(
                connection,
                tenant,
                doctor,
                status="Booked",
                service_date=day - timedelta(days=offset),
            )
    with migration.begin() as connection:
        connection.execute(
            text("ANALYZE public.appointments, public.appointment_price_snapshots")
        )
    with transaction(runtime, tenant) as connection:
        start = datetime.combine(day, time.min, ZoneInfo("Africa/Cairo")).astimezone(
            UTC
        )
        end = datetime.combine(
            day + timedelta(days=1), time.min, ZoneInfo("Africa/Cairo")
        ).astimezone(UTC)
        _arrive(
            connection,
            tenant,
            doctor,
            _appointment(
                connection,
                tenant,
                doctor,
                service_date=day,
            ),
        )
        patients = [_patient(connection, tenant), _patient(connection, tenant)]
        for patient in patients:
            connection.execute(
                text("""
                    UPDATE public.patients
                    SET phone_raw='01012345678', phone_normalized='+201012345678',
                        demographic_revision=demographic_revision+1
                    WHERE organization_id=:tenant AND id=:patient
                """),
                {"tenant": tenant, "patient": patient},
            )
        found = connection.scalars(
            text("""
                SELECT id FROM public.patients
                WHERE organization_id=:tenant AND phone_normalized=:phone
                ORDER BY id
            """),
            {"tenant": tenant, "phone": "+201012345678"},
        ).all()
        assert found == sorted(patients) and found[0] != found[1]

        queries = {
            "appointments_day_status_lane": """
                SELECT a.id FROM public.appointments a
                WHERE a.organization_id=:tenant AND a.service_date=:day
                  AND a.status=:status AND a.lane=:lane
            """,
            "active_queue_day_lane": """
                SELECT e.id FROM public.queue_entries e
                WHERE e.organization_id=:tenant
                  AND e.queue_day_id=(
                      SELECT q.id FROM public.queue_days q
                      WHERE q.organization_id=:tenant
                        AND q.service_date=:day AND q.lane=:lane
                  )
                  AND e.disposition='ACTIVE'
                ORDER BY e.arrival_sequence
            """,
            "family_phone": """
                SELECT p.id FROM public.patients p
                WHERE p.organization_id=:tenant AND p.phone_normalized=:phone
                ORDER BY p.id
            """,
            "completed_service_value": """
                SELECT sum(s.net_egp) FROM public.appointments a
                JOIN public.appointment_price_snapshots s
                  ON s.organization_id=a.organization_id AND s.appointment_id=a.id
                WHERE a.organization_id=:tenant AND a.service_date=:day
                  AND a.status='Completed'
            """,
            "completion_cancellation_events": """
                SELECT h.id FROM public.appointment_history h
                WHERE h.organization_id=:tenant
                  AND h.occurred_at>=:start AND h.occurred_at<:end
                  AND h.new_status IN ('Completed', 'Cancelled')
            """,
            "admission_events": """
                SELECT v.id FROM public.visits v
                WHERE v.organization_id=:tenant
                  AND v.admitted_at>=:start AND v.admitted_at<:end
            """,
        }
        params = {
            "tenant": tenant,
            "day": day,
            "status": "Waiting",
            "lane": "MAIN",
            "phone": "+201012345678",
            "start": start,
            "end": end,
        }
        assert connection.scalar(
            text(queries["completed_service_value"]), params
        ) == Decimal("100.00")
        index_requirements = {
            "appointments_day_status_lane": (
                ("appointments", ("organization_id", "service_date", "status", "lane")),
            ),
            "active_queue_day_lane": (
                ("queue_days", ("organization_id", "service_date")),
                ("queue_entries", ("organization_id", "queue_day_id")),
            ),
            "family_phone": (("patients", ("organization_id", "phone_normalized")),),
            "completion_cancellation_events": (
                ("appointment_history", ("organization_id", "occurred_at")),
            ),
            "admission_events": (("visits", ("organization_id", "admitted_at")),),
        }
        # Inspect representative usable index access paths even with tiny data.
        connection.execute(text("SET LOCAL enable_seqscan=off"))
        for name, statement in queries.items():
            plan = connection.scalar(
                text(f"EXPLAIN (FORMAT JSON, COSTS FALSE) {statement}"), params
            )
            assert plan and "Plan" in plan[0]
            print(f"D-11 T109 {name} EXPLAIN: {json.dumps(plan[0], sort_keys=True)}")
            if name == "appointments_day_status_lane":
                # A small tenant may use the existing counted-patient index for
                # tenant/day lookup; the full report index must still be valid.
                assert _index_path(
                    plan[0], "appointments", ("organization_id", "service_date")
                ), "Missing indexed tenant/day Appointment access"
                report_index = connection.execute(
                    text("""
                        SELECT i.indisvalid, i.indisready,
                               pg_get_indexdef(i.indexrelid, 1, true),
                               pg_get_indexdef(i.indexrelid, 2, true),
                               pg_get_indexdef(i.indexrelid, 3, true),
                               pg_get_indexdef(i.indexrelid, 4, true)
                        FROM pg_index i
                        WHERE i.indexrelid =
                            'public.appointments_day_status_lane_report'::regclass
                    """)
                ).one()
                assert report_index == (
                    True,
                    True,
                    "organization_id",
                    "service_date",
                    "status",
                    "lane",
                )
                continue
            if name == "completed_service_value":
                assert _bounded_completed_value_plan(plan[0]), (
                    "Completed value must use a dated Appointment index or a "
                    "tenant-indexed snapshot→Appointment PK join with date/status filtering"
                )
                continue
            assert all(
                _index_path(plan[0], relation, columns)
                for relation, columns in index_requirements[name]
            ), f"Missing tenant-first indexed date/lookup path for {name}"

    with transaction(runtime, other) as connection:
        assert (
            connection.scalar(
                text("""
                SELECT count(*) FROM public.patients
                WHERE organization_id=:tenant AND phone_normalized=:phone
            """),
                {"tenant": other, "phone": "+201012345678"},
            )
            == 0
        )
        assert other_doctor != doctor


def test_original_service_day_value_excludes_cancelled_visit_and_keeps_event_dates(
    appointment_storage,
):
    runtime, _, (tenant, _), (doctor, _) = appointment_storage
    with transaction(runtime, tenant) as connection:
        event_day = connection.scalar(
            text("SELECT (clock_timestamp() AT TIME ZONE 'Africa/Cairo')::date")
        )
        original_day = event_day - timedelta(days=1)
        prior_completed = _persist_with_status(
            connection,
            tenant,
            _patient(connection, tenant),
            doctor,
            "Completed",
            service_date=original_day,
        )
        displaced = persist_appointment(
            connection,
            _draft(_patient(connection, tenant), doctor, service_date=original_day),
            _query(tenant, doctor),
        )
        moved_id = _reschedule_booked(connection, tenant, doctor, displaced, event_day)
        patient = _patient(connection, tenant)
        retained = _persist_with_status(
            connection,
            tenant,
            patient,
            doctor,
            "In Examination",
            service_date=original_day,
        )
        cancelled_id = UUID(retained["id"])
        connection.execute(
            text("""
                UPDATE public.appointments
                SET status='Cancelled', revision=revision+1
                WHERE organization_id=:tenant AND id=:appointment
            """),
            {"tenant": tenant, "appointment": cancelled_id},
        )
        replacement = persist_appointment(
            connection,
            _draft(
                patient,
                doctor,
                service_date=original_day,
                classification="URGENT",
                price_snapshot={"base_egp": "120.00", "net_egp": "120.00"},
            ),
            _query(tenant, doctor),
        )
        replacement_id = UUID(replacement["id"])
        connection.execute(
            text("""
                INSERT INTO public.appointment_history (
                    id, organization_id, appointment_id, appointment_revision,
                    action, old_status, new_status, old_service_date,
                    new_service_date, old_number, new_number, actor_staff_id,
                    actor_role, reason, price_snapshot_appointment_id,
                    replacement_appointment_id)
                SELECT :history, organization_id, id, revision, 'DOCTOR_EXCEPTION',
                       'In Examination', 'Cancelled', service_date, service_date,
                       current_number, current_number, :doctor, 'DOCTOR_OWNER',
                       'Synthetic urgent replacement', id, :replacement
                FROM public.appointments WHERE organization_id=:tenant AND id=:appointment
            """),
            {
                "history": uuid4(),
                "tenant": tenant,
                "appointment": cancelled_id,
                "doctor": doctor,
                "replacement": replacement_id,
            },
        )
        _admit_saved(connection, tenant, doctor, replacement, complete=True)
        assert (
            connection.scalar(
                text("SELECT count(*) FROM public.visits WHERE appointment_id=:id"),
                {"id": cancelled_id},
            )
            == 1
        )

        service_values = dict(
            connection.execute(
                text("""
                SELECT a.service_date, sum(s.net_egp) FROM public.appointments a
                JOIN public.appointment_price_snapshots s
                  ON s.organization_id=a.organization_id AND s.appointment_id=a.id
                WHERE a.organization_id=:tenant AND a.status='Completed'
                GROUP BY a.service_date
            """),
                {"tenant": tenant},
            ).all()
        )
        assert service_values == {original_day: Decimal("220.00")}
        events = connection.execute(
            text("""
                SELECT h.appointment_id, h.new_status,
                       (h.occurred_at AT TIME ZONE 'Africa/Cairo')::date AS event_day,
                       h.new_service_date
                FROM public.appointment_history h
                WHERE h.organization_id=:tenant
                  AND h.new_status IN ('Completed','Cancelled')
                ORDER BY h.appointment_id
            """),
            {"tenant": tenant},
        ).all()
        assert {(row.appointment_id, row.new_status) for row in events} == {
            (UUID(prior_completed["id"]), "Completed"),
            (cancelled_id, "Cancelled"),
            (replacement_id, "Completed"),
        }
        assert all(row.event_day == event_day for row in events)
        assert all(row.new_service_date == original_day for row in events)
        assert (
            connection.scalar(
                text("""
                SELECT replacement_appointment_id
                FROM public.appointment_history
                WHERE organization_id=:tenant AND appointment_id=:id
                  AND new_status='Cancelled'
            """),
                {"tenant": tenant, "id": cancelled_id},
            )
            == replacement_id
        )
        assert connection.execute(
            text("""
                SELECT old_service_date, new_service_date
                FROM public.appointment_history
                WHERE organization_id=:tenant AND appointment_id=:id
                  AND action='RESCHEDULE'
            """),
            {"tenant": tenant, "id": moved_id},
        ).one() == (original_day, event_day)
        assert connection.execute(
            text("""
                SELECT status, service_date FROM public.appointments
                WHERE organization_id=:tenant AND id=:id
            """),
            {"tenant": tenant, "id": moved_id},
        ).one() == ("Booked", event_day)

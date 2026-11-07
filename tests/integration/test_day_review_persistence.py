"""D-11 dated reviews, unchanged care records, and tenant-scoped history."""

from datetime import UTC, date, datetime
from uuid import UUID, uuid4

import pytest
from carepulse.db import transaction
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from test_appointment_persistence import (
    _patient,
    _persist_with_status,
)
from test_appointment_persistence import appointment_storage as appointment_storage  # noqa: PLC0414

DAY = date(2026, 10, 27)
NEXT_DAY = date(2026, 10, 28)


def _review(connection, tenant, service_date, expected_revision, disposition, notes):
    return connection.execute(
        text("""
            SELECT day_review_id, revision FROM public.record_day_review(
                :tenant, :day, :expected, :disposition, :notes)
        """),
        {
            "tenant": tenant,
            "day": service_date,
            "expected": expected_revision,
            "disposition": disposition,
            "notes": notes,
        },
    ).one()


def _as_actor(connection, actor):
    connection.execute(
        text("SELECT set_config('app.staff_id', :actor, true)"),
        {"actor": str(actor)},
    )


def _care_rows(connection, tenant):
    return {
        table: connection.execute(
            text(
                f"SELECT row_to_json(t)::text FROM public.{table} t "
                "WHERE organization_id = :tenant ORDER BY 1"
            ),
            {"tenant": tenant},
        )
        .scalars()
        .all()
        for table in (
            "appointments",
            "appointment_history",
            "visits",
            "queue_days",
            "queue_entries",
        )
    }


def _cancel_admitted_visit(connection, tenant, doctor):
    retained = _persist_with_status(
        connection,
        tenant,
        _patient(connection, tenant),
        doctor,
        "In Examination",
        service_date=NEXT_DAY,
    )
    appointment = UUID(retained["id"])
    connection.execute(
        text("""
            UPDATE public.appointments SET status='Cancelled', revision=3
            WHERE organization_id=:tenant AND id=:appointment
        """),
        {"tenant": tenant, "appointment": appointment},
    )
    connection.execute(
        text("""
            INSERT INTO public.appointment_history (
                id, organization_id, appointment_id, appointment_revision,
                action, old_status, new_status, old_service_date,
                new_service_date, old_number, new_number, actor_staff_id,
                actor_role, reason, price_snapshot_appointment_id)
            SELECT :history, organization_id, id, 3, 'DOCTOR_EXCEPTION',
                   'In Examination', 'Cancelled', service_date, service_date,
                   current_number, current_number, :doctor, 'DOCTOR_OWNER',
                   'Synthetic exceptional cancellation', id
            FROM public.appointments WHERE organization_id=:tenant AND id=:appointment
        """),
        {
            "history": uuid4(),
            "tenant": tenant,
            "appointment": appointment,
            "doctor": doctor,
        },
    )
    return appointment


def test_all_six_states_and_retained_visit_survive_cairo_midnight_and_day_close(
    appointment_storage,
):
    runtime, _, (tenant, _), (doctor, _) = appointment_storage
    with transaction(runtime, tenant) as connection:
        for status in (
            "Booked",
            "Waiting",
            "In Examination",
            "Completed",
            "Cancelled",
            "No-show",
        ):
            _persist_with_status(
                connection,
                tenant,
                _patient(connection, tenant),
                doctor,
                status,
                service_date=DAY,
            )
        for status in ("Booked", "In Examination"):
            _persist_with_status(
                connection,
                tenant,
                _patient(connection, tenant),
                doctor,
                status,
                service_date=NEXT_DAY,
            )
        cancelled_visit = _cancel_admitted_visit(connection, tenant, doctor)
        _as_actor(connection, doctor)
        original = _care_rows(connection, tenant)
        assert (
            connection.scalar(
                text(
                    "SELECT count(*) FROM public.visits WHERE appointment_id=:appointment"
                ),
                {"appointment": cancelled_visit},
            )
            == 1
        )

        first = _review(connection, tenant, DAY, 0, "REVIEWED", "Unresolved arrival")
        with pytest.raises(DBAPIError) as blocked, connection.begin_nested():
            _review(connection, tenant, DAY, 1, "FINAL_CLOSED", "Still waiting")
        assert blocked.value.orig.sqlstate == "23514"
        closed = _review(connection, tenant, NEXT_DAY, 0, "FINAL_CLOSED", "Reviewed")
        assert first.revision == closed.revision == 1

        observed_dates = [
            connection.scalar(
                text(
                    "SELECT (CAST(:instant AS timestamptz) AT TIME ZONE 'Africa/Cairo')::date"
                ),
                {"instant": instant},
            )
            for instant in (
                datetime(2026, 10, 27, 20, 59, 59, tzinfo=UTC),
                datetime(2026, 10, 27, 21, 0, 1, tzinfo=UTC),
            )
        ]
        assert observed_dates == [DAY, NEXT_DAY]
        assert _care_rows(connection, tenant) == original
        counts = connection.execute(
            text("""
                SELECT service_date, status, count(*) FROM public.appointments
                WHERE organization_id=:tenant GROUP BY service_date, status
            """),
            {"tenant": tenant},
        ).all()
        assert {(day, status): count for day, status, count in counts} == {
            **{
                (DAY, status): 1
                for status in (
                    "Booked",
                    "Waiting",
                    "In Examination",
                    "Completed",
                    "Cancelled",
                    "No-show",
                )
            },
            (NEXT_DAY, "Booked"): 1,
            (NEXT_DAY, "In Examination"): 1,
            (NEXT_DAY, "Cancelled"): 1,
        }
        assert (
            connection.scalar(
                text("""
                SELECT count(*) FROM public.appointments
                WHERE organization_id=:tenant AND service_date=:day
                  AND status IN ('Booked', 'In Examination')
            """),
                {"tenant": tenant, "day": NEXT_DAY},
            )
            == 2
        )
        assert (
            connection.scalar(
                text("""
                SELECT count(*) FROM public.day_reviews
                WHERE organization_id=:tenant
            """),
                {"tenant": tenant},
            )
            == 2
        )


def test_review_amendments_append_history_and_stale_revision_cannot_overwrite(
    appointment_storage,
):
    runtime, migration, (tenant, _), (doctor, _) = appointment_storage
    with transaction(runtime, tenant) as connection:
        _as_actor(connection, doctor)
        first = _review(connection, tenant, DAY, 0, "REVIEWED", "First review")
        second = _review(connection, tenant, DAY, 1, "FINAL_CLOSED", "Final review")
        third = _review(connection, tenant, DAY, 2, "REVIEWED", "Amended")
        assert first.day_review_id == second.day_review_id == third.day_review_id
        assert (first.revision, second.revision, third.revision) == (1, 2, 3)
        with pytest.raises(DBAPIError) as stale, connection.begin_nested():
            _review(connection, tenant, DAY, 1, "FINAL_CLOSED", "Stale overwrite")
        assert stale.value.orig.sqlstate == "40001"
    with migration.connect() as connection:
        history = connection.execute(
            text("""
                SELECT revision, old_disposition, disposition, old_notes,
                       notes, actor_staff_id, occurred_at,
                       audit_event_id FROM public.day_review_history
                WHERE organization_id=:tenant AND day_review_id=:review
                ORDER BY revision
            """),
            {"tenant": tenant, "review": first.day_review_id},
        ).all()
        assert [
            (
                row.revision,
                row.old_disposition,
                row.disposition,
                row.old_notes,
                row.notes,
            )
            for row in history
        ] == [
            (1, None, "REVIEWED", None, "First review"),
            (2, "REVIEWED", "FINAL_CLOSED", "First review", "Final review"),
            (3, "FINAL_CLOSED", "REVIEWED", "Final review", "Amended"),
        ]
        assert all(row.actor_staff_id == doctor for row in history)
        assert all(row.occurred_at.microsecond == 0 for row in history)
        assert len({row.audit_event_id for row in history}) == 3
        assert connection.execute(
            text("""
                SELECT revision, disposition, notes, audit_event_id
                FROM public.day_reviews
                WHERE organization_id=:tenant AND id=:review
            """),
            {"tenant": tenant, "review": first.day_review_id},
        ).one() == (3, "REVIEWED", "Amended", history[-1].audit_event_id)
        for entry in history:
            assert connection.execute(
                text("""
                    SELECT action, actor_staff_id, occurred_at
                    FROM public.sensitive_audit
                    WHERE organization_id=:tenant AND id=:audit
                """),
                {"tenant": tenant, "audit": entry.audit_event_id},
            ).one() == ("DAY_REVIEW_RECORDED", doctor, entry.occurred_at)
        with pytest.raises(DBAPIError) as immutable, connection.begin_nested():
            connection.execute(
                text("""
                    UPDATE public.day_review_history SET notes='forged'
                    WHERE organization_id=:tenant AND day_review_id=:review AND revision=1
                """),
                {"tenant": tenant, "review": first.day_review_id},
            )
        assert immutable.value.orig.sqlstate == "23514"


def test_day_reviews_enforce_tenant_actor_context_and_operator_denial(
    appointment_storage,
    role_engine,
):
    runtime, migration, (tenant, other), (doctor, other_doctor) = appointment_storage
    with transaction(runtime, tenant) as connection:
        _as_actor(connection, doctor)
        review = _review(connection, tenant, DAY, 0, "REVIEWED", "Tenant A")
        with pytest.raises(DBAPIError) as forged, connection.begin_nested():
            _review(connection, other, NEXT_DAY, 0, "REVIEWED", "Foreign")
        assert forged.value.orig.sqlstate == "42501"
        _as_actor(connection, other_doctor)
        with pytest.raises(DBAPIError) as foreign_staff, connection.begin_nested():
            _review(connection, tenant, DAY, 1, "FINAL_CLOSED", "Foreign staff")
        assert foreign_staff.value.orig.sqlstate == "42501"
        _as_actor(connection, doctor)
        with pytest.raises(DBAPIError) as write_denied, connection.begin_nested():
            connection.execute(
                text("""
                    UPDATE public.day_reviews SET notes='forged'
                    WHERE organization_id=:tenant AND id=:review
                """),
                {"tenant": tenant, "review": review.day_review_id},
            )
        assert write_denied.value.orig.sqlstate == "42501"
    with transaction(runtime, other) as connection:
        assert connection.scalar(text("SELECT count(*) FROM public.day_reviews")) == 0
        assert (
            connection.scalar(text("SELECT count(*) FROM public.day_review_history"))
            == 0
        )
        _as_actor(connection, other_doctor)
        _review(connection, other, DAY, 0, "REVIEWED", "Tenant B")
        assert connection.scalar(text("SELECT count(*) FROM public.day_reviews")) == 1
    with runtime.connect() as connection:
        assert connection.scalar(text("SELECT count(*) FROM public.day_reviews")) == 0
        assert (
            connection.scalar(text("SELECT count(*) FROM public.day_review_history"))
            == 0
        )
        with pytest.raises(DBAPIError) as no_actor:
            _review(connection, tenant, NEXT_DAY, 0, "REVIEWED", "Missing session")
        assert no_actor.value.orig.sqlstate == "42501"
    operator = role_engine("carepulse_operator")
    try:
        with pytest.raises(DBAPIError) as denied, operator.connect() as connection:
            connection.execute(text("SELECT * FROM public.day_review_history"))
        assert denied.value.orig.sqlstate == "42501"
    finally:
        operator.dispose()
    with migration.connect() as connection:
        for table in ("day_reviews", "day_review_history"):
            assert connection.execute(
                text("""
                    SELECT relrowsecurity, relforcerowsecurity
                    FROM pg_class WHERE oid=to_regclass(:table)
                """),
                {"table": f"public.{table}"},
            ).one() == (True, True)
            for privilege in ("SELECT", "INSERT", "UPDATE", "DELETE", "TRUNCATE"):
                assert not connection.scalar(
                    text(
                        "SELECT has_table_privilege('carepulse_operator', :table, :privilege)"
                    ),
                    {"table": f"public.{table}", "privilege": privilege},
                )
        assert not connection.scalar(
            text("""
                SELECT has_function_privilege('carepulse_operator',
                    'public.record_day_review(uuid,date,bigint,text,text)', 'EXECUTE')
            """)
        )

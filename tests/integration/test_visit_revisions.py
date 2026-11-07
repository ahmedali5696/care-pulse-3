"""D-07 clinical corrections preserve terminal states and real before/after evidence."""

from uuid import uuid4

import pytest
from carepulse.db import transaction
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from test_appointment_persistence import appointment_storage as appointment_storage  # noqa: PLC0414
from test_visit_persistence import _complete, _receptionist, _seed, _visit


def _actor(connection, doctor, reason=None, kind="CORRECTION"):
    for name, value in (
        ("app.staff_id", str(doctor)),
        ("app.clinical_reason", reason or ""),
        ("app.clinical_change_kind", kind),
    ):
        connection.execute(
            text("SELECT set_config(:name, :value, true)"),
            {"name": name, "value": value},
        )


def test_receptionist_completion_preserves_independent_doctor_corrections(
    appointment_storage,
):
    runtime, migration, (tenant, _), (doctor, _) = appointment_storage
    receptionist = _receptionist(migration, tenant)
    with transaction(runtime, tenant) as connection:
        appointment = _seed(connection, tenant, doctor)
        visit = _visit(connection, appointment)
        _actor(connection, doctor, "Exam details")
        connection.execute(
            text(
                "UPDATE public.visits SET diagnosis='Original finding' WHERE id=:visit"
            ),
            {"visit": visit.id},
        )
        connection.execute(
            text("SELECT set_config('app.staff_id', :actor, true)"),
            {"actor": str(receptionist)},
        )
        completion = _complete(connection, tenant, appointment)
        assert _visit(connection, appointment).clinical_revision == 1
    with transaction(runtime, tenant) as connection:
        _actor(connection, doctor, "Post-completion correction")
        connection.execute(
            text(
                "UPDATE public.visits SET diagnosis='Corrected finding' WHERE id=:visit"
            ),
            {"visit": visit.id},
        )
        assert _visit(connection, appointment).clinical_revision == 2
        assert connection.execute(
            text("""
                SELECT revision, actor_staff_id, actor_role, change_kind
                FROM public.clinical_revisions
                WHERE visit_id=:visit ORDER BY revision
            """),
            {"visit": visit.id},
        ).all() == [
            (1, doctor, "DOCTOR_OWNER", "CORRECTION"),
            (2, doctor, "DOCTOR_OWNER", "CORRECTION"),
        ]
        assert connection.execute(
            text("""
                SELECT id, actor_staff_id, actor_role, completed_at
                FROM public.completion_attestations
                WHERE organization_id=:tenant AND appointment_id=:appointment
            """),
            {"tenant": tenant, "appointment": appointment},
        ).one() == (
            completion.attestation_id,
            receptionist,
            "RECEPTIONIST",
            completion.completed_at,
        )
        assert (
            connection.scalar(
                text("SELECT status FROM public.appointments WHERE id=:id"),
                {"id": appointment},
            )
            == "Completed"
        )


@pytest.mark.parametrize("terminal", ["Completed", "Cancelled"])
def test_terminal_doctor_correction_keeps_status_and_actual_revision(
    appointment_storage, terminal
):
    runtime, migration, (tenant, _), (doctor, _) = appointment_storage
    with transaction(runtime, tenant) as connection:
        appointment = _seed(
            connection, tenant, doctor, complete=terminal == "Completed"
        )
        if terminal == "Cancelled":
            connection.execute(
                text(
                    "UPDATE public.appointments SET status='Cancelled', revision=3 WHERE id=:id"
                ),
                {"id": appointment},
            )
            connection.execute(
                text("""
                    INSERT INTO public.appointment_history
                        (id, organization_id, appointment_id, appointment_revision,
                         action, old_status, new_status, old_service_date,
                         new_service_date, old_number, new_number, actor_staff_id,
                         actor_role, reason, price_snapshot_appointment_id)
                    SELECT :history, organization_id, id, 3, 'DOCTOR_EXCEPTION',
                           'In Examination', 'Cancelled', service_date, service_date,
                           current_number, current_number, :doctor, 'DOCTOR_OWNER',
                           'Synthetic exception', id
                    FROM public.appointments WHERE id=:id
                """),
                {"history": uuid4(), "doctor": doctor, "id": appointment},
            )
    with transaction(runtime, tenant) as connection:
        visit = _visit(connection, appointment)
        _actor(connection, doctor, "Corrected report")
        connection.execute(
            text("UPDATE public.visits SET diagnosis=:diagnosis WHERE id=:visit"),
            {"visit": visit.id, "diagnosis": "Synthetic corrected diagnosis"},
        )
        record = connection.execute(
            text("""
                SELECT revision, change_kind, old_values, new_values, actor_staff_id,
                       actor_role, occurred_at, reason
                FROM public.clinical_revisions WHERE visit_id=:visit
            """),
            {"visit": visit.id},
        ).one()
        assert record.revision == 1 and record.change_kind == "CORRECTION"
        assert record.old_values["diagnosis"] is None
        assert record.new_values["diagnosis"] == "Synthetic corrected diagnosis"
        assert record.actor_staff_id == doctor and record.actor_role == "DOCTOR_OWNER"
        assert (
            record.occurred_at.microsecond == 0 and record.reason == "Corrected report"
        )
        assert (
            connection.scalar(
                text("SELECT clinical_revision FROM public.visits WHERE id=:id"),
                {"id": visit.id},
            )
            == 1
        )
        assert (
            connection.scalar(
                text("SELECT status FROM public.appointments WHERE id=:id"),
                {"id": appointment},
            )
            == terminal
        )
    with pytest.raises(DBAPIError) as denied, migration.begin() as connection:
        connection.execute(
            text(
                "UPDATE public.clinical_revisions SET reason='Tampered' WHERE visit_id=:id"
            ),
            {"id": visit.id},
        )
    assert denied.value.orig.sqlstate == "23514"
    with pytest.raises(DBAPIError) as denied, migration.begin() as connection:
        connection.execute(
            text("DELETE FROM public.clinical_revisions WHERE visit_id=:id"),
            {"id": visit.id},
        )
    assert denied.value.orig.sqlstate == "23514"


@pytest.mark.parametrize(
    "actor_kind,reason,kind,expected_state",
    [
        ("receptionist", "Reason", "CORRECTION", "42501"),
        ("foreign_doctor", "Reason", "CORRECTION", "42501"),
        ("doctor", None, "CORRECTION", "23514"),
        ("doctor", None, "DECISION_DEFAULT", "23514"),
        ("doctor", None, "DECISION_EXCEPTION", "23514"),
    ],
)
def test_unauthorized_or_unjustified_correction_denied(
    appointment_storage, actor_kind, reason, kind, expected_state
):
    runtime, migration, (tenant, _), (doctor, other_doctor) = appointment_storage
    receptionist = (
        _receptionist(migration, tenant) if actor_kind == "receptionist" else None
    )
    with transaction(runtime, tenant) as connection:
        appointment = _seed(connection, tenant, doctor, complete=True)
        visit = _visit(connection, appointment)
        if actor_kind == "receptionist":
            actor = receptionist
        elif actor_kind == "foreign_doctor":
            actor = other_doctor
        else:
            actor = doctor
        _actor(connection, actor, reason, kind)
        with pytest.raises(DBAPIError) as denied, connection.begin_nested():
            connection.execute(
                text("UPDATE public.visits SET diagnosis='Unauthorized' WHERE id=:id"),
                {"id": visit.id},
            )
        assert denied.value.orig.sqlstate == expected_state
        assert (
            connection.scalar(
                text(
                    "SELECT count(*) FROM public.clinical_revisions WHERE visit_id=:id"
                ),
                {"id": visit.id},
            )
            == 0
        )


def test_default_decision_is_only_reasonless_revision(appointment_storage):
    runtime, _, (tenant, _), (doctor, _) = appointment_storage
    with transaction(runtime, tenant) as connection:
        appointment = _seed(connection, tenant, doctor, complete=True)
        visit = _visit(connection, appointment)
        _actor(connection, doctor, kind="DECISION_DEFAULT")
        connection.execute(
            text("""
                UPDATE public.visits SET followup_decision=CAST(:decision AS jsonb)
                WHERE id=:id
            """),
            {"decision": '{"kind":"DEFAULT"}', "id": visit.id},
        )
        assert connection.execute(
            text("""
                SELECT revision, change_kind, reason,
                       old_values->>'followup_decision',
                       new_values->'followup_decision'->>'kind'
                FROM public.clinical_revisions WHERE visit_id=:id
            """),
            {"id": visit.id},
        ).one() == (1, "DECISION_DEFAULT", None, None, "DEFAULT")
        with pytest.raises(DBAPIError) as denied, connection.begin_nested():
            connection.execute(
                text("""
                    INSERT INTO public.clinical_revisions
                        (id, organization_id, visit_id, revision, change_kind,
                         old_values, new_values, actor_staff_id, actor_role)
                    VALUES (:id, :tenant, :visit, 2, 'DECISION_DEFAULT',
                            '{}'::jsonb, '{}'::jsonb, :doctor, 'DOCTOR_OWNER')
                """),
                {"id": uuid4(), "tenant": tenant, "visit": visit.id, "doctor": doctor},
            )
        assert denied.value.orig.sqlstate == "42501"


def test_disabled_doctor_cannot_edit_clinical_content(appointment_storage):
    runtime, _, (tenant, _), (doctor, _) = appointment_storage
    with transaction(runtime, tenant) as connection:
        appointment = _seed(connection, tenant, doctor, complete=True)
        visit = _visit(connection, appointment)
        _actor(connection, doctor, "Attempt during disable")
        connection.execute(
            text("""
                UPDATE public.staff_accounts
                SET enabled=false, security_revision=security_revision+1
                WHERE id=:doctor
            """),
            {"doctor": doctor},
        )
        with pytest.raises(DBAPIError) as denied, connection.begin_nested():
            connection.execute(
                text("UPDATE public.visits SET diagnosis='Denied' WHERE id=:id"),
                {"id": visit.id},
            )
        assert denied.value.orig.sqlstate == "42501"
        connection.execute(
            text("""
                UPDATE public.staff_accounts
                SET enabled=true, security_revision=security_revision+1
                WHERE id=:doctor
            """),
            {"doctor": doctor},
        )
        assert (
            connection.scalar(
                text(
                    "SELECT count(*) FROM public.clinical_revisions WHERE visit_id=:id"
                ),
                {"id": visit.id},
            )
            == 0
        )

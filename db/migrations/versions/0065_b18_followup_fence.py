"""Permit only the prepared signed B-18 writer through the B-10 move fence."""

from alembic import op

revision = "0065_b18_followup_fence"
down_revision = "0064_b18_source_alias"
branch_labels = None
depends_on = None


def upgrade() -> None:
    connection = op.get_bind()
    definition = connection.exec_driver_sql(
        "SELECT pg_get_functiondef('public.guard_followup_move()'::regprocedure)"
    ).scalar_one()
    marker = "(OLD.service_date, OLD.booking_mode, OLD.starts_at, OLD.ends_at) THEN"
    if definition.count(marker) != 1:
        raise RuntimeError("Missing unique B-10 movement fence marker")
    connection.exec_driver_sql(
        definition.replace(
            marker,
            """(OLD.service_date, OLD.booking_mode, OLD.starts_at, OLD.ends_at)
               AND NOT EXISTS (SELECT 1 FROM public.f01_b18_authority() auth
                   WHERE session_user='carepulse_api' AND auth.stage='PRIMARY'
                       AND auth.number IS NOT NULL AND NOT auth.finished
                       AND auth.organization_id=OLD.organization_id AND auth.target_id=OLD.id
                       AND (auth.role='RECEPTIONIST' OR auth.staff_id=OLD.doctor_staff_id)
                       AND OLD.status IN ('Booked','Waiting') AND OLD.classification='ROUTINE'
                       AND OLD.lane='MAIN'
                       AND OLD.revision=(auth.command->>'expected_appointment_revision')::bigint
                       AND NEW.revision=OLD.revision+1 AND NEW.current_number=auth.number
                       AND NEW.service_date=(auth.command->>'service_date')::date
                       AND NEW.booking_mode=CASE WHEN auth.command->>'starts_at' IS NULL
                           THEN 'OPEN' ELSE 'EXACT_TIME' END
                       AND NEW.starts_at IS NOT DISTINCT FROM (auth.command->>'starts_at')::timestamptz
                       AND NEW.ends_at IS NOT DISTINCT FROM (auth.command->>'ends_at')::timestamptz
                       AND NEW.status=CASE WHEN NEW.service_date<>OLD.service_date THEN 'Booked'
                           ELSE OLD.status END) THEN""",
        ),
        execution_options={"no_parameters": True},
    )


def downgrade() -> None:
    raise RuntimeError("B-18 Follow-up movement fence migration is forward-only")

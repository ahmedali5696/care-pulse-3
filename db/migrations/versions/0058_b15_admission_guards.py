"""B-15: extend inherited B-12 queue guards for signed admission closure."""

from alembic import op

revision = "0058_b15_admission_guards"
down_revision = "0057_w3_queue_admit"
branch_labels = None
depends_on = None


def _rewrite(signature: str, marker: str, replacement: str) -> None:
    connection = op.get_bind()
    definition = connection.exec_driver_sql(
        f"SELECT pg_get_functiondef('{signature}'::regprocedure)"
    ).scalar_one()
    if definition.count(marker) != 1:
        raise RuntimeError(f"Missing unique B-15 guard marker in {signature}")
    connection.exec_driver_sql(
        definition.replace(marker, replacement),
        execution_options={"no_parameters": True},
    )


def upgrade() -> None:
    # The original B-12 guards distinguish arrivals/rank shifts. Only the
    # admission writer may instead prove a signed, history-backed closure.
    op.execute("""
        CREATE FUNCTION public.f01_b15_closed_entry_witness()
        RETURNS SETOF public.queue_entries
        LANGUAGE sql VOLATILE SET search_path = pg_catalog, pg_temp AS $$
            SELECT e.*
            FROM public.f01_b15_authority() a
            JOIN public.queue_entries e ON e.organization_id=a.organization_id
                AND e.appointment_id=a.target_id AND e.patient_id=a.patient_id
            JOIN public.appointments x ON x.organization_id=e.organization_id
                AND x.id=e.appointment_id AND x.patient_id=e.patient_id
                AND x.doctor_staff_id=e.doctor_staff_id
                AND x.service_date=e.service_date AND x.lane=e.lane
            JOIN public.appointment_history h ON h.organization_id=x.organization_id
                AND h.appointment_id=x.id AND h.appointment_revision=x.revision
            WHERE a.stage='PRIMARY'
                AND e.service_date=(a.command->>'date')::date
                AND e.lane=a.command->>'lane'
                AND x.status='In Examination'
                AND x.revision=(a.command->>'expected_appointment_revision')::bigint+1
                AND e.disposition='CLOSED'
                AND e.revision=(a.command->>'expected_queue_entry_revision')::bigint+1
                AND e.closure_history_revision=x.revision
                AND e.closure_queue_revision=(a.command->>'expected_queue_day_revision')::bigint
                AND e.closure_transaction_id=pg_current_xact_id()
                AND h.action='ADMITTED' AND h.old_status='Waiting'
                AND h.new_status='In Examination'
                AND h.old_service_date=e.service_date AND h.new_service_date=e.service_date
                AND h.old_number=x.current_number AND h.new_number=x.current_number
                AND h.actor_staff_id=a.staff_id AND h.actor_role=a.role
                AND h.reason IS NOT DISTINCT FROM a.command->>'non_head_reason'
                AND h.price_snapshot_appointment_id=x.id AND h.replacement_appointment_id IS NULL;
        $$;
        REVOKE ALL ON FUNCTION public.f01_b15_closed_entry_witness() FROM PUBLIC;
        ALTER FUNCTION public.f01_guard_queue_arrival_increment() SECURITY DEFINER;
    """)
    _rewrite(
        "public.f01_guard_queue_arrival_increment()",
        "ELSIF NEW.arrival_counter <> OLD.arrival_counter + 1 THEN",
        """ELSIF NEW.arrival_counter = OLD.arrival_counter
                    AND NEW.revision=OLD.revision+1
                    AND EXISTS (
                        SELECT 1 FROM public.f01_b15_closed_entry_witness() e
                        WHERE e.organization_id=OLD.organization_id
                            AND e.queue_day_id=OLD.id AND e.doctor_staff_id=OLD.doctor_staff_id
                            AND e.service_date=OLD.service_date AND e.lane=OLD.lane
                            AND e.closure_queue_revision=OLD.revision
                            AND e.closure_routine_cursor=OLD.routine_cursor
                    ) THEN
                    RETURN NEW;
                ELSIF NEW.arrival_counter <> OLD.arrival_counter + 1 THEN""",
    )
    _rewrite(
        "public.f01_assert_queue_rank_shift()",
        "IF OLD.routine_rank IS NULL OR NEW.routine_rank IS DISTINCT FROM OLD.routine_rank + 1",
        """IF OLD.disposition='ACTIVE' AND NEW.disposition='CLOSED'
                AND NEW.routine_rank IS NOT DISTINCT FROM OLD.routine_rank
                AND NEW.revision=OLD.revision+1
                AND EXISTS (
                    SELECT 1 FROM public.f01_b15_closed_entry_witness() e
                    JOIN public.queue_days q ON q.organization_id=e.organization_id
                        AND q.id=e.queue_day_id AND q.doctor_staff_id=e.doctor_staff_id
                        AND q.service_date=e.service_date AND q.lane=e.lane
                    WHERE e.organization_id=NEW.organization_id AND e.id=NEW.id
                        AND e.appointment_id=NEW.appointment_id AND e.patient_id=NEW.patient_id
                        AND e.queue_day_id=NEW.queue_day_id AND e.doctor_staff_id=NEW.doctor_staff_id
                        AND e.service_date=NEW.service_date AND e.lane=NEW.lane
                        AND e.revision=NEW.revision
                        AND e.routine_rank IS NOT DISTINCT FROM NEW.routine_rank
                        AND e.closure_history_revision=NEW.closure_history_revision
                        AND e.closure_queue_revision=NEW.closure_queue_revision
                        AND e.closure_transaction_id=NEW.closure_transaction_id
                        AND q.revision=e.closure_queue_revision+1
                        AND q.revision_transaction_id=pg_current_xact_id()
                ) THEN
                RETURN NULL;
            END IF;
            IF OLD.routine_rank IS NULL OR NEW.routine_rank IS DISTINCT FROM OLD.routine_rank + 1""",
    )


def downgrade() -> None:
    raise RuntimeError("B-15 admission guard migration is forward-only")

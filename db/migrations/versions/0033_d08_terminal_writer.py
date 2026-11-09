"""D-08: atomic terminal outcomes with protected Follow-up claim settlement."""

from alembic import op

revision = "0033_d08_terminal_writer"
down_revision = "0032_b10_cairo_offset_guard"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
        LOCK TABLE public.appointments, public.followup_claims,
            public.queue_entries, public.queue_days IN SHARE ROW EXCLUSIVE MODE
    """)
    # The original D-07 completion routine still owns positive examination
    # provenance. Only the new terminal entrypoint may settle a Follow-up claim.
    op.execute("""
        ALTER FUNCTION public.complete_examined_visit(uuid,uuid,bigint,boolean)
            RENAME TO d07_complete_examined_visit_raw;
        REVOKE ALL ON FUNCTION
            public.d07_complete_examined_visit_raw(uuid,uuid,bigint,boolean)
            FROM PUBLIC, carepulse_runtime;
    """)
    op.execute("""
        CREATE FUNCTION public.guard_d08_terminal_transition()
        RETURNS trigger LANGUAGE plpgsql SET search_path = pg_catalog AS $$
        BEGIN
            IF OLD.service_type = 'FOLLOW_UP'
               AND NEW.status IN ('Completed','Cancelled','No-show')
               AND OLD.status IS DISTINCT FROM NEW.status
               AND current_user <> 'carepulse_migration' THEN
                RAISE EXCEPTION 'Terminal change requires protected writer'
                    USING ERRCODE = '42501';
            END IF;
            RETURN NEW;
        END $$;
        CREATE TRIGGER a0_d08_terminal_transition_fence
        BEFORE UPDATE ON public.appointments FOR EACH ROW
        EXECUTE FUNCTION public.guard_d08_terminal_transition();
        REVOKE ALL ON FUNCTION public.guard_d08_terminal_transition()
            FROM PUBLIC, carepulse_runtime;
    """)
    # These newly reserved audit actions are authoritative only when emitted
    # by the migration-owned terminal writer, not via runtime INSERT privileges.
    op.execute("""
        CREATE FUNCTION public.guard_d08_terminal_audit()
        RETURNS trigger LANGUAGE plpgsql SET search_path = pg_catalog AS $$
        BEGIN
            IF NEW.action IN (
                'APPOINTMENT_CANCELLED',
                'APPOINTMENT_MARKED_NO_SHOW',
                'VISIT_EXAMINATION_CANCELLED'
            ) AND current_user <> 'carepulse_migration' THEN
                RAISE EXCEPTION 'Protected terminal audit action'
                    USING ERRCODE = '42501';
            END IF;
            RETURN NEW;
        END $$;
        CREATE TRIGGER sensitive_audit_d08_terminal_guard
        BEFORE INSERT ON public.sensitive_audit FOR EACH ROW
        EXECUTE FUNCTION public.guard_d08_terminal_audit();
        REVOKE ALL ON FUNCTION public.guard_d08_terminal_audit()
            FROM PUBLIC, carepulse_runtime;
    """)
    op.execute("""
        CREATE FUNCTION public.transition_terminal_appointment(
            target_organization uuid, target_appointment uuid,
            expected_revision bigint, command jsonb)
        RETURNS TABLE (terminal_history_revision bigint,
                       terminal_at timestamptz, attestation_id uuid)
        LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog AS $$
        DECLARE
            actor uuid;
            actor_role text;
            requested_operation text := command->>'operation';
            reason text := command->>'reason';
            proposed public.appointments%ROWTYPE;
            locked public.appointments%ROWTYPE;
            claim public.followup_claims%ROWTYPE;
            source_appointment uuid;
            source_visit uuid;
            locked_id uuid;
            queue_day uuid;
            queue_entry uuid;
            retained_visit uuid;
            exam_attestation uuid;
            next_revision bigint;
            event_at timestamptz;
            transition_history_id uuid;
            expected_day_revision bigint;
        BEGIN
            IF session_user <> 'carepulse_runtime'
               OR current_setting('transaction_isolation') <> 'read committed'
               OR target_organization IS NULL OR target_appointment IS NULL
               OR expected_revision IS NULL OR expected_revision < 0
               OR current_setting('app.organization_id', true)
                    IS DISTINCT FROM target_organization::text
               OR NULLIF(current_setting('app.staff_id', true), '') IS NULL
               OR jsonb_typeof(command) IS DISTINCT FROM 'object'
               OR requested_operation NOT IN ('COMPLETE','CANCEL','MARK_NO_SHOW')
               OR (command - 'operation' - 'reason'
                           - 'examination_occurred' - 'absence_confirmed'
                           - 'expected_claim_revision'
                           - 'expected_source_revision'
                           - 'expected_authorization_revision'
                           - 'expected_queue_day_revision') <> '{}'::jsonb
               OR jsonb_typeof(command->'operation') IS DISTINCT FROM 'string' THEN
                RAISE EXCEPTION 'Terminal request denied' USING ERRCODE = '42501';
            END IF;
            IF requested_operation = 'COMPLETE' THEN
                IF (command - 'operation' - 'examination_occurred'
                            - 'expected_claim_revision' - 'expected_source_revision'
                            - 'expected_authorization_revision') <> '{}'::jsonb
                   OR command->'examination_occurred' IS DISTINCT FROM 'true'::jsonb THEN
                    RAISE EXCEPTION 'Positive examination assertion required'
                        USING ERRCODE = '23514';
                END IF;
            ELSIF jsonb_typeof(command->'reason') IS DISTINCT FROM 'string'
               OR reason IS NULL OR reason = '' OR reason <> btrim(reason)
               OR reason ~ '[[:cntrl:]]' THEN
                RAISE EXCEPTION 'Reasoned terminal action required'
                    USING ERRCODE = '23514';
            ELSIF requested_operation = 'MARK_NO_SHOW' THEN
                IF (command - 'operation' - 'reason' - 'absence_confirmed'
                            - 'expected_claim_revision' - 'expected_source_revision'
                            - 'expected_authorization_revision'
                            - 'expected_queue_day_revision') <> '{}'::jsonb
                   OR command->'absence_confirmed' IS DISTINCT FROM 'true'::jsonb THEN
                    RAISE EXCEPTION 'Verified absence required' USING ERRCODE = '23514';
                END IF;
            ELSIF (command - 'operation' - 'reason'
                        - 'expected_claim_revision' - 'expected_source_revision'
                        - 'expected_authorization_revision'
                        - 'expected_queue_day_revision') <> '{}'::jsonb THEN
                RAISE EXCEPTION 'Unsupported cancellation fields' USING ERRCODE = '23514';
            END IF;
            actor := current_setting('app.staff_id', true)::uuid;
            PERFORM 1 FROM public.organizations
            WHERE id = target_organization AND state = 'ACTIVE' FOR SHARE;
            IF NOT FOUND THEN
                RAISE EXCEPTION 'Clinic inactive' USING ERRCODE = '42501';
            END IF;
            SELECT s.role INTO actor_role FROM public.staff_accounts s
            JOIN public."user" u ON u.id = s.auth_user_id
            WHERE s.organization_id = target_organization AND s.id = actor
              AND s.role IN ('DOCTOR_OWNER','RECEPTIONIST')
              AND s.enabled AND s.verified AND s.reset_block_operation_id IS NULL
              AND u."emailVerified" AND u.email = s.verified_email
            FOR SHARE OF s, u;
            IF NOT FOUND THEN
                RAISE EXCEPTION 'Terminal actor inactive' USING ERRCODE = '42501';
            END IF;
            -- These initial reads only discover immutable identifiers. All mutable
            -- state is checked again after the prescribed row locks.
            SELECT * INTO proposed FROM public.appointments
            WHERE organization_id = target_organization AND id = target_appointment;
            IF NOT FOUND THEN
                RAISE EXCEPTION 'Appointment unavailable' USING ERRCODE = '23514';
            END IF;
            IF proposed.service_type IS DISTINCT FROM 'FOLLOW_UP'
               OR proposed.classification IS DISTINCT FROM 'ROUTINE' THEN
                RAISE EXCEPTION 'Terminal writer is Follow-up only'
                    USING ERRCODE = '42501';
            END IF;
            SELECT c.source_appointment_id, c.source_visit_id
            INTO source_appointment, source_visit
            FROM public.followup_claims c
            WHERE c.organization_id = target_organization
              AND c.appointment_id = target_appointment
              AND c.state = 'ACTIVE';
            IF NOT FOUND THEN
                RAISE EXCEPTION 'Active preferential Follow-up claim unavailable'
                    USING ERRCODE = '23514';
            END IF;
            PERFORM 1 FROM public.patients
            WHERE organization_id = target_organization
              AND id = proposed.patient_id FOR UPDATE;
            IF NOT FOUND THEN
                RAISE EXCEPTION 'Patient unavailable' USING ERRCODE = '23514';
            END IF;
            -- D-11's Waiting-exit trigger also takes SCHEDULE; take it before
            -- existing Appointment locks to avoid reversing the policy barrier.
            PERFORM 1 FROM public.clinic_policy_heads
            WHERE organization_id = target_organization AND kind = 'SCHEDULE'
            FOR SHARE;
            IF NOT FOUND THEN
                RAISE EXCEPTION 'Schedule barrier missing' USING ERRCODE = '23514';
            END IF;
            PERFORM 1 FROM public.clinic_policy_heads
            WHERE organization_id = target_organization AND kind = 'FOLLOWUP'
            FOR SHARE;
            IF NOT FOUND THEN
                RAISE EXCEPTION 'Follow-up policy barrier missing'
                    USING ERRCODE = '23514';
            END IF;
            IF proposed.status = 'Waiting' THEN
                IF NOT COALESCE((command->>'expected_queue_day_revision')
                                ~ '^[1-9][0-9]*$',false) THEN
                    RAISE EXCEPTION 'Waiting queue revision required'
                        USING ERRCODE = '23514';
                END IF;
                expected_day_revision :=
                    (command->>'expected_queue_day_revision')::bigint;
                PERFORM 1 FROM public.doctor_controls
                WHERE organization_id = target_organization
                  AND doctor_staff_id = proposed.doctor_staff_id FOR UPDATE;
                IF NOT FOUND THEN
                    RAISE EXCEPTION 'Doctor control unavailable'
                        USING ERRCODE = '23514';
                END IF;
                SELECT q.id INTO queue_day FROM public.queue_days q
                WHERE q.organization_id = target_organization
                  AND q.doctor_staff_id = proposed.doctor_staff_id
                  AND q.service_date = proposed.service_date
                  AND q.lane = proposed.lane FOR UPDATE;
                IF NOT FOUND THEN
                    RAISE EXCEPTION 'Waiting QueueDay missing' USING ERRCODE = '23514';
                END IF;
                IF NOT EXISTS (
                    SELECT 1 FROM public.queue_days q
                    WHERE q.organization_id = target_organization AND q.id = queue_day
                      AND q.revision = expected_day_revision
                ) THEN
                    RAISE EXCEPTION 'Stale Waiting QueueDay' USING ERRCODE = '40001';
                END IF;
            END IF;
            -- A Follow-up terminal has two existing Appointments, potentially
            -- ordered either way. Both precede source Visit and claim locks.
            FOR locked_id IN
                SELECT DISTINCT ids.id FROM unnest(
                    ARRAY[target_appointment, source_appointment]) AS ids(id)
                WHERE ids.id IS NOT NULL ORDER BY ids.id
            LOOP
                PERFORM 1 FROM public.appointments
                WHERE organization_id = target_organization
                  AND id = locked_id FOR UPDATE;
                IF NOT FOUND THEN
                    RAISE EXCEPTION 'Terminal Appointment link missing'
                        USING ERRCODE = '23514';
                END IF;
            END LOOP;
            SELECT * INTO STRICT locked FROM public.appointments
            WHERE organization_id = target_organization AND id = target_appointment;
            IF locked.revision <> expected_revision
               OR (locked.patient_id, locked.doctor_staff_id, locked.service_date,
                   locked.status, locked.service_type)
                  IS DISTINCT FROM
                  (proposed.patient_id, proposed.doctor_staff_id, proposed.service_date,
                   proposed.status, proposed.service_type) THEN
                RAISE EXCEPTION 'Stale terminal Appointment'
                    USING ERRCODE = '40001';
            END IF;
            IF locked.service_type IS DISTINCT FROM 'FOLLOW_UP'
               OR locked.classification IS DISTINCT FROM 'ROUTINE' THEN
                RAISE EXCEPTION 'Terminal writer is Follow-up only'
                    USING ERRCODE = '42501';
            END IF;
            IF locked.status <> 'Waiting'
               AND command ? 'expected_queue_day_revision' THEN
                RAISE EXCEPTION 'Unexpected terminal revision token'
                    USING ERRCODE = '23514';
            END IF;
            IF (requested_operation = 'COMPLETE'
                AND locked.status <> 'In Examination')
               OR (requested_operation = 'CANCEL'
                   AND locked.status NOT IN ('Booked','Waiting','In Examination'))
               OR (requested_operation = 'MARK_NO_SHOW'
                   AND (locked.status <> 'Booked'
                        OR locked.service_date >=
                           (clock_timestamp() AT TIME ZONE 'Africa/Cairo')::date)) THEN
                RAISE EXCEPTION 'Unsupported terminal transition'
                    USING ERRCODE = '23514';
            END IF;
            IF requested_operation = 'CANCEL'
               AND locked.status = 'In Examination'
               AND (actor_role <> 'DOCTOR_OWNER'
                    OR actor <> locked.doctor_staff_id) THEN
                RAISE EXCEPTION 'Exam cancellation requires Doctor'
                    USING ERRCODE = '42501';
            END IF;
            PERFORM 1 FROM public.visits WHERE organization_id = target_organization
                AND id = source_visit FOR UPDATE;
            IF NOT FOUND THEN
                RAISE EXCEPTION 'Claim source Visit missing' USING ERRCODE = '23514';
            END IF;
            PERFORM 1 FROM public.followup_source_state
            WHERE organization_id = target_organization
              AND visit_id = source_visit FOR UPDATE;
            IF NOT FOUND THEN
                RAISE EXCEPTION 'Claim source state missing' USING ERRCODE = '23514';
            END IF;
            IF locked.status = 'In Examination' THEN
                SELECT id INTO retained_visit FROM public.visits
                WHERE organization_id = target_organization
                  AND appointment_id = target_appointment FOR UPDATE;
                IF NOT FOUND THEN
                    RAISE EXCEPTION 'Exam Visit missing' USING ERRCODE = '23514';
                END IF;
            END IF;
            SELECT * INTO claim FROM public.followup_claims c
            WHERE c.organization_id = target_organization
              AND c.appointment_id = target_appointment FOR UPDATE;
            IF NOT FOUND OR claim.state <> 'ACTIVE'
               OR claim.source_visit_id <> source_visit
               OR claim.source_appointment_id <> source_appointment
               OR claim.patient_id <> locked.patient_id
               OR claim.doctor_staff_id <> locked.doctor_staff_id
               OR NOT EXISTS (
                   SELECT 1 FROM public.appointment_price_snapshots p
                   WHERE p.organization_id = target_organization
                     AND p.appointment_id = target_appointment
                     AND p.service_type = 'FOLLOW_UP'
                     AND p.classification = 'ROUTINE'
                     AND p.entitlement_provenance = claim.policy_snapshot
               ) THEN
                RAISE EXCEPTION 'Follow-up claim no longer active'
                    USING ERRCODE = '40001';
            END IF;
            IF NOT (command ?& ARRAY['expected_claim_revision',
                                     'expected_source_revision',
                                     'expected_authorization_revision'])
               OR NOT COALESCE((command->>'expected_claim_revision')
                               ~ '^[1-9][0-9]*$',false)
               OR NOT COALESCE((command->>'expected_source_revision')
                               ~ '^(0|[1-9][0-9]*)$',false)
               OR NOT COALESCE((command->>'expected_authorization_revision')
                               ~ '^(0|[1-9][0-9]*)$',false)
               OR claim.revision::text IS DISTINCT FROM
                   command->>'expected_claim_revision' THEN
                RAISE EXCEPTION 'Stale terminal claim' USING ERRCODE = '40001';
            END IF;
            IF NOT EXISTS (
                SELECT 1 FROM public.followup_source_state s
                WHERE s.organization_id = target_organization
                  AND s.visit_id = source_visit
                  AND s.source_revision::text =
                      command->>'expected_source_revision'
            ) THEN
                RAISE EXCEPTION 'Stale terminal source' USING ERRCODE = '40001';
            END IF;
            -- Read the ledger in a fresh statement after the claim-lock wait.
            IF (SELECT COALESCE(max(e.authorization_revision),0)::text
                FROM public.followup_authorization_events e
                WHERE e.organization_id = target_organization
                  AND e.claim_id = claim.id)
               IS DISTINCT FROM command->>'expected_authorization_revision' THEN
                RAISE EXCEPTION 'Stale terminal authorization'
                    USING ERRCODE = '40001';
            END IF;
            IF requested_operation = 'COMPLETE' THEN
                SELECT c.attestation_id, c.completion_history_revision, c.completed_at
                INTO exam_attestation, next_revision, event_at
                FROM public.d07_complete_examined_visit_raw(
                    target_organization, target_appointment, expected_revision, TRUE) c;
            ELSE
                transition_history_id := gen_random_uuid();
                UPDATE public.appointments SET status = CASE requested_operation
                    WHEN 'CANCEL' THEN 'Cancelled' ELSE 'No-show' END,
                    revision = revision + 1
                WHERE organization_id = target_organization AND id = target_appointment;
                next_revision := expected_revision + 1;
                INSERT INTO public.appointment_history (
                    id, organization_id, appointment_id, appointment_revision,
                    action, old_status, new_status,
                    old_service_date, new_service_date, old_number, new_number,
                    actor_staff_id, actor_role, reason, price_snapshot_appointment_id)
                VALUES (transition_history_id, target_organization, target_appointment,
                    next_revision, CASE WHEN locked.status = 'In Examination'
                        THEN 'DOCTOR_EXCEPTION' ELSE 'STATUS' END, locked.status,
                    CASE requested_operation WHEN 'CANCEL' THEN 'Cancelled'
                        ELSE 'No-show' END,
                    locked.service_date, locked.service_date,
                    locked.current_number, locked.current_number,
                    actor, actor_role, reason, target_appointment)
                RETURNING occurred_at INTO event_at;
                IF locked.status = 'Waiting' THEN
                    UPDATE public.queue_entries SET disposition = 'CLOSED',
                        closure_history_revision = next_revision,
                        revision = revision + 1
                    WHERE organization_id = target_organization
                      AND appointment_id = target_appointment
                      AND queue_day_id = queue_day AND disposition = 'ACTIVE'
                    RETURNING id INTO queue_entry;
                    IF queue_entry IS NULL THEN
                        RAISE EXCEPTION 'Waiting entry unavailable' USING ERRCODE = '23514';
                    END IF;
                    UPDATE public.queue_days SET revision = revision + 1
                    WHERE organization_id = target_organization AND id = queue_day;
                END IF;
                INSERT INTO public.sensitive_audit (
                    id, organization_id, actor_staff_id, actor_role, action, reason,
                    correlation_id, before_state, after_state, occurred_at)
                VALUES (gen_random_uuid(), target_organization, actor, actor_role,
                    CASE WHEN locked.status = 'In Examination'
                         THEN 'VISIT_EXAMINATION_CANCELLED'
                         WHEN requested_operation = 'MARK_NO_SHOW'
                         THEN 'APPOINTMENT_MARKED_NO_SHOW'
                         ELSE 'APPOINTMENT_CANCELLED' END,
                    reason, transition_history_id::text,
                    jsonb_build_object('appointment_id', target_appointment,
                                       'revision', expected_revision, 'status', locked.status,
                                       'history_id', transition_history_id,
                                       'claim_id', claim.id,
                                       'source_visit_id', source_visit,
                                       'source_appointment_id', source_appointment,
                                       'visit_id', retained_visit),
                    jsonb_build_object('appointment_id', target_appointment,
                                       'revision', next_revision,
                                       'history_id', transition_history_id,
                                       'claim_id', claim.id,
                                       'source_visit_id', source_visit,
                                       'source_appointment_id', source_appointment,
                                       'visit_id', retained_visit,
                                       'status', CASE requested_operation
                                                WHEN 'CANCEL' THEN 'Cancelled'
                                                ELSE 'No-show' END)
                        || CASE WHEN requested_operation = 'MARK_NO_SHOW'
                            THEN jsonb_build_object('absence_confirmed', true)
                            ELSE '{}'::jsonb END,
                    event_at);
            END IF;
            UPDATE public.followup_claims SET
                state = CASE requested_operation WHEN 'COMPLETE'
                             THEN 'CONSUMED' ELSE 'RELEASED' END,
                revision = revision + 1,
                terminal_history_revision = next_revision,
                terminal_at = event_at
            WHERE organization_id = target_organization
              AND id = claim.id AND state = 'ACTIVE';
            IF NOT FOUND THEN
                RAISE EXCEPTION 'Claim terminal settlement lost'
                    USING ERRCODE = '40001';
            END IF;
            terminal_history_revision := next_revision;
            terminal_at := event_at;
            attestation_id := exam_attestation;
            RETURN NEXT;
        END $$
    """)
    # Preserve the D-07 public signature for existing New Examination callers.
    # Preferential completion must settle its claim through the terminal writer.
    op.execute("""
        CREATE FUNCTION public.complete_examined_visit(
            target_organization uuid, target_appointment uuid,
            expected_revision bigint, examination_occurred boolean)
        RETURNS TABLE (attestation_id uuid,
                       completion_history_revision bigint,
                       completed_at timestamptz)
        LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog AS $$
        BEGIN
            IF session_user <> 'carepulse_runtime'
               OR examination_occurred IS DISTINCT FROM TRUE
               OR NOT EXISTS (
                   SELECT 1 FROM public.appointments
                   WHERE organization_id = target_organization
                     AND id = target_appointment AND service_type = 'NEW_EXAMINATION'
               ) THEN
                RAISE EXCEPTION 'Legacy completion is New Examination only'
                    USING ERRCODE = '42501';
            END IF;
            RETURN QUERY
                SELECT c.attestation_id, c.completion_history_revision, c.completed_at
                FROM public.d07_complete_examined_visit_raw(
                    target_organization, target_appointment,
                    expected_revision, examination_occurred) c;
        END $$
    """)
    # This is a migration/acceptance foundation; no new runtime terminal
    # command is authorized until its later signed API and review.
    op.execute("""
        REVOKE ALL ON FUNCTION
            public.transition_terminal_appointment(uuid,uuid,bigint,jsonb)
            FROM PUBLIC, carepulse_runtime;
        REVOKE ALL ON FUNCTION
            public.complete_examined_visit(uuid,uuid,bigint,boolean)
            FROM PUBLIC;
        GRANT EXECUTE ON FUNCTION
            public.complete_examined_visit(uuid,uuid,bigint,boolean)
            TO carepulse_runtime;
    """)


def downgrade() -> None:
    raise RuntimeError("D-08 protected terminal writer is forward-only")

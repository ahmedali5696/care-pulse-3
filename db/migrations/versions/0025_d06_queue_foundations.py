"""D-06: dated queue, shared Doctor control and scoped approval foundations.

No operational admission commands or HTTP endpoints are installed here.
"""

from alembic import op

revision = "0025_d06_queue_foundations"
down_revision = "0024_b06_followup_window"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # No earlier schema recorded genuine queue attendance. Refuse a populated
    # predecessor with Waiting instead of manufacturing arrival evidence.
    # Alembic's transactional DDL preserves the exact predecessor on failure.
    op.execute("""
        DO $$ BEGIN
            IF EXISTS (SELECT 1 FROM public.appointments
                       WHERE status = 'Waiting') THEN
                RAISE EXCEPTION 'D-06 upgrade requires genuine Waiting queue evidence'
                    USING ERRCODE = '23514';
            END IF;
        END $$
    """)
    op.execute("""
        CREATE TABLE public.queue_days (
            id uuid PRIMARY KEY,
            organization_id uuid NOT NULL REFERENCES public.organizations(id),
            doctor_staff_id uuid NOT NULL,
            service_date date NOT NULL CHECK (isfinite(service_date)),
            lane text NOT NULL CHECK (lane IN ('MAIN', 'EMERGENCY')),
            revision bigint NOT NULL DEFAULT 1 CHECK (revision >= 1),
            -- Deferred QueueEntry guard verifies an actual QueueDay revision
            -- in the same transaction; never lock the day from an entry trigger.
            revision_transaction_id xid8,
            queue_policy_kind text NOT NULL DEFAULT 'QUEUE'
                CHECK (queue_policy_kind = 'QUEUE'),
            queue_policy_version bigint NOT NULL CHECK (queue_policy_version >= 1),
            arrival_counter bigint NOT NULL DEFAULT 0 CHECK (arrival_counter >= 0),
            routine_cursor bigint NOT NULL DEFAULT 0 CHECK (routine_cursor >= 0),
            UNIQUE (organization_id, id),
            UNIQUE (organization_id, service_date, lane),
            UNIQUE (organization_id, id, doctor_staff_id, service_date, lane),
            FOREIGN KEY (organization_id, doctor_staff_id)
                REFERENCES public.staff_accounts(organization_id, id),
            FOREIGN KEY (organization_id, queue_policy_kind, queue_policy_version)
                REFERENCES public.clinic_policy_versions(organization_id, kind, version),
            CHECK (lane = 'MAIN' OR routine_cursor = 0)
        )
    """)
    op.execute("""
        CREATE TABLE public.queue_entries (
            id uuid PRIMARY KEY,
            organization_id uuid NOT NULL,
            queue_day_id uuid NOT NULL,
            appointment_id uuid NOT NULL,
            patient_id uuid NOT NULL,
            doctor_staff_id uuid NOT NULL,
            service_date date NOT NULL CHECK (isfinite(service_date)),
            lane text NOT NULL CHECK (lane IN ('MAIN', 'EMERGENCY')),
            arrival_sequence bigint NOT NULL CHECK (arrival_sequence > 0),
            attested_by_staff_id uuid NOT NULL,
            attested_at timestamptz NOT NULL,
            routine_rank bigint CHECK (routine_rank IS NULL OR routine_rank > 0),
            -- Set by the closure guard, never by a caller. Retains whether
            -- this entry was the ordinary MAIN Routine head at admission.
            ordinary_head_admitted boolean NOT NULL DEFAULT false,
            disposition text NOT NULL CHECK (disposition IN ('ACTIVE', 'CLOSED')),
            closure_history_revision bigint CHECK
                (closure_history_revision IS NULL OR closure_history_revision > 0),
            closure_queue_revision bigint CHECK
                (closure_queue_revision IS NULL OR closure_queue_revision >= 1),
            closure_transaction_id xid8,
            closure_routine_cursor bigint CHECK
                (closure_routine_cursor IS NULL OR closure_routine_cursor >= 0),
            revision bigint NOT NULL DEFAULT 1 CHECK (revision >= 1),
            UNIQUE (organization_id, id),
            UNIQUE (organization_id, queue_day_id, arrival_sequence),
            FOREIGN KEY (organization_id, queue_day_id, doctor_staff_id,
                         service_date, lane)
                REFERENCES public.queue_days
                    (organization_id, id, doctor_staff_id, service_date, lane),
            FOREIGN KEY (organization_id, appointment_id)
                REFERENCES public.appointments(organization_id, id),
            FOREIGN KEY (organization_id, patient_id)
                REFERENCES public.patients(organization_id, id),
            FOREIGN KEY (organization_id, attested_by_staff_id)
                REFERENCES public.staff_accounts(organization_id, id),
            FOREIGN KEY (organization_id, appointment_id, closure_history_revision)
                REFERENCES public.appointment_history
                    (organization_id, appointment_id, appointment_revision)
                DEFERRABLE INITIALLY DEFERRED,
            CHECK ((disposition = 'ACTIVE' AND closure_history_revision IS NULL
                    AND closure_queue_revision IS NULL
                    AND closure_transaction_id IS NULL
                    AND closure_routine_cursor IS NULL)
                OR (disposition = 'CLOSED' AND closure_history_revision IS NOT NULL
                    AND closure_queue_revision IS NOT NULL
                    AND closure_transaction_id IS NOT NULL
                    AND closure_routine_cursor IS NOT NULL)),
            CHECK (routine_rank IS NULL OR lane = 'MAIN'),
            CHECK (NOT ordinary_head_admitted OR
                   (disposition = 'CLOSED' AND lane = 'MAIN'
                    AND routine_rank IS NOT NULL))
        )
    """)
    op.execute("""
        CREATE UNIQUE INDEX queue_entries_one_active_appointment
        ON public.queue_entries (organization_id, appointment_id)
        WHERE disposition = 'ACTIVE'
    """)
    op.execute("""
        CREATE UNIQUE INDEX queue_entries_unique_active_routine_rank
        ON public.queue_entries (organization_id, queue_day_id, routine_rank)
        WHERE disposition = 'ACTIVE' AND routine_rank IS NOT NULL
    """)
    op.execute("""
        CREATE TABLE public.doctor_controls (
            organization_id uuid NOT NULL REFERENCES public.organizations(id),
            doctor_staff_id uuid NOT NULL,
            revision bigint NOT NULL DEFAULT 1 CHECK (revision >= 1),
            capacity_policy_kind text NOT NULL DEFAULT 'CAPACITY'
                CHECK (capacity_policy_kind = 'CAPACITY'),
            capacity_policy_version bigint NOT NULL CHECK (capacity_policy_version >= 1),
            PRIMARY KEY (organization_id, doctor_staff_id),
            FOREIGN KEY (organization_id, doctor_staff_id)
                REFERENCES public.staff_accounts(organization_id, id),
            FOREIGN KEY (organization_id, capacity_policy_kind, capacity_policy_version)
                REFERENCES public.clinic_policy_versions(organization_id, kind, version)
        )
    """)
    op.execute("""
        CREATE TABLE public.doctor_admission_confirmations (
            id uuid PRIMARY KEY,
            organization_id uuid NOT NULL REFERENCES public.organizations(id),
            issuing_doctor_staff_id uuid NOT NULL,
            doctor_staff_id uuid NOT NULL,
            patient_id uuid NOT NULL,
            appointment_id uuid NOT NULL,
            service_date date NOT NULL CHECK (isfinite(service_date)),
            lane text NOT NULL CHECK (lane IN ('MAIN', 'EMERGENCY')),
            issued_at timestamptz NOT NULL,
            expires_at timestamptz NOT NULL CHECK (expires_at > issued_at),
            appointment_revision bigint NOT NULL CHECK (appointment_revision >= 0),
            queue_day_revision bigint NOT NULL CHECK (queue_day_revision >= 1),
            control_revision bigint NOT NULL CHECK (control_revision >= 1),
            capacity_revision bigint NOT NULL CHECK (capacity_revision >= 1),
            admission_revision bigint NOT NULL CHECK (admission_revision >= 1),
            state text NOT NULL CHECK (state IN ('ISSUED', 'CONSUMED', 'REVOKED')),
            consuming_admission_revision bigint CHECK
                (consuming_admission_revision IS NULL OR consuming_admission_revision > 0),
            consumed_at timestamptz,
            revoked_by_staff_id uuid,
            revoked_at timestamptz,
            revoke_reason text,
            UNIQUE (organization_id, id),
            FOREIGN KEY (organization_id, issuing_doctor_staff_id)
                REFERENCES public.staff_accounts(organization_id, id),
            FOREIGN KEY (organization_id, doctor_staff_id)
                REFERENCES public.doctor_controls(organization_id, doctor_staff_id),
            FOREIGN KEY (organization_id, patient_id)
                REFERENCES public.patients(organization_id, id),
            FOREIGN KEY (organization_id, appointment_id)
                REFERENCES public.appointments(organization_id, id),
            FOREIGN KEY (organization_id, service_date, lane)
                REFERENCES public.queue_days(organization_id, service_date, lane),
            FOREIGN KEY (organization_id, appointment_id, consuming_admission_revision)
                REFERENCES public.appointment_history
                    (organization_id, appointment_id, appointment_revision)
                DEFERRABLE INITIALLY DEFERRED,
            FOREIGN KEY (organization_id, revoked_by_staff_id)
                REFERENCES public.staff_accounts(organization_id, id),
            CHECK (issuing_doctor_staff_id = doctor_staff_id),
            CHECK (
                (state = 'ISSUED' AND consuming_admission_revision IS NULL
                    AND consumed_at IS NULL AND revoked_by_staff_id IS NULL
                    AND revoked_at IS NULL AND revoke_reason IS NULL)
                OR (state = 'CONSUMED' AND consuming_admission_revision IS NOT NULL
                    AND consumed_at IS NOT NULL AND consumed_at >= issued_at
                    AND consumed_at < expires_at
                    AND revoked_by_staff_id IS NULL AND revoked_at IS NULL
                    AND revoke_reason IS NULL)
                OR (state = 'REVOKED' AND consuming_admission_revision IS NULL
                    AND consumed_at IS NULL AND revoked_by_staff_id IS NOT NULL
                    AND revoked_at IS NOT NULL AND revoked_at >= issued_at
                    AND revoke_reason IS NOT NULL
                    AND btrim(revoke_reason) <> '')
            )
        )
    """)
    for table in (
        "queue_days",
        "queue_entries",
        "doctor_controls",
        "doctor_admission_confirmations",
    ):
        op.execute(f"ALTER TABLE public.{table} ENABLE ROW LEVEL SECURITY")
        op.execute(f"ALTER TABLE public.{table} FORCE ROW LEVEL SECURITY")
        op.execute(f"""
            CREATE POLICY {table}_runtime ON public.{table}
            FOR ALL TO carepulse_runtime
            USING (organization_id =
                NULLIF(current_setting('app.organization_id', true), '')::uuid)
            WITH CHECK (organization_id =
                NULLIF(current_setting('app.organization_id', true), '')::uuid)
        """)
        op.execute(f"""
            CREATE POLICY {table}_migration ON public.{table}
            FOR ALL TO carepulse_migration USING (true) WITH CHECK (true)
        """)
        op.execute(f"REVOKE ALL ON public.{table} FROM PUBLIC")
        op.execute(f"GRANT SELECT ON public.{table} TO carepulse_runtime")

    op.execute("GRANT INSERT ON public.queue_days TO carepulse_runtime")
    op.execute("""
        GRANT UPDATE (revision, arrival_counter, routine_cursor)
        ON public.queue_days TO carepulse_runtime
    """)
    # Runtime may only supply membership scope. Provenance is generated from
    # relational Waiting history and the PostgreSQL clock/arrival counter.
    op.execute("""
        GRANT INSERT (id, organization_id, queue_day_id, appointment_id,
                      patient_id, doctor_staff_id, service_date, lane,
                      routine_rank, disposition)
        ON public.queue_entries TO carepulse_runtime
    """)
    op.execute("""
        GRANT UPDATE (routine_rank, disposition, closure_history_revision, revision)
        ON public.queue_entries TO carepulse_runtime
    """)
    # Only the later privileged initializer may create a shared control row.
    op.execute("""
        GRANT UPDATE (revision, capacity_policy_version)
        ON public.doctor_controls TO carepulse_runtime
    """)
    op.execute(
        "GRANT INSERT ON public.doctor_admission_confirmations TO carepulse_runtime"
    )
    op.execute("""
        GRANT UPDATE (state, consuming_admission_revision, consumed_at,
                      revoked_by_staff_id, revoked_at, revoke_reason)
        ON public.doctor_admission_confirmations TO carepulse_runtime
    """)

    # T095: validate the *current* committed-image pair in both directions.
    # Constraint triggers may fire for an earlier event after several changes
    # to the same row; NEW.status/disposition is not a final-state source.
    op.execute("""
        CREATE FUNCTION public.assert_queue_membership_for(
            target_organization uuid, target_appointment uuid)
        RETURNS void LANGUAGE plpgsql SECURITY DEFINER
        SET search_path = pg_catalog AS $$
        DECLARE
            current_appointment public.appointments%ROWTYPE;
            active_count bigint;
            matching_count bigint;
        BEGIN
            SELECT * INTO current_appointment FROM public.appointments
            WHERE organization_id = target_organization AND id = target_appointment;

            IF NOT FOUND THEN
                IF EXISTS (SELECT 1 FROM public.queue_entries e
                           WHERE e.organization_id = target_organization
                             AND e.appointment_id = target_appointment
                             AND e.disposition = 'ACTIVE') THEN
                    RAISE EXCEPTION 'Active entry without appointment'
                        USING ERRCODE = '23514';
                END IF;
                RETURN;
            END IF;

            SELECT count(*), count(*) FILTER (WHERE
                e.patient_id = current_appointment.patient_id
                AND e.doctor_staff_id = current_appointment.doctor_staff_id
                AND e.service_date = current_appointment.service_date
                AND e.lane = current_appointment.lane
                AND ((current_appointment.classification = 'ROUTINE'
                      AND e.lane = 'MAIN' AND e.routine_rank IS NOT NULL)
                  OR (current_appointment.classification <> 'ROUTINE'
                      AND e.routine_rank IS NULL)))
            INTO active_count, matching_count
            FROM public.queue_entries e
            WHERE e.organization_id = target_organization
              AND e.appointment_id = target_appointment
              AND e.disposition = 'ACTIVE';

            IF (current_appointment.status = 'Waiting'
                AND (active_count <> 1 OR matching_count <> 1))
               OR (current_appointment.status <> 'Waiting' AND active_count <> 0)
               OR (current_appointment.status = 'Waiting' AND NOT EXISTS (
                   SELECT 1 FROM public.staff_accounts s
                   WHERE s.organization_id = target_organization
                     AND s.id = current_appointment.doctor_staff_id
                     AND s.role = 'DOCTOR_OWNER')) THEN
                RAISE EXCEPTION 'Appointment/queue final state inconsistent'
                    USING ERRCODE = '23514';
            END IF;
        END $$
    """)
    op.execute("""
        CREATE FUNCTION public.assert_appointment_queue_final() RETURNS trigger
        LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog AS $$
        BEGIN
            PERFORM public.assert_queue_membership_for(
                NEW.organization_id, NEW.id);
            RETURN NULL;
        END $$
    """)
    op.execute("""
        CREATE CONSTRAINT TRIGGER appointment_queue_final
        AFTER INSERT OR UPDATE ON public.appointments
        DEFERRABLE INITIALLY DEFERRED FOR EACH ROW
        EXECUTE FUNCTION public.assert_appointment_queue_final()
    """)
    op.execute("""
        CREATE FUNCTION public.assert_queue_entry_final() RETURNS trigger
        LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog AS $$
        BEGIN
            IF TG_OP = 'DELETE' THEN
                PERFORM public.assert_queue_membership_for(
                    OLD.organization_id, OLD.appointment_id);
            ELSE
                PERFORM public.assert_queue_membership_for(
                    NEW.organization_id, NEW.appointment_id);
                IF TG_OP = 'UPDATE' AND
                   (OLD.organization_id, OLD.appointment_id)
                     IS DISTINCT FROM (NEW.organization_id, NEW.appointment_id) THEN
                    PERFORM public.assert_queue_membership_for(
                        OLD.organization_id, OLD.appointment_id);
                END IF;
            END IF;
            RETURN NULL;
        END $$
    """)
    op.execute("""
        CREATE CONSTRAINT TRIGGER queue_entry_appointment_final
        AFTER INSERT OR UPDATE OR DELETE ON public.queue_entries
        DEFERRABLE INITIALLY DEFERRED FOR EACH ROW
        EXECUTE FUNCTION public.assert_queue_entry_final()
    """)
    for signature in (
        "assert_queue_membership_for(uuid, uuid)",
        "assert_appointment_queue_final()",
        "assert_queue_entry_final()",
    ):
        op.execute(f"REVOKE ALL ON FUNCTION public.{signature} FROM PUBLIC")

    # This is a row-identity guard, not the T096 control-row initializer.
    # Do not take a late QueueDay/DoctorControl FOR UPDATE lock in a trigger.
    op.execute("""
        CREATE FUNCTION public.guard_queue_day_identity_and_cursor() RETURNS trigger
        LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog AS $$
        DECLARE
            effective_version bigint;
        BEGIN
            IF NOT EXISTS (
                SELECT 1 FROM public.staff_accounts s
                WHERE s.organization_id = NEW.organization_id
                  AND s.id = NEW.doctor_staff_id AND s.role = 'DOCTOR_OWNER'
            ) THEN
                RAISE EXCEPTION 'Queue Doctor must be clinic Doctor'
                    USING ERRCODE = '23514';
            END IF;
            IF TG_OP = 'UPDATE' THEN
                IF (NEW.id, NEW.organization_id, NEW.doctor_staff_id,
                    NEW.service_date, NEW.lane, NEW.queue_policy_kind,
                    NEW.queue_policy_version) IS DISTINCT FROM
                   (OLD.id, OLD.organization_id, OLD.doctor_staff_id,
                    OLD.service_date, OLD.lane, OLD.queue_policy_kind,
                    OLD.queue_policy_version)
                   OR NEW.revision <> OLD.revision + 1 THEN
                    RAISE EXCEPTION 'Original queue partition/policy immutable'
                        USING ERRCODE = '23514';
                END IF;
                IF NEW.routine_cursor IS DISTINCT FROM OLD.routine_cursor THEN
                    IF NEW.lane <> 'MAIN' OR
                       -- The cursor stores the admitted Routine head's booked
                       -- number, not a count of admissions: gaps are valid.
                       NEW.routine_cursor <= OLD.routine_cursor OR NOT EXISTS (
                        SELECT 1 FROM public.queue_entries e
                        JOIN public.appointments a
                          ON a.organization_id = e.organization_id
                         AND a.id = e.appointment_id
                        JOIN public.appointment_history h
                          ON h.organization_id = e.organization_id
                         AND h.appointment_id = e.appointment_id
                         AND h.appointment_revision = e.closure_history_revision
                        WHERE e.organization_id = NEW.organization_id
                          AND e.queue_day_id = NEW.id
                          AND e.doctor_staff_id = NEW.doctor_staff_id
                          AND e.service_date = NEW.service_date
                          AND e.lane = 'MAIN' AND e.disposition = 'CLOSED'
                          AND e.ordinary_head_admitted
                          -- Require a freshly closed entry in this transaction.
                          -- The captured prior cursor also prevents a second
                          -- advancement against that same closure in one xact.
                          AND e.closure_transaction_id = pg_current_xact_id()
                          AND e.closure_routine_cursor = OLD.routine_cursor
                          AND e.closure_queue_revision = OLD.revision
                          AND a.status = 'In Examination'
                          AND a.classification = 'ROUTINE'
                          AND a.patient_id = e.patient_id
                          AND a.doctor_staff_id = e.doctor_staff_id
                          AND a.service_date = e.service_date
                          AND a.revision = h.appointment_revision
                          AND a.current_number = h.new_number
                          AND h.new_number = NEW.routine_cursor
                          AND h.old_status = 'Waiting'
                          AND h.new_status = 'In Examination'
                          AND h.old_service_date = NEW.service_date
                          AND h.new_service_date = NEW.service_date
                          AND h.reason IS NULL
                    ) THEN
                        RAISE EXCEPTION 'Cursor lacks ordinary Routine-head admission'
                            USING ERRCODE = '23514';
                    END IF;
                END IF;
                IF NEW.arrival_counter < OLD.arrival_counter
                   OR NEW.arrival_counter > OLD.arrival_counter + 1 THEN
                    RAISE EXCEPTION 'Queue arrival counter must advance one at a time'
                        USING ERRCODE = '23514';
                END IF;
            ELSE
                IF NEW.revision <> 1 OR NEW.arrival_counter NOT IN (0, 1)
                THEN
                    RAISE EXCEPTION 'Invalid initial queue counter/revision'
                        USING ERRCODE = '23514';
                END IF;
                -- D-04 publishes prospectively: head.current_version may be
                -- future-effective or pending for this original service day.
                SELECT v.version INTO effective_version
                FROM public.clinic_policy_versions v
                JOIN public.clinic_policy_heads h
                  ON h.organization_id = v.organization_id
                 AND h.kind = v.kind
                 AND v.version <= h.current_version
                WHERE v.organization_id = NEW.organization_id
                  AND v.kind = 'QUEUE' AND NOT v.pending_effective
                  AND (v.version = 1 OR
                       (v.effective_on IS NOT NULL
                        AND v.effective_on <= NEW.service_date))
                ORDER BY v.version DESC LIMIT 1;
                IF effective_version IS NULL
                   OR NEW.queue_policy_version <> effective_version THEN
                    RAISE EXCEPTION 'Queue policy is not effective for service date'
                        USING ERRCODE = '23514';
                END IF;
            END IF;
            NEW.revision_transaction_id := pg_current_xact_id();
            RETURN NEW;
        END $$
    """)
    op.execute("""
        CREATE TRIGGER queue_day_identity_and_cursor
        BEFORE INSERT OR UPDATE ON public.queue_days FOR EACH ROW
        EXECUTE FUNCTION public.guard_queue_day_identity_and_cursor()
    """)
    # No phantom arrivals: after all queued changes in this transaction the
    # counter equals immutable-in-history entries, active plus closed.
    op.execute("""
        CREATE FUNCTION public.assert_queue_arrival_count() RETURNS trigger
        LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog AS $$
        DECLARE
            current_counter bigint;
            actual_entries bigint;
        BEGIN
            SELECT arrival_counter INTO current_counter
            FROM public.queue_days
            WHERE organization_id = NEW.organization_id AND id = NEW.id;
            SELECT count(*) INTO actual_entries FROM public.queue_entries
            WHERE organization_id = NEW.organization_id
              AND queue_day_id = NEW.id;
            IF current_counter IS DISTINCT FROM actual_entries THEN
                RAISE EXCEPTION 'Queue arrival ledger does not match counter'
                    USING ERRCODE = '23514';
            END IF;
            RETURN NULL;
        END $$
    """)
    op.execute("""
        CREATE CONSTRAINT TRIGGER queue_arrival_count_final
        AFTER INSERT OR UPDATE ON public.queue_days
        DEFERRABLE INITIALLY DEFERRED FOR EACH ROW
        EXECUTE FUNCTION public.assert_queue_arrival_count()
    """)
    op.execute("REVOKE ALL ON FUNCTION public.assert_queue_arrival_count() FROM PUBLIC")
    op.execute("""
        CREATE FUNCTION public.guard_doctor_control_identity() RETURNS trigger
        LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog AS $$
        BEGIN
            IF NOT EXISTS (
                SELECT 1 FROM public.staff_accounts s
                WHERE s.organization_id = NEW.organization_id
                  AND s.id = NEW.doctor_staff_id AND s.role = 'DOCTOR_OWNER'
            ) OR (TG_OP = 'UPDATE' AND
                   (NEW.organization_id, NEW.doctor_staff_id,
                    NEW.capacity_policy_kind) IS DISTINCT FROM
                   (OLD.organization_id, OLD.doctor_staff_id,
                    OLD.capacity_policy_kind)) OR
               (TG_OP = 'UPDATE' AND NEW.revision <> OLD.revision + 1) THEN
                RAISE EXCEPTION 'Invalid shared Doctor control identity'
                    USING ERRCODE = '23514';
            END IF;
            RETURN NEW;
        END $$
    """)
    op.execute("""
        CREATE TRIGGER doctor_control_identity
        BEFORE INSERT OR UPDATE ON public.doctor_controls FOR EACH ROW
        EXECUTE FUNCTION public.guard_doctor_control_identity()
    """)
    op.execute("""
        CREATE FUNCTION public.guard_queue_entry_history() RETURNS trigger
        LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog AS $$
        DECLARE
            expected_sequence bigint;
            source_actor uuid;
        BEGIN
            IF TG_OP = 'INSERT' THEN
                IF NEW.ordinary_head_admitted THEN
                    RAISE EXCEPTION 'Only server can attest Routine head'
                        USING ERRCODE = '23514';
                END IF;
                IF NEW.disposition <> 'ACTIVE'
                   OR NEW.closure_queue_revision IS NOT NULL
                   OR NEW.closure_transaction_id IS NOT NULL
                   OR NEW.closure_routine_cursor IS NOT NULL THEN
                    RAISE EXCEPTION 'Queue entry must begin active'
                        USING ERRCODE = '23514';
                END IF;
                -- Arrival rank and attestation instant are database-owned,
                -- not caller-supplied. The actor is bound to the immutable
                -- appointment transition evidence, not a caller-set GUC.
                SELECT q.arrival_counter INTO expected_sequence
                FROM public.queue_days q
                WHERE q.organization_id = NEW.organization_id
                  AND q.id = NEW.queue_day_id;
                IF expected_sequence IS NULL OR expected_sequence < 1
                   OR expected_sequence <> (
                       SELECT coalesce(max(e.arrival_sequence), 0) + 1
                       FROM public.queue_entries e
                       WHERE e.organization_id = NEW.organization_id
                         AND e.queue_day_id = NEW.queue_day_id
                   ) THEN
                    RAISE EXCEPTION 'Arrival counter not advanced in order'
                        USING ERRCODE = '23514';
                END IF;
                SELECT h.actor_staff_id INTO source_actor
                FROM public.appointments a
                JOIN public.appointment_history h
                  ON h.organization_id = a.organization_id
                 AND h.appointment_id = a.id
                 AND h.appointment_revision = a.revision
                JOIN public.staff_accounts s
                  ON s.organization_id = h.organization_id
                 AND s.id = h.actor_staff_id
                 AND s.role = h.actor_role
                WHERE a.organization_id = NEW.organization_id
                  AND a.id = NEW.appointment_id AND a.status = 'Waiting'
                  AND h.new_status = 'Waiting'
                  AND s.enabled AND s.verified;
                IF source_actor IS NULL THEN
                    RAISE EXCEPTION 'Attestation requires Waiting actor evidence'
                        USING ERRCODE = '23514';
                END IF;
                NEW.arrival_sequence := expected_sequence;
                NEW.attested_at := clock_timestamp();
                NEW.attested_by_staff_id := source_actor;
                RETURN NEW;
            END IF;
            IF (NEW.id, NEW.organization_id, NEW.queue_day_id,
                NEW.appointment_id, NEW.patient_id, NEW.doctor_staff_id,
                NEW.service_date, NEW.lane, NEW.arrival_sequence,
                NEW.attested_by_staff_id, NEW.attested_at)
               IS DISTINCT FROM
               (OLD.id, OLD.organization_id, OLD.queue_day_id,
                OLD.appointment_id, OLD.patient_id, OLD.doctor_staff_id,
                OLD.service_date, OLD.lane, OLD.arrival_sequence,
                OLD.attested_by_staff_id, OLD.attested_at)
               OR OLD.disposition = 'CLOSED'
               OR NEW.revision <> OLD.revision + 1
               OR (NEW.disposition = 'ACTIVE' AND
                   (NEW.closure_history_revision IS NOT NULL
                    OR NEW.closure_queue_revision IS NOT NULL
                    OR NEW.closure_transaction_id IS NOT NULL
                    OR NEW.closure_routine_cursor IS NOT NULL
                    OR NEW.ordinary_head_admitted)) THEN
                RAISE EXCEPTION 'Queue attendance/history immutable'
                    USING ERRCODE = '23514';
            END IF;
            IF NEW.disposition = 'CLOSED' THEN
                IF NEW.routine_rank IS DISTINCT FROM OLD.routine_rank THEN
                    RAISE EXCEPTION 'Closing cannot rewrite last Routine rank'
                        USING ERRCODE = '23514';
                END IF;
                -- A valid FK to *some* history row is not closure provenance.
                -- It must be the current Waiting exit for this entry's
                -- original day and assigned number. A legitimate exit is
                -- admission, reasoned cancellation or cross-day reschedule.
                PERFORM 1
                FROM public.appointments a
                JOIN public.appointment_history h
                  ON h.organization_id = a.organization_id
                 AND h.appointment_id = a.id
                 AND h.appointment_revision = NEW.closure_history_revision
                JOIN public.appointment_number_assignments old_assignment
                  ON old_assignment.organization_id = h.organization_id
                 AND old_assignment.appointment_id = h.appointment_id
                 AND old_assignment.service_date = OLD.service_date
                 AND old_assignment.number = h.old_number
                WHERE a.organization_id = OLD.organization_id
                  AND a.id = OLD.appointment_id
                  AND a.patient_id = OLD.patient_id
                  AND a.doctor_staff_id = OLD.doctor_staff_id
                  AND a.lane = OLD.lane
                  AND a.revision = h.appointment_revision
                  AND a.status = h.new_status
                  AND a.service_date = h.new_service_date
                  AND a.current_number = h.new_number
                  AND h.old_status = 'Waiting'
                  AND h.old_service_date = OLD.service_date
                  AND (
                    (h.new_status IN ('In Examination', 'Cancelled')
                     AND h.new_service_date = OLD.service_date
                     AND h.new_number = h.old_number)
                    OR
                    (h.new_status = 'Booked'
                     AND h.new_service_date <> OLD.service_date
                     AND a.classification = 'ROUTINE'
                     AND a.lane = 'MAIN')
                  );
                IF NOT FOUND THEN
                    RAISE EXCEPTION 'Queue closure requires matching Waiting exit'
                        USING ERRCODE = '23514';
                END IF;
                -- The caller already holds this QueueDay (after DoctorControl).
                -- Retain the actual pre-closure revision to reject a stale
                -- approval even if an unrelated edit incremented QueueDay.
                SELECT q.revision, q.routine_cursor
                INTO NEW.closure_queue_revision, NEW.closure_routine_cursor
                FROM public.queue_days q
                WHERE q.organization_id = OLD.organization_id
                  AND q.id = OLD.queue_day_id
                  AND q.doctor_staff_id = OLD.doctor_staff_id
                  AND q.service_date = OLD.service_date AND q.lane = OLD.lane;
                IF NOT FOUND THEN
                    RAISE EXCEPTION 'QueueDay missing at entry close'
                        USING ERRCODE = '23514';
                END IF;
                NEW.closure_transaction_id := pg_current_xact_id();
                -- Snapshot head proof *before* deactivation and reindexing.
                -- A justified Doctor exception has a nonempty history reason
                -- and therefore never earns an ordinary-head cursor proof.
                NEW.ordinary_head_admitted := COALESCE((
                    OLD.lane = 'MAIN' AND OLD.routine_rank IS NOT NULL
                    AND EXISTS (
                        SELECT 1 FROM public.appointments a
                        JOIN public.appointment_history h
                          ON h.organization_id = a.organization_id
                         AND h.appointment_id = a.id
                         AND h.appointment_revision = NEW.closure_history_revision
                        WHERE a.organization_id = OLD.organization_id
                          AND a.id = OLD.appointment_id
                          AND a.status = 'In Examination'
                          AND a.revision = NEW.closure_history_revision
                          AND a.classification = 'ROUTINE'
                          AND a.lane = 'MAIN'
                          AND a.patient_id = OLD.patient_id
                          AND a.doctor_staff_id = OLD.doctor_staff_id
                          AND a.service_date = OLD.service_date
                          AND h.old_status = 'Waiting'
                          AND h.new_status = 'In Examination'
                          AND h.old_service_date = OLD.service_date
                          AND h.new_service_date = OLD.service_date
                          AND h.reason IS NULL
                    )
                    AND NOT EXISTS (
                        SELECT 1 FROM public.queue_entries preceding
                        JOIN public.appointments p
                          ON p.organization_id = preceding.organization_id
                         AND p.id = preceding.appointment_id
                        WHERE preceding.organization_id = OLD.organization_id
                          AND preceding.queue_day_id = OLD.queue_day_id
                          AND preceding.id <> OLD.id
                          AND preceding.disposition = 'ACTIVE'
                          AND (p.classification = 'URGENT' OR
                               (p.classification = 'ROUTINE'
                                AND preceding.routine_rank < OLD.routine_rank))
                    )
                ), false);
            END IF;
            RETURN NEW;
        END $$
    """)
    op.execute("""
        CREATE TRIGGER queue_entry_attendance_history
        BEFORE INSERT OR UPDATE ON public.queue_entries FOR EACH ROW
        EXECUTE FUNCTION public.guard_queue_entry_history()
    """)
    # The actual QueueDay write belongs to the command's earlier lock scope.
    # Constraint proof is deferred so a queue-entry change can precede its
    # caller-owned revision bump; no entry trigger takes a reverse row lock.
    op.execute("""
        CREATE FUNCTION public.assert_queue_entry_day_revised() RETURNS trigger
        LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog AS $$
        BEGIN
            IF NOT EXISTS (
                SELECT 1 FROM public.queue_days q
                WHERE q.organization_id = NEW.organization_id
                  AND q.id = NEW.queue_day_id
                  AND q.revision_transaction_id = pg_current_xact_id()
            ) THEN
                RAISE EXCEPTION 'QueueEntry change requires same-transaction QueueDay revision'
                    USING ERRCODE = '23514';
            END IF;
            RETURN NULL;
        END $$
    """)
    op.execute("""
        CREATE CONSTRAINT TRIGGER queue_entry_day_revised
        AFTER INSERT OR UPDATE ON public.queue_entries
        DEFERRABLE INITIALLY DEFERRED FOR EACH ROW
        EXECUTE FUNCTION public.assert_queue_entry_day_revised()
    """)
    for signature in (
        "guard_queue_day_identity_and_cursor()",
        "guard_doctor_control_identity()",
        "guard_queue_entry_history()",
        "assert_queue_entry_day_revised()",
    ):
        op.execute(f"REVOKE ALL ON FUNCTION public.{signature} FROM PUBLIC")

    # B-15 and D-06 simulation callers take this helper as their first
    # admission-specific lock. It locks the ONE Doctor control *before*
    # the chosen dated QueueDay and only then counts every date and lane.
    # No admission, Visit or workflow is performed here.
    op.execute("""
        CREATE FUNCTION public.lock_doctor_admission_capacity(
            target_organization uuid, target_doctor uuid,
            target_date date, target_lane text)
        RETURNS TABLE (active_examinations bigint, configured_capacity bigint,
                       paused boolean, queue_revision bigint)
        LANGUAGE plpgsql SECURITY INVOKER SET search_path = pg_catalog AS $$
        DECLARE
            control_row public.doctor_controls%ROWTYPE;
            day_row public.queue_days%ROWTYPE;
        BEGIN
            SELECT * INTO STRICT control_row FROM public.doctor_controls
            WHERE organization_id = target_organization
              AND doctor_staff_id = target_doctor FOR UPDATE;
            IF NOT EXISTS (
                SELECT 1 FROM public.clinic_policy_heads head
                WHERE head.organization_id = target_organization
                  AND head.kind = 'CAPACITY'
                  AND head.current_version = control_row.capacity_policy_version
            ) THEN
                RAISE EXCEPTION 'Stale Doctor capacity policy'
                    USING ERRCODE = '23514';
            END IF;
            SELECT * INTO STRICT day_row FROM public.queue_days
            WHERE organization_id = target_organization
              AND doctor_staff_id = target_doctor
              AND service_date = target_date AND lane = target_lane FOR UPDATE;
            RETURN QUERY
            SELECT count(a.id), (p.payload->>'capacity')::bigint,
                   (p.payload->>'paused')::boolean, day_row.revision
            FROM public.clinic_policy_versions p
            LEFT JOIN public.appointments a
              ON a.organization_id = target_organization
             AND a.doctor_staff_id = target_doctor
             AND a.status = 'In Examination'
            WHERE p.organization_id = target_organization
              AND p.kind = 'CAPACITY'
              AND p.version = control_row.capacity_policy_version
            GROUP BY p.payload;
        END $$
    """)
    op.execute("""
        REVOKE ALL ON FUNCTION
        public.lock_doctor_admission_capacity(uuid, uuid, date, text) FROM PUBLIC
    """)
    op.execute("""
        GRANT EXECUTE ON FUNCTION
        public.lock_doctor_admission_capacity(uuid, uuid, date, text)
        TO carepulse_runtime
    """)

    # Only an authenticated, live Doctor may issue or explicitly revoke.
    # SECURITY DEFINER is restricted to checking relational identity; the
    # runtime's transaction-local app.staff_id is set by the signed BFF path.
    op.execute("""
        CREATE FUNCTION public.require_confirmation_doctor(
            target_organization uuid, target_doctor uuid)
        RETURNS void LANGUAGE plpgsql SECURITY DEFINER
        SET search_path = pg_catalog AS $$
        BEGIN
            IF session_user = 'carepulse_runtime' AND
               (current_setting('app.organization_id', true)
                    IS DISTINCT FROM target_organization::text
                OR current_setting('app.staff_id', true)
                    IS DISTINCT FROM target_doctor::text) THEN
                RAISE EXCEPTION 'Doctor approval actor mismatch'
                    USING ERRCODE = '42501';
            END IF;
            IF NOT EXISTS (
                SELECT 1 FROM public.staff_accounts s
                JOIN public.organizations o ON o.id = s.organization_id
                WHERE s.organization_id = target_organization
                  AND s.id = target_doctor AND s.role = 'DOCTOR_OWNER'
                  AND s.enabled AND s.verified
                  AND s.reset_block_operation_id IS NULL AND o.state = 'ACTIVE'
            ) THEN
                RAISE EXCEPTION 'Doctor approval requires active clinic Doctor'
                    USING ERRCODE = '42501';
            END IF;
        END $$
    """)
    op.execute("""
        CREATE FUNCTION public.assert_confirmation_issued_scope(
            target public.doctor_admission_confirmations)
        RETURNS void LANGUAGE plpgsql SECURITY DEFINER
        SET search_path = pg_catalog AS $$
        BEGIN
            IF NOT EXISTS (
                SELECT 1 FROM public.appointments a
                JOIN public.queue_days q
                  ON q.organization_id = a.organization_id
                 AND q.doctor_staff_id = a.doctor_staff_id
                 AND q.service_date = a.service_date AND q.lane = a.lane
                JOIN public.doctor_controls c
                  ON c.organization_id = a.organization_id
                 AND c.doctor_staff_id = a.doctor_staff_id
                JOIN public.clinic_policy_heads capacity_head
                  ON capacity_head.organization_id = a.organization_id
                 AND capacity_head.kind = 'CAPACITY'
                JOIN public.clinic_policy_versions capacity_policy
                  ON capacity_policy.organization_id = capacity_head.organization_id
                 AND capacity_policy.kind = 'CAPACITY'
                 AND capacity_policy.version = capacity_head.current_version
                JOIN public.clinic_policy_heads admission_head
                  ON admission_head.organization_id = a.organization_id
                 AND admission_head.kind = 'ADMISSION'
                WHERE a.organization_id = target.organization_id
                  AND a.id = target.appointment_id
                  AND a.patient_id = target.patient_id
                  AND a.doctor_staff_id = target.doctor_staff_id
                  AND a.service_date = target.service_date
                  AND a.lane = target.lane AND a.status = 'Waiting'
                  AND a.revision = target.appointment_revision
                  AND q.revision = target.queue_day_revision
                  AND c.revision = target.control_revision
                  AND c.capacity_policy_version = capacity_head.current_version
                  AND capacity_head.revision = target.capacity_revision
                  AND admission_head.revision = target.admission_revision
                  AND (capacity_policy.payload->>'capacity')::bigint > 1
                  AND EXISTS (
                      SELECT 1 FROM public.queue_entries e
                      WHERE e.organization_id = a.organization_id
                        AND e.appointment_id = a.id
                        AND e.queue_day_id = q.id
                        AND e.patient_id = a.patient_id
                        AND e.doctor_staff_id = a.doctor_staff_id
                        AND e.service_date = a.service_date
                        AND e.lane = a.lane AND e.disposition = 'ACTIVE')
                  AND EXISTS (
                      SELECT 1 FROM public.appointments current_exam
                      WHERE current_exam.organization_id = a.organization_id
                        AND current_exam.doctor_staff_id = a.doctor_staff_id
                        AND current_exam.status = 'In Examination')
            ) THEN
                RAISE EXCEPTION 'Doctor approval scope/revision stale'
                    USING ERRCODE = '23514';
            END IF;
        END $$
    """)
    op.execute("""
        CREATE FUNCTION public.guard_admission_confirmation() RETURNS trigger
        LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog AS $$
        DECLARE
            consumed_actor uuid;
        BEGIN
            IF TG_OP = 'INSERT' THEN
                PERFORM public.require_confirmation_doctor(
                    NEW.organization_id, NEW.issuing_doctor_staff_id);
                IF NEW.state <> 'ISSUED' OR
                   NEW.issuing_doctor_staff_id <> NEW.doctor_staff_id THEN
                    RAISE EXCEPTION 'Doctor may issue only an unused approval'
                        USING ERRCODE = '42501';
                END IF;
                IF session_user = 'carepulse_runtime' THEN
                    NEW.issued_at := clock_timestamp();
                END IF;
                IF NEW.expires_at <= clock_timestamp() THEN
                    RAISE EXCEPTION 'Doctor approval already expired'
                        USING ERRCODE = '23514';
                END IF;
                PERFORM public.assert_confirmation_issued_scope(NEW);
                RETURN NEW;
            END IF;

            IF OLD.state <> 'ISSUED' OR
               (NEW.id, NEW.organization_id, NEW.issuing_doctor_staff_id,
                NEW.doctor_staff_id, NEW.patient_id, NEW.appointment_id,
                NEW.service_date, NEW.lane, NEW.issued_at, NEW.expires_at,
                NEW.appointment_revision, NEW.queue_day_revision,
                NEW.control_revision, NEW.capacity_revision,
                NEW.admission_revision)
               IS DISTINCT FROM
               (OLD.id, OLD.organization_id, OLD.issuing_doctor_staff_id,
                OLD.doctor_staff_id, OLD.patient_id, OLD.appointment_id,
                OLD.service_date, OLD.lane, OLD.issued_at, OLD.expires_at,
                OLD.appointment_revision, OLD.queue_day_revision,
                OLD.control_revision, OLD.capacity_revision,
                OLD.admission_revision) THEN
                RAISE EXCEPTION 'Approval identity/state immutable'
                    USING ERRCODE = '23514';
            END IF;
            IF NEW.state = 'REVOKED' THEN
                PERFORM public.require_confirmation_doctor(
                    OLD.organization_id, OLD.doctor_staff_id);
                IF NEW.revoked_by_staff_id IS DISTINCT FROM OLD.doctor_staff_id
                   OR NEW.consuming_admission_revision IS NOT NULL
                   OR NEW.consumed_at IS NOT NULL THEN
                    RAISE EXCEPTION 'Only issuing Doctor may revoke unused approval'
                        USING ERRCODE = '42501';
                END IF;
                IF session_user = 'carepulse_runtime' THEN
                    NEW.revoked_at := clock_timestamp();
                END IF;
            ELSIF NEW.state = 'CONSUMED' THEN
                IF NEW.consuming_admission_revision IS NULL
                   OR NEW.consuming_admission_revision <> OLD.appointment_revision + 1
                   OR NEW.revoked_by_staff_id IS NOT NULL
                   OR NEW.revoked_at IS NOT NULL OR NEW.revoke_reason IS NOT NULL THEN
                    RAISE EXCEPTION 'Invalid admission consumption revision'
                        USING ERRCODE = '23514';
                END IF;
                IF session_user = 'carepulse_runtime' THEN
                    SELECT h.actor_staff_id INTO consumed_actor
                    FROM public.appointment_history h
                    WHERE h.organization_id = OLD.organization_id
                      AND h.appointment_id = OLD.appointment_id
                      AND h.appointment_revision = NEW.consuming_admission_revision
                      AND h.old_status = 'Waiting'
                      AND h.new_status = 'In Examination';
                    IF consumed_actor IS NULL OR
                       consumed_actor::text IS DISTINCT FROM
                           current_setting('app.staff_id', true) THEN
                        RAISE EXCEPTION 'Admission actor mismatch'
                            USING ERRCODE = '42501';
                    END IF;
                    NEW.consumed_at := clock_timestamp();
                END IF;
                -- Strictly exclusive: even equality at expiry is too late.
                IF clock_timestamp() >= OLD.expires_at OR
                   NEW.consumed_at IS NULL OR NEW.consumed_at >= OLD.expires_at
                   OR NEW.consumed_at < OLD.issued_at THEN
                    RAISE EXCEPTION 'Doctor approval expired'
                        USING ERRCODE = '23514';
                END IF;
            ELSE
                RAISE EXCEPTION 'Approval can transition only once'
                    USING ERRCODE = '23514';
            END IF;
            RETURN NEW;
        END $$
    """)
    op.execute("""
        CREATE TRIGGER doctor_confirmation_state_guard
        BEFORE INSERT OR UPDATE ON public.doctor_admission_confirmations
        FOR EACH ROW EXECUTE FUNCTION public.guard_admission_confirmation()
    """)
    op.execute("""
        CREATE UNIQUE INDEX one_confirmation_per_admission
        ON public.doctor_admission_confirmations
            (organization_id, appointment_id, consuming_admission_revision)
        WHERE state = 'CONSUMED'
    """)

    # Deferred proof uses the final stored confirmation, appointment, history
    # and closed queue entry. The admission changes Appointment and QueueDay
    # revisions by one; the other captured policy/control revisions must still
    # equal their live values. Nothing below locks QueueDay after its control.
    op.execute("""
        CREATE FUNCTION public.assert_admission_confirmation_final() RETURNS trigger
        LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog AS $$
        DECLARE
            approval public.doctor_admission_confirmations%ROWTYPE;
        BEGIN
            SELECT * INTO approval FROM public.doctor_admission_confirmations
            WHERE organization_id = NEW.organization_id AND id = NEW.id;
            IF NOT FOUND THEN RETURN NULL; END IF;
            IF approval.state = 'ISSUED' THEN
                PERFORM public.assert_confirmation_issued_scope(approval);
            ELSIF approval.state = 'CONSUMED' AND NOT EXISTS (
                SELECT 1 FROM public.appointments a
                JOIN public.appointment_history h
                  ON h.organization_id = a.organization_id
                 AND h.appointment_id = a.id
                 AND h.appointment_revision = approval.consuming_admission_revision
                JOIN public.queue_entries e
                  ON e.organization_id = a.organization_id
                 AND e.appointment_id = a.id
                 AND e.closure_history_revision = h.appointment_revision
                JOIN public.queue_days q
                  ON q.organization_id = e.organization_id
                 AND q.id = e.queue_day_id
                JOIN public.doctor_controls c
                  ON c.organization_id = a.organization_id
                 AND c.doctor_staff_id = a.doctor_staff_id
                JOIN public.clinic_policy_heads capacity_head
                  ON capacity_head.organization_id = a.organization_id
                 AND capacity_head.kind = 'CAPACITY'
                JOIN public.clinic_policy_versions capacity_policy
                  ON capacity_policy.organization_id = capacity_head.organization_id
                 AND capacity_policy.kind = 'CAPACITY'
                 AND capacity_policy.version = capacity_head.current_version
                JOIN public.clinic_policy_heads admission_head
                  ON admission_head.organization_id = a.organization_id
                 AND admission_head.kind = 'ADMISSION'
                JOIN public.clinic_policy_versions admission_policy
                  ON admission_policy.organization_id = admission_head.organization_id
                 AND admission_policy.kind = 'ADMISSION'
                 AND admission_policy.version = admission_head.current_version
                JOIN public.staff_accounts actor
                  ON actor.organization_id = h.organization_id
                 AND actor.id = h.actor_staff_id
                JOIN public.staff_accounts doctor
                  ON doctor.organization_id = a.organization_id
                 AND doctor.id = a.doctor_staff_id
                JOIN public.organizations clinic
                  ON clinic.id = a.organization_id
                WHERE a.organization_id = approval.organization_id
                  AND a.id = approval.appointment_id
                  AND a.patient_id = approval.patient_id
                  AND a.doctor_staff_id = approval.doctor_staff_id
                  AND a.service_date = approval.service_date
                  AND a.lane = approval.lane
                  AND a.status = 'In Examination'
                  AND a.revision = approval.appointment_revision + 1
                  AND a.revision = approval.consuming_admission_revision
                  AND h.old_status = 'Waiting'
                  AND h.new_status = 'In Examination'
                  AND h.old_service_date = approval.service_date
                  AND h.new_service_date = approval.service_date
                  AND h.new_number = a.current_number
                  AND h.occurred_at <= approval.consumed_at
                  AND e.patient_id = approval.patient_id
                  AND e.doctor_staff_id = approval.doctor_staff_id
                  AND e.service_date = approval.service_date
                  AND e.lane = approval.lane AND e.disposition = 'CLOSED'
                  AND e.closure_queue_revision = approval.queue_day_revision
                  AND q.doctor_staff_id = approval.doctor_staff_id
                  AND q.service_date = approval.service_date
                  AND q.lane = approval.lane
                  AND q.revision = approval.queue_day_revision + 1
                  AND c.revision = approval.control_revision
                  AND c.capacity_policy_version = capacity_head.current_version
                  AND capacity_head.revision = approval.capacity_revision
                  AND admission_head.revision = approval.admission_revision
                  AND clinic.state = 'ACTIVE'
                  AND doctor.role = 'DOCTOR_OWNER'
                  AND doctor.enabled AND doctor.verified
                  AND doctor.reset_block_operation_id IS NULL
                  AND (capacity_policy.payload->>'paused')::boolean = false
                  AND (capacity_policy.payload->>'capacity')::bigint >= 2
                  AND (SELECT count(*) FROM public.appointments active_exam
                       WHERE active_exam.organization_id = a.organization_id
                         AND active_exam.doctor_staff_id = a.doctor_staff_id
                         AND active_exam.status = 'In Examination') BETWEEN 2
                      AND (capacity_policy.payload->>'capacity')::bigint
                  AND actor.role = h.actor_role
                  AND actor.enabled AND actor.verified
                  AND actor.reset_block_operation_id IS NULL
                  AND (admission_policy.payload->>'actors' = 'BOTH'
                       OR (admission_policy.payload->>'actors' = 'DOCTOR'
                           AND actor.role = 'DOCTOR_OWNER')
                       OR (admission_policy.payload->>'actors' = 'RECEPTIONIST'
                           AND actor.role = 'RECEPTIONIST'))
            ) THEN
                RAISE EXCEPTION 'Admission confirmation lacks genuine scoped history'
                    USING ERRCODE = '23514';
            END IF;
            RETURN NULL;
        END $$
    """)
    op.execute("""
        CREATE CONSTRAINT TRIGGER admission_confirmation_final
        AFTER INSERT OR UPDATE ON public.doctor_admission_confirmations
        DEFERRABLE INITIALLY DEFERRED FOR EACH ROW
        EXECUTE FUNCTION public.assert_admission_confirmation_final()
    """)
    for signature in (
        "require_confirmation_doctor(uuid, uuid)",
        "assert_confirmation_issued_scope(public.doctor_admission_confirmations)",
        "guard_admission_confirmation()",
        "assert_admission_confirmation_final()",
    ):
        op.execute(f"REVOKE ALL ON FUNCTION public.{signature} FROM PUBLIC")

    # D-04 policy seed's established NOLOGIN owner can create a control row
    # for an Owner added after the Organization. No runtime/operator EXECUTE,
    # no duplicate stored capacity or pause state, no broad staff write grant.
    op.execute("""
        CREATE POLICY staff_queue_seed_owner_read ON public.staff_accounts
        FOR SELECT TO carepulse_policy_seed USING (role = 'DOCTOR_OWNER')
    """)
    op.execute("GRANT SELECT ON public.staff_accounts TO carepulse_policy_seed")
    op.execute("""
        CREATE POLICY doctor_controls_seed_access ON public.doctor_controls
        FOR ALL TO carepulse_policy_seed USING (true) WITH CHECK (true)
    """)
    op.execute(
        "GRANT SELECT, INSERT ON public.doctor_controls TO carepulse_policy_seed"
    )
    op.execute("""
        CREATE FUNCTION public.seed_doctor_control_for_staff()
        RETURNS trigger LANGUAGE plpgsql SECURITY DEFINER
        SET search_path = pg_catalog AS $$
        BEGIN
            IF NEW.role = 'DOCTOR_OWNER' AND NEW.enabled AND NEW.verified THEN
                INSERT INTO public.doctor_controls
                    (organization_id, doctor_staff_id, capacity_policy_version)
                SELECT NEW.organization_id, NEW.id, h.current_version
                FROM public.clinic_policy_heads h
                WHERE h.organization_id = NEW.organization_id
                  AND h.kind = 'CAPACITY'
                ON CONFLICT (organization_id, doctor_staff_id) DO NOTHING;
            END IF;
            RETURN NEW;
        END $$
    """)
    op.execute("""
        REVOKE ALL ON FUNCTION public.seed_doctor_control_for_staff() FROM PUBLIC
    """)
    op.execute("""
        CREATE TRIGGER staff_doctor_control_seed
        AFTER INSERT OR UPDATE OF enabled, verified, role ON public.staff_accounts
        FOR EACH ROW EXECUTE FUNCTION public.seed_doctor_control_for_staff()
    """)
    op.execute("GRANT USAGE, CREATE ON SCHEMA public TO carepulse_policy_seed")
    op.execute("""
        ALTER FUNCTION public.seed_doctor_control_for_staff()
        OWNER TO carepulse_policy_seed
    """)
    op.execute("REVOKE CREATE ON SCHEMA public FROM carepulse_policy_seed")
    # Backfill runs only after the privilege-restricted trigger is installed.
    op.execute("""
        INSERT INTO public.doctor_controls
            (organization_id, doctor_staff_id, capacity_policy_version)
        SELECT s.organization_id, s.id, h.current_version
        FROM public.staff_accounts s
        JOIN public.clinic_policy_heads h ON h.organization_id = s.organization_id
            AND h.kind = 'CAPACITY'
        WHERE s.role = 'DOCTOR_OWNER' AND s.enabled AND s.verified
        ON CONFLICT (organization_id, doctor_staff_id) DO NOTHING
    """)


def downgrade() -> None:
    raise RuntimeError("D-06 queue foundations migration is forward-only")

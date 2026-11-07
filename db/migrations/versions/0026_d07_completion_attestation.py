"""D-07: prospective positive completion evidence and revision-zero source state."""

from alembic import op

revision = "0026_d07_completion_attestation"
down_revision = "0025_d06_queue_foundations"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Historical Completed rows remain unverified; evidence is never backfilled.
    op.execute("""
        ALTER TABLE public.sensitive_audit
        ADD CONSTRAINT sensitive_audit_tenant_id_unique
        UNIQUE (organization_id, id)
    """)
    op.execute("""
        CREATE TABLE public.completion_attestations (
            id uuid PRIMARY KEY,
            organization_id uuid NOT NULL REFERENCES public.organizations(id),
            appointment_id uuid NOT NULL,
            visit_id uuid NOT NULL,
            completion_history_revision bigint NOT NULL
                CHECK (completion_history_revision > 0),
            examination_occurred boolean NOT NULL
                CHECK (examination_occurred IS TRUE),
            actor_staff_id uuid NOT NULL,
            actor_role text NOT NULL
                CHECK (actor_role IN ('DOCTOR_OWNER', 'RECEPTIONIST')),
            completed_at timestamptz NOT NULL
                CHECK (isfinite(completed_at)
                       AND completed_at = date_trunc('second', completed_at)),
            sensitive_audit_id uuid NOT NULL,
            UNIQUE (organization_id, id),
            UNIQUE (organization_id, appointment_id),
            UNIQUE (organization_id, sensitive_audit_id),
            FOREIGN KEY (organization_id, appointment_id)
                REFERENCES public.appointments(organization_id, id),
            FOREIGN KEY (organization_id, visit_id)
                REFERENCES public.visits(organization_id, id),
            FOREIGN KEY (organization_id, appointment_id,
                         completion_history_revision)
                REFERENCES public.appointment_history
                    (organization_id, appointment_id, appointment_revision),
            FOREIGN KEY (organization_id, actor_staff_id)
                REFERENCES public.staff_accounts(organization_id, id),
            FOREIGN KEY (organization_id, sensitive_audit_id)
                REFERENCES public.sensitive_audit(organization_id, id)
        )
    """)
    op.execute("""
        CREATE TABLE public.followup_source_state (
            organization_id uuid NOT NULL,
            visit_id uuid NOT NULL,
            source_revision bigint NOT NULL DEFAULT 0
                CHECK (source_revision = 0),
            provenance_state text NOT NULL DEFAULT 'UNKNOWN'
                CHECK (provenance_state = 'UNKNOWN'),
            PRIMARY KEY (organization_id, visit_id),
            FOREIGN KEY (organization_id, visit_id)
                REFERENCES public.visits(organization_id, id)
        )
    """)
    for relation in ("completion_attestations", "followup_source_state"):
        op.execute(f"ALTER TABLE public.{relation} ENABLE ROW LEVEL SECURITY")
        op.execute(f"ALTER TABLE public.{relation} FORCE ROW LEVEL SECURITY")
        op.execute(f"""
            CREATE POLICY {relation}_runtime ON public.{relation}
            FOR SELECT TO carepulse_runtime
            USING (organization_id =
                NULLIF(current_setting('app.organization_id', true), '')::uuid)
        """)
        op.execute(f"""
            CREATE POLICY {relation}_migration ON public.{relation}
            FOR ALL TO carepulse_migration USING (true) WITH CHECK (true)
        """)
        op.execute(f"REVOKE ALL ON public.{relation} FROM PUBLIC")
        op.execute(f"GRANT SELECT ON public.{relation} TO carepulse_runtime")

    # A runtime SQL statement cannot create an attestation or reserved audit
    # independently. The definer routine below is the sole runtime write seam.
    op.execute("""
        CREATE FUNCTION public.guard_completion_attestation() RETURNS trigger
        LANGUAGE plpgsql SET search_path = pg_catalog AS $$
        BEGIN
            IF TG_OP <> 'INSERT' THEN
                RAISE EXCEPTION 'Completion evidence is immutable'
                    USING ERRCODE = '23514';
            END IF;
            IF current_user <> 'carepulse_migration' THEN
                RAISE EXCEPTION 'Completion evidence requires protected writer'
                    USING ERRCODE = '42501';
            END IF;
            RETURN NEW;
        END $$
    """)
    op.execute("""
        CREATE TRIGGER completion_attestation_guard
        BEFORE INSERT OR UPDATE OR DELETE ON public.completion_attestations
        FOR EACH ROW EXECUTE FUNCTION public.guard_completion_attestation()
    """)
    op.execute("""
        CREATE FUNCTION public.deny_completion_attestation_truncate()
        RETURNS trigger LANGUAGE plpgsql SET search_path = pg_catalog AS $$
        BEGIN
            RAISE EXCEPTION 'Completion evidence cannot be truncated'
                USING ERRCODE = '23514';
        END $$
    """)
    op.execute("""
        CREATE TRIGGER completion_attestation_no_truncate
        BEFORE TRUNCATE ON public.completion_attestations
        FOR EACH STATEMENT
        EXECUTE FUNCTION public.deny_completion_attestation_truncate()
    """)
    op.execute("""
        CREATE FUNCTION public.guard_completion_audit() RETURNS trigger
        LANGUAGE plpgsql SET search_path = pg_catalog AS $$
        BEGIN
            IF NEW.action = 'VISIT_EXAMINATION_COMPLETED'
               AND current_user <> 'carepulse_migration' THEN
                RAISE EXCEPTION 'Completion audit requires protected writer'
                    USING ERRCODE = '42501';
            END IF;
            RETURN NEW;
        END $$
    """)
    op.execute("""
        CREATE TRIGGER sensitive_audit_completion_guard
        BEFORE INSERT ON public.sensitive_audit
        FOR EACH ROW EXECUTE FUNCTION public.guard_completion_audit()
    """)
    op.execute("""
        CREATE FUNCTION public.guard_prospective_completion() RETURNS trigger
        LANGUAGE plpgsql SET search_path = pg_catalog AS $$
        BEGIN
            IF TG_OP = 'INSERT' THEN
                IF NEW.status = 'Completed' THEN
                    RAISE EXCEPTION 'Appointment cannot start Completed'
                        USING ERRCODE = '23514';
                END IF;
            ELSIF NEW.status = 'Completed' AND OLD.status <> 'Completed' THEN
                IF OLD.status <> 'In Examination'
                   OR current_user <> 'carepulse_migration' THEN
                    RAISE EXCEPTION 'Completion requires protected examination writer'
                        USING ERRCODE = '42501';
                END IF;
            END IF;
            RETURN NEW;
        END $$
    """)
    op.execute("""
        CREATE TRIGGER appointment_prospective_completion_guard
        BEFORE INSERT OR UPDATE ON public.appointments
        FOR EACH ROW EXECUTE FUNCTION public.guard_prospective_completion()
    """)

    # Two independent deferred edges: a new completion needs its attestation,
    # and a new attestation must match a genuine new completion and audit.
    op.execute("""
        CREATE FUNCTION public.assert_positive_exam_completion(
            target_organization uuid, target_appointment uuid,
            target_revision bigint)
        RETURNS void LANGUAGE plpgsql SECURITY DEFINER
        SET search_path = pg_catalog AS $$
        BEGIN
            IF NOT EXISTS (
                SELECT 1
                FROM public.completion_attestations c
                JOIN public.appointments a
                  ON a.organization_id = c.organization_id
                 AND a.id = c.appointment_id
                JOIN public.visits v
                  ON v.organization_id = c.organization_id
                 AND v.id = c.visit_id
                 AND v.appointment_id = a.id
                 AND v.patient_id = a.patient_id
                 AND v.doctor_staff_id = a.doctor_staff_id
                 AND v.service_date = a.service_date
                JOIN public.appointment_history h
                  ON h.organization_id = c.organization_id
                 AND h.appointment_id = c.appointment_id
                 AND h.appointment_revision = c.completion_history_revision
                JOIN public.appointment_history prior
                  ON prior.organization_id = h.organization_id
                 AND prior.appointment_id = h.appointment_id
                 AND prior.appointment_revision = h.appointment_revision - 1
                JOIN public.appointment_history admission
                  ON admission.organization_id = v.organization_id
                 AND admission.appointment_id = v.appointment_id
                 AND admission.appointment_revision = v.admission_history_revision
                JOIN public.sensitive_audit audit
                  ON audit.organization_id = c.organization_id
                 AND audit.id = c.sensitive_audit_id
                WHERE c.organization_id = target_organization
                  AND c.appointment_id = target_appointment
                  AND c.completion_history_revision = target_revision
                  AND c.examination_occurred IS TRUE
                  AND a.status = 'Completed' AND a.revision = target_revision
                  AND v.admission_history_revision < target_revision
                  AND v.admitted_at = admission.occurred_at
                  AND admission.old_status = 'Waiting'
                  AND admission.new_status = 'In Examination'
                  AND admission.new_service_date = a.service_date
                  AND admission.occurred_at <= h.occurred_at
                  AND h.action = 'STATUS' AND h.reason = 'Examination completed'
                  AND h.old_status = 'In Examination'
                  AND h.new_status = 'Completed'
                  AND h.new_service_date = a.service_date
                  AND h.old_service_date = a.service_date
                  AND h.old_number = a.current_number
                  AND h.new_number = a.current_number
                  AND h.price_snapshot_appointment_id = a.id
                  AND prior.new_status = 'In Examination'
                  AND prior.new_service_date = h.old_service_date
                  AND prior.new_number = h.old_number
                  AND prior.occurred_at <= h.occurred_at
                  AND c.actor_staff_id = h.actor_staff_id
                  AND c.actor_role = h.actor_role
                  AND c.completed_at = h.occurred_at
                  AND audit.action = 'VISIT_EXAMINATION_COMPLETED'
                  AND audit.actor_staff_id = c.actor_staff_id
                  AND audit.actor_role = c.actor_role
                  AND audit.operator_id IS NULL
                  AND audit.recovery_operation_id IS NULL
                  AND audit.reason = 'Examination completion'
                  AND audit.occurred_at = c.completed_at
                  AND audit.correlation_id = c.id::text
                  AND audit.before_state = jsonb_build_object(
                      'appointment_id', a.id, 'visit_id', v.id,
                      'appointment_revision', target_revision - 1,
                      'status', 'In Examination')
                  AND audit.after_state = jsonb_build_object(
                      'appointment_id', a.id, 'visit_id', v.id,
                      'appointment_revision', target_revision,
                      'status', 'Completed', 'examination_occurred', true)
            ) THEN
                RAISE EXCEPTION 'Missing or inconsistent positive examination evidence'
                    USING ERRCODE = '23514';
            END IF;
        END $$
    """)
    op.execute("""
        CREATE FUNCTION public.check_new_completion() RETURNS trigger
        LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog AS $$
        BEGIN
            PERFORM public.assert_positive_exam_completion(
                NEW.organization_id, NEW.id, NEW.revision);
            RETURN NULL;
        END $$
    """)
    op.execute("""
        CREATE CONSTRAINT TRIGGER appointment_completion_attested
        AFTER UPDATE ON public.appointments
        DEFERRABLE INITIALLY DEFERRED
        FOR EACH ROW WHEN (OLD.status IS DISTINCT FROM NEW.status
                           AND NEW.status = 'Completed')
        EXECUTE FUNCTION public.check_new_completion()
    """)
    op.execute("""
        CREATE FUNCTION public.check_new_attestation() RETURNS trigger
        LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog AS $$
        BEGIN
            PERFORM public.assert_positive_exam_completion(
                NEW.organization_id, NEW.appointment_id,
                NEW.completion_history_revision);
            RETURN NULL;
        END $$
    """)
    op.execute("""
        CREATE CONSTRAINT TRIGGER completion_attestation_matched
        AFTER INSERT ON public.completion_attestations
        DEFERRABLE INITIALLY DEFERRED FOR EACH ROW
        EXECUTE FUNCTION public.check_new_attestation()
    """)

    # The signed/live API transaction must authorize this call. A caller-set
    # app.staff_id GUC is defense in depth, not independent actor authentication.
    op.execute("""
        CREATE FUNCTION public.complete_examined_visit(
            target_organization uuid, target_appointment uuid,
            expected_revision bigint, examination_occurred boolean)
        RETURNS TABLE (attestation_id uuid,
                       completion_history_revision bigint,
                       completed_at timestamptz)
        LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog AS $$
        DECLARE
            actual_actor uuid;
            actual_role text;
            locked_appointment public.appointments%ROWTYPE;
            locked_visit public.visits%ROWTYPE;
            audit_id uuid;
        BEGIN
            IF session_user <> 'carepulse_runtime'
               OR target_organization IS NULL OR target_appointment IS NULL
               OR expected_revision IS NULL OR examination_occurred IS DISTINCT FROM TRUE
               OR current_setting('app.organization_id', true)
                  IS DISTINCT FROM target_organization::text
               OR NULLIF(current_setting('app.staff_id', true), '') IS NULL THEN
                RAISE EXCEPTION 'Examination completion denied'
                    USING ERRCODE = '42501';
            END IF;
            actual_actor := current_setting('app.staff_id', true)::uuid;
            PERFORM 1 FROM public.organizations
            WHERE id = target_organization AND state = 'ACTIVE' FOR SHARE;
            IF NOT FOUND THEN
                RAISE EXCEPTION 'Clinic is inactive' USING ERRCODE = '42501';
            END IF;
            SELECT s.role INTO actual_role FROM public.staff_accounts s
            WHERE s.organization_id = target_organization
              AND s.id = actual_actor
              AND s.role IN ('DOCTOR_OWNER', 'RECEPTIONIST')
              AND s.enabled AND s.verified
              AND s.reset_block_operation_id IS NULL
            FOR SHARE;
            IF NOT FOUND THEN
                RAISE EXCEPTION 'Completion actor is inactive'
                    USING ERRCODE = '42501';
            END IF;
            SELECT * INTO locked_appointment FROM public.appointments a
            WHERE a.organization_id = target_organization
              AND a.id = target_appointment FOR UPDATE;
            IF NOT FOUND OR locked_appointment.status <> 'In Examination'
               OR locked_appointment.revision <> expected_revision THEN
                RAISE EXCEPTION 'Appointment is not at expected examination revision'
                    USING ERRCODE = '40001';
            END IF;
            SELECT * INTO locked_visit FROM public.visits v
            WHERE v.organization_id = target_organization
              AND v.appointment_id = target_appointment
              AND v.patient_id = locked_appointment.patient_id
              AND v.doctor_staff_id = locked_appointment.doctor_staff_id
              AND v.service_date = locked_appointment.service_date
              AND v.admission_history_revision <= expected_revision
            FOR UPDATE;
            IF NOT FOUND OR NOT EXISTS (
                SELECT 1 FROM public.appointment_history h
                WHERE h.organization_id = target_organization
                  AND h.appointment_id = target_appointment
                  AND h.appointment_revision = expected_revision
                  AND h.new_status = 'In Examination'
                  AND h.new_service_date = locked_appointment.service_date
                  AND h.new_number = locked_appointment.current_number
            ) THEN
                RAISE EXCEPTION 'Matching admission Visit required'
                    USING ERRCODE = '23514';
            END IF;

            UPDATE public.appointments SET status = 'Completed',
                revision = revision + 1
            WHERE organization_id = target_organization AND id = target_appointment;
            completion_history_revision := expected_revision + 1;
            INSERT INTO public.appointment_history (
                id, organization_id, appointment_id, appointment_revision,
                action, old_status, new_status, old_service_date, new_service_date,
                old_number, new_number, actor_staff_id, actor_role,
                reason, price_snapshot_appointment_id)
            VALUES (
                gen_random_uuid(), target_organization, target_appointment,
                completion_history_revision, 'STATUS', 'In Examination', 'Completed',
                locked_appointment.service_date, locked_appointment.service_date,
                locked_appointment.current_number, locked_appointment.current_number,
                actual_actor, actual_role, 'Examination completed', target_appointment)
            RETURNING occurred_at INTO completed_at;

            attestation_id := gen_random_uuid();
            audit_id := gen_random_uuid();
            INSERT INTO public.sensitive_audit (
                id, organization_id, actor_staff_id, actor_role,
                action, reason, correlation_id, before_state, after_state,
                occurred_at)
            VALUES (
                audit_id, target_organization, actual_actor, actual_role,
                'VISIT_EXAMINATION_COMPLETED', 'Examination completion',
                attestation_id::text,
                jsonb_build_object(
                    'appointment_id', target_appointment, 'visit_id', locked_visit.id,
                    'appointment_revision', expected_revision,
                    'status', 'In Examination'),
                jsonb_build_object(
                    'appointment_id', target_appointment, 'visit_id', locked_visit.id,
                    'appointment_revision', completion_history_revision,
                    'status', 'Completed', 'examination_occurred', true),
                completed_at);
            INSERT INTO public.completion_attestations (
                id, organization_id, appointment_id, visit_id,
                completion_history_revision, examination_occurred,
                actor_staff_id, actor_role, completed_at, sensitive_audit_id)
            VALUES (
                attestation_id, target_organization, target_appointment,
                locked_visit.id, completion_history_revision, TRUE,
                actual_actor, actual_role, completed_at, audit_id);
            RETURN NEXT;
        END $$
    """)
    for signature in (
        "guard_completion_attestation()",
        "deny_completion_attestation_truncate()",
        "guard_completion_audit()",
        "guard_prospective_completion()",
        "assert_positive_exam_completion(uuid,uuid,bigint)",
        "check_new_completion()",
        "check_new_attestation()",
        "complete_examined_visit(uuid,uuid,bigint,boolean)",
    ):
        op.execute(f"REVOKE ALL ON FUNCTION public.{signature} FROM PUBLIC")
    op.execute("""
        GRANT EXECUTE ON FUNCTION
        public.complete_examined_visit(uuid,uuid,bigint,boolean)
        TO carepulse_runtime
    """)


def downgrade() -> None:
    raise RuntimeError("D-07 completion evidence migration is forward-only")

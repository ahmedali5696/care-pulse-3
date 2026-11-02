"""D-07: admission-linked Visits and immutable Doctor clinical revisions."""

from alembic import op

revision = "0021_d07_visits"
down_revision = "0020_b07_manual_slot_weekly"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Earlier releases had no Visit storage. Never invent a missing admission.
    op.execute("""
        LOCK TABLE public.appointments, public.appointment_history
        IN SHARE ROW EXCLUSIVE MODE
    """)
    op.execute("""
        DO $$
        BEGIN
            IF EXISTS (
                SELECT 1 FROM public.appointments a
                WHERE a.status IN ('In Examination', 'Completed')
                   OR EXISTS (
                       SELECT 1 FROM public.appointment_history h
                       WHERE h.organization_id = a.organization_id
                         AND h.appointment_id = a.id
                         AND (h.old_status = 'In Examination'
                              OR h.new_status = 'In Examination')
                   )
            ) THEN
                RAISE EXCEPTION 'Existing admissions require reviewed Visit migration'
                    USING ERRCODE = '23514';
            END IF;
        END $$
    """)
    op.execute("""
        CREATE TABLE public.visits (
            id uuid PRIMARY KEY,
            organization_id uuid NOT NULL REFERENCES public.organizations(id),
            appointment_id uuid NOT NULL,
            patient_id uuid NOT NULL,
            doctor_staff_id uuid NOT NULL,
            service_date date NOT NULL CHECK (isfinite(service_date)),
            admission_history_revision bigint NOT NULL
                CHECK (admission_history_revision > 0),
            admitted_at timestamptz NOT NULL,
            clinical_revision bigint NOT NULL DEFAULT 0 CHECK (clinical_revision >= 0),
            examination text,
            findings text,
            diagnosis text,
            medical_notes text,
            treatment text,
            tests text,
            followup_decision jsonb,
            UNIQUE (organization_id, id),
            UNIQUE (organization_id, appointment_id),
            FOREIGN KEY (organization_id, appointment_id)
                REFERENCES public.appointments(organization_id, id),
            FOREIGN KEY (organization_id, patient_id)
                REFERENCES public.patients(organization_id, id),
            FOREIGN KEY (organization_id, doctor_staff_id)
                REFERENCES public.staff_accounts(organization_id, id),
            FOREIGN KEY (organization_id, appointment_id, admission_history_revision)
                REFERENCES public.appointment_history
                    (organization_id, appointment_id, appointment_revision)
        )
    """)
    op.execute("""
        CREATE TABLE public.clinical_revisions (
            id uuid PRIMARY KEY,
            organization_id uuid NOT NULL REFERENCES public.organizations(id),
            visit_id uuid NOT NULL,
            revision bigint NOT NULL CHECK (revision >= 1),
            change_kind text NOT NULL CHECK (change_kind IN
                ('CORRECTION', 'DECISION_DEFAULT', 'DECISION_EXCEPTION', 'EXTENSION')),
            old_values jsonb NOT NULL,
            new_values jsonb NOT NULL,
            actor_staff_id uuid NOT NULL,
            actor_role text NOT NULL CHECK (actor_role = 'DOCTOR_OWNER'),
            occurred_at timestamptz NOT NULL DEFAULT date_trunc('second', clock_timestamp())
                CHECK (occurred_at = date_trunc('second', occurred_at)),
            reason text CHECK (reason IS NULL OR btrim(reason) <> ''),
            UNIQUE (organization_id, id),
            UNIQUE (organization_id, visit_id, revision),
            FOREIGN KEY (organization_id, visit_id)
                REFERENCES public.visits(organization_id, id),
            FOREIGN KEY (organization_id, actor_staff_id)
                REFERENCES public.staff_accounts(organization_id, id),
            CONSTRAINT clinical_revision_reason_required CHECK
                (change_kind = 'DECISION_DEFAULT' OR
                 (reason IS NOT NULL AND btrim(reason) <> ''))
        )
    """)
    op.execute("""
        CREATE FUNCTION public.guard_visit() RETURNS trigger
        LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog AS $$
        DECLARE
            actor uuid;
            admission_time timestamptz;
            kind text;
            justification text;
            old_content jsonb;
            new_content jsonb;
        BEGIN
            IF TG_OP = 'DELETE' THEN
                RAISE EXCEPTION 'Visit deletion forbidden' USING ERRCODE = '23514';
            END IF;
            IF session_user = 'carepulse_runtime' THEN
                IF NULLIF(current_setting('app.staff_id', true), '') IS NULL THEN
                    RAISE EXCEPTION 'Clinical actor required' USING ERRCODE = '42501';
                END IF;
                actor := current_setting('app.staff_id', true)::uuid;
            END IF;
            IF TG_OP = 'INSERT' THEN
                IF NEW.clinical_revision <> 0 OR
                   (NEW.examination, NEW.findings, NEW.diagnosis,
                    NEW.medical_notes, NEW.treatment, NEW.tests,
                    NEW.followup_decision) IS DISTINCT FROM
                   (NULL, NULL, NULL, NULL, NULL, NULL, NULL) THEN
                    RAISE EXCEPTION 'Admission creates an empty Visit'
                        USING ERRCODE = '23514';
                END IF;
                SELECT h.occurred_at INTO admission_time
                    FROM public.appointments a
                    JOIN public.appointment_history h
                      ON h.organization_id = a.organization_id
                     AND h.appointment_id = a.id
                     AND h.appointment_revision = NEW.admission_history_revision
                    JOIN public.staff_accounts s
                      ON s.organization_id = h.organization_id
                     AND s.id = h.actor_staff_id
                    WHERE a.organization_id = NEW.organization_id
                      AND a.id = NEW.appointment_id
                      AND a.patient_id = NEW.patient_id
                      AND a.doctor_staff_id = NEW.doctor_staff_id
                      AND a.service_date = NEW.service_date
                      AND a.status = 'In Examination'
                      AND a.revision = NEW.admission_history_revision
                      AND h.old_status = 'Waiting' AND h.new_status = 'In Examination'
                      AND h.new_service_date = NEW.service_date
                      AND s.enabled AND s.verified
                      AND s.role = h.actor_role
                      AND s.role IN ('DOCTOR_OWNER', 'RECEPTIONIST')
                      AND (session_user <> 'carepulse_runtime' OR
                           h.actor_staff_id = actor);
                IF admission_time IS NULL THEN
                    RAISE EXCEPTION 'Visit requires authorized admission evidence'
                        USING ERRCODE = '42501';
                END IF;
                NEW.admitted_at := admission_time;
            ELSE
                IF (NEW.id, NEW.organization_id, NEW.appointment_id,
                    NEW.patient_id, NEW.doctor_staff_id, NEW.service_date,
                    NEW.admission_history_revision, NEW.admitted_at) IS DISTINCT FROM
                   (OLD.id, OLD.organization_id, OLD.appointment_id,
                    OLD.patient_id, OLD.doctor_staff_id, OLD.service_date,
                    OLD.admission_history_revision, OLD.admitted_at)
                   OR NEW.clinical_revision <> OLD.clinical_revision THEN
                    RAISE EXCEPTION 'Visit identity or clinical revision invalid'
                        USING ERRCODE = '23514';
                END IF;
                IF session_user = 'carepulse_runtime' AND actor::text IS DISTINCT FROM
                   OLD.doctor_staff_id::text THEN
                    RAISE EXCEPTION 'Doctor only clinical update'
                        USING ERRCODE = '42501';
                END IF;
                IF NOT EXISTS (
                    SELECT 1 FROM public.staff_accounts s
                    WHERE s.organization_id = OLD.organization_id
                      AND s.id = OLD.doctor_staff_id
                      AND s.role = 'DOCTOR_OWNER'
                      AND s.enabled AND s.verified
                ) THEN
                    RAISE EXCEPTION 'Doctor not authorized' USING ERRCODE = '42501';
                END IF;
                old_content := jsonb_build_object(
                    'examination', OLD.examination, 'findings', OLD.findings,
                    'diagnosis', OLD.diagnosis, 'medical_notes', OLD.medical_notes,
                    'treatment', OLD.treatment, 'tests', OLD.tests,
                    'followup_decision', OLD.followup_decision);
                new_content := jsonb_build_object(
                    'examination', NEW.examination, 'findings', NEW.findings,
                    'diagnosis', NEW.diagnosis, 'medical_notes', NEW.medical_notes,
                    'treatment', NEW.treatment, 'tests', NEW.tests,
                    'followup_decision', NEW.followup_decision);
                IF old_content = new_content THEN
                    RAISE EXCEPTION 'Clinical revision requires a change'
                        USING ERRCODE = '23514';
                END IF;
                NEW.clinical_revision := OLD.clinical_revision + 1;
                kind := COALESCE(NULLIF(current_setting('app.clinical_change_kind', true), ''),
                                 'CORRECTION');
                justification := NULLIF(btrim(current_setting('app.clinical_reason', true)), '');
                IF kind NOT IN ('CORRECTION','DECISION_DEFAULT',
                                'DECISION_EXCEPTION','EXTENSION') OR
                   (kind <> 'DECISION_DEFAULT' AND justification IS NULL) OR
                   (kind = 'DECISION_DEFAULT' AND (
                        (NEW.examination, NEW.findings, NEW.diagnosis,
                         NEW.medical_notes, NEW.treatment, NEW.tests) IS DISTINCT FROM
                        (OLD.examination, OLD.findings, OLD.diagnosis,
                         OLD.medical_notes, OLD.treatment, OLD.tests)
                        OR NEW.followup_decision ->> 'kind' IS DISTINCT FROM 'DEFAULT'
                   )) THEN
                    RAISE EXCEPTION 'Invalid clinical change evidence'
                        USING ERRCODE = '23514';
                END IF;
                INSERT INTO public.clinical_revisions
                    (id, organization_id, visit_id, revision, change_kind,
                     old_values, new_values, actor_staff_id, actor_role,
                     occurred_at, reason)
                VALUES (gen_random_uuid(), OLD.organization_id, OLD.id,
                        NEW.clinical_revision, kind, old_content, new_content,
                        OLD.doctor_staff_id, 'DOCTOR_OWNER',
                        date_trunc('second', clock_timestamp()), justification);
            END IF;
            RETURN NEW;
        END $$
    """)
    op.execute("""
        CREATE TRIGGER visits_guard
        BEFORE INSERT OR UPDATE OR DELETE ON public.visits
        FOR EACH ROW EXECUTE FUNCTION public.guard_visit()
    """)
    op.execute("""
        CREATE FUNCTION public.deny_clinical_revision_mutation() RETURNS trigger
        LANGUAGE plpgsql SET search_path = pg_catalog AS $$
        BEGIN
            RAISE EXCEPTION 'Clinical revisions are immutable' USING ERRCODE = '23514';
        END $$
    """)
    op.execute("""
        CREATE TRIGGER clinical_revisions_immutable
        BEFORE UPDATE OR DELETE ON public.clinical_revisions
        FOR EACH ROW EXECUTE FUNCTION public.deny_clinical_revision_mutation()
    """)
    op.execute("""
        CREATE FUNCTION public.assert_visit_appointment_consistency() RETURNS trigger
        LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog AS $$
        DECLARE
            org uuid;
            appt uuid;
            current_status text;
            admitted boolean;
            has_visit boolean;
        BEGIN
            org := NEW.organization_id;
            appt := CASE WHEN TG_TABLE_NAME = 'visits'
                         THEN NEW.appointment_id ELSE NEW.id END;
            SELECT a.status INTO current_status
            FROM public.appointments a
            WHERE a.organization_id = org AND a.id = appt;
            SELECT EXISTS (
                SELECT 1 FROM public.appointment_history h
                WHERE h.organization_id = org AND h.appointment_id = appt
                  AND h.old_status = 'Waiting' AND h.new_status = 'In Examination'
            ) INTO admitted;
            SELECT EXISTS (
                SELECT 1 FROM public.visits v
                WHERE v.organization_id = org AND v.appointment_id = appt
            ) INTO has_visit;
            IF (current_status IN ('In Examination','Completed') AND
                (NOT admitted OR NOT has_visit))
               OR (current_status = 'Cancelled' AND admitted <> has_visit)
               OR (current_status IN ('Booked','Waiting','No-show') AND
                   (admitted OR has_visit)) THEN
                RAISE EXCEPTION 'Appointment and admission Visit inconsistent'
                    USING ERRCODE = '23514';
            END IF;
            RETURN NULL;
        END $$
    """)
    for relation in ("appointments", "visits"):
        op.execute(f"""
            CREATE CONSTRAINT TRIGGER visit_appointment_consistency
            AFTER INSERT OR UPDATE ON public.{relation}
            DEFERRABLE INITIALLY DEFERRED
            FOR EACH ROW EXECUTE FUNCTION public.assert_visit_appointment_consistency()
        """)
    for function in (
        "guard_visit()",
        "deny_clinical_revision_mutation()",
        "assert_visit_appointment_consistency()",
    ):
        op.execute(f"REVOKE ALL ON FUNCTION public.{function} FROM PUBLIC")
    for relation in ("visits", "clinical_revisions"):
        op.execute(f"ALTER TABLE public.{relation} ENABLE ROW LEVEL SECURITY")
        op.execute(f"ALTER TABLE public.{relation} FORCE ROW LEVEL SECURITY")
        op.execute(f"""
            CREATE POLICY {relation}_runtime ON public.{relation}
            FOR ALL TO carepulse_runtime
            USING (organization_id =
                NULLIF(current_setting('app.organization_id', true), '')::uuid)
            WITH CHECK (organization_id =
                NULLIF(current_setting('app.organization_id', true), '')::uuid)
        """)
        op.execute(f"""
            CREATE POLICY {relation}_migration ON public.{relation}
            FOR ALL TO carepulse_migration USING (true) WITH CHECK (true)
        """)
        op.execute(f"GRANT SELECT ON public.{relation} TO carepulse_runtime")
    op.execute("GRANT INSERT ON public.visits TO carepulse_runtime")
    op.execute("""
        GRANT UPDATE (examination, findings, diagnosis,
                      medical_notes, treatment, tests, followup_decision)
        ON public.visits TO carepulse_runtime
    """)


def downgrade() -> None:
    raise RuntimeError("D-07 Visit migration is forward-only")

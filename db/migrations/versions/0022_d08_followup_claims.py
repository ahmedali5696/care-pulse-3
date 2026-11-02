"""D-08: immutable preferential entitlement and transaction-bound claim ledger."""

from alembic import op

revision = "0022_d08_followup_claims"
down_revision = "0021_d07_visits"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Existing unpaired preferential records cannot acquire invented entitlement.
    op.execute("LOCK TABLE public.appointments IN SHARE ROW EXCLUSIVE MODE")
    op.execute("""
        DO $$ BEGIN
            IF EXISTS (SELECT 1 FROM public.appointments
                       WHERE service_type = 'FOLLOW_UP') THEN
                RAISE EXCEPTION 'Unverified existing follow-up requires reviewed migration'
                    USING ERRCODE = '23514';
            END IF;
        END $$
    """)
    op.execute("""
        CREATE TABLE public.followup_claims (
            id uuid PRIMARY KEY,
            organization_id uuid NOT NULL REFERENCES public.organizations(id),
            appointment_id uuid NOT NULL,
            patient_id uuid NOT NULL,
            doctor_staff_id uuid NOT NULL,
            source_visit_id uuid NOT NULL,
            source_appointment_id uuid NOT NULL,
            policy_snapshot jsonb NOT NULL CHECK
                (jsonb_typeof(policy_snapshot) = 'object' AND policy_snapshot <> '{}'::jsonb),
            completion_local_date date NOT NULL CHECK (isfinite(completion_local_date)),
            approved_deadline date NOT NULL CHECK
                (isfinite(approved_deadline) AND approved_deadline >= completion_local_date),
            revision bigint NOT NULL DEFAULT 1 CHECK (revision >= 1),
            state text NOT NULL CHECK (state IN ('ACTIVE','RELEASED','CONSUMED')),
            terminal_history_revision bigint,
            terminal_at timestamptz,
            UNIQUE (organization_id, id),
            UNIQUE (organization_id, appointment_id),
            UNIQUE (organization_id, id, source_visit_id, appointment_id),
            FOREIGN KEY (organization_id, appointment_id)
                REFERENCES public.appointments(organization_id, id),
            FOREIGN KEY (organization_id, patient_id)
                REFERENCES public.patients(organization_id, id),
            FOREIGN KEY (organization_id, doctor_staff_id)
                REFERENCES public.staff_accounts(organization_id, id),
            FOREIGN KEY (organization_id, source_visit_id)
                REFERENCES public.visits(organization_id, id),
            FOREIGN KEY (organization_id, source_appointment_id)
                REFERENCES public.appointments(organization_id, id),
            FOREIGN KEY (organization_id, appointment_id)
                REFERENCES public.appointment_price_snapshots(organization_id, appointment_id)
                DEFERRABLE INITIALLY DEFERRED,
            FOREIGN KEY (organization_id, appointment_id, terminal_history_revision)
                REFERENCES public.appointment_history
                    (organization_id, appointment_id, appointment_revision)
                DEFERRABLE INITIALLY DEFERRED,
            CHECK ((state = 'ACTIVE' AND terminal_history_revision IS NULL
                    AND terminal_at IS NULL) OR
                   (state <> 'ACTIVE' AND terminal_history_revision IS NOT NULL
                    AND terminal_at IS NOT NULL))
        )
    """)
    op.execute("""
        CREATE UNIQUE INDEX followup_source_unavailable
        ON public.followup_claims(organization_id, source_visit_id)
        WHERE state IN ('ACTIVE', 'CONSUMED')
    """)
    op.execute("""
        CREATE TABLE public.followup_extensions (
            id uuid PRIMARY KEY,
            organization_id uuid NOT NULL REFERENCES public.organizations(id),
            source_visit_id uuid NOT NULL,
            claim_id uuid NOT NULL,
            appointment_id uuid NOT NULL,
            prior_deadline date NOT NULL CHECK (isfinite(prior_deadline)),
            extended_deadline date NOT NULL CHECK
                (isfinite(extended_deadline) AND extended_deadline >= prior_deadline),
            issuing_doctor_staff_id uuid NOT NULL,
            reason text NOT NULL CHECK (btrim(reason) <> ''),
            issued_at timestamptz NOT NULL,
            expected_source_revision bigint NOT NULL CHECK (expected_source_revision >= 0),
            expected_claim_revision bigint NOT NULL CHECK (expected_claim_revision >= 1),
            clinical_revision_id uuid NOT NULL,
            clinical_audit_id uuid NOT NULL REFERENCES public.clinical_audit(id),
            UNIQUE (organization_id, id),
            UNIQUE (organization_id, clinical_revision_id),
            UNIQUE (organization_id, clinical_audit_id),
            FOREIGN KEY (organization_id, claim_id, source_visit_id, appointment_id)
                REFERENCES public.followup_claims
                    (organization_id, id, source_visit_id, appointment_id),
            FOREIGN KEY (organization_id, source_visit_id)
                REFERENCES public.visits(organization_id, id),
            FOREIGN KEY (organization_id, appointment_id)
                REFERENCES public.appointments(organization_id, id),
            FOREIGN KEY (organization_id, issuing_doctor_staff_id)
                REFERENCES public.staff_accounts(organization_id, id),
            FOREIGN KEY (organization_id, clinical_revision_id)
                REFERENCES public.clinical_revisions(organization_id, id)
        )
    """)
    op.execute("""
        CREATE FUNCTION public.guard_followup_claim() RETURNS trigger
        LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog AS $$
        DECLARE
            source_row record;
            target_row record;
            price_provenance jsonb;
            source_completion timestamptz;
            booking_actor uuid;
        BEGIN
            IF TG_OP = 'DELETE' THEN
                RAISE EXCEPTION 'Follow-up evidence cannot be deleted' USING ERRCODE = '23514';
            ELSIF TG_OP = 'UPDATE' THEN
                IF OLD.state <> 'ACTIVE' OR NEW.state NOT IN ('RELEASED','CONSUMED')
                   OR NEW.revision <> OLD.revision + 1
                   OR (to_jsonb(NEW) - 'state' - 'revision' -
                       'terminal_history_revision' - 'terminal_at') IS DISTINCT FROM
                      (to_jsonb(OLD) - 'state' - 'revision' -
                       'terminal_history_revision' - 'terminal_at') THEN
                    RAISE EXCEPTION 'Follow-up claim evidence is immutable'
                        USING ERRCODE = '23514';
                END IF;
                RETURN NEW;
            END IF;
            IF NEW.state <> 'ACTIVE' OR NEW.revision <> 1 OR
               NEW.terminal_history_revision IS NOT NULL OR NEW.terminal_at IS NOT NULL THEN
                RAISE EXCEPTION 'Claim must begin as an active reservation'
                    USING ERRCODE = '23514';
            END IF;
            SELECT a.id, a.organization_id, a.patient_id, a.doctor_staff_id,
                   a.status, a.service_type, a.classification,
                   v.followup_decision, v.clinical_revision
            INTO source_row FROM public.visits v
            JOIN public.appointments a ON a.organization_id = v.organization_id
                                      AND a.id = v.appointment_id
            WHERE v.organization_id = NEW.organization_id AND v.id = NEW.source_visit_id
              AND a.id = NEW.source_appointment_id
            FOR UPDATE OF a, v;
            IF NOT FOUND OR source_row.status <> 'Completed'
               OR source_row.service_type <> 'NEW_EXAMINATION'
               OR source_row.classification NOT IN ('ROUTINE','URGENT','EMERGENCY')
               OR source_row.patient_id <> NEW.patient_id
               OR source_row.doctor_staff_id <> NEW.doctor_staff_id THEN
                RAISE EXCEPTION 'Invalid completed follow-up source' USING ERRCODE = '23514';
            END IF;
            -- A Doctor recommendation is explicit; missing/opted-out decisions
            -- cannot be treated as an implicit default approval.
            IF source_row.clinical_revision < 1
               OR COALESCE(source_row.followup_decision ->> 'kind', '')
                      NOT IN ('DEFAULT','DEADLINE') THEN
                RAISE EXCEPTION 'Doctor follow-up authorization required'
                    USING ERRCODE = '23514';
            END IF;
            SELECT h.occurred_at INTO source_completion
            FROM public.appointment_history h
            WHERE h.organization_id = NEW.organization_id
              AND h.appointment_id = NEW.source_appointment_id
              AND h.new_status = 'Completed'
            ORDER BY h.appointment_revision DESC LIMIT 1;
            IF source_completion IS NULL OR NEW.completion_local_date <>
                (source_completion AT TIME ZONE 'Africa/Cairo')::date THEN
                RAISE EXCEPTION 'Incorrect Cairo completion day' USING ERRCODE = '23514';
            END IF;
            SELECT h.actor_staff_id INTO booking_actor
            FROM public.appointment_history h
            WHERE h.organization_id = NEW.organization_id
              AND h.appointment_id = NEW.appointment_id AND h.action = 'CREATED';
            IF (source_row.followup_decision ->> 'kind' = 'DEFAULT'
                AND NEW.approved_deadline <> NEW.completion_local_date + 7)
               OR (source_row.followup_decision ->> 'kind' = 'DEADLINE'
                   AND (btrim(COALESCE(source_row.followup_decision ->> 'reason', '')) = ''
                        OR source_row.followup_decision ->> 'deadline_date'
                           IS DISTINCT FROM NEW.approved_deadline::text))
               OR NEW.policy_snapshot ->> 'source_visit_id'
                    IS DISTINCT FROM NEW.source_visit_id::text
               OR NEW.policy_snapshot ->> 'doctor_staff_id'
                    IS DISTINCT FROM NEW.doctor_staff_id::text
               OR NEW.policy_snapshot ->> 'source_revision'
                    IS DISTINCT FROM source_row.clinical_revision::text
               OR NEW.policy_snapshot ->> 'actor_staff_id'
                    IS DISTINCT FROM booking_actor::text
               OR NEW.policy_snapshot -> 'authorized' IS DISTINCT FROM 'true'::jsonb
               OR COALESCE((NEW.policy_snapshot ->> 'policy_revision') ~ '^[1-9][0-9]*$', false) = false THEN
                RAISE EXCEPTION 'Follow-up authorization snapshot is inconsistent'
                    USING ERRCODE = '23514';
            END IF;
            SELECT a.patient_id, a.doctor_staff_id, a.service_type,
                   a.classification, a.service_date
            INTO target_row FROM public.appointments a
            WHERE a.organization_id = NEW.organization_id AND a.id = NEW.appointment_id;
            SELECT s.entitlement_provenance INTO price_provenance
            FROM public.appointment_price_snapshots s
            WHERE s.organization_id = NEW.organization_id AND s.appointment_id = NEW.appointment_id;
            IF NOT FOUND OR target_row.patient_id IS DISTINCT FROM NEW.patient_id
               OR target_row.doctor_staff_id IS DISTINCT FROM NEW.doctor_staff_id
               OR target_row.service_type IS DISTINCT FROM 'FOLLOW_UP'
               OR target_row.classification IS DISTINCT FROM 'ROUTINE'
               OR target_row.service_date < NEW.completion_local_date
               OR target_row.service_date > NEW.approved_deadline
               OR price_provenance IS DISTINCT FROM NEW.policy_snapshot THEN
                RAISE EXCEPTION 'Follow-up target or frozen evidence mismatch'
                    USING ERRCODE = '23514';
            END IF;
            RETURN NEW;
        END $$
    """)
    op.execute("""
        CREATE TRIGGER followup_claim_guard
        BEFORE INSERT OR UPDATE OR DELETE ON public.followup_claims
        FOR EACH ROW EXECUTE FUNCTION public.guard_followup_claim()
    """)
    op.execute("""
        CREATE FUNCTION public.assert_followup_claim_consistency() RETURNS trigger
        LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog AS $$
        DECLARE
            org uuid;
            appt uuid;
            a record;
            c record;
            history_time timestamptz;
            history_status text;
        BEGIN
            org := NEW.organization_id;
            IF TG_TABLE_NAME = 'appointments' THEN
                appt := NEW.id;
            ELSE
                appt := NEW.appointment_id;
            END IF;
            SELECT status, service_type, patient_id, doctor_staff_id
            INTO a FROM public.appointments
            WHERE organization_id = org AND id = appt;
            SELECT id, state, revision, terminal_history_revision, terminal_at,
                   patient_id, doctor_staff_id INTO c
            FROM public.followup_claims WHERE organization_id = org AND appointment_id = appt;
            IF a.service_type = 'FOLLOW_UP' THEN
                IF c.id IS NULL OR c.patient_id <> a.patient_id
                   OR c.doctor_staff_id <> a.doctor_staff_id
                   OR (a.status IN ('Booked','Waiting','In Examination') AND c.state <> 'ACTIVE')
                   OR (a.status IN ('Cancelled','No-show') AND c.state <> 'RELEASED')
                   OR (a.status = 'Completed' AND c.state <> 'CONSUMED') THEN
                    RAISE EXCEPTION 'Follow-up outcome must match its claim'
                        USING ERRCODE = '23514';
                END IF;
                IF c.state <> 'ACTIVE' THEN
                    SELECT h.new_status, h.occurred_at INTO history_status, history_time
                    FROM public.appointment_history h
                    WHERE h.organization_id = org AND h.appointment_id = appt
                      AND h.appointment_revision = c.terminal_history_revision;
                    IF history_status IS DISTINCT FROM a.status OR
                       history_time IS DISTINCT FROM c.terminal_at THEN
                        RAISE EXCEPTION 'Claim terminal evidence does not match outcome'
                            USING ERRCODE = '23514';
                    END IF;
                END IF;
            ELSIF c.id IS NOT NULL THEN
                RAISE EXCEPTION 'Non-follow-up appointment has a claim'
                    USING ERRCODE = '23514';
            END IF;
            RETURN NULL;
        END $$
    """)
    for relation in ("appointments", "followup_claims"):
        op.execute(f"""
            CREATE CONSTRAINT TRIGGER followup_outcome_consistency
            AFTER INSERT OR UPDATE ON public.{relation}
            DEFERRABLE INITIALLY DEFERRED
            FOR EACH ROW EXECUTE FUNCTION public.assert_followup_claim_consistency()
        """)
    op.execute("""
        CREATE FUNCTION public.guard_followup_extension() RETURNS trigger
        LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog AS $$
        DECLARE
            claim_row record;
            source_row record;
            last_deadline date;
        BEGIN
            IF TG_OP <> 'INSERT' THEN
                RAISE EXCEPTION 'Follow-up extensions are immutable' USING ERRCODE = '23514';
            END IF;
            SELECT v.clinical_revision, v.doctor_staff_id,
                   r.id AS evidence_id, r.revision AS evidence_revision,
                   r.change_kind, r.actor_staff_id, r.new_values,
                   r.reason AS evidence_reason, r.occurred_at AS evidence_at
            INTO source_row FROM public.visits v
            JOIN public.clinical_revisions r ON r.organization_id = v.organization_id
                 AND r.visit_id = v.id AND r.id = NEW.clinical_revision_id
            JOIN public.appointments a ON a.organization_id = v.organization_id
                                      AND a.id = v.appointment_id
            WHERE v.organization_id = NEW.organization_id AND v.id = NEW.source_visit_id
            FOR UPDATE OF a, v;
            SELECT c.state, c.revision, c.approved_deadline, c.doctor_staff_id,
                   c.id, c.appointment_id
            INTO claim_row FROM public.followup_claims c
            WHERE c.organization_id = NEW.organization_id AND c.id = NEW.claim_id
              AND c.source_visit_id = NEW.source_visit_id
              AND c.appointment_id = NEW.appointment_id
            FOR UPDATE;
            IF (session_user = 'carepulse_runtime' AND
                NULLIF(current_setting('app.staff_id', true), '')
                    IS DISTINCT FROM NEW.issuing_doctor_staff_id::text)
               OR NOT FOUND OR claim_row.state <> 'ACTIVE'
               OR claim_row.revision <> NEW.expected_claim_revision
               OR source_row.evidence_id IS NULL
               OR source_row.evidence_revision <> NEW.expected_source_revision + 1
               OR source_row.clinical_revision <> source_row.evidence_revision
               OR source_row.change_kind <> 'EXTENSION'
               OR source_row.actor_staff_id <> NEW.issuing_doctor_staff_id
               OR source_row.doctor_staff_id <> NEW.issuing_doctor_staff_id
               OR source_row.new_values -> 'followup_decision' ->> 'extension_claim_id'
                    IS DISTINCT FROM NEW.claim_id::text
               OR source_row.new_values -> 'followup_decision' ->> 'extension_deadline'
                    IS DISTINCT FROM NEW.extended_deadline::text
               OR source_row.evidence_reason IS DISTINCT FROM NEW.reason
               OR source_row.evidence_at IS DISTINCT FROM NEW.issued_at
               OR claim_row.doctor_staff_id <> NEW.issuing_doctor_staff_id
               OR NOT EXISTS (
                   SELECT 1 FROM public.staff_accounts s
                   WHERE s.organization_id = NEW.organization_id
                     AND s.id = NEW.issuing_doctor_staff_id AND s.enabled AND s.verified
                     AND s.role = 'DOCTOR_OWNER')
               OR NOT EXISTS (
                   SELECT 1 FROM public.clinical_audit a
                   WHERE a.id = NEW.clinical_audit_id
                     AND a.organization_id = NEW.organization_id
                     AND a.doctor_staff_id = NEW.issuing_doctor_staff_id
                     AND a.clinical_revision = source_row.evidence_revision
                     AND a.correlation_id = NEW.clinical_revision_id::text)
            THEN
                RAISE EXCEPTION 'Invalid Doctor follow-up extension evidence'
                    USING ERRCODE = '23514';
            END IF;
            SELECT COALESCE(max(extended_deadline), claim_row.approved_deadline)
            INTO last_deadline FROM public.followup_extensions
            WHERE organization_id = NEW.organization_id AND claim_id = NEW.claim_id;
            IF NEW.prior_deadline <> GREATEST(claim_row.approved_deadline, last_deadline)
               OR NEW.extended_deadline < NEW.prior_deadline THEN
                RAISE EXCEPTION 'Follow-up extension cannot shorten entitlement'
                    USING ERRCODE = '23514';
            END IF;
            RETURN NEW;
        END $$
    """)
    op.execute("""
        CREATE TRIGGER followup_extension_guard
        BEFORE INSERT OR UPDATE OR DELETE ON public.followup_extensions
        FOR EACH ROW EXECUTE FUNCTION public.guard_followup_extension()
    """)
    for function in (
        "guard_followup_claim()",
        "assert_followup_claim_consistency()",
        "guard_followup_extension()",
    ):
        op.execute(f"REVOKE ALL ON FUNCTION public.{function} FROM PUBLIC")
    for relation in ("followup_claims", "followup_extensions"):
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
        op.execute(f"GRANT SELECT, INSERT ON public.{relation} TO carepulse_runtime")
    op.execute("""
        GRANT UPDATE (state, terminal_history_revision, terminal_at, revision)
        ON public.followup_claims TO carepulse_runtime
    """)


def downgrade() -> None:
    raise RuntimeError("D-08 follow-up migration is forward-only")

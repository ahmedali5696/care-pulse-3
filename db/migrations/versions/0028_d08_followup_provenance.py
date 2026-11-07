"""D-08 Option A, synthetic-only: operational evidence, never clinical authorship.

G3 / Issue #26 remains BLOCKED: caller-set runtime GUCs are not independent
database authentication. These writers require signed/live API authorization.
"""

from alembic import op

revision = "0028_d08_followup_provenance"
down_revision = "0027_d11_day_review"
branch_labels = None
depends_on = None


def upgrade() -> None:
    _legacy_boundary()
    _source_ledger()
    _authorization_ledger()
    _writers()
    _claim_guards()
    _privileges()


def _legacy_boundary() -> None:
    # Lock out in-flight legacy writes before freezing their semantic ancestry.
    op.execute("""
        LOCK TABLE public.visits, public.clinical_revisions,
                   public.followup_claims, public.followup_extensions,
                   public.followup_source_state IN SHARE ROW EXCLUSIVE MODE
    """)
    op.execute("""
        CREATE FUNCTION public.d08_legacy_source(org uuid, visit uuid)
        RETURNS jsonb LANGUAGE plpgsql SET search_path = pg_catalog AS $$
        DECLARE
            v public.visits%ROWTYPE;
            r public.clinical_revisions%ROWTYPE;
            prior jsonb := jsonb_build_object(
                'examination', NULL, 'findings', NULL, 'diagnosis', NULL,
                'medical_notes', NULL, 'treatment', NULL, 'tests', NULL,
                'followup_decision', NULL);
            sequence bigint := 0;
            decision jsonb;
            next_decision jsonb;
            author uuid;
        BEGIN
            SELECT * INTO STRICT v FROM public.visits
            WHERE organization_id = org AND id = visit;
            FOR r IN SELECT * FROM public.clinical_revisions
                     WHERE organization_id = org AND visit_id = visit
                     ORDER BY revision LOOP
                sequence := sequence + 1;
                next_decision := NULLIF(r.new_values->'followup_decision', 'null'::jsonb)
                                 - 'extension_claim_id' - 'extension_deadline'
                                 - 'extension_evidence_id';
                IF r.revision <> sequence OR r.old_values IS DISTINCT FROM prior
                   OR r.actor_staff_id <> v.doctor_staff_id
                   OR r.actor_role <> 'DOCTOR_OWNER'
                   OR NOT EXISTS (
                       SELECT 1 FROM public.clinical_audit a
                       WHERE a.organization_id = org
                         AND a.doctor_staff_id = r.actor_staff_id
                         AND a.clinical_revision = r.revision
                         AND a.correlation_id = r.id::text)
                   OR (r.change_kind = 'EXTENSION'
                       AND next_decision IS DISTINCT FROM decision) THEN
                    RETURN jsonb_build_object('state', 'ANCESTRY_UNKNOWN');
                END IF;
                -- CORRECTION is a decision when its semantic values change.
                IF next_decision IS DISTINCT FROM decision THEN
                    author := r.actor_staff_id;
                    decision := next_decision;
                END IF;
                prior := r.new_values;
            END LOOP;
            IF sequence <> v.clinical_revision OR prior IS DISTINCT FROM
               jsonb_build_object(
                   'examination', v.examination, 'findings', v.findings,
                   'diagnosis', v.diagnosis, 'medical_notes', v.medical_notes,
                   'treatment', v.treatment, 'tests', v.tests,
                   'followup_decision', v.followup_decision) THEN
                RETURN jsonb_build_object('state', 'ANCESTRY_UNKNOWN');
            END IF;
            RETURN jsonb_build_object('state', 'VERIFIED', 'decision', decision,
                                      'actor_staff_id', author,
                                      'actor_role', CASE WHEN author IS NOT NULL
                                                        THEN 'DOCTOR_OWNER' END);
        END $$
    """)
    op.execute("""
        ALTER TABLE public.followup_source_state
            DROP CONSTRAINT followup_source_state_source_revision_check,
            DROP CONSTRAINT followup_source_state_provenance_state_check,
            ADD CHECK (source_revision >= 0),
            ADD CHECK (provenance_state IN ('VERIFIED', 'ANCESTRY_UNKNOWN')),
            ADD COLUMN appointment_id uuid,
            ADD COLUMN patient_id uuid,
            ADD COLUMN doctor_staff_id uuid,
            ADD COLUMN baseline_clinical_revision bigint,
            ADD COLUMN baseline_decision jsonb,
            ADD COLUMN baseline_actor_staff_id uuid,
            ADD COLUMN baseline_actor_role text,
            ADD COLUMN decision jsonb,
            ADD COLUMN decision_actor_staff_id uuid,
            ADD COLUMN decision_actor_role text
    """)
    # Freeze ancestry only; VERIFIED never substitutes for positive examination evidence.
    op.execute("""
        INSERT INTO public.followup_source_state(organization_id, visit_id, provenance_state)
        SELECT organization_id, id, 'ANCESTRY_UNKNOWN' FROM public.visits
        ON CONFLICT (organization_id, visit_id) DO NOTHING
    """)
    op.execute("""
        UPDATE public.followup_source_state s SET
            appointment_id = v.appointment_id, patient_id = v.patient_id,
            doctor_staff_id = v.doctor_staff_id,
            baseline_clinical_revision = v.clinical_revision,
            provenance_state = b.proof->>'state',
            baseline_decision = NULLIF(b.proof->'decision', 'null'::jsonb),
            decision = NULLIF(b.proof->'decision', 'null'::jsonb),
            baseline_actor_staff_id = (b.proof->>'actor_staff_id')::uuid,
            decision_actor_staff_id = (b.proof->>'actor_staff_id')::uuid,
            baseline_actor_role = b.proof->>'actor_role',
            decision_actor_role = b.proof->>'actor_role'
        FROM public.visits v
        CROSS JOIN LATERAL (
            SELECT public.d08_legacy_source(v.organization_id, v.id) AS proof
        ) b
        WHERE (s.organization_id, s.visit_id) = (v.organization_id, v.id)
    """)
    op.execute("""
        ALTER TABLE public.followup_source_state
            ALTER COLUMN provenance_state DROP DEFAULT,
            ALTER COLUMN appointment_id SET NOT NULL,
            ALTER COLUMN patient_id SET NOT NULL,
            ALTER COLUMN doctor_staff_id SET NOT NULL,
            ALTER COLUMN baseline_clinical_revision SET NOT NULL,
            ADD FOREIGN KEY (organization_id, appointment_id)
                REFERENCES public.appointments(organization_id, id),
            ADD FOREIGN KEY (organization_id, patient_id)
                REFERENCES public.patients(organization_id, id),
            ADD FOREIGN KEY (organization_id, doctor_staff_id)
                REFERENCES public.staff_accounts(organization_id, id),
            ADD FOREIGN KEY (organization_id, baseline_actor_staff_id)
                REFERENCES public.staff_accounts(organization_id, id),
            ADD FOREIGN KEY (organization_id, decision_actor_staff_id)
                REFERENCES public.staff_accounts(organization_id, id)
    """)
    op.execute("""
        CREATE FUNCTION public.fence_legacy_followup_decision() RETURNS trigger
        LANGUAGE plpgsql SET search_path = pg_catalog AS $$
        BEGIN
            IF NEW.followup_decision IS DISTINCT FROM OLD.followup_decision THEN
                RAISE EXCEPTION 'Use operational source decision writer'
                    USING ERRCODE = '42501';
            END IF;
            RETURN NEW;
        END $$
    """)
    op.execute("""
        CREATE TRIGGER visits_followup_fence BEFORE UPDATE ON public.visits
        FOR EACH ROW EXECUTE FUNCTION public.fence_legacy_followup_decision()
    """)
    op.execute("""
        REVOKE UPDATE (followup_decision) ON public.visits FROM carepulse_runtime;
        REVOKE INSERT ON public.followup_extensions FROM carepulse_runtime
    """)
    op.execute("""
        CREATE OR REPLACE FUNCTION public.guard_followup_extension() RETURNS trigger
        LANGUAGE plpgsql SET search_path = pg_catalog AS $$
        BEGIN
            RAISE EXCEPTION 'Legacy extension ledger is frozen'
                USING ERRCODE = '42501';
        END $$
    """)


def _source_ledger() -> None:
    op.execute("""
        CREATE TABLE public.followup_source_decision_events (
            id uuid PRIMARY KEY,
            organization_id uuid NOT NULL,
            source_visit_id uuid NOT NULL,
            source_revision bigint NOT NULL CHECK (source_revision > 0),
            clinical_revision bigint NOT NULL CHECK (clinical_revision >= 0),
            policy_version bigint NOT NULL,
            policy_kind text NOT NULL DEFAULT 'FOLLOWUP' CHECK (policy_kind = 'FOLLOWUP'),
            before_decision jsonb,
            before_actor_staff_id uuid,
            before_actor_role text,
            decision jsonb NOT NULL,
            actor_staff_id uuid NOT NULL,
            actor_role text NOT NULL CHECK (actor_role IN ('DOCTOR_OWNER','RECEPTIONIST')),
            reason text,
            occurred_at timestamptz NOT NULL,
            sensitive_audit_id uuid NOT NULL,
            UNIQUE (organization_id, id),
            UNIQUE (organization_id, source_visit_id, source_revision),
            UNIQUE (organization_id, sensitive_audit_id),
            FOREIGN KEY (organization_id, source_visit_id)
                REFERENCES public.followup_source_state(organization_id, visit_id),
            FOREIGN KEY (organization_id, actor_staff_id)
                REFERENCES public.staff_accounts(organization_id, id),
            FOREIGN KEY (organization_id, before_actor_staff_id)
                REFERENCES public.staff_accounts(organization_id, id),
            FOREIGN KEY (organization_id, sensitive_audit_id)
                REFERENCES public.sensitive_audit(organization_id, id),
            FOREIGN KEY (organization_id, policy_kind, policy_version)
                REFERENCES public.clinic_policy_versions(organization_id, kind, version),
            CHECK (isfinite(occurred_at)
                   AND occurred_at = date_trunc('second', occurred_at))
        )
    """)
    op.execute("""
        CREATE FUNCTION public.guard_d08_source_state() RETURNS trigger
        LANGUAGE plpgsql SET search_path = pg_catalog AS $$
        DECLARE v public.visits%ROWTYPE;
        BEGIN
            IF TG_OP = 'DELETE' OR current_user <> 'carepulse_migration' THEN
                RAISE EXCEPTION 'Protected source state' USING ERRCODE = '42501';
            END IF;
            IF TG_OP = 'INSERT' THEN
                SELECT * INTO STRICT v FROM public.visits
                WHERE organization_id = NEW.organization_id AND id = NEW.visit_id;
                IF NEW.source_revision <> 0 OR
                   (NEW.appointment_id, NEW.patient_id, NEW.doctor_staff_id,
                    NEW.baseline_clinical_revision) IS DISTINCT FROM
                   (v.appointment_id, v.patient_id, v.doctor_staff_id,
                    v.clinical_revision) OR
                   (NEW.decision, NEW.decision_actor_staff_id, NEW.decision_actor_role)
                   IS DISTINCT FROM (NEW.baseline_decision,
                       NEW.baseline_actor_staff_id, NEW.baseline_actor_role) THEN
                    RAISE EXCEPTION 'Invalid revision-zero source' USING ERRCODE = '23514';
                END IF;
            ELSE
                IF NEW.source_revision <> OLD.source_revision + 1 OR
                   (to_jsonb(NEW) - 'source_revision' - 'decision' -
                     'decision_actor_staff_id' - 'decision_actor_role') IS DISTINCT FROM
                   (to_jsonb(OLD) - 'source_revision' - 'decision' -
                     'decision_actor_staff_id' - 'decision_actor_role') THEN
                    RAISE EXCEPTION 'Source baseline is immutable' USING ERRCODE = '23514';
                END IF;
                IF NOT EXISTS (
                    SELECT 1 FROM public.followup_source_decision_events e
                    WHERE e.organization_id = NEW.organization_id
                      AND e.source_visit_id = NEW.visit_id
                      AND e.source_revision = NEW.source_revision
                      AND (e.decision, e.actor_staff_id, e.actor_role) IS NOT DISTINCT FROM
                          (NEW.decision, NEW.decision_actor_staff_id, NEW.decision_actor_role)) THEN
                    RAISE EXCEPTION 'Head advancement requires matching event' USING ERRCODE = '23514';
                END IF;
            END IF;
            RETURN NEW;
        END $$
    """)
    op.execute("""
        CREATE TRIGGER followup_source_state_guard
        BEFORE INSERT OR UPDATE OR DELETE ON public.followup_source_state
        FOR EACH ROW EXECUTE FUNCTION public.guard_d08_source_state()
    """)
    op.execute("""
        CREATE FUNCTION public.check_d08_source_chain() RETURNS trigger
        LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog AS $$
        DECLARE
            s public.followup_source_state%ROWTYPE;
            e public.followup_source_decision_events%ROWTYPE;
            visit uuid;
            previous_decision jsonb;
            previous_actor uuid;
            previous_role text;
            sequence bigint := 0;
        BEGIN
            visit := COALESCE(to_jsonb(NEW)->>'source_visit_id',
                              to_jsonb(NEW)->>'visit_id')::uuid;
            SELECT * INTO STRICT s FROM public.followup_source_state
            WHERE organization_id = NEW.organization_id AND visit_id = visit;
            previous_decision := s.baseline_decision;
            previous_actor := s.baseline_actor_staff_id;
            previous_role := s.baseline_actor_role;
            FOR e IN SELECT * FROM public.followup_source_decision_events
                     WHERE organization_id = s.organization_id
                       AND source_visit_id = visit ORDER BY source_revision LOOP
                sequence := sequence + 1;
                IF e.source_revision <> sequence OR
                   (e.before_decision, e.before_actor_staff_id, e.before_actor_role)
                   IS DISTINCT FROM (previous_decision, previous_actor, previous_role)
                   OR NOT EXISTS (
                       SELECT 1 FROM public.sensitive_audit a
                       WHERE a.organization_id = e.organization_id
                         AND a.id = e.sensitive_audit_id
                         AND a.action = 'FOLLOWUP_SOURCE_DECISION'
                         AND a.actor_staff_id = e.actor_staff_id
                         AND a.actor_role = e.actor_role
                         AND a.occurred_at = e.occurred_at
                         AND a.correlation_id = e.id::text
                         AND a.reason IS NOT DISTINCT FROM e.reason
                         AND a.before_state = jsonb_build_object(
                             'source_visit_id', visit, 'source_revision', sequence - 1,
                             'decision', previous_decision, 'actor_staff_id', previous_actor,
                             'actor_role', previous_role)
                         AND a.after_state = jsonb_build_object(
                             'source_visit_id', visit, 'source_revision', sequence,
                             'decision', e.decision, 'actor_staff_id', e.actor_staff_id,
                             'actor_role', e.actor_role)) THEN
                    RAISE EXCEPTION 'Source event chain inconsistent' USING ERRCODE = '23514';
                END IF;
                previous_decision := e.decision;
                previous_actor := e.actor_staff_id;
                previous_role := e.actor_role;
            END LOOP;
            IF s.source_revision <> sequence OR
               (s.decision, s.decision_actor_staff_id, s.decision_actor_role)
               IS DISTINCT FROM (previous_decision, previous_actor, previous_role) THEN
                RAISE EXCEPTION 'Source head/event mismatch' USING ERRCODE = '23514';
            END IF;
            RETURN NULL;
        END $$
    """)
    for relation in ("followup_source_state", "followup_source_decision_events"):
        op.execute(f"""
            CREATE CONSTRAINT TRIGGER d08_source_chain
            AFTER INSERT OR UPDATE ON public.{relation}
            DEFERRABLE INITIALLY DEFERRED FOR EACH ROW
            EXECUTE FUNCTION public.check_d08_source_chain()
        """)


def _authorization_ledger() -> None:
    op.execute("""
        CREATE TABLE public.followup_authorization_events (
            id uuid PRIMARY KEY,
            organization_id uuid NOT NULL,
            claim_id uuid NOT NULL,
            source_visit_id uuid NOT NULL,
            appointment_id uuid NOT NULL,
            authorization_revision bigint NOT NULL CHECK (authorization_revision > 0),
            kind text NOT NULL CHECK (kind IN ('NEW_DATE','EXTENSION')),
            prior_deadline date NOT NULL CHECK (isfinite(prior_deadline)),
            new_deadline date NOT NULL CHECK (isfinite(new_deadline)),
            selected_date date,
            booking_mode text,
            starts_at timestamptz,
            ends_at timestamptz,
            source_revision bigint NOT NULL CHECK (source_revision >= 0),
            clinical_revision bigint NOT NULL CHECK (clinical_revision >= 0),
            policy_version bigint NOT NULL,
            policy_kind text NOT NULL DEFAULT 'FOLLOWUP' CHECK (policy_kind = 'FOLLOWUP'),
            claim_revision bigint NOT NULL CHECK (claim_revision > 0),
            appointment_revision bigint NOT NULL CHECK (appointment_revision >= 0),
            actor_staff_id uuid NOT NULL,
            actor_role text NOT NULL CHECK (actor_role IN ('DOCTOR_OWNER','RECEPTIONIST')),
            reason text,
            occurred_at timestamptz NOT NULL CHECK (isfinite(occurred_at)
                AND occurred_at = date_trunc('second', occurred_at)),
            sensitive_audit_id uuid NOT NULL,
            UNIQUE (organization_id, id),
            UNIQUE (organization_id, claim_id, authorization_revision),
            UNIQUE (organization_id, sensitive_audit_id),
            FOREIGN KEY (organization_id, claim_id, source_visit_id, appointment_id)
                REFERENCES public.followup_claims
                    (organization_id, id, source_visit_id, appointment_id),
            FOREIGN KEY (organization_id, source_visit_id)
                REFERENCES public.followup_source_state(organization_id, visit_id),
            FOREIGN KEY (organization_id, actor_staff_id)
                REFERENCES public.staff_accounts(organization_id, id),
            FOREIGN KEY (organization_id, sensitive_audit_id)
                REFERENCES public.sensitive_audit(organization_id, id),
            FOREIGN KEY (organization_id, policy_kind, policy_version)
                REFERENCES public.clinic_policy_versions(organization_id, kind, version),
            CHECK ((kind = 'EXTENSION' AND new_deadline >= prior_deadline
                    AND selected_date IS NULL AND booking_mode IS NULL
                    AND starts_at IS NULL AND ends_at IS NULL
                    AND reason IS NOT NULL AND btrim(reason) <> '') OR
                   (kind = 'NEW_DATE' AND authorization_revision = 1
                    AND appointment_revision = 0 AND selected_date IS NOT NULL
                    AND selected_date = new_deadline
                    AND new_deadline > prior_deadline AND booking_mode IS NOT NULL
                    AND ((booking_mode = 'OPEN' AND starts_at IS NULL AND ends_at IS NULL)
                         OR (booking_mode = 'EXACT_TIME' AND starts_at IS NOT NULL
                             AND ends_at IS NOT NULL AND isfinite(starts_at) AND isfinite(ends_at)
                             AND starts_at < ends_at
                             AND (starts_at AT TIME ZONE 'Africa/Cairo')::date = selected_date))
                    AND (actor_role = 'RECEPTIONIST' OR
                         (reason IS NOT NULL AND btrim(reason) <> ''))))
        )
    """)
    op.execute("""
        CREATE UNIQUE INDEX followup_new_date_once
        ON public.followup_authorization_events(organization_id, claim_id)
        WHERE kind = 'NEW_DATE'
    """)
    op.execute("""
        CREATE FUNCTION public.d08_legacy_deadline(org uuid, claim uuid)
        RETURNS date LANGUAGE plpgsql SET search_path = pg_catalog AS $$
        DECLARE
            c public.followup_claims%ROWTYPE;
            e public.followup_extensions%ROWTYPE;
            deadline date;
        BEGIN
            SELECT * INTO STRICT c FROM public.followup_claims
            WHERE organization_id = org AND id = claim;
            deadline := c.approved_deadline;
            FOR e IN SELECT * FROM public.followup_extensions
                     WHERE organization_id = org AND claim_id = claim
                     ORDER BY expected_source_revision LOOP
                IF e.prior_deadline <> deadline OR e.extended_deadline < deadline
                   OR NOT EXISTS (
                       SELECT 1 FROM public.clinical_revisions r
                       JOIN public.clinical_audit a ON a.id = e.clinical_audit_id
                         AND a.organization_id = r.organization_id
                       WHERE r.organization_id = org AND r.id = e.clinical_revision_id
                         AND r.visit_id = c.source_visit_id AND r.change_kind = 'EXTENSION'
                         AND r.actor_staff_id = e.issuing_doctor_staff_id
                         AND r.actor_staff_id = c.doctor_staff_id
                         AND r.revision = e.expected_source_revision + 1
                         AND r.reason = e.reason AND r.occurred_at = e.issued_at
                         AND r.new_values->'followup_decision'->>'extension_claim_id'
                             = claim::text
                         AND r.new_values->'followup_decision'->>'extension_deadline'
                             = e.extended_deadline::text
                         AND a.doctor_staff_id = r.actor_staff_id
                         AND a.clinical_revision = r.revision
                         AND a.correlation_id = r.id::text) THEN
                    RAISE EXCEPTION 'Unverifiable legacy extension' USING ERRCODE = '23514';
                END IF;
                deadline := e.extended_deadline;
            END LOOP;
            RETURN deadline;
        END $$
    """)
    op.execute("""
        CREATE FUNCTION public.check_d08_authorization_chain() RETURNS trigger
        LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog AS $$
        DECLARE
            c public.followup_claims%ROWTYPE;
            e public.followup_authorization_events%ROWTYPE;
            claim uuid;
            sequence bigint := 0;
            deadline date;
            declared text;
        BEGIN
            claim := CASE WHEN TG_TABLE_NAME = 'followup_claims'
                          THEN NEW.id ELSE (to_jsonb(NEW)->>'claim_id')::uuid END;
            SELECT * INTO STRICT c FROM public.followup_claims
            WHERE organization_id = NEW.organization_id AND id = claim;
            declared := c.policy_snapshot->>'new_date_event_id';
            -- Legacy terminal transitions never requalify historical evidence.
            IF TG_TABLE_NAME = 'followup_claims' AND TG_OP = 'UPDATE' THEN
                RETURN NULL;
            END IF;
            IF c.policy_snapshot->>'schema_version' = '2' THEN
                deadline := c.approved_deadline;
            ELSE
                deadline := public.d08_legacy_deadline(c.organization_id, c.id);
            END IF;
            FOR e IN SELECT * FROM public.followup_authorization_events
                     WHERE organization_id = c.organization_id AND claim_id = claim
                     ORDER BY authorization_revision LOOP
                sequence := sequence + 1;
                IF e.authorization_revision <> sequence OR e.prior_deadline <> deadline
                   OR NOT EXISTS (
                       SELECT 1 FROM public.sensitive_audit audit
                       WHERE audit.organization_id = e.organization_id
                         AND audit.id = e.sensitive_audit_id
                         AND audit.action = 'FOLLOWUP_' || e.kind
                         AND audit.actor_staff_id = e.actor_staff_id
                         AND audit.actor_role = e.actor_role
                         AND audit.reason IS NOT DISTINCT FROM e.reason
                         AND audit.occurred_at = e.occurred_at
                         AND audit.correlation_id = e.id::text
                         AND audit.before_state = jsonb_build_object(
                             'claim_id', claim, 'authorization_revision', sequence - 1,
                             'deadline', e.prior_deadline)
                         AND audit.after_state = to_jsonb(e) - 'sensitive_audit_id') THEN
                    RAISE EXCEPTION 'Authorization chain inconsistent' USING ERRCODE = '23514';
                END IF;
                IF e.kind = 'NEW_DATE' THEN
                    IF declared IS DISTINCT FROM e.id::text OR
                       c.policy_snapshot->>'selected_date' IS DISTINCT FROM e.selected_date::text
                       OR c.policy_snapshot->>'actor_staff_id'
                          IS DISTINCT FROM e.actor_staff_id::text
                       OR c.policy_snapshot->>'actor_role' IS DISTINCT FROM e.actor_role
                       OR c.policy_snapshot->>'source_decision_revision'
                          IS DISTINCT FROM e.source_revision::text
                       OR c.policy_snapshot->>'source_clinical_revision'
                          IS DISTINCT FROM e.clinical_revision::text
                       OR c.policy_snapshot->>'policy_revision'
                          IS DISTINCT FROM e.policy_version::text
                       OR NOT EXISTS (
                           SELECT 1 FROM public.appointment_history h
                           JOIN public.appointment_price_snapshots p
                             ON (p.organization_id, p.appointment_id) =
                                (h.organization_id, h.appointment_id)
                           WHERE h.organization_id = c.organization_id
                             AND h.appointment_id = c.appointment_id
                             AND h.action = 'CREATED' AND h.appointment_revision = 0
                             AND h.actor_staff_id = e.actor_staff_id
                             AND h.actor_role = e.actor_role
                             AND h.new_service_date = e.selected_date
                             AND p.entitlement_provenance = c.policy_snapshot)
                       OR c.policy_snapshot->>'booking_mode' IS DISTINCT FROM e.booking_mode
                       OR c.policy_snapshot->'starts_at' IS DISTINCT FROM COALESCE(to_jsonb(e.starts_at), 'null'::jsonb)
                       OR c.policy_snapshot->'ends_at' IS DISTINCT FROM COALESCE(to_jsonb(e.ends_at), 'null'::jsonb) THEN
                        RAISE EXCEPTION 'NEW_DATE creation evidence mismatch'
                            USING ERRCODE = '23514';
                    END IF;
                END IF;
                deadline := e.new_deadline;
            END LOOP;
            IF declared IS NOT NULL AND NOT EXISTS (
                SELECT 1 FROM public.followup_authorization_events event
                WHERE event.organization_id = c.organization_id AND event.claim_id = claim
                  AND event.id::text = declared AND event.kind = 'NEW_DATE') THEN
                RAISE EXCEPTION 'Declared NEW_DATE event missing' USING ERRCODE = '23514';
            END IF;
            RETURN NULL;
        END $$
    """)
    for relation in ("followup_claims", "followup_authorization_events"):
        op.execute(f"""
            CREATE CONSTRAINT TRIGGER d08_authorization_chain
            AFTER INSERT OR UPDATE ON public.{relation}
            DEFERRABLE INITIALLY DEFERRED FOR EACH ROW
            EXECUTE FUNCTION public.check_d08_authorization_chain()
        """)


def _writers() -> None:
    _source_context()
    _decision_writer()
    _authorization_writer()
    _creation_writer()


def _source_context() -> None:
    op.execute("""
        CREATE FUNCTION public.d08_context(org uuid, visit uuid, expected jsonb)
        RETURNS jsonb LANGUAGE plpgsql SET search_path = pg_catalog AS $$
        DECLARE
            actor uuid;
            role text;
            v public.visits%ROWTYPE;
            a public.appointments%ROWTYPE;
            s public.followup_source_state%ROWTYPE;
            policy bigint;
            payload jsonb;
            window_days numeric;
            proof jsonb;
            completion date;
            deadline date;
        BEGIN
            IF session_user <> 'carepulse_runtime'
               OR current_setting('transaction_isolation') <> 'read committed'
               OR current_setting('app.organization_id', true) IS DISTINCT FROM org::text
               OR NULLIF(current_setting('app.staff_id', true), '') IS NULL
               OR jsonb_typeof(expected) IS DISTINCT FROM 'object'
               OR (expected - 'source_revision' - 'clinical_revision' - 'policy_version'
                    - 'authorization_revision' - 'claim_revision' - 'appointment_revision') <> '{}'::jsonb THEN
                RAISE EXCEPTION 'Follow-up context denied' USING ERRCODE = '42501';
            END IF;
            actor := current_setting('app.staff_id', true)::uuid;
            PERFORM 1 FROM public.organizations WHERE id = org AND state = 'ACTIVE' FOR SHARE;
            IF NOT FOUND THEN
                RAISE EXCEPTION 'Inactive clinic' USING ERRCODE = '42501';
            END IF;
            SELECT staff.role INTO role FROM public.staff_accounts staff
            JOIN public."user" u ON u.id = staff.auth_user_id
            WHERE staff.organization_id = org AND staff.id = actor
              AND staff.enabled AND staff.verified AND staff.reset_block_operation_id IS NULL
              AND u."emailVerified" AND u.email = staff.verified_email
              AND staff.role IN ('DOCTOR_OWNER','RECEPTIONIST') FOR SHARE OF staff, u;
            IF NOT FOUND THEN
                RAISE EXCEPTION 'Inactive actor' USING ERRCODE = '42501';
            END IF;
            PERFORM 1 FROM public.patients patient
            JOIN public.visits source ON source.organization_id = patient.organization_id
                                    AND source.patient_id = patient.id
            WHERE source.organization_id = org AND source.id = visit FOR UPDATE OF patient;
            IF NOT FOUND THEN
                RAISE EXCEPTION 'Source unavailable' USING ERRCODE = '23514';
            END IF;
            PERFORM 1 FROM public.clinic_policy_heads
            WHERE organization_id = org AND kind = 'FOLLOWUP' FOR SHARE;
            SELECT h.current_version, p.payload INTO policy, payload
            FROM public.clinic_policy_heads h JOIN public.clinic_policy_versions p
              ON (p.organization_id, p.kind, p.version) =
                 (h.organization_id, h.kind, h.current_version)
            WHERE h.organization_id = org AND h.kind = 'FOLLOWUP';
            IF policy IS NULL OR expected->>'policy_version' IS DISTINCT FROM policy::text
               OR jsonb_typeof(payload->'last_day_inclusive') IS DISTINCT FROM 'number' THEN
                RAISE EXCEPTION 'Stale or invalid FOLLOWUP policy' USING ERRCODE = '40001';
            END IF;
            -- Boundary cases: 0 and 365 inclusive; fractions and outside values deny.
            window_days := (payload->>'last_day_inclusive')::numeric;
            IF window_days < 0 OR window_days > 365 OR window_days <> trunc(window_days) THEN
                RAISE EXCEPTION 'Invalid FOLLOWUP window' USING ERRCODE = '23514';
            END IF;
            SELECT appt.* INTO a FROM public.appointments appt
            JOIN public.visits source ON source.organization_id = appt.organization_id
                                    AND source.appointment_id = appt.id
            WHERE source.organization_id = org AND source.id = visit FOR UPDATE OF appt;
            IF NOT FOUND THEN
                RAISE EXCEPTION 'Source unavailable' USING ERRCODE = '23514';
            END IF;
            PERFORM 1 FROM public.visits WHERE organization_id = org AND id = visit FOR UPDATE;
            -- Fresh statements after parent locks, including a wait on an absent ledger.
            SELECT * INTO STRICT v FROM public.visits WHERE organization_id = org AND id = visit;
            IF a.status <> 'Completed' OR a.service_type <> 'NEW_EXAMINATION'
               OR (v.patient_id, v.doctor_staff_id, v.service_date) IS DISTINCT FROM
                  (a.patient_id, a.doctor_staff_id, a.service_date)
               OR expected->>'clinical_revision' IS DISTINCT FROM v.clinical_revision::text THEN
                RAISE EXCEPTION 'Unqualified or stale source' USING ERRCODE = '40001';
            END IF;
            PERFORM public.assert_positive_exam_completion(org, a.id, a.revision);
            SELECT (completed_at AT TIME ZONE 'Africa/Cairo')::date INTO STRICT completion
            FROM public.completion_attestations WHERE organization_id = org AND visit_id = visit;
            SELECT * INTO s FROM public.followup_source_state
            WHERE organization_id = org AND visit_id = visit;
            IF NOT FOUND THEN
                proof := public.d08_legacy_source(org, visit);
                INSERT INTO public.followup_source_state (
                    organization_id, visit_id, appointment_id, patient_id, doctor_staff_id,
                    baseline_clinical_revision, provenance_state, baseline_decision,
                    baseline_actor_staff_id, baseline_actor_role,
                    decision, decision_actor_staff_id, decision_actor_role)
                VALUES (org, visit, a.id, a.patient_id, a.doctor_staff_id,
                    v.clinical_revision, proof->>'state', NULLIF(proof->'decision','null'::jsonb),
                    (proof->>'actor_staff_id')::uuid, proof->>'actor_role',
                    NULLIF(proof->'decision','null'::jsonb),
                    (proof->>'actor_staff_id')::uuid, proof->>'actor_role');
            END IF;
            PERFORM 1 FROM public.followup_source_state
            WHERE organization_id = org AND visit_id = visit FOR UPDATE;
            SELECT * INTO STRICT s FROM public.followup_source_state
            WHERE organization_id = org AND visit_id = visit;
            IF s.provenance_state <> 'VERIFIED' THEN
                RAISE EXCEPTION 'ANCESTRY_UNKNOWN' USING ERRCODE = '23514';
            END IF;
            IF expected->>'source_revision' IS DISTINCT FROM s.source_revision::text THEN
                RAISE EXCEPTION 'Stale source decision revision' USING ERRCODE = '40001';
            END IF;
            deadline := completion + window_days::integer;
            IF s.decision IS NOT NULL THEN
                PERFORM public.d08_validate_decision(s.decision, completion);
                IF s.decision->>'kind' = 'DEADLINE' THEN
                    deadline := (s.decision->>'deadline_date')::date;
                END IF;
            END IF;
            RETURN jsonb_build_object(
                'actor_staff_id', actor, 'actor_role', role,
                'source', to_jsonb(s), 'clinical_revision', v.clinical_revision,
                'policy_version', policy, 'completion_date', completion, 'deadline', deadline);
        END $$
    """)
    op.execute("""
        CREATE FUNCTION public.d08_validate_decision(decision jsonb, completion date)
        RETURNS void LANGUAGE plpgsql SET search_path = pg_catalog AS $$
        DECLARE kind text := decision->>'kind'; deadline date;
        BEGIN
            IF jsonb_typeof(decision) IS DISTINCT FROM 'object'
               OR kind IS NULL OR kind NOT IN ('DEFAULT','OPTED_OUT','DEADLINE')
               OR (decision - 'kind' - 'reason' - 'deadline_date' - 'eligible') <> '{}'::jsonb
               OR (decision ? 'reason' AND jsonb_typeof(decision->'reason') <> 'string')
               OR (kind <> 'DEFAULT' AND NULLIF(btrim(decision->>'reason'), '') IS NULL)
               OR (decision ? 'eligible' AND
                   decision->'eligible' IS DISTINCT FROM to_jsonb(kind <> 'OPTED_OUT'))
               OR (kind <> 'DEADLINE' AND decision ? 'deadline_date') THEN
                RAISE EXCEPTION 'Invalid source decision' USING ERRCODE = '23514';
            END IF;
            IF kind = 'DEADLINE' THEN
                IF COALESCE(decision->>'deadline_date', '') !~ '^[0-9]{4}-[0-9]{2}-[0-9]{2}$' THEN
                    RAISE EXCEPTION 'Concrete Cairo date required' USING ERRCODE = '23514';
                END IF;
                deadline := (decision->>'deadline_date')::date;
                IF deadline IS NULL OR NOT isfinite(deadline) OR deadline < completion THEN
                    RAISE EXCEPTION 'Invalid Cairo deadline' USING ERRCODE = '23514';
                END IF;
            END IF;
        END $$
    """)
    op.execute("""
        CREATE FUNCTION public.d08_check_authorized_date(context jsonb, day date)
        RETURNS void LANGUAGE plpgsql SET search_path = pg_catalog AS $$
        BEGIN
            IF day IS NULL OR NOT isfinite(day)
               OR day < (context->>'completion_date')::date
               OR context->'source'->'decision'->>'kind' = 'OPTED_OUT'
               OR (context->>'actor_role' = 'RECEPTIONIST'
                   AND context->'source'->>'decision_actor_role' = 'DOCTOR_OWNER'
                   AND context->'source'->'decision'->>'kind' = 'DEADLINE'
                   AND day > (context->'source'->'decision'->>'deadline_date')::date) THEN
                RAISE EXCEPTION 'Source restriction forbids date' USING ERRCODE = '23514';
            END IF;
        END $$
    """)


def _decision_writer() -> None:
    op.execute("""
        CREATE FUNCTION public.record_followup_source_decision(
            org uuid, visit uuid, expected jsonb, decision jsonb)
        RETURNS bigint LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog AS $$
        DECLARE
            context jsonb;
            s public.followup_source_state%ROWTYPE;
            event uuid := gen_random_uuid(); audit uuid := gen_random_uuid();
            moment timestamptz;
        BEGIN
            context := public.d08_context(org, visit, expected);
            moment := date_trunc('second', clock_timestamp());
            SELECT * INTO STRICT s FROM public.followup_source_state
            WHERE organization_id = org AND visit_id = visit;
            PERFORM public.d08_validate_decision(decision, (context->>'completion_date')::date);
            IF context->>'actor_role' <> 'DOCTOR_OWNER'
               AND s.decision_actor_role = 'DOCTOR_OWNER'
               AND s.decision->>'kind' IN ('OPTED_OUT','DEADLINE') THEN
                RAISE EXCEPTION 'Only Doctor may replace Doctor restriction'
                    USING ERRCODE = '42501';
            END IF;
            INSERT INTO public.sensitive_audit (
                id, organization_id, actor_staff_id, actor_role, action, reason,
                correlation_id, occurred_at, before_state, after_state)
            VALUES (audit, org, (context->>'actor_staff_id')::uuid, context->>'actor_role',
                'FOLLOWUP_SOURCE_DECISION', decision->>'reason', event::text, moment,
                jsonb_build_object('source_visit_id', visit, 'source_revision', s.source_revision,
                    'decision', s.decision, 'actor_staff_id', s.decision_actor_staff_id,
                    'actor_role', s.decision_actor_role),
                jsonb_build_object('source_visit_id', visit, 'source_revision', s.source_revision + 1,
                    'decision', decision, 'actor_staff_id', context->>'actor_staff_id',
                    'actor_role', context->>'actor_role'));
            INSERT INTO public.followup_source_decision_events (
                id, organization_id, source_visit_id, source_revision, clinical_revision,
                policy_version, before_decision, before_actor_staff_id, before_actor_role,
                decision, actor_staff_id, actor_role, reason, occurred_at, sensitive_audit_id)
            VALUES (event, org, visit, s.source_revision + 1,
                (context->>'clinical_revision')::bigint, (context->>'policy_version')::bigint,
                s.decision, s.decision_actor_staff_id, s.decision_actor_role, decision,
                (context->>'actor_staff_id')::uuid, context->>'actor_role',
                decision->>'reason', moment, audit);
            UPDATE public.followup_source_state SET source_revision = source_revision + 1,
                decision = record_followup_source_decision.decision,
                decision_actor_staff_id = (context->>'actor_staff_id')::uuid,
                decision_actor_role = context->>'actor_role'
            WHERE organization_id = org AND visit_id = visit;
            RETURN s.source_revision + 1;
        END $$
    """)


def _authorization_writer() -> None:
    op.execute("""
        CREATE FUNCTION public.d08_append_authorization(event public.followup_authorization_events)
        RETURNS void LANGUAGE plpgsql SET search_path = pg_catalog AS $$
        BEGIN
            INSERT INTO public.sensitive_audit (
                id, organization_id, actor_staff_id, actor_role, action, reason,
                correlation_id, occurred_at, before_state, after_state)
            VALUES (event.sensitive_audit_id, event.organization_id, event.actor_staff_id,
                event.actor_role, 'FOLLOWUP_' || event.kind, event.reason,
                event.id::text, event.occurred_at,
                jsonb_build_object('claim_id', event.claim_id,
                    'authorization_revision', event.authorization_revision - 1,
                    'deadline', event.prior_deadline),
                to_jsonb(event) - 'sensitive_audit_id');
            INSERT INTO public.followup_authorization_events SELECT event.*;
        END $$
    """)
    op.execute("""
        CREATE FUNCTION public.extend_followup_claim(
            org uuid, claim uuid, expected jsonb, extension jsonb)
        RETURNS bigint LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog AS $$
        DECLARE
            c public.followup_claims%ROWTYPE;
            a public.appointments%ROWTYPE;
            context jsonb;
            e public.followup_authorization_events%ROWTYPE;
            prior public.followup_authorization_events%ROWTYPE;
        BEGIN
            SELECT * INTO c FROM public.followup_claims WHERE organization_id = org AND id = claim;
            IF NOT FOUND THEN
                RAISE EXCEPTION 'Claim unavailable' USING ERRCODE = '23514';
            END IF;
            context := public.d08_context(org, c.source_visit_id, expected);
            IF jsonb_typeof(extension) IS DISTINCT FROM 'object'
               OR (extension - 'deadline_date' - 'reason') <> '{}'::jsonb
               OR jsonb_typeof(extension->'reason') IS DISTINCT FROM 'string'
               OR COALESCE(extension->>'deadline_date', '') !~ '^[0-9]{4}-[0-9]{2}-[0-9]{2}$' THEN
                RAISE EXCEPTION 'Invalid confirmed extension' USING ERRCODE = '23514';
            END IF;
            PERFORM 1 FROM public.appointments WHERE organization_id = org
                AND id = c.appointment_id FOR UPDATE;
            PERFORM 1 FROM public.followup_claims WHERE organization_id = org AND id = claim FOR UPDATE;
            SELECT * INTO STRICT c FROM public.followup_claims WHERE organization_id = org AND id = claim;
            SELECT * INTO STRICT a FROM public.appointments WHERE organization_id = org AND id = c.appointment_id;
            IF c.state <> 'ACTIVE' OR a.status NOT IN ('Booked','Waiting','In Examination')
               OR expected->>'claim_revision' IS DISTINCT FROM c.revision::text
               OR expected->>'appointment_revision' IS DISTINCT FROM a.revision::text THEN
                RAISE EXCEPTION 'Claim no longer at expected active revision' USING ERRCODE = '40001';
            END IF;
            SELECT * INTO prior FROM public.followup_authorization_events
            WHERE organization_id = org AND claim_id = claim ORDER BY authorization_revision DESC LIMIT 1;
            e.authorization_revision := COALESCE(prior.authorization_revision, 0) + 1;
            IF expected->>'authorization_revision' IS DISTINCT FROM (e.authorization_revision - 1)::text THEN
                RAISE EXCEPTION 'Stale authorization revision' USING ERRCODE = '40001';
            END IF;
            e.prior_deadline := COALESCE(prior.new_deadline, public.d08_legacy_deadline(org, claim));
            e.new_deadline := (extension->>'deadline_date')::date;
            e.reason := NULLIF(btrim(extension->>'reason'), '');
            IF e.reason IS NULL OR e.new_deadline IS NULL OR NOT isfinite(e.new_deadline)
               OR e.new_deadline < e.prior_deadline THEN
                RAISE EXCEPTION 'Extension requires reason and nonshortening date' USING ERRCODE = '23514';
            END IF;
            PERFORM public.d08_check_authorized_date(context, e.new_deadline);
            e.id := gen_random_uuid(); e.organization_id := org; e.claim_id := claim;
            e.source_visit_id := c.source_visit_id; e.appointment_id := c.appointment_id;
            e.kind := 'EXTENSION'; e.source_revision := (context->'source'->>'source_revision')::bigint;
            e.clinical_revision := (context->>'clinical_revision')::bigint;
            e.policy_version := (context->>'policy_version')::bigint;
            e.policy_kind := 'FOLLOWUP';
            e.claim_revision := c.revision; e.appointment_revision := a.revision;
            e.actor_staff_id := (context->>'actor_staff_id')::uuid; e.actor_role := context->>'actor_role';
            e.occurred_at := date_trunc('second', clock_timestamp()); e.sensitive_audit_id := gen_random_uuid();
            PERFORM public.d08_append_authorization(e);
            RETURN e.authorization_revision;
        END $$
    """)


def _creation_writer() -> None:
    # Persistence only: B-10 must perform scheduling, signed/live checks and
    # receipt/outbox work in this same outer transaction before exposing a route.
    op.execute("""
        CREATE FUNCTION public.create_followup_claim(
            org uuid, visit uuid, expected jsonb, booking jsonb)
        RETURNS uuid LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog AS $$
        DECLARE
            context jsonb;
            source public.followup_source_state%ROWTYPE;
            target uuid := gen_random_uuid(); claim uuid := gen_random_uuid();
            assignment uuid := gen_random_uuid(); number bigint;
            day date := (booking->>'service_date')::date;
            mode text := booking->>'booking_mode';
            starts timestamptz := (booking->>'starts_at')::timestamptz;
            ends timestamptz := (booking->>'ends_at')::timestamptz;
            snapshot jsonb;
            price jsonb := booking->'price_snapshot';
            e public.followup_authorization_events%ROWTYPE;
        BEGIN
            context := public.d08_context(org, visit, expected);
            IF jsonb_typeof(booking) IS DISTINCT FROM 'object'
               OR (booking - 'service_date' - 'booking_mode' - 'starts_at' - 'ends_at'
                    - 'price_snapshot' - 'exception_reason') <> '{}'::jsonb
               OR (booking ? 'exception_reason'
                   AND jsonb_typeof(booking->'exception_reason') <> 'string') THEN
                RAISE EXCEPTION 'Invalid claim creation input' USING ERRCODE = '23514';
            END IF;
            SELECT * INTO STRICT source FROM public.followup_source_state
            WHERE organization_id = org AND visit_id = visit;
            PERFORM public.d08_check_authorized_date(context, day);
            PERFORM 1 FROM public.patients WHERE organization_id = org
                AND id = source.patient_id AND NOT archived FOR UPDATE;
            IF NOT FOUND THEN
                RAISE EXCEPTION 'Patient unavailable for new booking' USING ERRCODE = '23514';
            END IF;
            snapshot := jsonb_build_object(
                'schema_version', 2, 'source_visit_id', visit,
                'source_decision_revision', source.source_revision,
                'source_clinical_revision', (context->>'clinical_revision')::bigint,
                'doctor_staff_id', source.doctor_staff_id,
                'actor_staff_id', context->>'actor_staff_id', 'actor_role', context->>'actor_role',
                'authorized', true, 'policy_revision', (context->>'policy_version')::bigint,
                'selected_date', day, 'booking_mode', mode,
                'starts_at', starts, 'ends_at', ends);
            IF day > (context->>'deadline')::date THEN
                e.id := gen_random_uuid(); e.kind := 'NEW_DATE';
                e.reason := NULLIF(btrim(booking->>'exception_reason'), '');
                IF context->>'actor_role' = 'DOCTOR_OWNER' AND e.reason IS NULL THEN
                    RAISE EXCEPTION 'Doctor NEW_DATE requires reason' USING ERRCODE = '23514';
                END IF;
                snapshot := snapshot || jsonb_build_object('new_date_event_id', e.id);
            ELSIF booking ? 'exception_reason' THEN
                RAISE EXCEPTION 'NEW_DATE is only an outside-window creation' USING ERRCODE = '23514';
            END IF;
            INSERT INTO public.appointment_day_counters(organization_id, service_date, last_number)
            VALUES (org, day, 1) ON CONFLICT (organization_id, service_date)
            DO UPDATE SET last_number = public.appointment_day_counters.last_number + 1
            RETURNING last_number INTO number;
            INSERT INTO public.appointments (
                id, organization_id, patient_id, doctor_staff_id, service_date, status,
                service_type, classification, lane, booking_mode, starts_at, ends_at,
                current_assignment_id, current_number)
            VALUES (target, org, source.patient_id, source.doctor_staff_id, day, 'Booked',
                'FOLLOW_UP', 'ROUTINE', 'MAIN', mode, starts, ends, assignment, number);
            INSERT INTO public.appointment_number_assignments (
                id, organization_id, appointment_id, service_date, number)
            VALUES (assignment, org, target, day, number);
            INSERT INTO public.appointment_price_snapshots (
                organization_id, appointment_id, service_type, classification,
                base_egp, discount_type, discount_value, discount_reason, is_free, net_egp,
                pricing_version, creation_standard_base_egp, entitlement_provenance)
            VALUES (org, target, 'FOLLOW_UP', 'ROUTINE', (price->>'base_egp')::numeric,
                price->>'discount_type', (price->>'discount_value')::numeric,
                price->>'discount_reason', (price->>'is_free')::boolean,
                (price->>'net_egp')::numeric, (price->>'pricing_version')::bigint,
                (price->>'creation_standard_base_egp')::numeric, snapshot);
            INSERT INTO public.appointment_history (
                id, organization_id, appointment_id, appointment_revision, action, new_status,
                new_service_date, new_number, actor_staff_id, actor_role, price_snapshot_appointment_id)
            VALUES (gen_random_uuid(), org, target, 0, 'CREATED', 'Booked', day, number,
                (context->>'actor_staff_id')::uuid, context->>'actor_role', target);
            INSERT INTO public.followup_claims (
                id, organization_id, appointment_id, patient_id, doctor_staff_id,
                source_visit_id, source_appointment_id, policy_snapshot, completion_local_date,
                approved_deadline, state)
            VALUES (claim, org, target, source.patient_id, source.doctor_staff_id,
                visit, source.appointment_id, snapshot, (context->>'completion_date')::date,
                (context->>'deadline')::date, 'ACTIVE');
            IF e.id IS NOT NULL THEN
                e.organization_id := org; e.claim_id := claim; e.source_visit_id := visit;
                e.appointment_id := target; e.authorization_revision := 1;
                e.prior_deadline := (context->>'deadline')::date; e.new_deadline := day;
                e.selected_date := day; e.booking_mode := mode; e.starts_at := starts; e.ends_at := ends;
                e.source_revision := source.source_revision;
                e.clinical_revision := (context->>'clinical_revision')::bigint;
                e.policy_version := (context->>'policy_version')::bigint;
                e.policy_kind := 'FOLLOWUP';
                e.claim_revision := 1; e.appointment_revision := 0;
                e.actor_staff_id := (context->>'actor_staff_id')::uuid; e.actor_role := context->>'actor_role';
                e.occurred_at := date_trunc('second', clock_timestamp()); e.sensitive_audit_id := gen_random_uuid();
                PERFORM public.d08_append_authorization(e);
            END IF;
            RETURN claim;
        END $$
    """)


def _claim_guards() -> None:
    op.execute("""
        REVOKE INSERT ON public.followup_claims FROM carepulse_runtime
    """)
    op.execute("""
        CREATE OR REPLACE FUNCTION public.guard_followup_claim() RETURNS trigger
        LANGUAGE plpgsql SET search_path = pg_catalog AS $$
        DECLARE
            a public.appointments%ROWTYPE;
            s public.followup_source_state%ROWTYPE;
        BEGIN
            IF TG_OP = 'DELETE' THEN
                RAISE EXCEPTION 'Follow-up evidence immutable' USING ERRCODE = '23514';
            ELSIF TG_OP = 'UPDATE' THEN
                IF OLD.state <> 'ACTIVE' OR NEW.state NOT IN ('RELEASED','CONSUMED')
                   OR NEW.revision <> OLD.revision + 1 OR
                   (to_jsonb(NEW) - 'state' - 'revision' - 'terminal_history_revision' - 'terminal_at')
                   IS DISTINCT FROM
                   (to_jsonb(OLD) - 'state' - 'revision' - 'terminal_history_revision' - 'terminal_at') THEN
                    RAISE EXCEPTION 'Follow-up claim evidence immutable' USING ERRCODE = '23514';
                END IF;
                RETURN NEW;
            END IF;
            IF current_user <> 'carepulse_migration' OR NEW.policy_snapshot->>'schema_version' IS DISTINCT FROM '2'
               OR NEW.state <> 'ACTIVE' OR NEW.revision <> 1
               OR NEW.terminal_history_revision IS NOT NULL OR NEW.terminal_at IS NOT NULL THEN
                RAISE EXCEPTION 'Use v2 claim creation writer' USING ERRCODE = '42501';
            END IF;
            SELECT * INTO STRICT a FROM public.appointments
            WHERE organization_id = NEW.organization_id AND id = NEW.appointment_id;
            SELECT * INTO STRICT s FROM public.followup_source_state
            WHERE organization_id = NEW.organization_id AND visit_id = NEW.source_visit_id;
            IF (NEW.patient_id, NEW.doctor_staff_id, NEW.source_appointment_id)
               IS DISTINCT FROM (s.patient_id, s.doctor_staff_id, s.appointment_id)
               OR (a.patient_id, a.doctor_staff_id, a.service_type, a.classification)
                  IS DISTINCT FROM (s.patient_id, s.doctor_staff_id, 'FOLLOW_UP'::text, 'ROUTINE'::text)
               OR a.service_date < NEW.completion_local_date
               OR (a.service_date > NEW.approved_deadline
                   AND NEW.policy_snapshot->>'new_date_event_id' IS NULL)
               OR NOT EXISTS (
                   SELECT 1 FROM public.appointment_price_snapshots p
                   WHERE p.organization_id = NEW.organization_id AND p.appointment_id = a.id
                     AND p.entitlement_provenance = NEW.policy_snapshot) THEN
                RAISE EXCEPTION 'Frozen claim/target mismatch' USING ERRCODE = '23514';
            END IF;
            RETURN NEW;
        END $$
    """)
    _movement_guard()


def _movement_guard() -> None:
    op.execute("""
        CREATE FUNCTION public.guard_followup_move() RETURNS trigger
        LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog AS $$
        DECLARE
            c public.followup_claims%ROWTYPE;
            s public.followup_source_state%ROWTYPE;
            v public.visits%ROWTYPE;
            context jsonb;
            policy bigint;
            deadline date;
        BEGIN
            IF OLD.service_type <> 'FOLLOW_UP' OR
               (NEW.service_date, NEW.booking_mode, NEW.starts_at, NEW.ends_at) IS NOT DISTINCT FROM
               (OLD.service_date, OLD.booking_mode, OLD.starts_at, OLD.ends_at) THEN
                RETURN NEW;
            END IF;
            SELECT * INTO STRICT c FROM public.followup_claims
            WHERE organization_id = OLD.organization_id AND appointment_id = OLD.id;
            SELECT current_version INTO policy FROM public.clinic_policy_heads
            WHERE organization_id = OLD.organization_id AND kind = 'FOLLOWUP';
            SELECT * INTO STRICT v FROM public.visits
            WHERE organization_id = OLD.organization_id AND id = c.source_visit_id;
            SELECT * INTO s FROM public.followup_source_state
            WHERE organization_id = OLD.organization_id AND visit_id = c.source_visit_id;
            context := public.d08_context(OLD.organization_id, c.source_visit_id,
                jsonb_build_object('source_revision', COALESCE(s.source_revision, 0),
                    'clinical_revision', v.clinical_revision, 'policy_version', policy));
            PERFORM 1 FROM public.followup_claims WHERE organization_id = OLD.organization_id
                AND id = c.id FOR UPDATE;
            SELECT * INTO STRICT c FROM public.followup_claims
            WHERE organization_id = OLD.organization_id AND appointment_id = OLD.id;
            PERFORM public.d08_check_authorized_date(context, NEW.service_date);
            -- NEW_DATE never authorizes a later move, even back to the original interval.
            SELECT max(new_deadline) INTO deadline FROM public.followup_authorization_events
            WHERE organization_id = OLD.organization_id AND claim_id = c.id AND kind = 'EXTENSION';
            IF c.state <> 'ACTIVE' OR NEW.service_date > GREATEST(c.approved_deadline,
                    COALESCE(deadline, public.d08_legacy_deadline(OLD.organization_id, c.id))) THEN
                RAISE EXCEPTION 'Move requires prior confirmed EXTENSION' USING ERRCODE = '23514';
            END IF;
            RETURN NEW;
        END $$
    """)
    op.execute("""
        CREATE TRIGGER appointment_followup_move BEFORE UPDATE ON public.appointments
        FOR EACH ROW EXECUTE FUNCTION public.guard_followup_move()
    """)


def _privileges() -> None:
    op.execute("""
        CREATE FUNCTION public.guard_d08_event() RETURNS trigger
        LANGUAGE plpgsql SET search_path = pg_catalog AS $$
        DECLARE
            s public.followup_source_state%ROWTYPE;
            c public.followup_claims%ROWTYPE;
            previous_revision bigint;
        BEGIN
            IF TG_OP <> 'INSERT' OR current_user <> 'carepulse_migration' THEN
                RAISE EXCEPTION 'Protected immutable Follow-up event' USING ERRCODE = '42501';
            END IF;
            IF TG_TABLE_NAME = 'followup_source_decision_events' THEN
                PERFORM 1 FROM public.followup_source_state
                WHERE organization_id = NEW.organization_id AND visit_id = NEW.source_visit_id FOR UPDATE;
                SELECT * INTO STRICT s FROM public.followup_source_state
                WHERE organization_id = NEW.organization_id AND visit_id = NEW.source_visit_id;
                IF NEW.source_revision <> s.source_revision + 1 OR
                   (NEW.before_decision, NEW.before_actor_staff_id, NEW.before_actor_role)
                   IS DISTINCT FROM (s.decision, s.decision_actor_staff_id, s.decision_actor_role) THEN
                    RAISE EXCEPTION 'Invalid source event successor' USING ERRCODE = '23514';
                END IF;
            ELSE
                PERFORM 1 FROM public.followup_claims
                WHERE organization_id = NEW.organization_id AND id = NEW.claim_id FOR UPDATE;
                SELECT * INTO STRICT c FROM public.followup_claims
                WHERE organization_id = NEW.organization_id AND id = NEW.claim_id;
                SELECT authorization_revision INTO previous_revision
                FROM public.followup_authorization_events WHERE organization_id = NEW.organization_id
                  AND claim_id = NEW.claim_id ORDER BY authorization_revision DESC LIMIT 1;
                IF c.state <> 'ACTIVE' OR NEW.claim_revision <> c.revision
                   OR NEW.authorization_revision <> COALESCE(previous_revision, 0) + 1 THEN
                    RAISE EXCEPTION 'Authorization requires active serialized successor' USING ERRCODE = '23514';
                END IF;
            END IF;
            RETURN NEW;
        END $$
    """)
    op.execute("""
        CREATE FUNCTION public.guard_d08_audit() RETURNS trigger
        LANGUAGE plpgsql SET search_path = pg_catalog AS $$
        BEGIN
            IF NEW.action IN ('FOLLOWUP_SOURCE_DECISION','FOLLOWUP_NEW_DATE','FOLLOWUP_EXTENSION')
               AND current_user <> 'carepulse_migration' THEN
                RAISE EXCEPTION 'Reserved Follow-up audit' USING ERRCODE = '42501';
            END IF;
            RETURN NEW;
        END $$
    """)
    op.execute("""
        CREATE TRIGGER sensitive_audit_d08_guard BEFORE INSERT ON public.sensitive_audit
        FOR EACH ROW EXECUTE FUNCTION public.guard_d08_audit()
    """)
    for relation in (
        "followup_source_decision_events",
        "followup_authorization_events",
    ):
        op.execute(f"ALTER TABLE public.{relation} ENABLE ROW LEVEL SECURITY")
        op.execute(f"ALTER TABLE public.{relation} FORCE ROW LEVEL SECURITY")
        op.execute(f"""
            CREATE POLICY {relation}_runtime ON public.{relation}
            FOR SELECT TO carepulse_runtime USING (organization_id =
                NULLIF(current_setting('app.organization_id', true), '')::uuid)
        """)
        op.execute(f"""
            CREATE POLICY {relation}_migration ON public.{relation}
            FOR ALL TO carepulse_migration USING (true) WITH CHECK (true)
        """)
        op.execute(f"REVOKE ALL ON public.{relation} FROM PUBLIC")
        op.execute(f"GRANT SELECT ON public.{relation} TO carepulse_runtime")
        op.execute(f"""
            CREATE TRIGGER d08_event_guard BEFORE INSERT OR UPDATE OR DELETE ON public.{relation}
            FOR EACH ROW EXECUTE FUNCTION public.guard_d08_event()
        """)
    for relation in (
        "followup_source_state",
        "followup_source_decision_events",
        "followup_authorization_events",
        "followup_claims",
        "followup_extensions",
    ):
        op.execute(f"""
            CREATE TRIGGER d08_no_truncate BEFORE TRUNCATE ON public.{relation}
            FOR EACH STATEMENT EXECUTE FUNCTION public.deny_completion_attestation_truncate()
        """)
    for signature in (
        "d08_legacy_source(uuid,uuid)",
        "fence_legacy_followup_decision()",
        "guard_d08_source_state()",
        "check_d08_source_chain()",
        "d08_legacy_deadline(uuid,uuid)",
        "check_d08_authorization_chain()",
        "d08_context(uuid,uuid,jsonb)",
        "d08_validate_decision(jsonb,date)",
        "d08_check_authorized_date(jsonb,date)",
        "guard_followup_move()",
        "d08_append_authorization(public.followup_authorization_events)",
        "guard_d08_event()",
        "guard_d08_audit()",
        "record_followup_source_decision(uuid,uuid,jsonb,jsonb)",
        "extend_followup_claim(uuid,uuid,jsonb,jsonb)",
        "create_followup_claim(uuid,uuid,jsonb,jsonb)",
    ):
        op.execute(f"REVOKE ALL ON FUNCTION public.{signature} FROM PUBLIC")
    for name in (
        "record_followup_source_decision",
        "extend_followup_claim",
        "create_followup_claim",
    ):
        op.execute(
            f"GRANT EXECUTE ON FUNCTION public.{name}(uuid,uuid,jsonb,jsonb) TO carepulse_runtime"
        )


def downgrade() -> None:
    raise RuntimeError("D-08 provenance migration is forward-only")

"""D-11: tenant-scoped dated reviews and the shared attendance/close barrier."""

from alembic import op

revision = "0027_d11_day_review"
down_revision = "0026_d07_completion_attestation"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
        CREATE TABLE public.day_reviews (
            id uuid PRIMARY KEY,
            organization_id uuid NOT NULL REFERENCES public.organizations(id),
            service_date date NOT NULL CHECK (isfinite(service_date)),
            revision bigint NOT NULL DEFAULT 1 CHECK (revision >= 1),
            disposition text NOT NULL
                CHECK (disposition IN ('REVIEWED', 'FINAL_CLOSED')),
            actor_staff_id uuid NOT NULL,
            occurred_at timestamptz NOT NULL
                CHECK (isfinite(occurred_at)
                       AND occurred_at = date_trunc('second', occurred_at)),
            audit_event_id uuid NOT NULL,
            notes text,
            UNIQUE (organization_id, id),
            UNIQUE (organization_id, service_date),
            UNIQUE (organization_id, audit_event_id),
            FOREIGN KEY (organization_id, actor_staff_id)
                REFERENCES public.staff_accounts(organization_id, id),
            FOREIGN KEY (organization_id, audit_event_id)
                REFERENCES public.sensitive_audit(organization_id, id)
        )
    """)
    op.execute("""
        CREATE TABLE public.day_review_history (
            id uuid PRIMARY KEY,
            organization_id uuid NOT NULL,
            day_review_id uuid NOT NULL,
            revision bigint NOT NULL CHECK (revision >= 1),
            old_disposition text
                CHECK (old_disposition IS NULL OR
                       old_disposition IN ('REVIEWED', 'FINAL_CLOSED')),
            disposition text NOT NULL
                CHECK (disposition IN ('REVIEWED', 'FINAL_CLOSED')),
            old_notes text,
            notes text,
            old_actor_staff_id uuid,
            old_occurred_at timestamptz,
            actor_staff_id uuid NOT NULL,
            occurred_at timestamptz NOT NULL
                CHECK (isfinite(occurred_at)
                       AND occurred_at = date_trunc('second', occurred_at)),
            audit_event_id uuid NOT NULL,
            UNIQUE (organization_id, day_review_id, revision),
            UNIQUE (organization_id, audit_event_id),
            FOREIGN KEY (organization_id, day_review_id)
                REFERENCES public.day_reviews(organization_id, id),
            FOREIGN KEY (organization_id, actor_staff_id)
                REFERENCES public.staff_accounts(organization_id, id),
            FOREIGN KEY (organization_id, old_actor_staff_id)
                REFERENCES public.staff_accounts(organization_id, id),
            FOREIGN KEY (organization_id, audit_event_id)
                REFERENCES public.sensitive_audit(organization_id, id),
            CHECK ((revision = 1 AND old_disposition IS NULL
                    AND old_notes IS NULL AND old_actor_staff_id IS NULL
                    AND old_occurred_at IS NULL)
                OR (revision > 1 AND old_disposition IS NOT NULL
                    AND old_actor_staff_id IS NOT NULL
                    AND old_occurred_at IS NOT NULL))
        )
    """)
    op.execute("""
        CREATE INDEX appointments_day_status_lane_report
        ON public.appointments (organization_id, service_date, status, lane)
    """)
    op.execute("""
        CREATE INDEX queue_entries_active_day_report
        ON public.queue_entries (organization_id, queue_day_id)
        WHERE disposition = 'ACTIVE'
    """)
    op.execute("""
        CREATE INDEX appointment_history_event_report
        ON public.appointment_history (organization_id, occurred_at)
    """)
    op.execute("""
        CREATE INDEX visits_admission_event_report
        ON public.visits (organization_id, admitted_at)
    """)

    for relation in ("day_reviews", "day_review_history"):
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

    # Every prospective attendance writer must cross the same policy head
    # before changing membership, including the first absent dated partition.
    op.execute("""
        CREATE FUNCTION public.lock_day_review_attendance(target_organization uuid)
        RETURNS void LANGUAGE plpgsql SECURITY DEFINER
        SET search_path = pg_catalog AS $$
        BEGIN
            IF session_user = 'carepulse_runtime'
               AND current_setting('app.organization_id', true)
                   IS DISTINCT FROM target_organization::text THEN
                RAISE EXCEPTION 'Day attendance tenant denied'
                    USING ERRCODE = '42501';
            END IF;
            PERFORM 1 FROM public.clinic_policy_heads
            WHERE organization_id = target_organization AND kind = 'SCHEDULE'
            FOR SHARE;
            IF NOT FOUND THEN
                RAISE EXCEPTION 'Schedule attendance barrier unavailable'
                    USING ERRCODE = '23514';
            END IF;
        END $$
    """)
    op.execute("""
        CREATE FUNCTION public.guard_day_review_queue_day_insert()
        RETURNS trigger LANGUAGE plpgsql SECURITY DEFINER
        SET search_path = pg_catalog AS $$
        BEGIN
            PERFORM public.lock_day_review_attendance(NEW.organization_id);
            RETURN NEW;
        END $$
    """)
    op.execute("""
        CREATE TRIGGER a_day_review_queue_day_insert
        BEFORE INSERT ON public.queue_days FOR EACH ROW
        EXECUTE FUNCTION public.guard_day_review_queue_day_insert()
    """)
    op.execute("""
        CREATE FUNCTION public.guard_day_review_waiting_membership()
        RETURNS trigger LANGUAGE plpgsql SECURITY DEFINER
        SET search_path = pg_catalog AS $$
        BEGIN
            IF TG_OP = 'INSERT' THEN
                IF NEW.status = 'Waiting' THEN
                    PERFORM public.lock_day_review_attendance(NEW.organization_id);
                END IF;
            ELSIF (OLD.status = 'Waiting' OR NEW.status = 'Waiting')
                  AND (OLD.status IS DISTINCT FROM NEW.status
                       OR OLD.service_date IS DISTINCT FROM NEW.service_date
                       OR OLD.lane IS DISTINCT FROM NEW.lane) THEN
                PERFORM public.lock_day_review_attendance(NEW.organization_id);
            END IF;
            RETURN NEW;
        END $$
    """)
    op.execute("""
        CREATE TRIGGER a_day_review_waiting_membership
        BEFORE INSERT OR UPDATE ON public.appointments FOR EACH ROW
        EXECUTE FUNCTION public.guard_day_review_waiting_membership()
    """)
    op.execute("""
        CREATE FUNCTION public.guard_day_review_active_entry()
        RETURNS trigger LANGUAGE plpgsql SECURITY DEFINER
        SET search_path = pg_catalog AS $$
        BEGIN
            IF TG_OP = 'DELETE' THEN
                IF OLD.disposition = 'ACTIVE' THEN
                    PERFORM public.lock_day_review_attendance(OLD.organization_id);
                END IF;
                RETURN OLD;
            END IF;
            IF (TG_OP = 'INSERT' AND NEW.disposition = 'ACTIVE')
               OR (TG_OP = 'UPDATE' AND
                   (OLD.disposition = 'ACTIVE' OR NEW.disposition = 'ACTIVE')) THEN
                PERFORM public.lock_day_review_attendance(NEW.organization_id);
            END IF;
            RETURN NEW;
        END $$
    """)
    op.execute("""
        CREATE TRIGGER a_day_review_active_entry
        BEFORE INSERT OR UPDATE OR DELETE ON public.queue_entries
        FOR EACH ROW EXECUTE FUNCTION public.guard_day_review_active_entry()
    """)
    op.execute("""
        CREATE FUNCTION public.assert_day_review_clear(
            target_organization uuid, target_date date)
        RETURNS void LANGUAGE plpgsql SECURITY DEFINER
        SET search_path = pg_catalog AS $$
        BEGIN
            IF EXISTS (
                SELECT 1 FROM public.appointments
                WHERE organization_id = target_organization
                  AND service_date = target_date AND status = 'Waiting'
            ) OR EXISTS (
                SELECT 1 FROM public.queue_entries
                WHERE organization_id = target_organization
                  AND service_date = target_date AND disposition = 'ACTIVE'
            ) THEN
                RAISE EXCEPTION 'Unresolved day attendance blocks final closure'
                    USING ERRCODE = '23514';
            END IF;
        END $$
    """)

    # The review and its audit/history are written by a single guarded
    # database operation. Direct runtime table writes cannot forge evidence.
    op.execute("""
        CREATE FUNCTION public.guard_day_review_current()
        RETURNS trigger LANGUAGE plpgsql
        SET search_path = pg_catalog AS $$
        BEGIN
            IF TG_OP = 'DELETE' THEN
                RAISE EXCEPTION 'Day review evidence is immutable'
                    USING ERRCODE = '23514';
            END IF;
            IF current_user <> 'carepulse_migration' THEN
                RAISE EXCEPTION 'Day review requires protected writer'
                    USING ERRCODE = '42501';
            END IF;
            IF TG_OP = 'UPDATE' AND (
                (NEW.id, NEW.organization_id, NEW.service_date) IS DISTINCT FROM
                (OLD.id, OLD.organization_id, OLD.service_date)
                OR NEW.revision <> OLD.revision + 1
            ) THEN
                RAISE EXCEPTION 'Day review identity or revision immutable'
                    USING ERRCODE = '23514';
            END IF;
            IF TG_OP = 'INSERT' AND NEW.revision <> 1 THEN
                RAISE EXCEPTION 'Day review starts at revision one'
                    USING ERRCODE = '23514';
            END IF;
            IF NOT EXISTS (
                SELECT 1 FROM public.sensitive_audit audit
                JOIN public.staff_accounts s
                  ON s.organization_id = audit.organization_id
                 AND s.id = audit.actor_staff_id
                 AND s.role = audit.actor_role
                WHERE audit.organization_id = NEW.organization_id
                  AND audit.id = NEW.audit_event_id
                  AND audit.actor_staff_id = NEW.actor_staff_id
                  AND audit.action = 'DAY_REVIEW_RECORDED'
                  AND audit.operator_id IS NULL
                  AND audit.recovery_operation_id IS NULL
                  AND audit.occurred_at = NEW.occurred_at
                  AND audit.correlation_id =
                      NEW.id::text || ':' || NEW.revision::text
                  AND audit.after_state = jsonb_build_object(
                      'service_date', NEW.service_date,
                      'revision', NEW.revision,
                      'disposition', NEW.disposition, 'notes', NEW.notes)
            ) THEN
                RAISE EXCEPTION 'Day review audit evidence mismatch'
                    USING ERRCODE = '23514';
            END IF;
            RETURN NEW;
        END $$
    """)
    op.execute("""
        CREATE TRIGGER day_review_current_guard
        BEFORE INSERT OR UPDATE OR DELETE ON public.day_reviews
        FOR EACH ROW EXECUTE FUNCTION public.guard_day_review_current()
    """)
    op.execute("""
        CREATE FUNCTION public.guard_day_review_audit()
        RETURNS trigger LANGUAGE plpgsql SET search_path = pg_catalog AS $$
        BEGIN
            IF NEW.action = 'DAY_REVIEW_RECORDED'
               AND current_user <> 'carepulse_migration' THEN
                RAISE EXCEPTION 'Day review audit requires protected writer'
                    USING ERRCODE = '42501';
            END IF;
            RETURN NEW;
        END $$
    """)
    op.execute("""
        CREATE TRIGGER day_review_sensitive_audit_guard
        BEFORE INSERT ON public.sensitive_audit FOR EACH ROW
        EXECUTE FUNCTION public.guard_day_review_audit()
    """)
    op.execute("""
        CREATE FUNCTION public.guard_day_review_history()
        RETURNS trigger LANGUAGE plpgsql
        SET search_path = pg_catalog AS $$
        BEGIN
            IF TG_OP <> 'INSERT' THEN
                RAISE EXCEPTION 'Day review history is immutable'
                    USING ERRCODE = '23514';
            END IF;
            IF current_user <> 'carepulse_migration' THEN
                RAISE EXCEPTION 'Day review history requires protected writer'
                    USING ERRCODE = '42501';
            END IF;
            IF NEW.revision > 1 AND NOT EXISTS (
                SELECT 1 FROM public.day_review_history prior
                WHERE prior.organization_id = NEW.organization_id
                  AND prior.day_review_id = NEW.day_review_id
                  AND prior.revision = NEW.revision - 1
                  AND prior.disposition = NEW.old_disposition
                  AND prior.notes IS NOT DISTINCT FROM NEW.old_notes
                  AND prior.actor_staff_id = NEW.old_actor_staff_id
                  AND prior.occurred_at = NEW.old_occurred_at
            ) THEN
                RAISE EXCEPTION 'Day review history amendment chain missing'
                    USING ERRCODE = '23514';
            END IF;
            RETURN NEW;
        END $$
    """)
    op.execute("""
        CREATE TRIGGER day_review_history_guard
        BEFORE INSERT OR UPDATE OR DELETE ON public.day_review_history
        FOR EACH ROW EXECUTE FUNCTION public.guard_day_review_history()
    """)
    op.execute("""
        CREATE FUNCTION public.deny_day_review_truncate()
        RETURNS trigger LANGUAGE plpgsql SET search_path = pg_catalog AS $$
        BEGIN
            RAISE EXCEPTION 'Day review evidence cannot be truncated'
                USING ERRCODE = '23514';
        END $$
    """)
    for relation in ("day_reviews", "day_review_history"):
        op.execute(f"""
            CREATE TRIGGER {relation}_no_truncate
            BEFORE TRUNCATE ON public.{relation} FOR EACH STATEMENT
            EXECUTE FUNCTION public.deny_day_review_truncate()
        """)

    # Both checks are deferred: the final image and the accompanying history
    # must remain consistent even after several edits in one transaction.
    op.execute("""
        CREATE FUNCTION public.check_day_review_commit()
        RETURNS trigger LANGUAGE plpgsql SECURITY DEFINER
        SET search_path = pg_catalog AS $$
        BEGIN
            IF NOT EXISTS (
                SELECT 1 FROM public.day_review_history h
                WHERE h.organization_id = NEW.organization_id
                  AND h.day_review_id = NEW.id
                  AND h.revision = NEW.revision
                  AND h.disposition = NEW.disposition
                  AND h.notes IS NOT DISTINCT FROM NEW.notes
                  AND h.actor_staff_id = NEW.actor_staff_id
                  AND h.occurred_at = NEW.occurred_at
                  AND h.audit_event_id = NEW.audit_event_id
            ) THEN
                RAISE EXCEPTION 'Day review lacks matching immutable history'
                    USING ERRCODE = '23514';
            END IF;
            IF EXISTS (
                SELECT 1 FROM public.day_reviews current_review
                WHERE current_review.organization_id = NEW.organization_id
                  AND current_review.id = NEW.id
                  AND current_review.disposition = 'FINAL_CLOSED'
            ) THEN
                PERFORM public.assert_day_review_clear(
                    NEW.organization_id, NEW.service_date);
            END IF;
            RETURN NULL;
        END $$
    """)
    op.execute("""
        CREATE CONSTRAINT TRIGGER day_review_committed_evidence
        AFTER INSERT OR UPDATE ON public.day_reviews
        DEFERRABLE INITIALLY DEFERRED FOR EACH ROW
        EXECUTE FUNCTION public.check_day_review_commit()
    """)
    op.execute("""
        CREATE FUNCTION public.record_day_review(
            target_organization uuid, target_date date,
            expected_revision bigint, requested_disposition text, requested_notes text)
        RETURNS TABLE (day_review_id uuid, revision bigint)
        LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog AS $$
        DECLARE
            actual_actor uuid;
            actual_role text;
            prior_review public.day_reviews%ROWTYPE;
            next_revision bigint;
            review_time timestamptz;
            new_audit_id uuid;
            prior_state jsonb;
        BEGIN
            IF session_user <> 'carepulse_runtime'
               OR target_organization IS NULL OR target_date IS NULL
               OR NOT isfinite(target_date)
               OR requested_disposition NOT IN ('REVIEWED', 'FINAL_CLOSED')
               OR requested_disposition IS NULL
               OR expected_revision IS NULL OR expected_revision < 0
               OR current_setting('app.organization_id', true)
                  IS DISTINCT FROM target_organization::text
               OR NULLIF(current_setting('app.staff_id', true), '') IS NULL THEN
                RAISE EXCEPTION 'Day review denied' USING ERRCODE = '42501';
            END IF;
            actual_actor := current_setting('app.staff_id', true)::uuid;
            PERFORM 1 FROM public.organizations
            WHERE id = target_organization AND state = 'ACTIVE' FOR SHARE;
            IF NOT FOUND THEN
                RAISE EXCEPTION 'Clinic is inactive' USING ERRCODE = '42501';
            END IF;
            SELECT s.role
            INTO actual_role
            FROM public.staff_accounts s
            WHERE s.organization_id = target_organization
              AND s.id = actual_actor
              AND s.role IN ('DOCTOR_OWNER', 'RECEPTIONIST')
              AND s.enabled AND s.verified
              AND s.reset_block_operation_id IS NULL
            FOR SHARE;
            IF NOT FOUND THEN
                RAISE EXCEPTION 'Day review actor unavailable'
                    USING ERRCODE = '42501';
            END IF;

            IF requested_disposition = 'FINAL_CLOSED' THEN
                IF current_setting('transaction_isolation')
                   NOT IN ('read committed', 'read uncommitted') THEN
                    RAISE EXCEPTION 'Final close requires a fresh post-lock snapshot'
                        USING ERRCODE = '25000';
                END IF;
                PERFORM 1 FROM public.clinic_policy_heads
                WHERE organization_id = target_organization AND kind = 'SCHEDULE'
                FOR UPDATE;
                IF NOT FOUND THEN
                    RAISE EXCEPTION 'Schedule close barrier unavailable'
                        USING ERRCODE = '23514';
                END IF;
                PERFORM public.assert_day_review_clear(target_organization, target_date);
            END IF;

            SELECT * INTO prior_review FROM public.day_reviews r
            WHERE r.organization_id = target_organization
              AND r.service_date = target_date
            FOR UPDATE;
            IF FOUND THEN
                IF prior_review.revision <> expected_revision THEN
                    RAISE EXCEPTION 'Stale day review revision'
                        USING ERRCODE = '40001';
                END IF;
                day_review_id := prior_review.id;
                next_revision := prior_review.revision + 1;
                prior_state := jsonb_build_object(
                    'service_date', prior_review.service_date,
                    'revision', prior_review.revision,
                    'disposition', prior_review.disposition,
                    'notes', prior_review.notes);
            ELSE
                IF expected_revision <> 0 THEN
                    RAISE EXCEPTION 'Stale day review revision'
                        USING ERRCODE = '40001';
                END IF;
                day_review_id := gen_random_uuid();
                next_revision := 1;
            END IF;

            revision := next_revision;
            review_time := date_trunc('second', clock_timestamp());
            new_audit_id := gen_random_uuid();
            INSERT INTO public.sensitive_audit (
                id, organization_id, actor_staff_id, actor_role, action,
                reason, correlation_id, before_state, after_state, occurred_at)
            VALUES (
                new_audit_id, target_organization, actual_actor, actual_role,
                'DAY_REVIEW_RECORDED',
                CASE WHEN revision = 1 THEN 'Day review'
                     ELSE 'Day review amendment' END,
                day_review_id::text || ':' || revision::text, prior_state,
                jsonb_build_object(
                    'service_date', target_date, 'revision', revision,
                    'disposition', requested_disposition, 'notes', requested_notes),
                review_time);

            IF revision = 1 THEN
                INSERT INTO public.day_reviews (
                    id, organization_id, service_date, revision, disposition,
                    actor_staff_id, occurred_at, audit_event_id, notes)
                VALUES (
                    day_review_id, target_organization, target_date, revision,
                    requested_disposition, actual_actor, review_time,
                    new_audit_id, requested_notes);
            ELSE
                UPDATE public.day_reviews
                SET revision = next_revision,
                    disposition = requested_disposition,
                    actor_staff_id = actual_actor, occurred_at = review_time,
                    audit_event_id = new_audit_id, notes = requested_notes
                WHERE organization_id = target_organization
                  AND id = day_review_id;
            END IF;

            INSERT INTO public.day_review_history (
                id, organization_id, day_review_id, revision,
                old_disposition, disposition, old_notes, notes,
                old_actor_staff_id, old_occurred_at,
                actor_staff_id, occurred_at, audit_event_id)
            VALUES (
                gen_random_uuid(), target_organization, day_review_id, revision,
                prior_review.disposition, requested_disposition,
                prior_review.notes, requested_notes,
                prior_review.actor_staff_id, prior_review.occurred_at,
                actual_actor, review_time, new_audit_id);

            IF requested_disposition = 'FINAL_CLOSED' THEN
                PERFORM public.assert_day_review_clear(target_organization, target_date);
            END IF;
            RETURN NEXT;
        END $$
    """)

    for signature in (
        "lock_day_review_attendance(uuid)",
        "guard_day_review_queue_day_insert()",
        "guard_day_review_waiting_membership()",
        "guard_day_review_active_entry()",
        "assert_day_review_clear(uuid,date)",
        "guard_day_review_current()",
        "guard_day_review_audit()",
        "guard_day_review_history()",
        "deny_day_review_truncate()",
        "check_day_review_commit()",
        "record_day_review(uuid,date,bigint,text,text)",
    ):
        op.execute(f"REVOKE ALL ON FUNCTION public.{signature} FROM PUBLIC")
    op.execute("""
        GRANT EXECUTE ON FUNCTION public.record_day_review(
            uuid,date,bigint,text,text) TO carepulse_runtime
    """)


def downgrade() -> None:
    raise RuntimeError("D-11 day review migration is forward-only")

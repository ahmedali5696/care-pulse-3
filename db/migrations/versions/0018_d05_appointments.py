"""D-05: tenant-qualified appointments, frozen prices and irreversible numbering."""

from alembic import op

revision = "0018_d05_appointments"
down_revision = "0017_d04_policy_versions"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
        CREATE TABLE public.appointments (
            id uuid PRIMARY KEY,
            organization_id uuid NOT NULL REFERENCES public.organizations(id),
            patient_id uuid NOT NULL,
            doctor_staff_id uuid NOT NULL,
            service_date date NOT NULL CHECK (isfinite(service_date)),
            status text NOT NULL CHECK (status IN
                ('Booked','Waiting','In Examination','Completed','Cancelled','No-show')),
            service_type text NOT NULL CHECK
                (service_type IN ('NEW_EXAMINATION','FOLLOW_UP')),
            classification text NOT NULL CHECK
                (classification IN ('ROUTINE','URGENT','EMERGENCY')),
            lane text NOT NULL CHECK (lane IN ('MAIN','EMERGENCY')),
            booking_mode text NOT NULL CHECK (booking_mode IN ('OPEN','EXACT_TIME')),
            starts_at timestamptz,
            ends_at timestamptz,
            current_assignment_id uuid NOT NULL,
            current_number bigint NOT NULL CHECK (current_number > 0),
            revision bigint NOT NULL DEFAULT 0 CHECK (revision >= 0),
            created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
            updated_at timestamptz NOT NULL DEFAULT clock_timestamp(),
            UNIQUE (organization_id, id),
            UNIQUE (organization_id, id, service_type, classification),
            FOREIGN KEY (organization_id, patient_id)
                REFERENCES public.patients (organization_id, id),
            FOREIGN KEY (organization_id, doctor_staff_id)
                REFERENCES public.staff_accounts (organization_id, id),
            CONSTRAINT appointment_lane_class_valid CHECK
                ((classification = 'EMERGENCY' AND lane = 'EMERGENCY') OR
                 (classification <> 'EMERGENCY' AND lane = 'MAIN')),
            CONSTRAINT appointment_followup_class_valid CHECK
                (service_type <> 'FOLLOW_UP' OR classification = 'ROUTINE'),
            CONSTRAINT appointment_interval_valid CHECK
                ((booking_mode = 'OPEN' AND starts_at IS NULL AND ends_at IS NULL)
                 OR (booking_mode = 'EXACT_TIME' AND starts_at IS NOT NULL
                     AND ends_at IS NOT NULL AND starts_at < ends_at
                     AND (starts_at AT TIME ZONE 'Africa/Cairo')::date = service_date))
        )
    """)
    op.execute("""
        CREATE UNIQUE INDEX appointments_one_counted_patient_day
        ON public.appointments (organization_id, patient_id, service_date)
        WHERE status IN ('Booked','Waiting','In Examination','Completed')
    """)
    op.execute("""
        CREATE TABLE public.appointment_day_counters (
            organization_id uuid NOT NULL REFERENCES public.organizations(id),
            service_date date NOT NULL CHECK (isfinite(service_date)),
            last_number bigint NOT NULL CHECK (last_number > 0),
            PRIMARY KEY (organization_id, service_date)
        )
    """)
    op.execute("""
        CREATE TABLE public.appointment_number_assignments (
            id uuid PRIMARY KEY,
            organization_id uuid NOT NULL REFERENCES public.organizations(id),
            appointment_id uuid NOT NULL,
            service_date date NOT NULL CHECK (isfinite(service_date)),
            number bigint NOT NULL CHECK (number > 0),
            prior_assignment_id uuid,
            assigned_at timestamptz NOT NULL DEFAULT clock_timestamp(),
            UNIQUE (organization_id, id),
            UNIQUE (organization_id, service_date, number),
            UNIQUE (organization_id, appointment_id, service_date, id, number),
            FOREIGN KEY (organization_id, appointment_id)
                REFERENCES public.appointments (organization_id, id)
                DEFERRABLE INITIALLY DEFERRED,
            FOREIGN KEY (organization_id, prior_assignment_id)
                REFERENCES public.appointment_number_assignments (organization_id, id),
            FOREIGN KEY (organization_id, service_date)
                REFERENCES public.appointment_day_counters (organization_id, service_date)
        )
    """)
    op.execute("""
        ALTER TABLE public.appointments ADD CONSTRAINT appointment_current_assignment_fk
        FOREIGN KEY (organization_id, id, service_date,
                     current_assignment_id, current_number)
        REFERENCES public.appointment_number_assignments
            (organization_id, appointment_id, service_date, id, number)
        DEFERRABLE INITIALLY DEFERRED
    """)
    op.execute("""
        CREATE TABLE public.appointment_price_snapshots (
            organization_id uuid NOT NULL,
            appointment_id uuid NOT NULL,
            service_type text NOT NULL,
            classification text NOT NULL,
            base_egp numeric NOT NULL,
            discount_type text NOT NULL CHECK
                (discount_type IN ('NONE','FIXED','PERCENT')),
            discount_value numeric NOT NULL DEFAULT 0,
            discount_reason text,
            is_free boolean NOT NULL,
            net_egp numeric NOT NULL,
            pricing_kind text NOT NULL DEFAULT 'PRICE' CHECK (pricing_kind = 'PRICE'),
            pricing_version bigint NOT NULL CHECK (pricing_version > 0),
            creation_standard_base_egp numeric NOT NULL,
            entitlement_provenance jsonb,
            created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
            PRIMARY KEY (organization_id, appointment_id),
            FOREIGN KEY (organization_id, appointment_id, service_type, classification)
                REFERENCES public.appointments
                    (organization_id, id, service_type, classification)
                DEFERRABLE INITIALLY DEFERRED,
            FOREIGN KEY (organization_id, pricing_kind, pricing_version)
                REFERENCES public.clinic_policy_versions
                    (organization_id, kind, version),
            CONSTRAINT appointment_price_money_valid CHECK (
                base_egp >= 0 AND base_egp < 'Infinity'::numeric
                AND creation_standard_base_egp >= 0
                AND creation_standard_base_egp < 'Infinity'::numeric
                AND net_egp >= 0 AND net_egp < 'Infinity'::numeric
                AND trunc(base_egp, 2) = base_egp
                AND trunc(creation_standard_base_egp, 2) = creation_standard_base_egp
                AND trunc(net_egp, 2) = net_egp),
            CONSTRAINT appointment_price_discount_valid CHECK (
                discount_value >= 0 AND discount_value < 'Infinity'::numeric
                AND ((discount_type = 'NONE' AND discount_value = 0
                      AND discount_reason IS NULL)
                  OR (discount_type = 'FIXED' AND discount_value <= base_egp
                      AND trunc(discount_value, 2) = discount_value
                      AND discount_reason IS NOT NULL
                      AND btrim(discount_reason) <> '')
                  OR (discount_type = 'PERCENT' AND discount_value <= 100
                      AND discount_reason IS NOT NULL
                      AND btrim(discount_reason) <> ''))),
            CONSTRAINT appointment_price_final_valid CHECK (
                net_egp = round(base_egp - CASE discount_type
                    WHEN 'FIXED' THEN discount_value
                    WHEN 'PERCENT' THEN base_egp * discount_value / 100
                    ELSE 0 END, 2)
                AND is_free = (net_egp = 0)
                AND (service_type <> 'NEW_EXAMINATION'
                     OR classification = 'URGENT'
                     OR base_egp = creation_standard_base_egp)
                AND (classification <> 'URGENT' OR
                     (NOT is_free AND base_egp > creation_standard_base_egp
                      AND net_egp > creation_standard_base_egp))),
            CONSTRAINT appointment_entitlement_shape CHECK
                ((service_type = 'FOLLOW_UP' AND entitlement_provenance IS NOT NULL
                  AND jsonb_typeof(entitlement_provenance) = 'object'
                  AND entitlement_provenance <> CAST('{}' AS jsonb)) OR
                 (service_type <> 'FOLLOW_UP' AND
                  (entitlement_provenance IS NULL OR
                   jsonb_typeof(entitlement_provenance) = 'object')))
        )
    """)
    op.execute("""
        CREATE TABLE public.appointment_history (
            id uuid PRIMARY KEY,
            organization_id uuid NOT NULL,
            appointment_id uuid NOT NULL,
            appointment_revision bigint NOT NULL CHECK (appointment_revision >= 0),
            action text NOT NULL CHECK (btrim(action) <> ''),
            old_status text,
            new_status text NOT NULL CHECK
                (new_status IN ('Booked','Waiting','In Examination',
                                'Completed','Cancelled','No-show')),
            old_service_date date,
            new_service_date date NOT NULL CHECK (isfinite(new_service_date)),
            old_number bigint CHECK (old_number > 0),
            new_number bigint NOT NULL CHECK (new_number > 0),
            actor_staff_id uuid NOT NULL,
            actor_role text NOT NULL CHECK (actor_role IN ('DOCTOR_OWNER','RECEPTIONIST')),
            occurred_at timestamptz NOT NULL DEFAULT date_trunc('second',clock_timestamp()),
            reason text,
            price_snapshot_appointment_id uuid NOT NULL,
            replacement_appointment_id uuid,
            FOREIGN KEY (organization_id, appointment_id)
                REFERENCES public.appointments (organization_id, id),
            FOREIGN KEY (organization_id, price_snapshot_appointment_id)
                REFERENCES public.appointment_price_snapshots
                    (organization_id, appointment_id)
                DEFERRABLE INITIALLY DEFERRED,
            FOREIGN KEY (organization_id, replacement_appointment_id)
                REFERENCES public.appointments (organization_id, id)
                DEFERRABLE INITIALLY DEFERRED,
            FOREIGN KEY (organization_id, actor_staff_id)
                REFERENCES public.staff_accounts (organization_id, id),
            UNIQUE (organization_id, appointment_id, appointment_revision),
            CHECK (price_snapshot_appointment_id = appointment_id),
            CHECK (replacement_appointment_id IS NULL OR
                   replacement_appointment_id <> appointment_id),
            CHECK ((action = 'CREATED' AND appointment_revision = 0
                    AND old_status IS NULL
                    AND old_service_date IS NULL AND old_number IS NULL)
                   OR (action <> 'CREATED' AND appointment_revision > 0
                    AND old_status IN
                       ('Booked','Waiting','In Examination',
                        'Completed','Cancelled','No-show')
                    AND old_service_date IS NOT NULL
                    AND isfinite(old_service_date) AND old_number > 0)),
            CHECK (reason IS NULL OR btrim(reason) <> ''),
            CHECK (occurred_at = date_trunc('second', occurred_at))
        )
    """)
    op.execute("""
        CREATE UNIQUE INDEX appointment_history_one_creation
        ON public.appointment_history (organization_id, appointment_id)
        WHERE action = 'CREATED'
    """)
    op.execute("""
        CREATE FUNCTION public.guard_appointment() RETURNS trigger
        LANGUAGE plpgsql SET search_path = pg_catalog AS $$
        BEGIN
            IF TG_OP = 'DELETE' THEN
                RAISE EXCEPTION 'Appointment deletion forbidden' USING ERRCODE = '23514';
            END IF;
            IF TG_OP = 'INSERT' THEN
                IF NEW.revision <> 0 THEN
                    RAISE EXCEPTION 'Invalid initial appointment revision'
                        USING ERRCODE = '23514';
                END IF;
                NEW.created_at := clock_timestamp();
                NEW.updated_at := NEW.created_at;
                -- Share D-03's patient row lock with archiving and other new-ID writers.
                PERFORM 1 FROM public.patients
                WHERE organization_id = NEW.organization_id AND id = NEW.patient_id
                  AND NOT archived FOR UPDATE;
                IF NOT FOUND THEN
                    RAISE EXCEPTION 'Patient unavailable for new appointment'
                        USING ERRCODE = '23514';
                END IF;
                PERFORM 1 FROM public.staff_accounts
                WHERE organization_id = NEW.organization_id AND id = NEW.doctor_staff_id
                  AND role = 'DOCTOR_OWNER';
                IF NOT FOUND THEN
                    RAISE EXCEPTION 'Appointment doctor must be clinic owner'
                        USING ERRCODE = '23514';
                END IF;
            ELSE
                IF OLD.status IN ('Completed','Cancelled','No-show') THEN
                    RAISE EXCEPTION 'Terminal appointment is immutable'
                        USING ERRCODE = '23514';
                END IF;
                IF (NEW.id,NEW.organization_id,NEW.patient_id,NEW.doctor_staff_id,
                    NEW.service_type,NEW.classification,NEW.lane,NEW.created_at)
                   IS DISTINCT FROM
                   (OLD.id,OLD.organization_id,OLD.patient_id,OLD.doctor_staff_id,
                    OLD.service_type,OLD.classification,OLD.lane,OLD.created_at)
                   OR NEW.revision <> OLD.revision + 1 THEN
                    RAISE EXCEPTION 'Immutable appointment identity or stale revision'
                        USING ERRCODE = '23514';
                END IF;
                IF (OLD.status = 'In Examination' OR OLD.classification = 'URGENT')
                   AND NEW.service_date <> OLD.service_date THEN
                    RAISE EXCEPTION 'Appointment cannot move to another day'
                        USING ERRCODE = '23514';
                END IF;
                IF OLD.status = 'In Examination' AND
                   (NEW.booking_mode,NEW.starts_at,NEW.ends_at,NEW.current_number,
                    NEW.current_assignment_id) IS DISTINCT FROM
                   (OLD.booking_mode,OLD.starts_at,OLD.ends_at,OLD.current_number,
                    OLD.current_assignment_id) THEN
                    RAISE EXCEPTION 'In Examination cannot be rescheduled'
                        USING ERRCODE = '23514';
                END IF;
                IF (NEW.service_date = OLD.service_date AND
                    (NEW.current_assignment_id,NEW.current_number) IS DISTINCT FROM
                    (OLD.current_assignment_id,OLD.current_number)) OR
                   (NEW.service_date <> OLD.service_date AND
                    NEW.current_assignment_id = OLD.current_assignment_id) THEN
                    RAISE EXCEPTION 'Invalid current number reassignment'
                        USING ERRCODE = '23514';
                END IF;
                NEW.updated_at := clock_timestamp();
            END IF;
            RETURN NEW;
        END $$
    """)
    op.execute("""
        CREATE TRIGGER appointments_guard
        BEFORE INSERT OR UPDATE OR DELETE ON public.appointments
        FOR EACH ROW EXECUTE FUNCTION public.guard_appointment()
    """)
    op.execute("""
        CREATE FUNCTION public.guard_appointment_counter() RETURNS trigger
        LANGUAGE plpgsql SET search_path = pg_catalog AS $$
        BEGIN
            IF TG_OP = 'DELETE' OR
               (TG_OP = 'INSERT' AND NEW.last_number <> 1) THEN
                RAISE EXCEPTION 'Invalid counter creation/deletion'
                    USING ERRCODE = '23514';
            END IF;
            IF TG_OP = 'UPDATE' AND (
                (NEW.organization_id,NEW.service_date) IS DISTINCT FROM
                (OLD.organization_id,OLD.service_date) OR
                NEW.last_number <> OLD.last_number + 1) THEN
                RAISE EXCEPTION 'Counter must increment exactly once'
                    USING ERRCODE = '23514';
            END IF;
            RETURN NEW;
        END $$
    """)
    op.execute("""
        CREATE TRIGGER appointment_counter_guard
        BEFORE INSERT OR UPDATE OR DELETE ON public.appointment_day_counters
        FOR EACH ROW EXECUTE FUNCTION public.guard_appointment_counter()
    """)
    op.execute("""
        CREATE FUNCTION public.guard_appointment_number_insert() RETURNS trigger
        LANGUAGE plpgsql SET search_path = pg_catalog AS $$
        DECLARE ceiling bigint;
        BEGIN
            SELECT last_number INTO ceiling
            FROM public.appointment_day_counters
            WHERE organization_id = NEW.organization_id AND service_date = NEW.service_date
            FOR UPDATE;
            IF ceiling IS NULL OR NEW.number <> ceiling THEN
                RAISE EXCEPTION 'Unallocated appointment number'
                    USING ERRCODE = '23514';
            END IF;
            IF NEW.prior_assignment_id IS NOT NULL AND NOT EXISTS (
                SELECT 1 FROM public.appointment_number_assignments
                WHERE id = NEW.prior_assignment_id
                  AND organization_id = NEW.organization_id
                  AND appointment_id = NEW.appointment_id
            ) THEN
                RAISE EXCEPTION 'Invalid previous assignment'
                    USING ERRCODE = '23514';
            END IF;
            NEW.assigned_at := clock_timestamp();
            RETURN NEW;
        END $$
    """)
    op.execute("""
        CREATE TRIGGER appointment_number_insert_guard
        BEFORE INSERT ON public.appointment_number_assignments
        FOR EACH ROW EXECUTE FUNCTION public.guard_appointment_number_insert()
    """)
    op.execute("""
        CREATE FUNCTION public.guard_appointment_history_insert() RETURNS trigger
        LANGUAGE plpgsql SET search_path = pg_catalog AS $$
        BEGIN
            IF NOT EXISTS (
                SELECT 1 FROM public.staff_accounts s
                WHERE s.organization_id = NEW.organization_id
                  AND s.id = NEW.actor_staff_id AND s.role = NEW.actor_role
            ) OR NOT EXISTS (
                SELECT 1 FROM public.appointments a
                WHERE a.organization_id = NEW.organization_id
                  AND a.id = NEW.appointment_id
                  AND a.revision = NEW.appointment_revision
                  AND a.status = NEW.new_status
                  AND a.service_date = NEW.new_service_date
                  AND a.current_number = NEW.new_number
            ) OR NOT EXISTS (
                SELECT 1 FROM public.appointment_number_assignments n
                WHERE n.organization_id = NEW.organization_id
                  AND n.appointment_id = NEW.appointment_id
                  AND n.service_date = NEW.new_service_date
                  AND n.number = NEW.new_number
            ) OR (NEW.appointment_revision > 0 AND NOT EXISTS (
                SELECT 1 FROM public.appointment_history h
                WHERE h.organization_id = NEW.organization_id
                  AND h.appointment_id = NEW.appointment_id
                  AND h.appointment_revision = NEW.appointment_revision - 1
                  AND h.new_status = NEW.old_status
                  AND h.new_service_date = NEW.old_service_date
                  AND h.new_number = NEW.old_number
            )) THEN
                RAISE EXCEPTION 'Inconsistent appointment history'
                    USING ERRCODE = '23514';
            END IF;
            NEW.occurred_at := date_trunc('second', clock_timestamp());
            RETURN NEW;
        END $$
    """)
    op.execute("""
        CREATE TRIGGER appointment_history_insert_guard
        BEFORE INSERT ON public.appointment_history
        FOR EACH ROW EXECUTE FUNCTION public.guard_appointment_history_insert()
    """)
    op.execute("""
        CREATE FUNCTION public.deny_appointment_evidence_mutation() RETURNS trigger
        LANGUAGE plpgsql SET search_path = pg_catalog AS $$
        BEGIN
            RAISE EXCEPTION 'Immutable appointment evidence' USING ERRCODE = '23514';
        END $$
    """)
    for relation in (
        "appointment_price_snapshots",
        "appointment_number_assignments",
        "appointment_history",
    ):
        op.execute(f"""
            CREATE TRIGGER {relation}_immutable
            BEFORE UPDATE OR DELETE ON public.{relation}
            FOR EACH ROW EXECUTE FUNCTION public.deny_appointment_evidence_mutation()
        """)
    op.execute("""
        CREATE FUNCTION public.assert_appointment_evidence_complete() RETURNS trigger
        LANGUAGE plpgsql SET search_path = pg_catalog AS $$
        BEGIN
            IF NOT EXISTS (
                SELECT 1 FROM public.appointment_price_snapshots s
                WHERE s.organization_id = NEW.organization_id
                  AND s.appointment_id = NEW.id
            ) OR NOT EXISTS (
                SELECT 1 FROM public.appointment_number_assignments a
                WHERE (a.organization_id, a.appointment_id, a.service_date, a.id, a.number) =
                      (NEW.organization_id, NEW.id, NEW.service_date,
                       NEW.current_assignment_id, NEW.current_number)
            ) OR NOT EXISTS (
                SELECT 1 FROM public.appointment_history h
                WHERE h.organization_id = NEW.organization_id
                  AND h.appointment_id = NEW.id AND h.action = 'CREATED'
            ) OR NOT EXISTS (
                SELECT 1 FROM public.appointment_history h
                WHERE h.organization_id = NEW.organization_id
                  AND h.appointment_id = NEW.id
                  AND h.appointment_revision = NEW.revision
                  AND h.new_status = NEW.status
                  AND h.new_service_date = NEW.service_date
                  AND h.new_number = NEW.current_number
            ) OR (TG_OP = 'UPDATE' AND NOT EXISTS (
                SELECT 1 FROM public.appointment_history h
                WHERE h.organization_id = NEW.organization_id
                  AND h.appointment_id = NEW.id
                  AND h.appointment_revision = NEW.revision
                  AND h.old_status = OLD.status
                  AND h.old_service_date = OLD.service_date
                  AND h.old_number = OLD.current_number
            )) THEN
                RAISE EXCEPTION 'Incomplete confirmed appointment evidence'
                    USING ERRCODE = '23514';
            END IF;
            RETURN NULL;
        END $$
    """)
    op.execute("""
        CREATE CONSTRAINT TRIGGER appointment_evidence_committed
        AFTER INSERT OR UPDATE ON public.appointments
        DEFERRABLE INITIALLY DEFERRED
        FOR EACH ROW EXECUTE FUNCTION public.assert_appointment_evidence_complete()
    """)
    op.execute("""
        CREATE FUNCTION public.assert_counter_assignment_complete() RETURNS trigger
        LANGUAGE plpgsql SET search_path = pg_catalog AS $$
        BEGIN
            IF NOT EXISTS (
                SELECT 1 FROM public.appointment_number_assignments a
                WHERE a.organization_id = NEW.organization_id
                  AND a.service_date = NEW.service_date
                  AND a.number = NEW.last_number
            ) THEN
                RAISE EXCEPTION 'Number counter advanced without an assignment'
                    USING ERRCODE = '23514';
            END IF;
            RETURN NULL;
        END $$
    """)
    op.execute("""
        CREATE CONSTRAINT TRIGGER appointment_counter_assignment_committed
        AFTER INSERT OR UPDATE ON public.appointment_day_counters
        DEFERRABLE INITIALLY DEFERRED
        FOR EACH ROW EXECUTE FUNCTION public.assert_counter_assignment_complete()
    """)
    op.execute("""
        CREATE FUNCTION public.assert_assignment_history_complete() RETURNS trigger
        LANGUAGE plpgsql SET search_path = pg_catalog AS $$
        BEGIN
            IF NOT EXISTS (
                SELECT 1 FROM public.appointment_history h
                WHERE h.organization_id = NEW.organization_id
                  AND h.appointment_id = NEW.appointment_id
                  AND h.new_service_date = NEW.service_date
                  AND h.new_number = NEW.number
            ) THEN
                RAISE EXCEPTION 'Assigned number has no appointment history'
                    USING ERRCODE = '23514';
            END IF;
            RETURN NULL;
        END $$
    """)
    op.execute("""
        CREATE CONSTRAINT TRIGGER appointment_assignment_history_committed
        AFTER INSERT ON public.appointment_number_assignments
        DEFERRABLE INITIALLY DEFERRED
        FOR EACH ROW EXECUTE FUNCTION public.assert_assignment_history_complete()
    """)
    for function in (
        "guard_appointment()",
        "guard_appointment_counter()",
        "guard_appointment_number_insert()",
        "guard_appointment_history_insert()",
        "deny_appointment_evidence_mutation()",
        "assert_appointment_evidence_complete()",
        "assert_counter_assignment_complete()",
        "assert_assignment_history_complete()",
    ):
        op.execute(f"REVOKE ALL ON FUNCTION public.{function} FROM PUBLIC")
    for relation in (
        "appointments",
        "appointment_day_counters",
        "appointment_number_assignments",
        "appointment_price_snapshots",
        "appointment_history",
    ):
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
    op.execute("GRANT UPDATE ON public.appointments TO carepulse_runtime")
    op.execute("GRANT UPDATE ON public.appointment_day_counters TO carepulse_runtime")


def downgrade() -> None:
    raise RuntimeError("D-05 appointment migration is forward-only")

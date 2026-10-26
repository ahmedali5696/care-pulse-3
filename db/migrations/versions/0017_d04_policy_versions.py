"""D-04 policy/schedule persistence; revision 0006 is already occupied.

Forward-only after verified 0016_d03_patient_name_bounds. No D-05 tables.
"""

from alembic import op

revision = "0017_d04_policy_versions"
down_revision = "0016_d03_patient_name_bounds"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
        CREATE FUNCTION public.valid_clinic_policy(kind text, value jsonb)
        RETURNS boolean LANGUAGE plpgsql IMMUTABLE SET search_path = pg_catalog AS $$
        DECLARE amount text;
        BEGIN
            IF jsonb_typeof(value) <> 'object' THEN RETURN false; END IF;
            IF kind = 'PRICE' THEN
                IF NOT value ?& ARRAY['standard_new_exam_egp',
                    'preferential_followup_egp', 'urgent_base_egp', 'urgent_enabled']
                   OR jsonb_typeof(value->'urgent_enabled') <> 'boolean' THEN
                    RETURN false;
                END IF;
                FOREACH amount IN ARRAY ARRAY['standard_new_exam_egp',
                    'preferential_followup_egp','urgent_base_egp'] LOOP
                    IF jsonb_typeof(value->amount) NOT IN ('number', 'null') THEN
                        RETURN false;
                    END IF;
                    IF jsonb_typeof(value->amount) = 'number'
                       AND (value->>amount)::numeric < 0 THEN RETURN false; END IF;
                END LOOP;
                IF (value->>'urgent_enabled')::boolean AND
                    (value->>'urgent_base_egp' IS NULL OR
                     value->>'standard_new_exam_egp' IS NULL OR
                     (value->>'urgent_base_egp')::numeric <=
                     (value->>'standard_new_exam_egp')::numeric) THEN
                    RETURN false;
                END IF;
                RETURN true;
            ELSIF kind = 'SCHEDULE' THEN
                RETURN value ?& ARRAY['booking_mode','duration_minutes',
                    'holiday_booking_enabled','routine_walk_in_enabled',
                    'allow_overlap','no_show_cutoff']
                  AND value->>'booking_mode' IN ('OPEN','EXACT_TIME')
                  AND jsonb_typeof(value->'holiday_booking_enabled') = 'boolean'
                  AND jsonb_typeof(value->'routine_walk_in_enabled') = 'boolean'
                  AND jsonb_typeof(value->'allow_overlap') = 'boolean'
                  AND jsonb_typeof(value->'duration_minutes') IN ('null','number')
                  AND (value->>'duration_minutes' IS NULL OR
                       ((value->>'duration_minutes')::numeric > 0
                        AND (value->>'duration_minutes')::numeric =
                            trunc((value->>'duration_minutes')::numeric)))
                  AND jsonb_typeof(value->'no_show_cutoff') = 'object'
                  AND (value->'no_show_cutoff') ?& ARRAY['enabled','local_time']
                  AND jsonb_typeof(value->'no_show_cutoff'->'enabled') = 'boolean'
                  AND jsonb_typeof(value->'no_show_cutoff'->'local_time') IN
                      ('null','string')
                  AND (value->'no_show_cutoff'->>'local_time' IS NULL OR
                       (value->'no_show_cutoff'->>'local_time') ~
                       '^([01][0-9]|2[0-3]):[0-5][0-9]$');
            ELSIF kind = 'QUEUE' THEN
                RETURN value ?& ARRAY['routine_order','late_depth']
                  AND value->>'routine_order' IN ('BOOKED_NUMBER','ARRIVAL_FIFO')
                  AND jsonb_typeof(value->'late_depth') = 'number'
                  AND (value->>'late_depth')::numeric BETWEEN 0 AND 2147483647
                  AND (value->>'late_depth')::numeric =
                      trunc((value->>'late_depth')::numeric);
            ELSIF kind = 'ADMISSION' THEN
                RETURN value ? 'actors'
                  AND value->>'actors' IN ('BOTH','DOCTOR','RECEPTIONIST');
            ELSIF kind = 'CAPACITY' THEN
                RETURN value ?& ARRAY['capacity','paused']
                  AND jsonb_typeof(value->'paused') = 'boolean'
                  AND jsonb_typeof(value->'capacity') = 'number'
                  AND (value->>'capacity')::numeric BETWEEN 1 AND 2147483647
                  AND (value->>'capacity')::numeric =
                      trunc((value->>'capacity')::numeric);
            ELSIF kind = 'FOLLOWUP' THEN
                RETURN value ?& ARRAY['completion_day_zero','last_day_inclusive']
                  AND value->'completion_day_zero' = 'true'::jsonb
                  AND jsonb_typeof(value->'last_day_inclusive') = 'number'
                  AND (value->>'last_day_inclusive')::numeric BETWEEN 0 AND 365
                  AND (value->>'last_day_inclusive')::numeric =
                      trunc((value->>'last_day_inclusive')::numeric);
            END IF;
            RETURN false;
        END
        $$
    """)
    op.execute("""
        CREATE TABLE public.clinic_policy_versions (
            organization_id uuid NOT NULL REFERENCES public.organizations(id),
            kind text NOT NULL CHECK
                (kind IN ('PRICE','SCHEDULE','QUEUE','ADMISSION','CAPACITY','FOLLOWUP')),
            version bigint NOT NULL CHECK (version >= 1),
            payload jsonb NOT NULL,
            effective_on date,
            pending_effective boolean NOT NULL DEFAULT false,
            created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
            created_by_staff_id uuid,
            PRIMARY KEY (organization_id, kind, version),
            CONSTRAINT clinic_policy_payload_valid CHECK
                (public.valid_clinic_policy(kind, payload) IS TRUE),
            CHECK (kind = 'QUEUE' OR
                   (effective_on IS NULL AND NOT pending_effective)),
            CHECK (kind <> 'QUEUE' OR NOT pending_effective OR effective_on IS NULL),
            CHECK (kind <> 'QUEUE' OR pending_effective OR
                   effective_on IS NOT NULL OR version = 1),
            FOREIGN KEY (organization_id, created_by_staff_id)
                REFERENCES public.staff_accounts(organization_id, id)
        )
    """)
    op.execute("""
        CREATE TABLE public.clinic_policy_heads (
            organization_id uuid NOT NULL,
            kind text NOT NULL,
            current_version bigint NOT NULL CHECK (current_version >= 1),
            revision bigint NOT NULL CHECK (revision >= 1),
            queue_effective_on date,
            queue_pending boolean NOT NULL DEFAULT false,
            updated_at timestamptz NOT NULL DEFAULT clock_timestamp(),
            PRIMARY KEY (organization_id, kind),
            FOREIGN KEY (organization_id, kind, current_version)
                REFERENCES public.clinic_policy_versions
                    (organization_id, kind, version),
            CHECK (kind = 'QUEUE' OR
                   (queue_effective_on IS NULL AND NOT queue_pending)),
            CHECK (kind <> 'QUEUE' OR queue_pending OR
                   queue_effective_on IS NOT NULL OR current_version = 1),
            CHECK (kind <> 'QUEUE' OR NOT queue_pending OR queue_effective_on IS NULL)
        )
    """)
    op.execute("""
        CREATE TABLE public.clinic_schedule_intervals (
            organization_id uuid NOT NULL,
            policy_kind text NOT NULL DEFAULT 'SCHEDULE'
                CHECK (policy_kind = 'SCHEDULE'),
            policy_version bigint NOT NULL,
            interval_kind text NOT NULL CHECK
                (interval_kind IN ('WORK', 'BREAK', 'OVERRIDE_WORK', 'MANUAL_SLOT')),
            weekday smallint,
            service_date date,
            starts_at time NOT NULL,
            ends_at time NOT NULL,
            FOREIGN KEY (organization_id, policy_kind, policy_version)
                REFERENCES public.clinic_policy_versions
                    (organization_id, kind, version),
            CHECK (starts_at < ends_at),
            CHECK ((interval_kind IN ('WORK','BREAK') AND
                    weekday IS NOT NULL AND weekday BETWEEN 1 AND 7
                    AND service_date IS NULL)
                   OR (interval_kind IN ('OVERRIDE_WORK','MANUAL_SLOT') AND
                       weekday IS NULL AND service_date IS NOT NULL
                       AND isfinite(service_date))),
            UNIQUE NULLS NOT DISTINCT (organization_id, policy_kind, policy_version,
                    interval_kind, weekday, service_date, starts_at, ends_at)
        )
    """)
    op.execute("""
        CREATE TABLE public.clinic_schedule_holidays (
            organization_id uuid NOT NULL,
            policy_kind text NOT NULL DEFAULT 'SCHEDULE'
                CHECK (policy_kind = 'SCHEDULE'),
            policy_version bigint NOT NULL,
            service_date date NOT NULL CHECK (isfinite(service_date)),
            PRIMARY KEY (organization_id, policy_version, service_date),
            FOREIGN KEY (organization_id, policy_kind, policy_version)
                REFERENCES public.clinic_policy_versions
                    (organization_id, kind, version)
        )
    """)
    op.execute("""
        CREATE TABLE public.clinic_policy_previews (
            id uuid PRIMARY KEY,
            organization_id uuid NOT NULL,
            policy_kind text NOT NULL,
            policy_version bigint NOT NULL,
            policy_revision bigint NOT NULL CHECK (policy_revision > 0),
            service_day_revision bigint NOT NULL DEFAULT 0 CHECK
                (service_day_revision >= 0),
            actor_staff_id uuid NOT NULL,
            service_date date,
            selected_refs jsonb NOT NULL CHECK (jsonb_typeof(selected_refs) = 'array'),
            affected_refs jsonb NOT NULL CHECK (jsonb_typeof(affected_refs) = 'array'),
            record_revisions jsonb NOT NULL CHECK
                (jsonb_typeof(record_revisions) = 'object'),
            candidate_payload jsonb NOT NULL CHECK
                (jsonb_typeof(candidate_payload) = 'object'),
            created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
            FOREIGN KEY (organization_id, policy_kind, policy_version)
                REFERENCES public.clinic_policy_versions
                    (organization_id, kind, version),
            FOREIGN KEY (organization_id, actor_staff_id)
                REFERENCES public.staff_accounts(organization_id, id)
        )
    """)
    # Schedule children belong only to the next unpublished version. Lock the
    # head to serialize child writes with publishing a version and forbid
    # backdating an interval or holiday into a pinned historical version.
    op.execute("""
        CREATE FUNCTION public.guard_schedule_draft_insert() RETURNS trigger
        LANGUAGE plpgsql SET search_path = pg_catalog AS $$
        DECLARE published bigint;
        BEGIN
            SELECT current_version INTO published
            FROM public.clinic_policy_heads
            WHERE organization_id = NEW.organization_id AND kind = 'SCHEDULE'
            FOR UPDATE;
            IF published IS NULL OR NEW.policy_version <> published + 1 THEN
                RAISE EXCEPTION 'Schedule version is already published'
                    USING ERRCODE = '23514';
            END IF;
            RETURN NEW;
        END $$
    """)
    for table in ("clinic_schedule_intervals", "clinic_schedule_holidays"):
        op.execute(f"""
            CREATE TRIGGER {table}_draft
            BEFORE INSERT ON public.{table}
            FOR EACH ROW EXECUTE FUNCTION public.guard_schedule_draft_insert()
        """)
    op.execute("""
        CREATE FUNCTION public.deny_policy_history_mutation() RETURNS trigger
        LANGUAGE plpgsql SET search_path = pg_catalog AS $$
        BEGIN
            RAISE EXCEPTION 'Immutable policy history' USING ERRCODE = '23514';
        END $$
    """)
    for table in (
        "clinic_policy_versions",
        "clinic_schedule_intervals",
        "clinic_schedule_holidays",
        "clinic_policy_previews",
    ):
        op.execute(f"""
            CREATE TRIGGER {table}_immutable
            BEFORE UPDATE OR DELETE ON public.{table}
            FOR EACH ROW EXECUTE FUNCTION public.deny_policy_history_mutation()
        """)
    op.execute("""
        CREATE FUNCTION public.guard_clinic_policy_head() RETURNS trigger
        LANGUAGE plpgsql SET search_path = pg_catalog AS $$
        BEGIN
            IF TG_OP = 'DELETE' THEN
                RAISE EXCEPTION 'Policy head deletion forbidden' USING ERRCODE = '23514';
            END IF;
            IF TG_OP = 'INSERT' THEN
                IF NEW.current_version <> 1 OR NEW.revision <> 1 THEN
                    RAISE EXCEPTION 'Invalid initial policy head' USING ERRCODE = '23514';
                END IF;
            ELSE
                IF (NEW.organization_id, NEW.kind) IS DISTINCT FROM
                   (OLD.organization_id, OLD.kind) OR
                   NEW.revision <> OLD.revision + 1 OR
                   NEW.current_version <> OLD.current_version + 1 THEN
                    RAISE EXCEPTION 'Invalid policy revision' USING ERRCODE = '40001';
                END IF;
            END IF;
            IF NOT EXISTS (
                SELECT 1 FROM public.clinic_policy_versions v
                WHERE v.organization_id = NEW.organization_id AND v.kind = NEW.kind
                  AND v.version = NEW.current_version
                  AND v.effective_on IS NOT DISTINCT FROM NEW.queue_effective_on
                  AND v.pending_effective = NEW.queue_pending
            ) THEN
                RAISE EXCEPTION 'Policy effectivity mismatch' USING ERRCODE = '23514';
            END IF;
            NEW.updated_at := clock_timestamp();
            RETURN NEW;
        END $$
    """)
    op.execute("""
        CREATE TRIGGER clinic_policy_heads_revision
        BEFORE INSERT OR UPDATE OR DELETE ON public.clinic_policy_heads
        FOR EACH ROW EXECUTE FUNCTION public.guard_clinic_policy_head()
    """)
    for table in (
        "clinic_policy_versions",
        "clinic_policy_heads",
        "clinic_schedule_intervals",
        "clinic_schedule_holidays",
        "clinic_policy_previews",
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
        op.execute(f"GRANT SELECT, INSERT ON public.{table} TO carepulse_runtime")
    op.execute("GRANT UPDATE ON public.clinic_policy_heads TO carepulse_runtime")
    op.execute("GRANT SELECT ON public.organizations TO carepulse_policy_seed")
    op.execute("""
        CREATE POLICY organization_policy_seed_read ON public.organizations
        FOR SELECT TO carepulse_policy_seed USING (true)
    """)
    for table in ("clinic_policy_versions", "clinic_policy_heads"):
        op.execute(f"""
            CREATE POLICY {table}_seed ON public.{table}
            FOR ALL TO carepulse_policy_seed USING (true) WITH CHECK (true)
        """)
        op.execute(f"GRANT SELECT, INSERT ON public.{table} TO carepulse_policy_seed")

    # JSON object colons are literal SQL, not SQLAlchemy named bind markers.
    # Use the driver-level execution boundary for this trusted fixed DDL.
    op.get_bind().exec_driver_sql("""
        CREATE FUNCTION public.seed_clinic_policy_defaults(target uuid)
        RETURNS void LANGUAGE plpgsql SECURITY DEFINER
        SET search_path = pg_catalog AS $$
        BEGIN
            PERFORM 1 FROM public.organizations WHERE id = target;
            IF NOT FOUND THEN
                RAISE EXCEPTION 'Unknown policy tenant' USING ERRCODE = '23503';
            END IF;
            INSERT INTO public.clinic_policy_versions
                (organization_id, kind, version, payload)
            SELECT target, kind, 1, payload FROM (VALUES
                ('PRICE', '{"standard_new_exam_egp":null,"preferential_followup_egp":null,"urgent_base_egp":null,"urgent_enabled":false}'::jsonb),
                ('SCHEDULE', '{"booking_mode":"OPEN","duration_minutes":null,"holiday_booking_enabled":false,"routine_walk_in_enabled":false,"allow_overlap":false,"no_show_cutoff":{"enabled":false,"local_time":null}}'::jsonb),
                ('QUEUE', '{"routine_order":"BOOKED_NUMBER","late_depth":5}'::jsonb),
                ('ADMISSION', '{"actors":"BOTH"}'::jsonb),
                ('CAPACITY', '{"capacity":1,"paused":false}'::jsonb),
                ('FOLLOWUP', '{"completion_day_zero":true,"last_day_inclusive":7}'::jsonb)
            ) AS seed(kind,payload)
            ON CONFLICT (organization_id, kind, version) DO NOTHING;
            INSERT INTO public.clinic_policy_heads
                (organization_id,kind,current_version,revision,queue_pending)
            SELECT organization_id,kind,1,1,false
            FROM public.clinic_policy_versions WHERE organization_id = target
              AND version = 1
            ON CONFLICT (organization_id,kind) DO NOTHING;
        END $$
    """)
    op.execute("""
        CREATE FUNCTION public.seed_policy_on_organization_insert()
        RETURNS trigger LANGUAGE plpgsql SECURITY DEFINER
        SET search_path = pg_catalog AS $$
        BEGIN
            PERFORM public.seed_clinic_policy_defaults(NEW.id);
            RETURN NEW;
        END $$
    """)
    # A NOLOGIN role owns both SECURITY DEFINER entry points. Migration role
    # can administer its own owned schema; CREATE is granted only for ownership
    # transfer, then immediately revoked.
    op.execute("GRANT USAGE, CREATE ON SCHEMA public TO carepulse_policy_seed")
    op.execute("""
        ALTER FUNCTION public.seed_clinic_policy_defaults(uuid)
        OWNER TO carepulse_policy_seed
    """)
    op.execute("""
        ALTER FUNCTION public.seed_policy_on_organization_insert()
        OWNER TO carepulse_policy_seed
    """)
    op.execute("REVOKE CREATE ON SCHEMA public FROM carepulse_policy_seed")
    for function in (
        "seed_clinic_policy_defaults(uuid)",
        "seed_policy_on_organization_insert()",
    ):
        op.execute(f"REVOKE ALL ON FUNCTION public.{function} FROM PUBLIC")
    op.execute("""
        GRANT EXECUTE ON FUNCTION public.seed_clinic_policy_defaults(uuid)
        TO carepulse_migration
    """)
    op.execute("""
        GRANT EXECUTE ON FUNCTION public.seed_policy_on_organization_insert()
        TO carepulse_migration
    """)
    op.execute("""
        CREATE TRIGGER organizations_policy_defaults
        AFTER INSERT ON public.organizations
        FOR EACH ROW EXECUTE FUNCTION public.seed_policy_on_organization_insert()
    """)
    op.execute("""
        REVOKE EXECUTE ON FUNCTION public.seed_policy_on_organization_insert()
        FROM carepulse_migration
    """)
    # Same transaction as trigger installation, and CREATE TRIGGER locks the
    # parent against concurrent inserts until commit.
    op.execute("""
        SELECT public.seed_clinic_policy_defaults(id)
        FROM public.organizations
    """)
    for function in (
        "valid_clinic_policy(text,jsonb)",
        "deny_policy_history_mutation()",
        "guard_clinic_policy_head()",
        "guard_schedule_draft_insert()",
    ):
        op.execute(f"REVOKE ALL ON FUNCTION public.{function} FROM PUBLIC")
    op.execute("""
        GRANT EXECUTE ON FUNCTION public.valid_clinic_policy(text,jsonb)
        TO carepulse_runtime, carepulse_policy_seed
    """)


def downgrade() -> None:
    raise RuntimeError("D-04 policy versions migration is forward-only")

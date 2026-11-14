"""F-01: replace shared runtime authority with operation-scoped API policies."""

from alembic import op

revision = "0042_f01_effective_grants"
down_revision = "0041_f01_runtime_identity"
branch_labels = None
depends_on = None


def _context(predicate: str) -> str:
    return f"EXISTS (SELECT 1 FROM public.f01_context() c WHERE {predicate})"


def _tenant(table: str, predicate: str) -> str:
    return _context(
        f"c.organization_id = {table}.organization_id::text AND ({predicate})"
    )


def _policy(
    table: str,
    read: str,
    insert: str | None = None,
    update: str | None = None,
    lock: bool = False,
) -> None:
    # UPDATE USING is also necessary for SELECT FOR UPDATE. A false WITH CHECK
    # permits the lock, without permitting a new row state.
    op.execute(
        f"CREATE POLICY f01_api_read ON public.{table} FOR SELECT TO carepulse_api USING ({read})"
    )
    if insert is not None:
        op.execute(
            f"CREATE POLICY f01_api_insert ON public.{table} FOR INSERT TO carepulse_api WITH CHECK ({insert})"
        )
    if lock or update is not None:
        op.execute(
            f"CREATE POLICY f01_api_update ON public.{table} FOR UPDATE TO carepulse_api USING ({read}) WITH CHECK ({update or 'false'})"
        )


def upgrade() -> None:
    # Existing auth/sender/operator and migration policies are independent.
    # Remove only the legacy runtime policies, including explicit column ACLs.
    op.execute("""
        DO $$ DECLARE item record; BEGIN
          FOR item IN SELECT schemaname, tablename, policyname FROM pg_policies
            WHERE schemaname = 'public' AND roles = ARRAY['carepulse_runtime']::name[]
          LOOP EXECUTE format('DROP POLICY %I ON %I.%I', item.policyname,
            item.schemaname, item.tablename); END LOOP;
          FOR item IN SELECT n.nspname, cl.relname, a.attname
            FROM pg_attribute a JOIN pg_class cl ON cl.oid=a.attrelid
            JOIN pg_namespace n ON n.oid=cl.relnamespace
            CROSS JOIN LATERAL aclexplode(a.attacl) acl
            WHERE n.nspname='public' AND acl.grantee='carepulse_runtime'::regrole
          LOOP EXECUTE format('REVOKE ALL (%I) ON %I.%I FROM carepulse_runtime',
            item.attname,item.nspname,item.relname); END LOOP;
        END $$;
        REVOKE ALL ON ALL TABLES IN SCHEMA public FROM carepulse_runtime;
        REVOKE ALL ON ALL SEQUENCES IN SCHEMA public FROM carepulse_runtime;
        DO $$ DECLARE item record; remaining text; BEGIN
          FOR item IN SELECT p.oid::regprocedure AS signature, r.rolname AS owner
            FROM pg_proc p JOIN pg_namespace n ON n.oid=p.pronamespace
            JOIN pg_roles r ON r.oid=p.proowner
            CROSS JOIN LATERAL aclexplode(p.proacl) acl
            WHERE n.nspname='public' AND acl.grantee='carepulse_runtime'::regrole
              AND p.prokind IN ('f','w')
          LOOP
            EXECUTE format('SET LOCAL ROLE %I', item.owner);
            EXECUTE format('REVOKE ALL ON FUNCTION %s FROM carepulse_runtime', item.signature);
            RESET ROLE;
          END LOOP;
          SELECT string_agg(format('%s owner=%s acl=%s', p.oid::regprocedure,
            p.proowner::regrole, COALESCE(p.proacl,acldefault('f',p.proowner))), '; ' ORDER BY p.oid)
          INTO remaining FROM pg_proc p JOIN pg_namespace n ON n.oid=p.pronamespace
          WHERE n.nspname='public' AND has_function_privilege('carepulse_runtime',p.oid,'EXECUTE');
          IF remaining IS NOT NULL THEN
            RAISE EXCEPTION 'Legacy runtime retains public routine execution: %', remaining;
          END IF;
        END $$;
        REVOKE USAGE ON SCHEMA public FROM carepulse_runtime;
        GRANT USAGE ON SCHEMA public TO carepulse_api, carepulse_web, carepulse_worker;
        GRANT SELECT ON public.alembic_version TO carepulse_api, carepulse_web, carepulse_worker;
        GRANT SELECT, INSERT, UPDATE ON public.auth_login_attempts TO carepulse_web;
        GRANT EXECUTE ON FUNCTION public.valid_clinic_policy(text,jsonb),
            public.t145_percent_valid(text),
            public.t145_decimal_compare_rational(text,numeric,numeric),
            public.t145_percent_net_matches(numeric,numeric,text) TO carepulse_api;
    """)
    primary = "c.stage = 'PRIMARY'"
    booking = "c.purpose = 'APPOINTMENT_BOOKING'"
    visit = "c.purpose = 'VISIT_FOLLOWUP'"
    recovery = "c.purpose = 'OWNER_RECOVERY' AND c.stage = 'PRIMARY'"
    accept = "c.purpose = 'RECEPTIONIST_ACCEPT' AND c.stage = 'PRIMARY'"
    staff = "c.purpose = 'STAFF_MANAGEMENT' AND c.role = 'DOCTOR_OWNER'"
    active_staff = "(c.role='DOCTOR_OWNER' OR (c.role='RECEPTIONIST' AND EXISTS (SELECT 1 FROM public.staff_accounts a WHERE a.id::text=c.staff_id AND a.operational_manager)))"
    operational = f"{booking} OR {visit} OR c.purpose IN ('SETTINGS_PRICING','SETTINGS_SCHEDULE','SLOTS')"
    op.execute("GRANT SELECT ON public.organizations TO carepulse_api")
    _policy("organizations", _context("c.organization_id = organizations.id::text"))
    staff_read = _tenant(
        "staff_accounts",
        f"staff_accounts.id::text=c.staff_id OR ({accept} AND staff_accounts.id=c.subject_staff_id) OR ({staff} AND staff_accounts.role='RECEPTIONIST' AND (c.target_id IS NULL OR staff_accounts.id=c.target_id)) OR (({booking} OR {visit}) AND staff_accounts.role='DOCTOR_OWNER')",
    )
    staff_write = _tenant(
        "staff_accounts",
        f"({staff} AND c.method='POST' AND staff_accounts.role='RECEPTIONIST' AND staff_accounts.id=c.target_id AND ((c.stage='PRIMARY' AND ((c.path LIKE '%/enable' AND staff_accounts.enabled) OR (c.path LIKE '%/disable' AND NOT staff_accounts.enabled) OR c.path LIKE '%/revoke-invite')) OR (c.stage='INVITATION_APPLY' AND c.path LIKE '%/invite'))) OR ({accept} AND staff_accounts.id=c.subject_staff_id AND staff_accounts.role='RECEPTIONIST' AND staff_accounts.verified) OR ({recovery} AND c.path='/v1/staff/recovery/reserve' AND staff_accounts.id::text=c.staff_id AND staff_accounts.role='DOCTOR_OWNER')",
    )
    _policy(
        "staff_accounts",
        staff_read,
        _tenant(
            "staff_accounts",
            f"{staff} AND {primary} AND c.method='POST' AND c.path='/v1/staff/receptionists' AND staff_accounts.role='RECEPTIONIST' AND staff_accounts.auth_user_id=c.native_user_id",
        ),
        staff_write,
        True,
    )
    op.execute("GRANT SELECT, INSERT ON public.staff_accounts TO carepulse_api")
    op.execute(
        "GRANT UPDATE (enabled, verified, security_revision, reset_block_operation_id, updated_at) ON public.staff_accounts TO carepulse_api"
    )
    invite_read = _tenant(
        "invitations",
        f"({staff} AND invitations.purpose='RECEPTIONIST' AND (c.target_id IS NULL OR invitations.staff_id=c.target_id)) OR ({accept} AND invitations.id=c.target_id)",
    )
    invite_write = _tenant(
        "invitations",
        f"({staff} AND invitations.purpose='RECEPTIONIST' AND invitations.staff_id=c.target_id AND c.method='POST' AND invitations.state='REVOKED' AND ((c.stage='PRIMARY' AND (c.path LIKE '%/disable' OR c.path LIKE '%/revoke-invite')) OR (c.stage='INVITATION_APPLY' AND c.path LIKE '%/invite'))) OR ({accept} AND invitations.id=c.target_id AND invitations.state='ACCEPTED' AND invitations.accepted_by_user_id=c.native_user_id)",
    )
    invite_insert = _tenant(
        "invitations",
        f"{staff} AND c.stage='INVITATION_APPLY' AND c.method='POST' AND c.path LIKE '%/invite' AND invitations.purpose='RECEPTIONIST' AND invitations.staff_id=c.target_id AND invitations.state='ISSUED' AND invitations.issued_by_staff_id::text=c.staff_id",
    )
    _policy("invitations", invite_read, invite_insert, invite_write, True)
    op.execute("GRANT SELECT, INSERT, UPDATE ON public.invitations TO carepulse_api")
    # Native credential data remains under its existing service login. API
    # reads only identity evidence for its bound actor or prepared resource.
    for table in ('"user"', "verification"):
        op.execute(
            f"ALTER TABLE public.{table} ENABLE ROW LEVEL SECURITY; ALTER TABLE public.{table} FORCE ROW LEVEL SECURITY"
        )
        op.execute(
            f"CREATE POLICY f01_native_auth ON public.{table} FOR ALL TO carepulse_auth, carepulse_migration USING (true) WITH CHECK (true)"
        )
        op.execute(
            f"CREATE POLICY f01_native_verifier ON public.{table} FOR ALL TO carepulse_identity_owner USING (true) WITH CHECK (false)"
        )
    _policy(
        '"user"',
        _context(
            """(c.native_user_id = "user".id AND c.purpose IN ('STAFF_MANAGEMENT','OWNER_RECOVERY','RECEPTIONIST_ACCEPT')) OR EXISTS (SELECT 1 FROM public.staff_accounts s WHERE s.auth_user_id="user".id AND s.id::text=c.staff_id)"""
        ),
    )
    op.execute(
        'GRANT SELECT (id, name, email, "emailVerified") ON public."user" TO carepulse_api'
    )
    _policy(
        "verification",
        _context(
            f"{recovery} AND EXISTS (SELECT 1 FROM public.delivery_jobs j WHERE j.verification_id=verification.id AND j.staff_id::text=c.staff_id AND j.proof_reference=c.proof_digest)"
        ),
    )
    op.execute(
        'GRANT SELECT (id, identifier, value, "expiresAt") ON public.verification TO carepulse_api'
    )
    # The sender retains its existing narrow proof-expiry read, scoped to
    # the tenant's password-reset delivery. FORCE RLS otherwise hides it.
    op.execute("""
        CREATE POLICY f01_verification_sender_read ON public.verification
        FOR SELECT TO carepulse_sender USING (
            EXISTS (SELECT 1 FROM public.delivery_jobs j
                WHERE j.verification_id=verification.id AND j.purpose='PASSWORD_RESET'
                AND j.organization_id=NULLIF(current_setting('app.organization_id', true), '')::uuid)
        )
    """)
    rec = _tenant(
        "recovery_operations",
        f"{recovery} AND recovery_operations.staff_id::text=c.staff_id",
    )
    _policy(
        "recovery_operations",
        rec,
        _tenant(
            "recovery_operations",
            f"{recovery} AND c.path='/v1/staff/recovery/reserve' AND recovery_operations.staff_id::text=c.staff_id AND recovery_operations.auth_user_id=c.native_user_id AND recovery_operations.proof_digest=c.proof_digest AND recovery_operations.purpose='PASSWORD_RESET'",
        ),
    )
    op.execute("GRANT SELECT, INSERT ON public.recovery_operations TO carepulse_api")
    _policy(
        "auth_recovery_completions",
        "EXISTS (SELECT 1 FROM public.recovery_operations r WHERE r.id=auth_recovery_completions.id)",
    )
    op.execute("GRANT SELECT ON public.auth_recovery_completions TO carepulse_api")

    patient = _tenant(
        "patients",
        f"(c.purpose='PATIENT_REGISTRY' AND (c.target_id IS NULL OR patients.id=c.target_id)) OR ({booking} AND (c.patient_id IS NULL OR patients.id=c.patient_id)) OR {visit}",
    )
    mutation = _tenant(
        "patients",
        f"c.purpose='PATIENT_REGISTRY' AND {primary} AND c.method<>'GET' AND patients.id=c.target_id",
    )
    _policy(
        "patients",
        patient,
        _tenant(
            "patients",
            f"c.purpose='PATIENT_REGISTRY' AND {primary} AND c.method='POST' AND c.path='/v1/patients'",
        ),
        mutation,
        True,
    )
    op.execute("GRANT SELECT, INSERT, UPDATE ON public.patients TO carepulse_api")
    # RLS checks the resulting row only; the historical lifecycle trigger
    # validates revisions but cannot distinguish signed demographic/archive routes.
    op.execute("""
        CREATE FUNCTION public.f01_guard_patient_operation() RETURNS trigger
        LANGUAGE plpgsql SET search_path = pg_catalog AS $$
        DECLARE ctx record;
        BEGIN
            IF session_user <> 'carepulse_api' THEN RETURN NEW; END IF;
            SELECT * INTO ctx FROM public.f01_context();
            IF NOT FOUND OR ctx.purpose <> 'PATIENT_REGISTRY' OR ctx.stage <> 'PRIMARY'
               OR ctx.organization_id IS DISTINCT FROM NEW.organization_id::text THEN
                RAISE EXCEPTION 'Patient operation scope denied' USING ERRCODE = '42501';
            END IF;
            IF TG_OP = 'INSERT' THEN
                IF ctx.method = 'POST' AND ctx.path = '/v1/patients'
                   AND NOT NEW.archived AND NEW.archived_at IS NULL
                   AND NEW.demographic_revision = 0 AND NEW.lifecycle_revision = 0
                   AND NEW.created_at = transaction_timestamp()
                   AND NEW.updated_at = NEW.created_at THEN RETURN NEW; END IF;
            ELSIF ctx.target_id = OLD.id AND ctx.target_id = NEW.id THEN
                IF ctx.method = 'PATCH'
                   AND (NEW.archived,NEW.archived_at,NEW.lifecycle_revision,NEW.updated_at)
                       IS NOT DISTINCT FROM
                       (OLD.archived,OLD.archived_at,OLD.lifecycle_revision,OLD.updated_at)
                THEN RETURN NEW;
                ELSIF ctx.method = 'POST' AND ctx.role = 'DOCTOR_OWNER'
                   AND ((ctx.path LIKE '%/archive' AND NOT OLD.archived AND NEW.archived)
                     OR (ctx.path LIKE '%/unarchive' AND OLD.archived AND NOT NEW.archived))
                   AND NEW.lifecycle_revision = OLD.lifecycle_revision + 1
                   AND (to_jsonb(NEW) - 'archived' - 'lifecycle_revision')
                       = (to_jsonb(OLD) - 'archived' - 'lifecycle_revision')
                THEN RETURN NEW;
                END IF;
            END IF;
            RAISE EXCEPTION 'Patient operation scope denied' USING ERRCODE = '42501';
        END $$;
        REVOKE ALL ON FUNCTION public.f01_guard_patient_operation() FROM PUBLIC;
        CREATE TRIGGER patients_f01_operation_guard BEFORE INSERT OR UPDATE ON public.patients
        FOR EACH ROW EXECUTE FUNCTION public.f01_guard_patient_operation();
    """)

    # Settings locks remain available to booking/qualification; writes are
    # confined to their exact signed settings route and primary transaction.
    for table in (
        "clinic_policy_versions",
        "clinic_policy_heads",
        "clinic_schedule_intervals",
        "clinic_schedule_holidays",
        "clinic_policy_previews",
    ):
        read = _tenant(
            table,
            "c.purpose='SETTINGS_SCHEDULE' AND clinic_policy_previews.actor_staff_id::text=c.staff_id"
            if table == "clinic_policy_previews"
            else operational,
        )
        kind = (
            f"((c.path='/v1/settings/pricing' AND c.method='PATCH' AND {table}.kind IN ('PRICE','FOLLOWUP')) OR (c.path='/v1/settings/schedule/activate' AND c.method='POST' AND {table}.kind='SCHEDULE'))"
            if table in ("clinic_policy_versions", "clinic_policy_heads")
            else "(c.path='/v1/settings/schedule' AND c.method='PATCH' OR c.path='/v1/settings/schedule/preview' AND c.method='POST') AND clinic_policy_previews.policy_kind='SCHEDULE'"
            if table == "clinic_policy_previews"
            else "c.path='/v1/settings/schedule/activate' AND c.method='POST'"
        )
        actor = (
            f" AND {table}.created_by_staff_id::text=c.staff_id"
            if table == "clinic_policy_versions"
            else f" AND {table}.actor_staff_id::text=c.staff_id AND {table}.actor_security_revision=c.security_revision"
            if table == "clinic_policy_previews"
            else ""
        )
        write = _tenant(
            table,
            f"{primary} AND {active_staff} AND c.method <> 'GET' AND {kind}{actor}",
        )
        _policy(table, read, write, write if table == "clinic_policy_heads" else None)
        op.execute(f"GRANT SELECT, INSERT ON public.{table} TO carepulse_api")
    op.execute("GRANT UPDATE ON public.clinic_policy_heads TO carepulse_api")

    appointment_read = f"{booking} OR {visit} OR c.purpose IN ('SLOTS','SETTINGS_SCHEDULE','SETTINGS_PRICING')"
    booking_write = f"{booking} AND {primary} AND c.method='POST'"
    for table in (
        "appointments",
        "appointment_day_counters",
        "appointment_number_assignments",
        "appointment_price_snapshots",
        "appointment_history",
    ):
        read = _tenant(table, appointment_read)
        write = _tenant(
            table,
            booking_write
            + (
                " AND appointments.patient_id=c.patient_id"
                if table == "appointments"
                else ""
            ),
        )
        _policy(
            table,
            read,
            write,
            write if table == "appointment_day_counters" else None,
            table == "appointments",
        )
        op.execute(f"GRANT SELECT, INSERT ON public.{table} TO carepulse_api")
    op.execute("GRANT UPDATE (id) ON public.appointments TO carepulse_api")
    op.execute("GRANT UPDATE ON public.appointment_day_counters TO carepulse_api")

    for table in (
        "visits",
        "completion_attestations",
        "followup_claims",
        "followup_extensions",
        "followup_source_state",
        "followup_source_decision_events",
        "followup_authorization_events",
    ):
        _policy(table, _tenant(table, f"{booking} OR {visit}"), lock=table == "visits")
    op.execute(
        "GRANT SELECT (id, organization_id, appointment_id, patient_id, doctor_staff_id, service_date, clinical_revision) ON public.visits TO carepulse_api"
    )
    op.execute("GRANT UPDATE (id) ON public.visits TO carepulse_api")
    for table in (
        "completion_attestations",
        "followup_claims",
        "followup_extensions",
        "followup_source_state",
        "followup_source_decision_events",
        "followup_authorization_events",
    ):
        op.execute(f"GRANT SELECT ON public.{table} TO carepulse_api")

    # The receipt namespace is the signed actor, route and idempotency key.
    receipt = _tenant(
        "idempotency_receipts",
        """idempotency_receipts.scope_kind='TENANT' AND idempotency_receipts.actor_kind='STAFF'
        AND idempotency_receipts.staff_id::text=c.staff_id AND idempotency_receipts.actor_id::text=c.staff_id
        AND idempotency_receipts.idempotency_key=c.idempotency_key AND c.stage IN ('PRIMARY','RECEIPT_RECOVERY')
        AND idempotency_receipts.operation=CASE
          WHEN c.path='/v1/patients' AND c.method='POST' THEN 'patients.create'
          WHEN c.path='/v1/appointments' AND c.method='POST' THEN 'appointments.book'
          WHEN c.path='/v1/staff/receptionists' AND c.method='POST' THEN 'staff.create'
          WHEN c.path='/v1/settings/pricing' AND c.method='PATCH' THEN 'settings.pricing.update'
          WHEN c.path='/v1/settings/schedule' AND c.method='PATCH' THEN 'settings.schedule.stage'
          WHEN c.path='/v1/settings/schedule/preview' AND c.method='POST' THEN 'settings.schedule.preview'
          WHEN c.path='/v1/settings/schedule/activate' AND c.method='POST' THEN 'settings.schedule.activate'
          WHEN c.purpose='VISIT_FOLLOWUP' AND c.method='PATCH' THEN 'visits.followup_decision'
          WHEN c.purpose='VISIT_FOLLOWUP' AND c.method='POST' THEN 'visits.extend_followup' END""",
    )
    _policy("idempotency_receipts", receipt, receipt + " AND " + _context(primary))
    op.execute("GRANT SELECT, INSERT ON public.idempotency_receipts TO carepulse_api")
    audit_actor = _tenant(
        "sensitive_audit",
        f"c.stage IN ('PRIMARY','INVITATION_APPLY') AND ((actor_staff_id::text=c.staff_id AND actor_role=c.role AND c.role IN ('DOCTOR_OWNER','RECEPTIONIST')) OR ({accept} AND actor_staff_id=c.subject_staff_id AND actor_role='RECEPTIONIST') OR ({recovery} AND actor_staff_id::text=c.staff_id AND actor_role='SYSTEM'))",
    )
    audit_action = _context("""(c.purpose='PATIENT_REGISTRY' AND ((c.method='POST' AND c.path='/v1/patients' AND sensitive_audit.action='patients.create')
        OR (c.method='PATCH' AND sensitive_audit.action='patients.update')
        OR (c.method='POST' AND c.path LIKE '%/archive' AND sensitive_audit.action='patients.archive')
        OR (c.method='POST' AND c.path LIKE '%/unarchive' AND sensitive_audit.action='patients.unarchive')))
        OR (c.purpose='APPOINTMENT_BOOKING' AND c.method='POST' AND sensitive_audit.action='appointments.book')
        OR (c.purpose='SETTINGS_PRICING' AND c.method='PATCH' AND sensitive_audit.action='settings.pricing.update')
        OR (c.purpose='SETTINGS_SCHEDULE' AND c.path='/v1/settings/schedule/activate' AND sensitive_audit.action='settings.schedule.activate')
        OR (c.purpose='STAFF_MANAGEMENT' AND c.method<>'GET' AND sensitive_audit.action IN ('RECEPTIONIST_CREATED','RECEPTIONIST_ENABLE','RECEPTIONIST_DISABLE','RECEPTIONIST_INVITE_ISSUED','RECEPTIONIST_INVITE_REVOKED'))
        OR (c.purpose='RECEPTIONIST_ACCEPT' AND sensitive_audit.action='RECEPTIONIST_INVITE_ACCEPTED')
        OR (c.purpose='OWNER_RECOVERY' AND c.path='/v1/staff/recovery/reserve' AND sensitive_audit.action='PASSWORD_RESET_RESERVED')""")
    _policy(
        "sensitive_audit",
        _tenant(
            "sensitive_audit",
            f"{recovery} AND ((sensitive_audit.actor_staff_id::text=c.staff_id AND sensitive_audit.action IN ('PASSWORD_RESET_RESERVED','PASSWORD_RESET_SUCCEEDED')) OR (sensitive_audit.actor_role='OPERATOR' AND sensitive_audit.action='RECOVERY_AUTH_INVALIDATED' AND EXISTS (SELECT 1 FROM public.recovery_operations r WHERE r.id=sensitive_audit.recovery_operation_id AND r.staff_id::text=c.staff_id)))",
        ),
        audit_actor + " AND operator_id IS NULL AND " + audit_action,
    )
    op.execute("GRANT SELECT, INSERT ON public.sensitive_audit TO carepulse_api")
    outbox = _tenant(
        "outbox_events",
        booking_write
        + " AND outbox_events.event_type='APPOINTMENT_BOOKED' AND outbox_events.aggregate_type='APPOINTMENT' AND EXISTS (SELECT 1 FROM public.appointments a WHERE a.id::text=outbox_events.aggregate_id AND a.organization_id=outbox_events.organization_id AND a.patient_id=c.patient_id)",
    )
    _policy("outbox_events", outbox, outbox)
    op.execute("GRANT SELECT, INSERT ON public.outbox_events TO carepulse_api")
    delivery = _tenant(
        "delivery_jobs",
        f"({staff} AND delivery_jobs.staff_id=c.target_id AND delivery_jobs.purpose='RECEPTIONIST') OR ({recovery} AND delivery_jobs.staff_id::text=c.staff_id AND delivery_jobs.purpose='PASSWORD_RESET')",
    )
    delivery_write = _tenant(
        "delivery_jobs",
        f"{staff} AND c.stage='INVITATION_APPLY' AND c.method='POST' AND c.path LIKE '%/invite' AND delivery_jobs.staff_id=c.target_id AND delivery_jobs.purpose='RECEPTIONIST' AND delivery_jobs.state='READY'",
    )
    _policy("delivery_jobs", delivery, delivery_write)
    op.execute("GRANT SELECT, INSERT ON public.delivery_jobs TO carepulse_api")
    material = (
        "EXISTS (SELECT 1 FROM public.delivery_jobs j WHERE j.id=protected_delivery_material.job_id AND "
        + delivery_write.replace("delivery_jobs.", "j.")
        + ")"
    )
    op.execute(
        "CREATE POLICY f01_api_insert ON public.protected_delivery_material "
        f"FOR INSERT TO carepulse_api WITH CHECK ({material})"
    )
    op.execute("GRANT INSERT ON public.protected_delivery_material TO carepulse_api")


def downgrade() -> None:
    raise RuntimeError("F-01 effective grant cutover is forward-only")

"""D-09 immutable evidence, nonclinical delivery fencing, replay and receipts."""

from alembic import op

revision = "0004_evidence"
down_revision = "0003_identity"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # A D-02 deferred staff/native-user trigger can observe an ACTIVE clinic.
    # Its prior combined condition accessed OLD.state even for those tables;
    # PL/pgSQL does not guarantee short-circuit evaluation. Correct it in this
    # forward migration without rewriting the already deployed D-02 revision.
    op.execute("""
        CREATE OR REPLACE FUNCTION public.assert_active_owner() RETURNS trigger
        LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog AS $$
        DECLARE
            org_id uuid;
            org_state text;
            first_activation timestamptz;
            actual_owners integer;
        BEGIN
            IF TG_TABLE_NAME = 'organizations' THEN
                org_id := NEW.id;
            ELSIF TG_TABLE_NAME = 'staff_accounts' THEN
                org_id := CASE WHEN TG_OP = 'DELETE' THEN OLD.organization_id
                               ELSE NEW.organization_id END;
            ELSE
                SELECT s.organization_id INTO org_id FROM public.staff_accounts s
                WHERE s.auth_user_id = CASE WHEN TG_OP = 'DELETE' THEN OLD.id
                                            ELSE NEW.id END;
            END IF;
            IF org_id IS NULL THEN
                RETURN NULL;
            END IF;
            SELECT state, first_activated_at INTO org_state, first_activation
            FROM public.organizations WHERE id = org_id FOR UPDATE;
            IF org_state = 'ACTIVE' THEN
                SELECT count(*) INTO actual_owners
                FROM public.staff_accounts s
                JOIN public."user" u ON u.id = s.auth_user_id
                WHERE s.organization_id = org_id
                  AND s.role = 'DOCTOR_OWNER' AND s.enabled AND s.verified
                  AND u."emailVerified" AND u.email = s.verified_email;
                IF actual_owners <> 1 THEN
                    RAISE EXCEPTION 'ACTIVE tenant requires one verified enabled Owner'
                        USING ERRCODE = '23514';
                END IF;
                IF TG_TABLE_NAME = 'organizations' THEN
                    IF OLD.state = 'PROVISIONING' THEN
                        IF first_activation IS NULL OR NOT EXISTS (
                            SELECT 1 FROM public.invitations i
                            JOIN public.staff_accounts s ON s.organization_id = i.organization_id
                            JOIN public."user" u ON u.id = s.auth_user_id
                            WHERE i.organization_id = org_id
                              AND i.purpose = 'FIRST_OWNER' AND i.state = 'ACCEPTED'
                              AND i.accepted_by_user_id = u.id
                              AND i.target_email = s.verified_email
                              AND s.role = 'DOCTOR_OWNER' AND s.enabled AND s.verified
                              AND i.accepted_at < i.expires_at
                        ) THEN
                            RAISE EXCEPTION 'Activation requires accepted first-Owner invitation'
                                USING ERRCODE = '23514';
                        END IF;
                    END IF;
                END IF;
            END IF;
            RETURN NULL;
        END
        $$
    """)
    op.execute("""
        CREATE TABLE public.sensitive_audit (
            id uuid PRIMARY KEY,
            organization_id uuid REFERENCES public.organizations(id),
            actor_staff_id uuid,
            operator_id uuid REFERENCES public.operator_identities(id),
            actor_role text NOT NULL CHECK
                (actor_role IN ('DOCTOR_OWNER', 'RECEPTIONIST', 'OPERATOR', 'SYSTEM')),
            action text NOT NULL CHECK (btrim(action) <> ''),
            reason text,
            before_state jsonb,
            after_state jsonb,
            correlation_id text NOT NULL CHECK (btrim(correlation_id) <> ''),
            recovery_operation_id uuid,
            occurred_at timestamptz NOT NULL DEFAULT date_trunc('second', clock_timestamp()),
            FOREIGN KEY (organization_id, actor_staff_id)
                REFERENCES public.staff_accounts(organization_id, id),
            FOREIGN KEY (organization_id, actor_staff_id, recovery_operation_id)
                REFERENCES public.recovery_operations(organization_id, staff_id, id),
            CHECK ((actor_role = 'OPERATOR' AND operator_id IS NOT NULL
                    AND actor_staff_id IS NULL AND reason IS NOT NULL
                    AND btrim(reason) <> '' AND before_state IS NULL
                    AND after_state IS NULL) OR
                   (actor_role IN ('DOCTOR_OWNER', 'RECEPTIONIST')
                    AND organization_id IS NOT NULL AND actor_staff_id IS NOT NULL
                    AND operator_id IS NULL) OR
                   (actor_role = 'SYSTEM' AND operator_id IS NULL)),
            CHECK (occurred_at = date_trunc('second', occurred_at)),
            CHECK (action <> 'PASSWORD_RESET_SUCCEEDED' OR
                   (recovery_operation_id IS NOT NULL
                    AND actor_staff_id IS NOT NULL))
        )
    """)
    op.execute("""
        CREATE UNIQUE INDEX sensitive_reset_once
        ON public.sensitive_audit(recovery_operation_id, action)
        WHERE action = 'PASSWORD_RESET_SUCCEEDED'
    """)
    op.execute("""
        CREATE TABLE public.clinical_audit (
            id uuid PRIMARY KEY,
            organization_id uuid NOT NULL REFERENCES public.organizations(id),
            doctor_staff_id uuid NOT NULL,
            clinical_revision bigint NOT NULL CHECK (clinical_revision >= 0),
            before_ref text,
            after_ref text,
            correlation_id text NOT NULL CHECK (btrim(correlation_id) <> ''),
            occurred_at timestamptz NOT NULL DEFAULT date_trunc('second', clock_timestamp()),
            CHECK (occurred_at = date_trunc('second', occurred_at)),
            FOREIGN KEY (organization_id, doctor_staff_id)
                REFERENCES public.staff_accounts(organization_id, id)
        )
    """)
    op.execute("""
        CREATE TABLE public.outbox_events (
            id uuid PRIMARY KEY,
            organization_id uuid NOT NULL REFERENCES public.organizations(id),
            event_type text NOT NULL CHECK (btrim(event_type) <> ''),
            aggregate_type text NOT NULL CHECK (btrim(aggregate_type) <> ''),
            aggregate_id text NOT NULL CHECK (btrim(aggregate_id) <> ''),
            payload jsonb NOT NULL,
            occurred_at timestamptz NOT NULL DEFAULT date_trunc('second', clock_timestamp()),
            CHECK (occurred_at = date_trunc('second', occurred_at))
        )
    """)
    op.execute("""
        CREATE TABLE public.delivery_jobs (
            id uuid PRIMARY KEY,
            organization_id uuid NOT NULL REFERENCES public.organizations(id),
            staff_id uuid,
            invitation_id uuid,
            verification_id text,
            recovery_operation_id uuid,
            proof_reference text NOT NULL CHECK (btrim(proof_reference) <> ''),
            purpose text NOT NULL CHECK
                (purpose IN ('FIRST_OWNER', 'RECEPTIONIST', 'PASSWORD_RESET')),
            recipient text NOT NULL CHECK (btrim(recipient) <> ''),
            template text NOT NULL CHECK (btrim(template) <> ''),
            logical_key text NOT NULL CHECK (btrim(logical_key) <> ''),
            proof_expires_at timestamptz NOT NULL,
            state text NOT NULL DEFAULT 'READY' CHECK
                (state IN ('READY', 'CLAIMED', 'SENT', 'FAILED', 'CANCELLED')),
            attempts integer NOT NULL DEFAULT 0 CHECK (attempts >= 0),
            not_before timestamptz NOT NULL DEFAULT clock_timestamp(),
            claim_owner uuid,
            claim_generation bigint NOT NULL DEFAULT 0 CHECK (claim_generation >= 0),
            claimed_at timestamptz,
            lease_expires_at timestamptz,
            last_outcome text,
            created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
            updated_at timestamptz NOT NULL DEFAULT clock_timestamp(),
            FOREIGN KEY (organization_id, staff_id)
                REFERENCES public.staff_accounts(organization_id, id),
            FOREIGN KEY (organization_id, purpose, recipient, invitation_id)
                REFERENCES public.invitations(organization_id, purpose, target_email, id),
            FOREIGN KEY (organization_id, staff_id, recovery_operation_id)
                REFERENCES public.recovery_operations(organization_id, staff_id, id),
            UNIQUE (organization_id, logical_key),
            CHECK ((purpose = 'FIRST_OWNER' AND staff_id IS NULL
                    AND invitation_id IS NOT NULL AND verification_id IS NULL
                    AND recovery_operation_id IS NULL) OR
                   (purpose = 'RECEPTIONIST' AND staff_id IS NOT NULL
                    AND invitation_id IS NOT NULL AND verification_id IS NULL
                    AND recovery_operation_id IS NULL) OR
                   (purpose = 'PASSWORD_RESET' AND staff_id IS NOT NULL
                    AND invitation_id IS NULL AND verification_id IS NOT NULL)),
            CHECK (proof_expires_at > created_at),
            CHECK ((state = 'CLAIMED') =
                (claim_owner IS NOT NULL AND claimed_at IS NOT NULL
                 AND lease_expires_at IS NOT NULL)),
            CHECK (claim_owner IS NOT NULL OR
                   (claimed_at IS NULL AND lease_expires_at IS NULL)),
            CHECK (lease_expires_at IS NULL OR lease_expires_at > claimed_at)
        )
    """)
    op.execute("""
        CREATE INDEX delivery_claim_candidates
        ON public.delivery_jobs(state, not_before, lease_expires_at)
        WHERE state IN ('READY', 'CLAIMED')
    """)
    op.execute("""
        CREATE UNIQUE INDEX delivery_one_invitation
        ON public.delivery_jobs(invitation_id) WHERE invitation_id IS NOT NULL
    """)
    op.execute("""
        CREATE UNIQUE INDEX delivery_one_verification
        ON public.delivery_jobs(verification_id) WHERE verification_id IS NOT NULL
    """)
    op.execute("""
        CREATE TABLE public.protected_delivery_material (
            job_id uuid PRIMARY KEY REFERENCES public.delivery_jobs(id),
            format_version integer NOT NULL DEFAULT 1 CHECK (format_version = 1),
            delivery_key_id text NOT NULL CHECK (btrim(delivery_key_id) <> ''),
            ciphertext_base64url text NOT NULL
                CHECK (length(ciphertext_base64url) = 512
                       AND ciphertext_base64url ~ '^[A-Za-z0-9_-]+$')
        )
    """)
    op.execute("""
        CREATE TABLE public.request_nonces (
            key_id text NOT NULL CHECK (btrim(key_id) <> ''),
            nonce text NOT NULL CHECK (btrim(nonce) <> ''),
            signed_at timestamptz NOT NULL,
            accepted_at timestamptz NOT NULL DEFAULT clock_timestamp(),
            PRIMARY KEY (key_id, nonce)
        )
    """)
    op.execute("""
        CREATE TABLE public.idempotency_receipts (
            id uuid PRIMARY KEY,
            scope_kind text NOT NULL CHECK
                (scope_kind IN ('TENANT', 'OPERATOR_CREATE')),
            scope_id uuid NOT NULL,
            actor_kind text NOT NULL CHECK (actor_kind IN ('STAFF', 'OPERATOR')),
            actor_id uuid NOT NULL,
            organization_id uuid REFERENCES public.organizations(id),
            staff_id uuid,
            operator_id uuid REFERENCES public.operator_identities(id),
            operation text NOT NULL CHECK (btrim(operation) <> ''),
            idempotency_key text NOT NULL CHECK (btrim(idempotency_key) <> ''),
            request_digest text NOT NULL CHECK
                (request_digest ~ '^[0-9a-f]{64}$'),
            result_projection jsonb,
            result_organization_id uuid REFERENCES public.organizations(id),
            result_revision bigint CHECK (result_revision IS NULL OR result_revision >= 0),
            created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
            FOREIGN KEY (organization_id, staff_id)
                REFERENCES public.staff_accounts(organization_id, id),
            UNIQUE (scope_kind, scope_id, actor_kind, actor_id,
                    operation, idempotency_key),
            CHECK ((scope_kind = 'TENANT' AND actor_kind = 'STAFF'
                    AND organization_id IS NOT NULL AND staff_id IS NOT NULL
                    AND scope_id = organization_id AND actor_id = staff_id
                    AND operator_id IS NULL AND result_organization_id IS NULL) OR
                   (scope_kind = 'OPERATOR_CREATE' AND actor_kind = 'OPERATOR'
                    AND organization_id IS NULL AND staff_id IS NULL
                    AND operator_id IS NOT NULL AND actor_id = operator_id
                    AND scope_id = operator_id AND operation = 'clinics.create'
                    AND result_projection IS NULL
                    AND result_organization_id IS NOT NULL))
        )
    """)

    op.execute("""
        CREATE FUNCTION public.guard_immutable_evidence() RETURNS trigger
        LANGUAGE plpgsql SET search_path = pg_catalog AS $$
        BEGIN
            RAISE EXCEPTION 'Evidence is immutable' USING ERRCODE = '23514';
        END
        $$
    """)
    op.execute("REVOKE ALL ON FUNCTION public.guard_immutable_evidence() FROM PUBLIC")
    for table in (
        "sensitive_audit",
        "clinical_audit",
        "outbox_events",
        "idempotency_receipts",
    ):
        op.execute(f"""
            CREATE TRIGGER {table}_immutable
            BEFORE UPDATE OR DELETE ON public.{table}
            FOR EACH ROW EXECUTE FUNCTION public.guard_immutable_evidence()
        """)
    op.execute("""
        CREATE FUNCTION public.guard_clinical_audit() RETURNS trigger
        LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog AS $$
        BEGIN
            IF session_user = 'carepulse_runtime' AND
               NEW.doctor_staff_id::text IS DISTINCT FROM
                   current_setting('app.staff_id', true) THEN
                RAISE EXCEPTION 'Clinical audit actor mismatch'
                    USING ERRCODE = '42501';
            END IF;
            IF NOT EXISTS (
                SELECT 1 FROM public.staff_accounts s
                WHERE s.organization_id = NEW.organization_id
                  AND s.id = NEW.doctor_staff_id AND s.role = 'DOCTOR_OWNER'
                  AND s.enabled AND s.verified
            ) THEN
                RAISE EXCEPTION 'Clinical audit requires active Doctor'
                    USING ERRCODE = '23514';
            END IF;
            RETURN NEW;
        END
        $$
    """)
    op.execute("REVOKE ALL ON FUNCTION public.guard_clinical_audit() FROM PUBLIC")
    op.execute("""
        CREATE TRIGGER clinical_audit_doctor
        BEFORE INSERT ON public.clinical_audit
        FOR EACH ROW EXECUTE FUNCTION public.guard_clinical_audit()
    """)

    op.execute("""
        CREATE FUNCTION public.guard_delivery_job() RETURNS trigger
        LANGUAGE plpgsql SET search_path = pg_catalog AS $$
        DECLARE
            db_now timestamptz := clock_timestamp();
            current_owner text := current_setting('app.delivery_claim_owner', true);
            current_generation text := current_setting('app.delivery_claim_generation', true);
            owned_lease boolean;
            proof_usable boolean;
        BEGIN
            IF TG_OP = 'DELETE' THEN
                RAISE EXCEPTION 'Delivery job deletion forbidden' USING ERRCODE = '23514';
            END IF;
            IF TG_OP = 'INSERT' THEN
                IF NEW.state <> 'READY' OR NEW.attempts <> 0 OR
                   NEW.claim_generation <> 0 OR NEW.claim_owner IS NOT NULL OR
                   NEW.last_outcome IS NOT NULL THEN
                    RAISE EXCEPTION 'Invalid initial delivery state' USING ERRCODE = '23514';
                END IF;
                RETURN NEW;
            END IF;
            IF (NEW.id, NEW.organization_id, NEW.staff_id, NEW.invitation_id,
                NEW.verification_id, NEW.recovery_operation_id, NEW.proof_reference,
                NEW.purpose, NEW.recipient, NEW.template, NEW.logical_key,
                NEW.proof_expires_at, NEW.created_at)
               IS DISTINCT FROM
               (OLD.id, OLD.organization_id, OLD.staff_id, OLD.invitation_id,
                OLD.verification_id, OLD.recovery_operation_id, OLD.proof_reference,
                OLD.purpose, OLD.recipient, OLD.template, OLD.logical_key,
                OLD.proof_expires_at, OLD.created_at) THEN
                RAISE EXCEPTION 'Immutable delivery issuance context' USING ERRCODE = '23514';
            END IF;
            IF OLD.state IN ('SENT', 'FAILED', 'CANCELLED') THEN
                RAISE EXCEPTION 'Delivery terminal state immutable' USING ERRCODE = '23514';
            END IF;
            IF NEW.state = 'CANCELLED' THEN
                IF OLD.invitation_id IS NOT NULL THEN
                    SELECT EXISTS (
                        SELECT 1 FROM public.invitations i
                        JOIN public.organizations o ON o.id = i.organization_id
                        WHERE i.id = OLD.invitation_id
                          AND i.organization_id = OLD.organization_id
                          AND i.target_email = OLD.recipient
                          AND i.state = 'ISSUED' AND i.expires_at > db_now
                          AND o.state IN ('PROVISIONING', 'ACTIVE')
                    ) INTO proof_usable;
                ELSE
                    SELECT EXISTS (
                        SELECT 1 FROM public.verification v
                        JOIN public.staff_accounts s ON s.id = OLD.staff_id
                           AND s.organization_id = OLD.organization_id
                        JOIN public.organizations o ON o.id = s.organization_id
                        WHERE v.id = OLD.verification_id AND v."expiresAt" > db_now
                          AND s.enabled AND s.verified AND o.state = 'ACTIVE'
                          AND s.verified_email = OLD.recipient
                    ) INTO proof_usable;
                END IF;
                IF proof_usable AND OLD.proof_expires_at > db_now THEN
                    RAISE EXCEPTION 'Cannot cancel usable delivery proof'
                        USING ERRCODE = '23514';
                END IF;
            END IF;
            owned_lease := COALESCE(
                OLD.state = 'CLAIMED'
                AND OLD.lease_expires_at > db_now
                AND current_owner = OLD.claim_owner::text
                AND current_generation = OLD.claim_generation::text, false);
            IF NEW.state = 'CLAIMED' THEN
                IF NOT ((OLD.state = 'READY' AND OLD.not_before <= db_now) OR
                        (OLD.state = 'CLAIMED' AND OLD.lease_expires_at <= db_now))
                   OR NEW.claim_generation <> OLD.claim_generation + 1
                   OR NEW.attempts <> OLD.attempts + 1
                   OR NEW.claim_owner IS NULL OR
                   (OLD.state = 'CLAIMED' AND NEW.claim_owner = OLD.claim_owner)
                   OR NEW.claimed_at IS NULL OR NEW.claimed_at > db_now
                   OR NEW.lease_expires_at <= db_now OR
                   NEW.not_before IS DISTINCT FROM OLD.not_before OR
                   NEW.last_outcome IS DISTINCT FROM OLD.last_outcome THEN
                    RAISE EXCEPTION 'Invalid delivery claim or reclaim' USING ERRCODE = '23514';
                END IF;
            ELSIF NEW.state IN ('SENT', 'READY', 'FAILED', 'CANCELLED') THEN
                IF NEW.claim_owner IS NOT NULL OR NEW.claimed_at IS NOT NULL OR
                   NEW.lease_expires_at IS NOT NULL OR
                   NEW.claim_generation <> OLD.claim_generation OR
                   NEW.attempts <> OLD.attempts OR
                   (OLD.state = 'CLAIMED' AND NOT owned_lease
                    AND NEW.state NOT IN ('CANCELLED', 'FAILED')) OR
                   (OLD.state = 'READY' AND NEW.state NOT IN ('CANCELLED', 'FAILED'))
                   THEN
                    RAISE EXCEPTION 'Stale or invalid delivery outcome' USING ERRCODE = '23514';
                END IF;
            ELSE
                RAISE EXCEPTION 'Invalid delivery transition' USING ERRCODE = '23514';
            END IF;
            NEW.updated_at := db_now;
            RETURN NEW;
        END
        $$
    """)
    op.execute("REVOKE ALL ON FUNCTION public.guard_delivery_job() FROM PUBLIC")
    op.execute("""
        CREATE TRIGGER delivery_jobs_guard
        BEFORE INSERT OR UPDATE OR DELETE ON public.delivery_jobs
        FOR EACH ROW EXECUTE FUNCTION public.guard_delivery_job()
    """)
    op.execute("""
        CREATE FUNCTION public.guard_delivery_material() RETURNS trigger
        LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog AS $$
        DECLARE
            job public.delivery_jobs%ROWTYPE;
        BEGIN
            IF TG_OP = 'UPDATE' THEN
                RAISE EXCEPTION 'Delivery ciphertext is immutable' USING ERRCODE = '23514';
            END IF;
            SELECT * INTO job FROM public.delivery_jobs
            WHERE id = CASE WHEN TG_OP = 'DELETE' THEN OLD.job_id ELSE NEW.job_id END
            FOR UPDATE;
            IF NOT FOUND THEN
                RAISE EXCEPTION 'Delivery job missing' USING ERRCODE = '23514';
            END IF;
            IF TG_OP = 'INSERT' THEN
                IF job.state <> 'READY' THEN
                    RAISE EXCEPTION 'Cannot add material after claim' USING ERRCODE = '23514';
                END IF;
                RETURN NEW;
            END IF;
            IF job.state NOT IN ('SENT', 'FAILED', 'CANCELLED')
               AND NOT COALESCE((
                   job.state = 'CLAIMED' AND
                   job.claim_owner::text = current_setting('app.delivery_claim_owner', true)
                   AND job.claim_generation::text =
                       current_setting('app.delivery_claim_generation', true)
                   AND job.lease_expires_at > clock_timestamp()
               ), false) THEN
                RAISE EXCEPTION 'Delivery material disposal lost claim'
                    USING ERRCODE = '23514';
            END IF;
            RETURN OLD;
        END
        $$
    """)
    op.execute("REVOKE ALL ON FUNCTION public.guard_delivery_material() FROM PUBLIC")
    op.execute("""
        CREATE TRIGGER delivery_material_guard
        BEFORE INSERT OR UPDATE OR DELETE ON public.protected_delivery_material
        FOR EACH ROW EXECUTE FUNCTION public.guard_delivery_material()
    """)
    op.execute("""
        CREATE FUNCTION public.assert_delivery_material_present() RETURNS trigger
        LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog AS $$
        BEGIN
            IF EXISTS (
                SELECT 1 FROM public.delivery_jobs j
                WHERE j.id = NEW.id AND j.state IN ('READY', 'CLAIMED')
                  AND NOT EXISTS (
                      SELECT 1 FROM public.protected_delivery_material m
                      WHERE m.job_id = j.id)
            ) THEN
                RAISE EXCEPTION 'Delivery issuance missing committed material'
                    USING ERRCODE = '23514';
            END IF;
            RETURN NULL;
        END
        $$
    """)
    op.execute("""
        REVOKE ALL ON FUNCTION public.assert_delivery_material_present() FROM PUBLIC
    """)
    op.execute("""
        CREATE CONSTRAINT TRIGGER delivery_material_committed
        AFTER INSERT ON public.delivery_jobs
        DEFERRABLE INITIALLY DEFERRED
        FOR EACH ROW EXECUTE FUNCTION public.assert_delivery_material_present()
    """)

    for table in (
        "sensitive_audit",
        "clinical_audit",
        "outbox_events",
        "delivery_jobs",
        "protected_delivery_material",
        "idempotency_receipts",
    ):
        op.execute(f"ALTER TABLE public.{table} ENABLE ROW LEVEL SECURITY")
        op.execute(f"ALTER TABLE public.{table} FORCE ROW LEVEL SECURITY")
        op.execute(f"""
            CREATE POLICY {table}_migration_access ON public.{table}
            FOR ALL TO carepulse_migration USING (true) WITH CHECK (true)
        """)
    tenant = "NULLIF(current_setting('app.organization_id', true), '')::uuid"
    for table in ("outbox_events", "delivery_jobs"):
        op.execute(f"""
            CREATE POLICY {table}_tenant_access ON public.{table}
            FOR ALL TO carepulse_runtime
            USING (organization_id = {tenant})
            WITH CHECK (organization_id = {tenant})
        """)
        if table == "delivery_jobs":
            columns = (
                "id, organization_id, staff_id, invitation_id, verification_id, "
                "recovery_operation_id, proof_reference, purpose, recipient, template, "
                "logical_key, proof_expires_at, state, attempts, not_before, "
                "claim_generation, claimed_at, lease_expires_at, last_outcome, "
                "created_at, updated_at"
            )
            op.execute(
                f"GRANT SELECT ({columns}), INSERT ON public.{table} TO carepulse_runtime"
            )
        else:
            op.execute(f"GRANT SELECT, INSERT ON public.{table} TO carepulse_runtime")
    op.execute("GRANT UPDATE ON public.delivery_jobs TO carepulse_runtime")
    op.execute("""
        CREATE FUNCTION public.is_delivery_claim_owner(
            requested_job uuid, requested_owner uuid, requested_generation bigint)
        RETURNS boolean
        LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog AS $$
        BEGIN
            PERFORM 1 FROM public.delivery_jobs j
            WHERE j.id = requested_job
              AND j.organization_id =
                NULLIF(current_setting('app.organization_id', true), '')::uuid
              AND j.state = 'CLAIMED' AND j.claim_owner = requested_owner
              AND j.claim_generation = requested_generation
              AND j.lease_expires_at > clock_timestamp()
            FOR UPDATE SKIP LOCKED;
            RETURN FOUND;
        END
        $$
    """)
    op.execute(
        "REVOKE ALL ON FUNCTION public.is_delivery_claim_owner(uuid,uuid,bigint) FROM PUBLIC"
    )
    op.execute(
        "GRANT EXECUTE ON FUNCTION public.is_delivery_claim_owner(uuid,uuid,bigint) TO carepulse_runtime"
    )

    for table in ("sensitive_audit", "clinical_audit"):
        op.execute(f"""
            CREATE POLICY {table}_insert ON public.{table}
            FOR INSERT TO carepulse_runtime
            WITH CHECK (organization_id = {tenant})
        """)
        op.execute(f"GRANT INSERT ON public.{table} TO carepulse_runtime")
    for table in ("sensitive_audit", "clinical_audit"):
        op.execute(f"""
            CREATE POLICY {table}_doctor_read ON public.{table}
            FOR SELECT TO carepulse_runtime
            USING (organization_id = {tenant} AND EXISTS (
                SELECT 1 FROM public.staff_accounts s
                WHERE s.organization_id = {table}.organization_id
                  AND s.id::text = current_setting('app.staff_id', true)
                  AND s.role = 'DOCTOR_OWNER' AND s.enabled AND s.verified
            ))
        """)
        op.execute(f"GRANT SELECT ON public.{table} TO carepulse_runtime")
    op.execute(f"""
        CREATE POLICY idempotency_receipts_tenant_access
        ON public.idempotency_receipts FOR ALL TO carepulse_runtime
        USING (scope_kind = 'TENANT' AND organization_id = {tenant})
        WITH CHECK (scope_kind = 'TENANT' AND organization_id = {tenant})
    """)
    op.execute(
        "GRANT SELECT, INSERT ON public.idempotency_receipts TO carepulse_runtime"
    )
    op.execute("""
        CREATE POLICY request_nonce_insert ON public.request_nonces
        FOR INSERT TO carepulse_runtime WITH CHECK (true)
    """)
    op.execute("ALTER TABLE public.request_nonces ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE public.request_nonces FORCE ROW LEVEL SECURITY")
    op.execute("""
        CREATE POLICY request_nonce_migration ON public.request_nonces
        FOR ALL TO carepulse_migration USING (true) WITH CHECK (true)
    """)
    op.execute("GRANT INSERT ON public.request_nonces TO carepulse_runtime")
    op.execute("""
        CREATE FUNCTION public.purge_request_nonces(configured_window interval)
        RETURNS integer
        LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog AS $$
        DECLARE
            removed_count integer;
        BEGIN
            IF configured_window IS NULL OR configured_window <= interval '0' THEN
                RAISE EXCEPTION 'Configured nonce retention window required'
                    USING ERRCODE = '22023';
            END IF;
            DELETE FROM public.request_nonces
            WHERE signed_at < clock_timestamp() - configured_window;
            GET DIAGNOSTICS removed_count = ROW_COUNT;
            RETURN removed_count;
        END
        $$
    """)
    op.execute("""
        REVOKE ALL ON FUNCTION public.purge_request_nonces(interval) FROM PUBLIC
    """)
    # Purge is deliberately migration/maintenance-only. Runtime cannot choose
    # a tiny interval to erase still-acceptable signed nonces.

    op.execute(f"""
        CREATE POLICY material_insert ON public.protected_delivery_material
        FOR INSERT TO carepulse_runtime WITH CHECK (
            EXISTS (SELECT 1 FROM public.delivery_jobs j WHERE j.id = job_id
                    AND j.organization_id = {tenant} AND j.state = 'READY')
        )
    """)
    op.execute(f"""
        CREATE POLICY material_delete ON public.protected_delivery_material
        FOR DELETE TO carepulse_runtime USING (
            EXISTS (SELECT 1 FROM public.delivery_jobs j WHERE j.id = job_id
                    AND j.organization_id = {tenant}
                    AND j.state IN ('SENT', 'FAILED', 'CANCELLED'))
        )
    """)
    op.execute("""
        GRANT SELECT (job_id), INSERT, DELETE ON public.protected_delivery_material
        TO carepulse_runtime
    """)
    op.execute(f"""
        CREATE POLICY material_locator ON public.protected_delivery_material
        FOR SELECT TO carepulse_runtime USING (
            EXISTS (SELECT 1 FROM public.delivery_jobs j WHERE j.id = job_id
                    AND j.organization_id = {tenant}
                    AND j.state IN ('SENT', 'FAILED', 'CANCELLED'))
        )
    """)
    op.execute("""
        CREATE FUNCTION public.claimed_delivery_material(
            requested_job uuid, requested_owner uuid, requested_generation bigint)
        RETURNS TABLE (format_version integer, delivery_key_id text,
                       ciphertext_base64url text)
        LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog AS $$
        BEGIN
            PERFORM 1 FROM public.delivery_jobs j WHERE j.id = requested_job
                AND j.organization_id =
                    NULLIF(current_setting('app.organization_id', true), '')::uuid
                AND j.state = 'CLAIMED' AND j.claim_owner = requested_owner
                AND j.claim_generation = requested_generation
                AND j.lease_expires_at > clock_timestamp()
                FOR UPDATE;
            IF NOT FOUND THEN
                RETURN;
            END IF;
            RETURN QUERY SELECT m.format_version, m.delivery_key_id,
                                m.ciphertext_base64url
                FROM public.protected_delivery_material m
                WHERE m.job_id = requested_job;
        END
        $$
    """)
    op.execute("""
        REVOKE ALL ON FUNCTION public.claimed_delivery_material(uuid,uuid,bigint)
        FROM PUBLIC
    """)
    op.execute("""
        GRANT EXECUTE ON FUNCTION
        public.claimed_delivery_material(uuid,uuid,bigint) TO carepulse_runtime
    """)
    # Operator functions expose only audited metadata actions; the shared
    # operator DB login cannot read clinical rows, proofs or raw receipt tables.
    op.execute("""
        CREATE FUNCTION public.read_operator_create_receipt(
            verified_operator uuid, supplied_digest text, requested_key text)
        RETURNS TABLE (request_digest text, result_organization_id uuid,
                       result_revision bigint)
        LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog AS $$
        BEGIN
            IF NOT EXISTS (
                SELECT 1 FROM public.operator_identities o
                WHERE o.id = verified_operator AND o.enabled
                  AND o.credential_digest = supplied_digest
            ) THEN
                RAISE EXCEPTION 'Operator authorization denied'
                    USING ERRCODE = '42501';
            END IF;
            RETURN QUERY
                SELECT r.request_digest, r.result_organization_id, r.result_revision
                FROM public.idempotency_receipts r
                WHERE r.scope_kind = 'OPERATOR_CREATE'
                  AND r.scope_id = verified_operator
                  AND r.actor_kind = 'OPERATOR'
                  AND r.actor_id = verified_operator
                  AND r.operation = 'clinics.create'
                  AND r.idempotency_key = requested_key;
        END
        $$
    """)
    op.execute("""
        CREATE FUNCTION public.write_operator_create_receipt(
            verified_operator uuid, supplied_digest text, new_receipt_id uuid,
            requested_key text, normalized_digest text, created_clinic_id uuid,
            committed_revision bigint)
        RETURNS void
        LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog AS $$
        BEGIN
            IF NOT EXISTS (
                SELECT 1 FROM public.operator_identities o
                WHERE o.id = verified_operator AND o.enabled
                  AND o.credential_digest = supplied_digest
            ) OR NOT EXISTS (
                SELECT 1 FROM public.organizations org
                WHERE org.id = created_clinic_id
                  AND org.created_by_operator_id = verified_operator
            ) THEN
                RAISE EXCEPTION 'Operator creation authorization denied'
                    USING ERRCODE = '42501';
            END IF;
            INSERT INTO public.idempotency_receipts (
                id, scope_kind, scope_id, actor_kind, actor_id, operator_id,
                operation, idempotency_key, request_digest,
                result_organization_id, result_revision)
            VALUES (
                new_receipt_id, 'OPERATOR_CREATE', verified_operator,
                'OPERATOR', verified_operator, verified_operator,
                'clinics.create', requested_key, normalized_digest,
                created_clinic_id, committed_revision);
        END
        $$
    """)
    op.execute("""
        CREATE FUNCTION public.issue_operator_first_owner_delivery(
            verified_operator uuid, supplied_digest text, new_job_id uuid,
            issued_invitation_id uuid, intended_recipient text,
            requested_template text, deduplication_key text,
            requested_key_id text, token_ciphertext text)
        RETURNS void
        LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog AS $$
        DECLARE
            invite public.invitations%ROWTYPE;
        BEGIN
            IF NOT EXISTS (
                SELECT 1 FROM public.operator_identities o
                WHERE o.id = verified_operator AND o.enabled
                  AND o.credential_digest = supplied_digest
            ) THEN
                RAISE EXCEPTION 'Operator authorization denied'
                    USING ERRCODE = '42501';
            END IF;
            SELECT * INTO invite FROM public.invitations
            WHERE id = issued_invitation_id AND issued_by_operator_id = verified_operator
              AND purpose = 'FIRST_OWNER' AND state = 'ISSUED'
              AND target_email = intended_recipient AND expires_at > clock_timestamp()
            FOR UPDATE;
            IF NOT FOUND THEN
                RAISE EXCEPTION 'Owner invitation is unavailable'
                    USING ERRCODE = '23514';
            END IF;
            INSERT INTO public.delivery_jobs (
                id, organization_id, invitation_id, proof_reference, purpose,
                recipient, template, logical_key, proof_expires_at)
            VALUES (
                new_job_id, invite.organization_id, invite.id, invite.id::text,
                'FIRST_OWNER', intended_recipient, requested_template,
                deduplication_key, invite.expires_at);
            INSERT INTO public.protected_delivery_material (
                job_id, format_version, delivery_key_id, ciphertext_base64url)
            VALUES (new_job_id, 1, requested_key_id, token_ciphertext);
        END
        $$
    """)
    op.execute("""
        CREATE FUNCTION public.append_operator_metadata_audit(
            verified_operator uuid, supplied_digest text, audit_id uuid,
            audited_clinic_id uuid, audited_action text,
            audited_reason text, audited_correlation text)
        RETURNS void
        LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog AS $$
        BEGIN
            IF NOT EXISTS (
                SELECT 1 FROM public.operator_identities o
                WHERE o.id = verified_operator AND o.enabled
                  AND o.credential_digest = supplied_digest
            ) THEN
                RAISE EXCEPTION 'Operator authorization denied'
                    USING ERRCODE = '42501';
            END IF;
            INSERT INTO public.sensitive_audit (
                id, organization_id, operator_id, actor_role, action,
                reason, correlation_id)
            VALUES (
                audit_id, audited_clinic_id, verified_operator,
                'OPERATOR', audited_action, audited_reason, audited_correlation);
        END
        $$
    """)
    for signature in (
        "read_operator_create_receipt(uuid,text,text)",
        "write_operator_create_receipt(uuid,text,uuid,text,text,uuid,bigint)",
        "issue_operator_first_owner_delivery(uuid,text,uuid,uuid,text,text,text,text,text)",
        "append_operator_metadata_audit(uuid,text,uuid,uuid,text,text,text)",
    ):
        op.execute(f"REVOKE ALL ON FUNCTION public.{signature} FROM PUBLIC")
        op.execute(
            f"GRANT EXECUTE ON FUNCTION public.{signature} TO carepulse_operator"
        )


def downgrade() -> None:
    op.execute("""
        DO $$
        BEGIN
            IF EXISTS (SELECT 1 FROM public.sensitive_audit)
               OR EXISTS (SELECT 1 FROM public.clinical_audit)
               OR EXISTS (SELECT 1 FROM public.outbox_events)
               OR EXISTS (SELECT 1 FROM public.delivery_jobs)
               OR EXISTS (SELECT 1 FROM public.protected_delivery_material)
               OR EXISTS (SELECT 1 FROM public.request_nonces)
               OR EXISTS (SELECT 1 FROM public.idempotency_receipts) THEN
                RAISE EXCEPTION 'Cannot downgrade populated D-09 evidence'
                    USING ERRCODE = '23514';
            END IF;
        END
        $$
    """)
    op.execute("DROP FUNCTION public.is_delivery_claim_owner(uuid,uuid,bigint)")
    op.execute("DROP FUNCTION public.purge_request_nonces(interval)")
    for signature in (
        "append_operator_metadata_audit(uuid,text,uuid,uuid,text,text,text)",
        "issue_operator_first_owner_delivery(uuid,text,uuid,uuid,text,text,text,text,text)",
        "write_operator_create_receipt(uuid,text,uuid,text,text,uuid,bigint)",
        "read_operator_create_receipt(uuid,text,text)",
    ):
        op.execute(f"DROP FUNCTION public.{signature}")
    op.execute("DROP FUNCTION public.claimed_delivery_material(uuid,uuid,bigint)")
    op.execute("DROP TABLE public.protected_delivery_material")
    op.execute("DROP FUNCTION public.guard_delivery_material()")
    op.execute("DROP TABLE public.idempotency_receipts")
    op.execute("DROP TABLE public.request_nonces")
    op.execute("DROP TABLE public.delivery_jobs")
    op.execute("DROP FUNCTION public.assert_delivery_material_present()")
    op.execute("DROP FUNCTION public.guard_delivery_job()")
    op.execute("DROP TABLE public.outbox_events")
    op.execute("DROP TABLE public.clinical_audit")
    op.execute("DROP FUNCTION public.guard_clinical_audit()")
    op.execute("DROP TABLE public.sensitive_audit")
    op.execute("DROP FUNCTION public.guard_immutable_evidence()")

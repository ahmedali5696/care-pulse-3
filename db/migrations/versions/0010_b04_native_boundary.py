"""B-04: database-authored evidence for native password recovery effects."""

from alembic import op

revision = "0010_b04_native_boundary"
down_revision = "0009_b04_recovery"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Historical app-authored completion rows cannot be distinguished from
    # fabricated completion. Stop deployment before changing grants until an
    # operator resolves any such data; never invent native evidence retroactively.
    op.execute("""
        DO $$ BEGIN
            IF EXISTS (SELECT 1 FROM public.recovery_operations
                       WHERE auth_done_at IS NOT NULL)
               OR EXISTS (SELECT 1 FROM public.auth_recovery_completions) THEN
                RAISE EXCEPTION 'Historical recovery requires operator reconciliation'
                    USING ERRCODE = '23514';
            END IF;
        END $$
    """)

    # Runtime owns clinic coordination. Only the separate auth login may mutate
    # Better Auth's native tables. Never grant either role DDL or BYPASSRLS.
    for table in ("user", "session", "account", "verification"):
        op.execute(f'REVOKE ALL ON public."{table}" FROM carepulse_runtime')
        if table != "session":
            op.execute(f'GRANT SELECT ON public."{table}" TO carepulse_runtime')
        op.execute(
            f'GRANT SELECT, INSERT, UPDATE, DELETE ON public."{table}" TO carepulse_auth'
        )

    # Runtime has neither SELECT nor UPDATE on native sessions. Only this exact
    # identity-bound definer can hold a share lock for an authorized session.
    op.execute("""
        CREATE FUNCTION public.lock_native_session_authority(
            target_tenant uuid, target_user text, target_session text)
        RETURNS TABLE (
            "expiresAt" timestamptz, "absoluteDeadline" timestamptz,
            "lastActivity" timestamptz, "securityRevision" integer)
        LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog AS $$
        BEGIN
            IF target_tenant IS NULL OR target_tenant IS DISTINCT FROM
               NULLIF(current_setting('app.organization_id', true), '')::uuid OR
               target_user IS NULL OR target_session IS NULL THEN
                RAISE EXCEPTION 'Session authority denied' USING ERRCODE = '42501';
            END IF;
            RETURN QUERY
                SELECT sess."expiresAt", sess."absoluteDeadline",
                       sess."lastActivity", sess."securityRevision"
                FROM public."session" sess
                WHERE sess.id = target_session AND sess."userId" = target_user
                  AND EXISTS (
                      SELECT 1 FROM public.staff_accounts s
                      JOIN public.organizations o ON o.id = s.organization_id
                      JOIN public."user" u ON u.id = s.auth_user_id
                      WHERE s.organization_id = target_tenant
                        AND s.auth_user_id = target_user
                        AND s.enabled AND s.verified
                        AND s.reset_block_operation_id IS NULL
                        AND o.state = 'ACTIVE'
                        AND u."emailVerified" AND u.email = s.verified_email
                        AND s.security_revision = sess."securityRevision"
                  )
                FOR SHARE OF sess;
        END $$
    """)

    tenant = "NULLIF(current_setting('app.organization_id', true), '')::uuid"
    op.execute(f"""
        CREATE POLICY organization_auth_read ON public.organizations
        FOR SELECT TO carepulse_auth USING (id = {tenant})
    """)
    op.execute("""
        GRANT SELECT (id, state, lifecycle_revision, first_activated_at)
        ON public.organizations TO carepulse_auth
    """)
    for table in ("staff_accounts", "recovery_operations"):
        op.execute(f"""
            CREATE POLICY {table}_auth_access ON public.{table}
            FOR ALL TO carepulse_auth
            USING (organization_id = {tenant})
            WITH CHECK (organization_id = {tenant})
        """)
    op.execute("GRANT SELECT ON public.staff_accounts TO carepulse_auth")
    op.execute(f"""
        CREATE POLICY invitations_auth_read ON public.invitations
        FOR SELECT TO carepulse_auth USING (organization_id = {tenant})
    """)
    op.execute("GRANT SELECT ON public.invitations TO carepulse_auth")
    for signature in (
        "lock_active_organization(uuid)",
        "resolve_login_tenant(text)",
        "verify_first_owner_preparation(uuid,uuid,text)",
        "resolve_recovery_tenant(text)",
        "resolve_recovery_operation(text)",
    ):
        op.execute(f"GRANT EXECUTE ON FUNCTION public.{signature} TO carepulse_auth")
    op.execute(
        "GRANT SELECT, UPDATE (auth_fence) ON public.recovery_operations TO carepulse_auth"
    )
    op.execute(f"""
        CREATE POLICY auth_recovery_completions_auth_read ON public.auth_recovery_completions
        FOR SELECT TO carepulse_auth USING (
            EXISTS (SELECT 1 FROM public.recovery_operations r
                    WHERE r.id = auth_recovery_completions.id
                      AND r.organization_id = {tenant}))
    """)
    op.execute("GRANT SELECT ON public.auth_recovery_completions TO carepulse_auth")

    # The native issuance callback also writes proof delivery, under the same
    # leased auth transaction and the same tenant RLS boundary.
    op.execute(f"""
        CREATE POLICY delivery_jobs_auth_issue ON public.delivery_jobs
        FOR INSERT TO carepulse_auth WITH CHECK (organization_id = {tenant}
            AND purpose = 'PASSWORD_RESET' AND state = 'READY')
    """)
    op.execute(f"""
        CREATE POLICY delivery_jobs_auth_read ON public.delivery_jobs
        FOR SELECT TO carepulse_auth USING (
            organization_id = {tenant} AND purpose = 'PASSWORD_RESET')
    """)
    op.execute("""
        GRANT SELECT (id, organization_id, state, purpose), INSERT
        ON public.delivery_jobs TO carepulse_auth
    """)
    op.execute(f"""
        CREATE POLICY material_auth_issue ON public.protected_delivery_material
        FOR INSERT TO carepulse_auth WITH CHECK (
            EXISTS (SELECT 1 FROM public.delivery_jobs j WHERE j.id = job_id
                    AND j.organization_id = {tenant} AND j.state = 'READY'
                    AND j.purpose = 'PASSWORD_RESET'))
    """)
    op.execute("GRANT INSERT ON public.protected_delivery_material TO carepulse_auth")
    op.execute(f"""
        CREATE POLICY sensitive_audit_auth_issue ON public.sensitive_audit
        FOR INSERT TO carepulse_auth WITH CHECK (
            organization_id = {tenant} AND actor_role = 'SYSTEM'
            AND action = 'PASSWORD_RESET_PROOF_ISSUED')
    """)
    op.execute("GRANT INSERT ON public.sensitive_audit TO carepulse_auth")

    # Do not trust app-written completion, terminal state or success audit.
    # SQL functions below perform these transitions using migration-owned rights.
    op.execute("REVOKE ALL ON public.recovery_operations FROM carepulse_runtime")
    op.execute(
        "GRANT SELECT, INSERT ON public.recovery_operations TO carepulse_runtime"
    )
    op.execute("REVOKE ALL ON public.auth_recovery_completions FROM carepulse_runtime")
    op.execute("GRANT SELECT ON public.auth_recovery_completions TO carepulse_runtime")
    op.execute(f"""
        ALTER POLICY sensitive_audit_insert ON public.sensitive_audit
        WITH CHECK (organization_id = {tenant} AND actor_role <> 'OPERATOR'
            AND action NOT IN ('PASSWORD_RESET_SUCCEEDED', 'RECOVERY_AUTH_INVALIDATED'))
    """)

    # No app role has DML on this private FORCE-RLS table. A row is created by
    # actual Better Auth credential UPDATE and proof DELETE triggers. A deferred
    # check forbids partial effects surviving any transaction boundary.
    op.execute("""
        CREATE TABLE public.recovery_native_effects (
            id uuid PRIMARY KEY REFERENCES public.recovery_operations(id),
            auth_fence bigint NOT NULL CHECK (auth_fence > 0),
            proof_consumed_at timestamptz,
            credential_changed_at timestamptz
        )
    """)
    op.execute("ALTER TABLE public.recovery_native_effects ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE public.recovery_native_effects FORCE ROW LEVEL SECURITY")
    op.execute("""
        CREATE POLICY recovery_native_effects_migration ON public.recovery_native_effects
        FOR ALL TO carepulse_migration USING (true) WITH CHECK (true)
    """)

    # Caller-visible verification includes both native effects and their exact
    # immutable completion binding. Session absence was checked at commit;
    # subsequent legitimate logins must not invalidate the completed fact.
    # Old app-inserted completion rows have no native effects and fail closed.
    op.execute(f"""
        CREATE FUNCTION public.recovery_native_verified(operation_id uuid)
        RETURNS boolean LANGUAGE sql STABLE SECURITY DEFINER
        SET search_path = pg_catalog AS $$
            SELECT EXISTS (
                SELECT 1 FROM public.recovery_operations r
                JOIN public.recovery_native_effects e ON e.id = r.id
                JOIN public.auth_recovery_completions c ON c.id = r.id
                WHERE r.id = operation_id AND r.organization_id = {tenant}
                  AND r.state IN ('AUTH_DONE', 'FINALIZED')
                  AND r.auth_done_at IS NOT NULL AND r.auth_fence > 0
                  AND r.auth_fence = e.auth_fence
                  AND e.proof_consumed_at IS NOT NULL
                  AND e.credential_changed_at IS NOT NULL
                  AND c.proof_digest = r.proof_digest
                  AND c.auth_user_id = r.auth_user_id
                  AND c.request_fingerprint = r.request_fingerprint
                  AND c.reserved_security_revision = r.reserved_security_revision
                  AND c.auth_fence = r.auth_fence
                  AND c.completed_at = r.auth_done_at
            )
        $$
    """)

    op.execute("""
        CREATE FUNCTION public.commit_native_recovery_effect(operation_id uuid)
        RETURNS void LANGUAGE plpgsql SECURITY DEFINER
        SET search_path = pg_catalog AS $$
        DECLARE op_row public.recovery_operations%ROWTYPE;
                evidence public.recovery_native_effects%ROWTYPE;
                completed timestamptz;
        BEGIN
            SELECT * INTO evidence FROM public.recovery_native_effects
            WHERE id = operation_id FOR UPDATE;
            IF NOT FOUND OR evidence.proof_consumed_at IS NULL OR
               evidence.credential_changed_at IS NULL THEN
                RETURN;
            END IF;
            SELECT * INTO op_row FROM public.recovery_operations
            WHERE id = operation_id FOR UPDATE;
            IF NOT FOUND OR op_row.state <> 'RESERVED' OR op_row.invalidated OR
               op_row.closed_at IS NOT NULL OR op_row.auth_done_at IS NOT NULL OR
               op_row.auth_fence <> evidence.auth_fence OR
               clock_timestamp() >= op_row.proof_expires_at OR
               NOT EXISTS (
                   SELECT 1 FROM public.staff_accounts s
                   JOIN public.organizations o ON o.id = s.organization_id
                   JOIN public."user" u ON u.id = s.auth_user_id
                   WHERE s.organization_id = op_row.organization_id
                     AND s.id = op_row.staff_id AND s.auth_user_id = op_row.auth_user_id
                     AND s.role = 'DOCTOR_OWNER' AND s.enabled AND s.verified
                     AND u."emailVerified" AND u.email = s.verified_email
                     AND s.reset_block_operation_id = op_row.id
                     AND s.security_revision = op_row.reserved_security_revision
                     AND o.state = 'ACTIVE'
                     AND o.lifecycle_revision = op_row.captured_lifecycle_revision
               ) OR EXISTS (
                   SELECT 1 FROM public."session" s WHERE s."userId" = op_row.auth_user_id
               ) OR (
                    SELECT count(*) FROM public."account" a
                    WHERE a."userId" = op_row.auth_user_id
                      AND a."providerId" = 'credential'
                      AND a.password IS NOT NULL
               ) <> 1 THEN
                RAISE EXCEPTION 'Native recovery effect not authorized'
                    USING ERRCODE = '23514';
            END IF;
            completed := clock_timestamp();
            IF completed >= op_row.proof_expires_at THEN
                RAISE EXCEPTION 'Native recovery proof expired' USING ERRCODE = '23514';
            END IF;
            UPDATE public.recovery_operations SET state = 'AUTH_DONE', auth_done_at = completed
            WHERE id = operation_id AND state = 'RESERVED';
            INSERT INTO public.auth_recovery_completions
                (id, proof_digest, auth_user_id, request_fingerprint,
                 reserved_security_revision, auth_fence, completed_at)
            VALUES (op_row.id, op_row.proof_digest, op_row.auth_user_id,
                    op_row.request_fingerprint, op_row.reserved_security_revision,
                    op_row.auth_fence, completed);
        END $$
    """)

    op.execute(f"""
        CREATE FUNCTION public.capture_native_recovery_effect()
        RETURNS trigger LANGUAGE plpgsql SECURITY DEFINER
        SET search_path = pg_catalog AS $$
        DECLARE recovery_id uuid; fence bigint; scoped_tenant uuid;
                linked_proof boolean;
        BEGIN
            IF session_user <> 'carepulse_auth' THEN
                RETURN NULL;
            END IF;
            scoped_tenant := {tenant};
            IF TG_TABLE_NAME = 'verification' THEN
                SELECT EXISTS (
                    SELECT 1 FROM public.delivery_jobs j
                     WHERE j.verification_id = OLD.id AND j.purpose = 'PASSWORD_RESET'
                )
                  INTO linked_proof;
                IF NOT linked_proof THEN RETURN NULL; END IF;
                -- Better Auth prunes expired verification rows before looking up
                -- a newer proof. Cleanup never counts as native consumption.
                IF clock_timestamp() >= OLD."expiresAt" THEN RETURN NULL; END IF;
                IF scoped_tenant IS NULL THEN
                    RAISE EXCEPTION 'Native recovery scope missing'
                        USING ERRCODE = '23514';
                END IF;
                SELECT r.id, r.auth_fence INTO recovery_id, fence
                FROM public.recovery_operations r
                JOIN public.delivery_jobs j ON j.verification_id = OLD.id
                    AND j.organization_id = r.organization_id
                    AND j.staff_id = r.staff_id AND j.proof_reference = r.proof_digest
                    AND j.purpose = 'PASSWORD_RESET' AND j.proof_expires_at = OLD."expiresAt"
                    AND j.state NOT IN ('FAILED', 'CANCELLED')
                JOIN public.staff_accounts s ON s.id = r.staff_id
                    AND s.organization_id = r.organization_id
                WHERE r.organization_id = scoped_tenant AND r.state = 'RESERVED'
                  AND NOT r.invalidated AND r.closed_at IS NULL
                  AND r.auth_user_id = OLD.value AND r.proof_reference = OLD.id
                  AND r.proof_expires_at = OLD."expiresAt"
                  AND j.recipient = s.verified_email
                  AND s.auth_user_id = OLD.value AND s.reset_block_operation_id = r.id
                  AND s.security_revision = r.reserved_security_revision
                  AND r.auth_fence > 0 AND clock_timestamp() < r.proof_expires_at
                FOR UPDATE OF r;
                IF NOT FOUND THEN
                    RAISE EXCEPTION 'Unreserved native proof consumption'
                        USING ERRCODE = '23514';
                END IF;
                INSERT INTO public.recovery_native_effects
                    (id, auth_fence, proof_consumed_at)
                VALUES (recovery_id, fence, clock_timestamp())
                ON CONFLICT (id) DO UPDATE
                  SET proof_consumed_at = COALESCE(
                      public.recovery_native_effects.proof_consumed_at,
                      EXCLUDED.proof_consumed_at)
                  WHERE public.recovery_native_effects.auth_fence = EXCLUDED.auth_fence;
            ELSIF TG_TABLE_NAME = 'account' THEN
                IF NEW."providerId" <> 'credential' OR
                   OLD.password IS NOT DISTINCT FROM NEW.password OR
                   NEW.password IS NULL THEN
                    RETURN NULL;
                END IF;
                IF (OLD.id, OLD."userId", OLD."providerId", OLD."accountId")
                   IS DISTINCT FROM
                   (NEW.id, NEW."userId", NEW."providerId", NEW."accountId") OR
                   NEW."accountId" <> NEW."userId" THEN
                    RAISE EXCEPTION 'Native credential identity changed'
                        USING ERRCODE = '23514';
                END IF;
                IF EXISTS (
                    SELECT 1 FROM public.staff_accounts s
                    WHERE s.auth_user_id = NEW."userId" AND s.role = 'DOCTOR_OWNER'
                      AND (scoped_tenant IS NULL OR s.organization_id <> scoped_tenant)
                ) THEN
                    RAISE EXCEPTION 'Owner credential scope denied'
                        USING ERRCODE = '23514';
                END IF;
                IF scoped_tenant IS NULL THEN RETURN NULL; END IF;
                SELECT r.id, r.auth_fence INTO recovery_id, fence
                FROM public.recovery_operations r
                JOIN public.staff_accounts s ON s.organization_id = r.organization_id
                    AND s.id = r.staff_id AND s.auth_user_id = r.auth_user_id
                WHERE r.organization_id = scoped_tenant AND r.auth_user_id = NEW."userId"
                  AND s.reset_block_operation_id = r.id
                  AND r.closed_at IS NULL AND r.state = 'RESERVED'
                  AND NOT r.invalidated AND r.auth_fence > 0
                FOR UPDATE OF r;
                IF NOT FOUND THEN
                    IF EXISTS (
                        SELECT 1 FROM public.staff_accounts s
                        WHERE s.organization_id = scoped_tenant AND s.auth_user_id = NEW."userId"
                          AND s.role = 'DOCTOR_OWNER') THEN
                        RAISE EXCEPTION 'Unreserved Owner credential mutation'
                            USING ERRCODE = '23514';
                    END IF;
                    RETURN NULL;
                END IF;
                IF clock_timestamp() >= (
                    SELECT proof_expires_at FROM public.recovery_operations
                    WHERE id = recovery_id) THEN
                    RAISE EXCEPTION 'Expired Owner credential mutation'
                        USING ERRCODE = '23514';
                END IF;
                INSERT INTO public.recovery_native_effects
                    (id, auth_fence, credential_changed_at)
                VALUES (recovery_id, fence, clock_timestamp())
                ON CONFLICT (id) DO UPDATE
                  SET credential_changed_at = COALESCE(
                      public.recovery_native_effects.credential_changed_at,
                      EXCLUDED.credential_changed_at)
                  WHERE public.recovery_native_effects.auth_fence = EXCLUDED.auth_fence;
            END IF;
            IF recovery_id IS NOT NULL THEN
                PERFORM public.commit_native_recovery_effect(recovery_id);
            END IF;
            RETURN NULL;
        END $$
    """)
    op.execute("""
        CREATE TRIGGER auth_proof_consumed AFTER DELETE ON public."verification"
        FOR EACH ROW EXECUTE FUNCTION public.capture_native_recovery_effect()
    """)
    op.execute("""
        CREATE TRIGGER auth_credential_changed AFTER UPDATE OF password ON public."account"
        FOR EACH ROW EXECUTE FUNCTION public.capture_native_recovery_effect()
    """)

    op.execute("""
        CREATE FUNCTION public.assert_native_recovery_commit()
        RETURNS trigger LANGUAGE plpgsql SECURITY DEFINER
        SET search_path = pg_catalog AS $$
        DECLARE current_effect public.recovery_native_effects%ROWTYPE;
                current_recovery public.recovery_operations%ROWTYPE;
        BEGIN
            SELECT * INTO current_effect FROM public.recovery_native_effects WHERE id = NEW.id;
            SELECT * INTO current_recovery FROM public.recovery_operations WHERE id = NEW.id;
            IF current_effect.proof_consumed_at IS NULL OR
               current_effect.credential_changed_at IS NULL OR
               current_recovery.state NOT IN ('AUTH_DONE', 'FINALIZED') OR
               current_recovery.auth_fence <> current_effect.auth_fence OR
               current_recovery.auth_done_at IS NULL OR
               NOT EXISTS (
                   SELECT 1 FROM public.auth_recovery_completions c
                   WHERE c.id = current_recovery.id
                     AND c.auth_user_id = current_recovery.auth_user_id
                     AND c.proof_digest = current_recovery.proof_digest
                     AND c.request_fingerprint = current_recovery.request_fingerprint
                     AND c.reserved_security_revision = current_recovery.reserved_security_revision
                     AND c.auth_fence = current_effect.auth_fence
                     AND c.completed_at = current_recovery.auth_done_at
               ) OR EXISTS (
                   SELECT 1 FROM public."session" s
                   WHERE s."userId" = current_recovery.auth_user_id
               ) THEN
                RAISE EXCEPTION 'Incomplete native recovery at commit'
                    USING ERRCODE = '23514';
            END IF;
            RETURN NULL;
        END $$
    """)
    op.execute("""
        CREATE CONSTRAINT TRIGGER recovery_native_commit_constraint
        AFTER INSERT OR UPDATE ON public.recovery_native_effects
        DEFERRABLE INITIALLY DEFERRED
        FOR EACH ROW EXECUTE FUNCTION public.assert_native_recovery_commit()
    """)

    # The Owner block is a durable authority fence. Even after abandonment of
    # an expired reservation, a new bound RESERVED successor must replace it
    # in the same transaction. A naked A -> NULL is never a valid replacement.
    op.execute("""
        CREATE FUNCTION public.guard_staff_recovery_block()
        RETURNS trigger LANGUAGE plpgsql SECURITY DEFINER
        SET search_path = pg_catalog AS $$
        DECLARE prior public.recovery_operations%ROWTYPE;
                successor public.recovery_operations%ROWTYPE;
        BEGIN
            -- Migration owns schema and controlled synthetic fixtures; only
            -- runtime/auth must obey this additional capability fence.
            IF session_user = 'carepulse_migration' THEN RETURN NEW; END IF;
            IF OLD.reset_block_operation_id IS NOT DISTINCT FROM
               NEW.reset_block_operation_id THEN
                RETURN NEW;
            END IF;
            IF NEW.reset_block_operation_id IS NULL THEN
                SELECT * INTO prior FROM public.recovery_operations
                WHERE id = OLD.reset_block_operation_id
                  AND organization_id = NEW.organization_id
                  AND staff_id = NEW.id AND auth_user_id = NEW.auth_user_id;
                IF NOT FOUND OR prior.state <> 'FINALIZED' OR prior.invalidated OR
                   NOT public.recovery_native_verified(prior.id) OR
                   NEW.security_revision <> prior.reserved_security_revision THEN
                    RAISE EXCEPTION 'Recovery block clearance denied'
                        USING ERRCODE = '23514';
                END IF;
                RETURN NEW;
            END IF;
            SELECT * INTO successor FROM public.recovery_operations
            WHERE id = NEW.reset_block_operation_id AND organization_id = NEW.organization_id
              AND staff_id = NEW.id AND auth_user_id = NEW.auth_user_id;
            IF NOT FOUND OR successor.state <> 'RESERVED' OR successor.invalidated OR
               successor.closed_at IS NOT NULL OR successor.auth_fence <> 0 OR
               NEW.role <> 'DOCTOR_OWNER' OR
               NEW.security_revision <> successor.reserved_security_revision OR
               NEW.revision <> successor.captured_staff_revision OR
               NOT EXISTS (
                   SELECT 1 FROM public.verification v
                   JOIN public.delivery_jobs j ON j.verification_id = v.id
                       AND j.organization_id = successor.organization_id
                       AND j.staff_id = successor.staff_id
                       AND j.purpose = 'PASSWORD_RESET'
                       AND j.proof_reference = successor.proof_digest
                   WHERE v.id = successor.proof_reference
                     AND v.value = successor.auth_user_id
                     AND v."expiresAt" = successor.proof_expires_at
                     AND j.proof_expires_at = successor.proof_expires_at
                     AND j.state NOT IN ('FAILED', 'CANCELLED')
                     AND clock_timestamp() < v."expiresAt"
               ) THEN
                RAISE EXCEPTION 'Recovery block successor denied'
                    USING ERRCODE = '23514';
            END IF;
            IF OLD.reset_block_operation_id IS NOT NULL THEN
                SELECT * INTO prior FROM public.recovery_operations
                WHERE id = OLD.reset_block_operation_id
                  AND organization_id = NEW.organization_id
                  AND staff_id = NEW.id AND auth_user_id = NEW.auth_user_id;
                IF NOT FOUND OR prior.state <> 'ABORTED' OR
                   prior.closed_at IS NULL OR
                   successor.reserved_security_revision <> prior.reserved_security_revision + 1 THEN
                    RAISE EXCEPTION 'Recovery block replacement denied'
                        USING ERRCODE = '23514';
                END IF;
            END IF;
            RETURN NEW;
        END $$
    """)
    op.execute("""
        CREATE TRIGGER zz_staff_recovery_block_guard
        BEFORE UPDATE ON public.staff_accounts
        FOR EACH ROW EXECUTE FUNCTION public.guard_staff_recovery_block()
    """)

    # Revocation and replacement remain coordinator-owned. A closed operation
    # is validated by existing deferred successor and staff-block constraints.
    op.execute(f"""
        CREATE FUNCTION public.abandon_owned_recovery(operation_id uuid)
        RETURNS text LANGUAGE plpgsql SECURITY DEFINER
        SET search_path = pg_catalog AS $$
        DECLARE old_row public.recovery_operations%ROWTYPE;
                lifecycle bigint; reference_revision text;
        BEGIN
            SELECT * INTO old_row FROM public.recovery_operations
            WHERE id = operation_id AND organization_id = {tenant} FOR UPDATE;
            IF NOT FOUND OR old_row.closed_at IS NOT NULL OR
               NOT EXISTS (
                   SELECT 1 FROM public.staff_accounts s
                   JOIN public.organizations o ON o.id = s.organization_id
                   WHERE s.organization_id = old_row.organization_id AND s.id = old_row.staff_id
                     AND s.auth_user_id = old_row.auth_user_id
                     AND s.role = 'DOCTOR_OWNER' AND s.enabled AND s.verified
                     AND s.security_revision = old_row.reserved_security_revision
                     AND s.revision = old_row.captured_staff_revision
                     AND s.reset_block_operation_id = old_row.id
                     AND o.state = 'ACTIVE'
               ) THEN
                RAISE EXCEPTION 'Recovery replacement unavailable' USING ERRCODE = '23514';
            END IF;
            IF old_row.state = 'RESERVED' AND old_row.auth_fence = 0
               AND old_row.auth_done_at IS NULL AND
               clock_timestamp() >= old_row.proof_expires_at AND
               NOT EXISTS (SELECT 1 FROM public.recovery_native_effects WHERE id = operation_id)
               AND NOT EXISTS (SELECT 1 FROM public.auth_recovery_completions WHERE id = operation_id) THEN
                UPDATE public.recovery_operations
                   SET state = 'ABORTED', closed_at = clock_timestamp(),
                       invalidated = true,
                       invalidation_reference = COALESCE(invalidation_reference,
                                                          'EXPIRED_INCOMPLETE')
                 WHERE id = operation_id;
            ELSIF old_row.state = 'AUTH_DONE' AND old_row.invalidated AND
                  public.recovery_native_verified(operation_id) THEN
                SELECT lifecycle_revision INTO lifecycle FROM public.organizations
                WHERE id = old_row.organization_id;
                reference_revision := substring(old_row.invalidation_reference from 18);
                IF old_row.invalidation_reference IS NULL OR
                   old_row.invalidation_reference NOT LIKE 'OPERATOR_SUSPEND:%' OR
                   reference_revision !~ '^[0-9]+$' OR
                   reference_revision::bigint <> old_row.captured_lifecycle_revision + 1 OR
                   lifecycle <> old_row.captured_lifecycle_revision + 2 OR
                   NOT EXISTS (
                       SELECT 1 FROM public.sensitive_audit a
                       WHERE a.organization_id = old_row.organization_id
                         AND a.recovery_operation_id = old_row.id
                         AND a.actor_role = 'OPERATOR'
                         AND a.action = 'RECOVERY_AUTH_INVALIDATED'
                         AND a.correlation_id = old_row.invalidation_reference) THEN
                    RAISE EXCEPTION 'Recovery revocation unverified' USING ERRCODE = '23514';
                END IF;
                UPDATE public.recovery_operations
                   SET state = 'ABORTED', closed_at = clock_timestamp()
                WHERE id = operation_id;
            ELSE
                RAISE EXCEPTION 'Recovery abandonment denied' USING ERRCODE = '23514';
            END IF;
            RETURN 'ABORTED';
        END $$
    """)

    op.execute(f"""
        CREATE FUNCTION public.finalize_verified_recovery(operation_id uuid)
        RETURNS text LANGUAGE plpgsql SECURITY DEFINER
        SET search_path = pg_catalog AS $$
        DECLARE record public.recovery_operations%ROWTYPE;
                staff public.staff_accounts%ROWTYPE;
                lifecycle bigint; clinic_state text;
        BEGIN
            SELECT * INTO record FROM public.recovery_operations
            WHERE id = operation_id AND organization_id = {tenant};
            IF NOT FOUND THEN
                RAISE EXCEPTION 'Recovery finalization denied' USING ERRCODE = '23514';
            END IF;
            SELECT o.lifecycle_revision, o.state INTO lifecycle, clinic_state
            FROM public.organizations o WHERE o.id = record.organization_id FOR UPDATE;
            SELECT * INTO staff FROM public.staff_accounts s
            WHERE s.organization_id = record.organization_id AND s.id = record.staff_id
              AND s.auth_user_id = record.auth_user_id FOR UPDATE;
            SELECT * INTO record FROM public.recovery_operations
            WHERE id = operation_id AND organization_id = {tenant} FOR UPDATE;
            IF NOT FOUND OR staff.id IS NULL OR
               NOT public.recovery_native_verified(operation_id) OR
               record.invalidated OR clinic_state <> 'ACTIVE' OR
               lifecycle <> record.captured_lifecycle_revision OR
               staff.role <> 'DOCTOR_OWNER' OR NOT staff.enabled OR NOT staff.verified OR
               staff.security_revision <> record.reserved_security_revision THEN
                RAISE EXCEPTION 'Recovery native completion unverified'
                    USING ERRCODE = '23514';
            END IF;
            IF record.state = 'FINALIZED' THEN
                IF staff.reset_block_operation_id IS NOT NULL OR NOT EXISTS (
                    SELECT 1 FROM public.sensitive_audit a
                    WHERE a.recovery_operation_id = record.id
                      AND a.organization_id = record.organization_id
                      AND a.actor_staff_id = record.staff_id
                      AND a.action = 'PASSWORD_RESET_SUCCEEDED') THEN
                    RAISE EXCEPTION 'Recovery audit missing' USING ERRCODE = '23514';
                END IF;
                RETURN 'FINALIZED';
            END IF;
            IF record.state <> 'AUTH_DONE' OR
               staff.reset_block_operation_id <> record.id OR
               staff.revision <> record.captured_staff_revision THEN
                RAISE EXCEPTION 'Recovery finalization state conflict' USING ERRCODE = '23514';
            END IF;
            UPDATE public.recovery_operations SET state = 'FINALIZED',
                closed_at = clock_timestamp() WHERE id = record.id;
            UPDATE public.staff_accounts SET reset_block_operation_id = NULL
            WHERE id = staff.id AND organization_id = record.organization_id
              AND reset_block_operation_id = record.id;
            INSERT INTO public.sensitive_audit
                (id, organization_id, actor_staff_id, actor_role, action,
                 reason, correlation_id, recovery_operation_id)
            VALUES (gen_random_uuid(), record.organization_id, record.staff_id,
                    'SYSTEM', 'PASSWORD_RESET_SUCCEEDED',
                    'Confirmed native credential reset', record.id::text, record.id);
            RETURN 'FINALIZED';
        END $$
    """)

    op.execute(f"""
        CREATE FUNCTION public.delete_staff_sessions(tenant_id uuid, native_user_id text)
        RETURNS integer LANGUAGE plpgsql SECURITY DEFINER
        SET search_path = pg_catalog AS $$
        DECLARE deleted integer; live_revision bigint;
        BEGIN
            IF tenant_id IS NULL OR tenant_id IS DISTINCT FROM {tenant} THEN
                RAISE EXCEPTION 'Session cleanup denied' USING ERRCODE = '42501';
            END IF;
            SELECT s.security_revision INTO live_revision
            FROM public.staff_accounts s
            WHERE s.organization_id = tenant_id AND s.auth_user_id = native_user_id
              AND s.role = 'RECEPTIONIST';
            IF NOT FOUND THEN
                RAISE EXCEPTION 'Session cleanup denied' USING ERRCODE = '42501';
            END IF;
            -- Cleanup is best effort after an authority revision increase.
            -- A runtime caller cannot destroy a currently authorized session.
            DELETE FROM public."session"
            WHERE "userId" = native_user_id
              AND "securityRevision" < live_revision;
            GET DIAGNOSTICS deleted = ROW_COUNT;
            RETURN deleted;
        END $$
    """)

    for signature in (
        "lock_native_session_authority(uuid,text,text)",
        "recovery_native_verified(uuid)",
        "commit_native_recovery_effect(uuid)",
        "capture_native_recovery_effect()",
        "assert_native_recovery_commit()",
        "guard_staff_recovery_block()",
        "abandon_owned_recovery(uuid)",
        "finalize_verified_recovery(uuid)",
        "delete_staff_sessions(uuid,text)",
    ):
        op.execute(f"REVOKE ALL ON FUNCTION public.{signature} FROM PUBLIC")
    for signature in (
        "lock_native_session_authority(uuid,text,text)",
        "recovery_native_verified(uuid)",
        "abandon_owned_recovery(uuid)",
        "finalize_verified_recovery(uuid)",
        "delete_staff_sessions(uuid,text)",
    ):
        op.execute(f"GRANT EXECUTE ON FUNCTION public.{signature} TO carepulse_runtime")


def downgrade() -> None:
    # Never downgrade the credential provenance boundary after a recovery
    # operation has been created. The older grants could forge completion.
    raise RuntimeError("B-04 native credential boundary is forward-only")

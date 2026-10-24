"""B-03 restricted operator commands and first-Owner bootstrap gate."""

from alembic import op

revision = "0008_provisioning_admin"
down_revision = "0007_bff_sessions"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
        CREATE TABLE public.operator_auth_attempts (
            operator_id uuid PRIMARY KEY,
            attempts integer NOT NULL CHECK (attempts > 0),
            window_start timestamptz NOT NULL
        )
    """)
    op.execute("REVOKE ALL ON public.operator_auth_attempts FROM PUBLIC")
    # These rows are inserted only by the SECURITY DEFINER operator commands.
    # A deferred constraint requires the full CLI command's audit, receipt and
    # protected delivery evidence at COMMIT, including for direct SQL callers.
    # Migration-role fixtures can still create unrelated synthetic tenant rows.
    op.execute("""
        CREATE TABLE public.operator_creation_pending (
            organization_id uuid PRIMARY KEY REFERENCES public.organizations(id),
            operator_id uuid NOT NULL REFERENCES public.operator_identities(id)
        )
    """)
    op.execute("""
        CREATE TABLE public.operator_invitation_pending (
            invitation_id uuid PRIMARY KEY REFERENCES public.invitations(id),
            organization_id uuid NOT NULL REFERENCES public.organizations(id),
            operator_id uuid NOT NULL REFERENCES public.operator_identities(id),
            expected_audit_action text NOT NULL CHECK
                (expected_audit_action IN
                  ('OWNER_INVITE_ISSUED', 'OWNER_INVITE_REISSUED'))
        )
    """)
    op.execute("""
        CREATE TABLE public.operator_action_pending (
            id uuid PRIMARY KEY,
            organization_id uuid NOT NULL REFERENCES public.organizations(id),
            operator_id uuid NOT NULL REFERENCES public.operator_identities(id),
            action text NOT NULL CHECK (action IN
                ('OWNER_INVITE_REVOKED', 'CLINIC_SUSPENDED', 'CLINIC_REACTIVATED')),
            correlation_id text NOT NULL CHECK (btrim(correlation_id) <> '')
        )
    """)
    op.execute("""
        CREATE FUNCTION public.assert_operator_provisioning_complete()
        RETURNS trigger LANGUAGE plpgsql SECURITY DEFINER
        SET search_path = pg_catalog AS $$
        BEGIN
            IF TG_TABLE_NAME = 'operator_creation_pending' THEN
                IF NOT EXISTS (
                    SELECT 1 FROM public.organizations o
                    JOIN public.idempotency_receipts r
                      ON r.result_organization_id = o.id
                     AND r.operator_id = NEW.operator_id
                     AND r.scope_kind = 'OPERATOR_CREATE'
                     AND r.scope_id = NEW.operator_id
                     AND r.actor_kind = 'OPERATOR'
                     AND r.actor_id = NEW.operator_id
                     AND r.operation = 'clinics.create'
                     AND r.result_revision = 0
                    JOIN public.sensitive_audit a
                      ON a.organization_id = o.id
                     AND a.operator_id = NEW.operator_id
                     AND a.actor_role = 'OPERATOR'
                     AND a.action = 'CLINIC_CREATED'
                     AND a.correlation_id = r.idempotency_key
                    JOIN public.invitations i
                      ON i.organization_id = o.id
                     AND i.issued_by_operator_id = NEW.operator_id
                     AND i.purpose = 'FIRST_OWNER' AND i.state = 'ISSUED'
                    JOIN public.delivery_jobs j ON j.invitation_id = i.id
                      AND j.organization_id = o.id AND j.purpose = 'FIRST_OWNER'
                    JOIN public.protected_delivery_material m ON m.job_id = j.id
                    WHERE o.id = NEW.organization_id
                      AND o.created_by_operator_id = NEW.operator_id
                ) THEN
                    RAISE EXCEPTION 'Incomplete operator clinic creation'
                        USING ERRCODE = '23514';
                END IF;
            ELSIF TG_TABLE_NAME = 'operator_invitation_pending' THEN
                IF NOT EXISTS (
                    SELECT 1 FROM public.invitations i
                    JOIN public.delivery_jobs j
                      ON j.invitation_id = i.id
                     AND j.organization_id = i.organization_id
                     AND j.purpose = 'FIRST_OWNER'
                    JOIN public.protected_delivery_material m ON m.job_id = j.id
                    JOIN public.sensitive_audit a
                      ON a.organization_id = i.organization_id
                     AND a.operator_id = NEW.operator_id
                     AND a.actor_role = 'OPERATOR'
                     AND a.action = NEW.expected_audit_action
                     AND a.correlation_id = i.id::text
                    WHERE i.id = NEW.invitation_id
                      AND i.organization_id = NEW.organization_id
                      AND i.issued_by_operator_id = NEW.operator_id
                      AND i.purpose = 'FIRST_OWNER' AND i.state = 'ISSUED'
                ) THEN
                    RAISE EXCEPTION 'Incomplete operator invitation issuance'
                        USING ERRCODE = '23514';
                END IF;
            ELSE
                IF NOT EXISTS (
                    SELECT 1 FROM public.sensitive_audit a
                    WHERE a.organization_id = NEW.organization_id
                      AND a.operator_id = NEW.operator_id
                      AND a.actor_role = 'OPERATOR'
                      AND a.action = NEW.action
                      AND a.correlation_id = NEW.correlation_id
                ) THEN
                    RAISE EXCEPTION 'Incomplete audited operator action'
                        USING ERRCODE = '23514';
                END IF;
            END IF;
            RETURN NULL;
        END
        $$
    """)
    op.execute("""
        REVOKE ALL ON FUNCTION public.assert_operator_provisioning_complete()
        FROM PUBLIC
    """)
    for table in (
        "operator_creation_pending",
        "operator_invitation_pending",
        "operator_action_pending",
    ):
        op.execute(f"""
            CREATE CONSTRAINT TRIGGER {table}_complete
            AFTER INSERT ON public.{table}
            DEFERRABLE INITIALLY DEFERRED
            FOR EACH ROW EXECUTE FUNCTION public.assert_operator_provisioning_complete()
        """)
    op.execute("""
        CREATE FUNCTION public.operator_secret_matches(target uuid, supplied text)
        RETURNS boolean LANGUAGE plpgsql SECURITY DEFINER
        SET search_path = pg_catalog AS $$
        DECLARE expected text; active boolean; difference integer := 0; index integer;
        BEGIN
            SELECT credential_digest, enabled INTO expected, active
            FROM public.operator_identities WHERE id = target;
            IF expected !~ '^[0-9a-f]{64}$' OR expected IS NULL THEN
                expected := repeat('0', 64);
            END IF;
            IF supplied !~ '^[0-9a-f]{64}$' OR supplied IS NULL THEN
                supplied := repeat('0', 64);
                difference := 1;
            END IF;
            FOR index IN 0..63 LOOP
                difference := difference | (
                    get_byte(convert_to(expected, 'UTF8'), index) #
                    get_byte(convert_to(supplied, 'UTF8'), index));
            END LOOP;
            RETURN COALESCE(active, false) AND difference = 0
                AND expected <> repeat('0', 64);
        END
        $$
    """)
    op.execute("""
        CREATE FUNCTION public.authenticate_operator(
            target uuid, supplied text, max_attempts integer, window_minutes integer)
        RETURNS boolean LANGUAGE plpgsql SECURITY DEFINER
        SET search_path = pg_catalog AS $$
        DECLARE attempt_count integer;
        BEGIN
            -- Limits are supplied by the required validated active policy;
            -- no timing or retry values are chosen by this migration.
            IF max_attempts IS NULL OR max_attempts <= 0 OR
               window_minutes IS NULL OR window_minutes <= 0 THEN
                RAISE EXCEPTION 'Operator authentication policy unavailable'
                    USING ERRCODE = '42501';
            END IF;
            INSERT INTO public.operator_auth_attempts(operator_id, attempts, window_start)
            VALUES (target, 1, clock_timestamp())
            ON CONFLICT (operator_id) DO UPDATE SET
              attempts = CASE WHEN clock_timestamp() >=
                         public.operator_auth_attempts.window_start +
                         make_interval(mins => window_minutes)
                    THEN 1 ELSE public.operator_auth_attempts.attempts + 1 END,
              window_start = CASE WHEN clock_timestamp() >=
                         public.operator_auth_attempts.window_start +
                         make_interval(mins => window_minutes)
                    THEN clock_timestamp() ELSE public.operator_auth_attempts.window_start END
            RETURNING attempts INTO attempt_count;
            RETURN attempt_count <= max_attempts
                AND public.operator_secret_matches(target, supplied);
        END
        $$
    """)
    op.execute("""
        CREATE FUNCTION public.operator_create_clinic(
            actor uuid, supplied text, clinic uuid, branch_name text)
        RETURNS bigint LANGUAGE plpgsql SECURITY DEFINER
        SET search_path = pg_catalog AS $$
        BEGIN
            IF NOT public.operator_secret_matches(actor, supplied) THEN
                RAISE EXCEPTION 'Operator denied' USING ERRCODE = '42501';
            END IF;
            INSERT INTO public.organizations(id, branch, created_by_operator_id)
            VALUES (clinic, branch_name, actor);
            INSERT INTO public.operator_creation_pending(organization_id, operator_id)
            VALUES (clinic, actor);
            RETURN 0;
        END
        $$
    """)
    op.execute("""
        CREATE FUNCTION public.operator_issue_owner_invitation(
            actor uuid, supplied text, clinic uuid, issued_invite uuid,
            target_email text, token_digest text, expires timestamptz,
            prior_invite uuid, expected_clinic_revision bigint,
            expected_invitation_revision bigint)
        RETURNS bigint LANGUAGE plpgsql SECURITY DEFINER
        SET search_path = pg_catalog AS $$
        DECLARE org public.organizations%ROWTYPE; old_invite public.invitations%ROWTYPE;
        BEGIN
            IF NOT public.operator_secret_matches(actor, supplied) THEN
                RAISE EXCEPTION 'Operator denied' USING ERRCODE = '42501';
            END IF;
            SELECT * INTO org FROM public.organizations WHERE id = clinic FOR UPDATE;
            IF NOT FOUND OR org.state <> 'PROVISIONING' OR
               org.lifecycle_revision <> expected_clinic_revision THEN
                RAISE EXCEPTION 'Invitation lifecycle conflict' USING ERRCODE = '40001';
            END IF;
            IF prior_invite IS NOT NULL THEN
                SELECT * INTO old_invite FROM public.invitations
                WHERE id = prior_invite AND organization_id = clinic
                  AND purpose = 'FIRST_OWNER' FOR UPDATE;
                IF NOT FOUND OR old_invite.state <> 'ISSUED' OR
                   old_invite.revision <> expected_invitation_revision OR
                   old_invite.target_email <> target_email THEN
                    RAISE EXCEPTION 'Invitation revision conflict' USING ERRCODE = '40001';
                END IF;
                UPDATE public.invitations SET state = 'REVOKED',
                    superseded_by_id = issued_invite,
                    revocation_reference = 'OPERATOR_REISSUE'
                WHERE id = prior_invite;
            ELSIF expected_invitation_revision <> 0 THEN
                RAISE EXCEPTION 'Unexpected invitation revision' USING ERRCODE = '40001';
            END IF;
            IF expires <= clock_timestamp() THEN
                RAISE EXCEPTION 'Invitation expiry unavailable' USING ERRCODE = '23514';
            END IF;
            INSERT INTO public.invitations (
                id, organization_id, purpose, target_email, proof_digest,
                expires_at, issued_by_operator_id)
            VALUES (issued_invite, clinic, 'FIRST_OWNER', target_email,
                    token_digest, expires, actor);
            INSERT INTO public.operator_invitation_pending
                (invitation_id, organization_id, operator_id, expected_audit_action)
            VALUES (issued_invite, clinic, actor,
                    CASE WHEN prior_invite IS NULL THEN 'OWNER_INVITE_ISSUED'
                         ELSE 'OWNER_INVITE_REISSUED' END);
            RETURN 0;
        END
        $$
    """)
    op.execute("""
        CREATE FUNCTION public.operator_revoke_owner_invitation(
            actor uuid, supplied text, clinic uuid, invite uuid,
            expected_clinic_revision bigint, expected_invitation_revision bigint)
        RETURNS bigint LANGUAGE plpgsql SECURITY DEFINER
        SET search_path = pg_catalog AS $$
        DECLARE org public.organizations%ROWTYPE; invitation public.invitations%ROWTYPE;
        BEGIN
            IF NOT public.operator_secret_matches(actor, supplied) THEN
                RAISE EXCEPTION 'Operator denied' USING ERRCODE = '42501';
            END IF;
            SELECT * INTO org FROM public.organizations WHERE id = clinic FOR UPDATE;
            IF NOT FOUND OR org.lifecycle_revision <> expected_clinic_revision OR
               org.state <> 'PROVISIONING' THEN
                RAISE EXCEPTION 'Clinic revision conflict' USING ERRCODE = '40001';
            END IF;
            SELECT * INTO invitation FROM public.invitations
            WHERE id = invite AND organization_id = clinic AND purpose = 'FIRST_OWNER'
            FOR UPDATE;
            IF NOT FOUND OR invitation.state <> 'ISSUED' OR
               invitation.revision <> expected_invitation_revision THEN
                RAISE EXCEPTION 'Invitation revision conflict' USING ERRCODE = '40001';
            END IF;
            UPDATE public.invitations SET state = 'REVOKED',
                revocation_reference = 'OPERATOR_REVOKE' WHERE id = invite;
            INSERT INTO public.operator_action_pending
                (id, organization_id, operator_id, action, correlation_id)
            VALUES (gen_random_uuid(), clinic, actor, 'OWNER_INVITE_REVOKED', invite::text);
            RETURN invitation.revision + 1;
        END
        $$
    """)
    op.execute("""
        CREATE FUNCTION public.operator_get_owner_invitation(
            actor uuid, supplied text, clinic uuid, invite uuid, reason text)
        RETURNS TABLE(target_email text, state text, revision bigint)
        LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog AS $$
        BEGIN
            IF NOT public.operator_secret_matches(actor, supplied) THEN
                RAISE EXCEPTION 'Operator denied' USING ERRCODE = '42501';
            END IF;
            IF reason IS NULL OR btrim(reason) = '' THEN
                RAISE EXCEPTION 'Operator reason required' USING ERRCODE = '42501';
            END IF;
            PERFORM public.append_operator_metadata_audit(
                actor, supplied, gen_random_uuid(), clinic,
                'OWNER_INVITE_LOOKUP', reason, invite::text);
            RETURN QUERY SELECT i.target_email, i.state, i.revision
            FROM public.invitations i
            WHERE i.organization_id = clinic AND i.id = invite
              AND i.purpose = 'FIRST_OWNER';
        END
        $$
    """)
    op.execute("""
        CREATE FUNCTION public.operator_change_clinic_state(
            actor uuid, supplied text, clinic uuid, expected_revision bigint,
            next_state text)
        RETURNS bigint LANGUAGE plpgsql SECURITY DEFINER
        SET search_path = pg_catalog AS $$
        DECLARE org public.organizations%ROWTYPE; staff_id uuid; operation_id uuid;
        BEGIN
            IF NOT public.operator_secret_matches(actor, supplied) OR
               next_state NOT IN ('SUSPENDED', 'ACTIVE') THEN
                RAISE EXCEPTION 'Operator denied' USING ERRCODE = '42501';
            END IF;
            SELECT * INTO org FROM public.organizations WHERE id = clinic FOR UPDATE;
            IF NOT FOUND OR org.lifecycle_revision <> expected_revision OR
               (next_state = 'ACTIVE' AND
                (org.state <> 'SUSPENDED' OR org.first_activated_at IS NULL)) OR
               (next_state = 'SUSPENDED' AND
                org.state NOT IN ('PROVISIONING', 'ACTIVE')) THEN
                RAISE EXCEPTION 'Clinic lifecycle conflict' USING ERRCODE = '40001';
            END IF;
            FOR staff_id IN SELECT id FROM public.staff_accounts
                WHERE organization_id = clinic ORDER BY id FOR UPDATE LOOP
                NULL;
            END LOOP;
            IF next_state = 'ACTIVE' AND NOT EXISTS (
                SELECT 1 FROM public.staff_accounts s
                JOIN public."user" u ON u.id = s.auth_user_id
                WHERE s.organization_id = clinic AND s.role = 'DOCTOR_OWNER'
                  AND s.enabled AND s.verified AND s.reset_block_operation_id IS NULL
                  AND u."emailVerified" AND u.email = s.verified_email
            ) THEN
                RAISE EXCEPTION 'Verified Owner unavailable' USING ERRCODE = '40001';
            END IF;
            IF next_state = 'SUSPENDED' THEN
                FOR operation_id IN SELECT id FROM public.recovery_operations
                    WHERE organization_id = clinic AND closed_at IS NULL
                    ORDER BY id FOR UPDATE LOOP
                    UPDATE public.recovery_operations SET invalidated = true,
                        invalidation_reference = 'OPERATOR_SUSPEND'
                    WHERE id = operation_id AND NOT invalidated;
                END LOOP;
            END IF;
            UPDATE public.organizations SET state = next_state WHERE id = clinic;
            INSERT INTO public.operator_action_pending
                (id, organization_id, operator_id, action, correlation_id)
            VALUES (gen_random_uuid(), clinic, actor,
                CASE WHEN next_state = 'SUSPENDED' THEN 'CLINIC_SUSPENDED'
                     ELSE 'CLINIC_REACTIVATED' END,
                clinic::text || ':' || (expected_revision + 1)::text || ':' || next_state);
            RETURN expected_revision + 1;
        END
        $$
    """)
    op.execute("""
        CREATE FUNCTION public.operator_list_clinics(actor uuid, supplied text, reason text)
        RETURNS TABLE (id uuid, branch text, state text, lifecycle_revision bigint,
                       first_activated_at timestamptz, invitation_id uuid,
                       invitation_state text, invitation_revision bigint)
        LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog AS $$
        BEGIN
            IF NOT public.operator_secret_matches(actor, supplied) THEN
                RAISE EXCEPTION 'Operator denied' USING ERRCODE = '42501';
            END IF;
            IF reason IS NULL OR btrim(reason) = '' THEN
                RAISE EXCEPTION 'Operator reason required' USING ERRCODE = '42501';
            END IF;
            PERFORM public.append_operator_metadata_audit(
                actor, supplied, gen_random_uuid(), NULL, 'CLINICS_LIST',
                reason, gen_random_uuid()::text);
            RETURN QUERY SELECT o.id, o.branch, o.state, o.lifecycle_revision,
                o.first_activated_at, i.id, i.state, i.revision
                FROM public.organizations o LEFT JOIN LATERAL (
                    SELECT v.id, v.state, v.revision FROM public.invitations v
                    WHERE v.organization_id = o.id AND v.purpose = 'FIRST_OWNER'
                    ORDER BY v.issued_at DESC, v.id DESC LIMIT 1
                ) i ON true ORDER BY o.created_at DESC, o.id DESC;
        END
        $$
    """)
    op.execute("""
        CREATE FUNCTION public.verify_first_owner_preparation(
            clinic uuid, invite uuid, supplied_digest text)
        RETURNS TABLE(target_email text, lifecycle_revision bigint,
                      invitation_revision bigint)
        LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog AS $$
        BEGIN
            IF clinic::text IS DISTINCT FROM
               NULLIF(current_setting('app.organization_id', true), '') THEN
                RETURN;
            END IF;
            RETURN QUERY SELECT i.target_email, o.lifecycle_revision, i.revision
            FROM public.organizations o JOIN public.invitations i
              ON i.organization_id = o.id
            WHERE o.id = clinic AND o.state = 'PROVISIONING'
              AND i.id = invite AND i.purpose = 'FIRST_OWNER'
              AND i.state = 'ISSUED' AND clock_timestamp() < i.expires_at
              AND supplied_digest ~ '^[0-9a-f]{64}$'
              AND i.proof_digest = supplied_digest;
        END
        $$
    """)
    op.execute("""
        CREATE FUNCTION public.activate_first_owner(
            clinic uuid, invite uuid, supplied_proof text,
            prepared_user text, expected_clinic_revision bigint,
            expected_invitation_revision bigint, new_staff uuid)
        RETURNS TABLE(owner_id uuid, current_revision bigint, changed boolean)
        LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog AS $$
        DECLARE org public.organizations%ROWTYPE;
                invited public.invitations%ROWTYPE;
                native_user public."user"%ROWTYPE;
                existing_owner uuid;
        BEGIN
            IF clinic::text IS DISTINCT FROM
               NULLIF(current_setting('app.organization_id', true), '') THEN
                RAISE EXCEPTION 'Bootstrap scope denied' USING ERRCODE = '42501';
            END IF;
            SELECT * INTO org FROM public.organizations WHERE id = clinic FOR UPDATE;
            SELECT * INTO invited FROM public.invitations
            WHERE id = invite AND organization_id = clinic FOR UPDATE;
            IF NOT FOUND OR org.id IS NULL OR invited.purpose <> 'FIRST_OWNER' OR
               invited.proof_digest <>
                 encode(sha256(convert_to(supplied_proof, 'UTF8')), 'hex') THEN
                RAISE EXCEPTION 'Bootstrap proof denied' USING ERRCODE = '42501';
            END IF;
            SELECT * INTO native_user FROM public."user"
            WHERE id = prepared_user AND "emailVerified" = true
              AND email = invited.target_email;
            IF NOT FOUND THEN
                RAISE EXCEPTION 'Unverified prepared identity' USING ERRCODE = '42501';
            END IF;
            IF invited.state = 'ACCEPTED' AND invited.accepted_by_user_id = prepared_user
               AND org.state = 'ACTIVE' THEN
                SELECT s.id INTO existing_owner FROM public.staff_accounts s
                WHERE s.organization_id = clinic AND s.auth_user_id = prepared_user
                  AND s.role = 'DOCTOR_OWNER' AND s.enabled AND s.verified;
                IF FOUND THEN
                    RETURN QUERY SELECT existing_owner, org.lifecycle_revision, false;
                    RETURN;
                END IF;
            END IF;
            IF org.state <> 'PROVISIONING' OR invited.state <> 'ISSUED' OR
               clock_timestamp() >= invited.expires_at OR
               org.lifecycle_revision <> expected_clinic_revision OR
               invited.revision <> expected_invitation_revision OR
               EXISTS (SELECT 1 FROM public.staff_accounts s
                       WHERE s.organization_id = clinic AND s.role = 'DOCTOR_OWNER') THEN
                RAISE EXCEPTION 'Bootstrap state conflict' USING ERRCODE = '40001';
            END IF;
            INSERT INTO public.staff_accounts
                (id, organization_id, auth_user_id, verified_email, role,
                 enabled, verified)
            VALUES (new_staff, clinic, prepared_user, invited.target_email,
                    'DOCTOR_OWNER', true, true);
            UPDATE public.invitations SET state = 'ACCEPTED',
                accepted_by_user_id = prepared_user,
                acceptance_reference = 'SIGNED_OWNER_ACTIVATION'
            WHERE id = invite;
            UPDATE public.organizations SET state = 'ACTIVE' WHERE id = clinic;
            INSERT INTO public.sensitive_audit
                (id, organization_id, actor_role, action, correlation_id)
            VALUES (gen_random_uuid(), clinic, 'SYSTEM',
                    'FIRST_OWNER_ACTIVATED', invite::text);
            RETURN QUERY SELECT new_staff, expected_clinic_revision + 1, true;
        END
        $$
    """)
    for signature in (
        "operator_secret_matches(uuid,text)",
        "authenticate_operator(uuid,text,integer,integer)",
        "operator_create_clinic(uuid,text,uuid,text)",
        "operator_issue_owner_invitation(uuid,text,uuid,uuid,text,text,timestamptz,uuid,bigint,bigint)",
        "operator_revoke_owner_invitation(uuid,text,uuid,uuid,bigint,bigint)",
        "operator_get_owner_invitation(uuid,text,uuid,uuid,text)",
        "operator_change_clinic_state(uuid,text,uuid,bigint,text)",
        "operator_list_clinics(uuid,text,text)",
        "verify_first_owner_preparation(uuid,uuid,text)",
        "activate_first_owner(uuid,uuid,text,text,bigint,bigint,uuid)",
    ):
        op.execute(f"REVOKE ALL ON FUNCTION public.{signature} FROM PUBLIC")
    for signature in (
        "authenticate_operator(uuid,text,integer,integer)",
        "operator_create_clinic(uuid,text,uuid,text)",
        "operator_issue_owner_invitation(uuid,text,uuid,uuid,text,text,timestamptz,uuid,bigint,bigint)",
        "operator_revoke_owner_invitation(uuid,text,uuid,uuid,bigint,bigint)",
        "operator_get_owner_invitation(uuid,text,uuid,uuid,text)",
        "operator_change_clinic_state(uuid,text,uuid,bigint,text)",
        "operator_list_clinics(uuid,text,text)",
    ):
        op.execute(
            f"GRANT EXECUTE ON FUNCTION public.{signature} TO carepulse_operator"
        )
    for signature in (
        "verify_first_owner_preparation(uuid,uuid,text)",
        "activate_first_owner(uuid,uuid,text,text,bigint,bigint,uuid)",
    ):
        op.execute(f"GRANT EXECUTE ON FUNCTION public.{signature} TO carepulse_runtime")
    op.execute("""
        REVOKE SELECT (id, branch, region, timezone, currency, state,
                       lifecycle_revision, created_at, updated_at)
        ON public.organizations FROM carepulse_operator
    """)
    op.execute("""
        REVOKE SELECT (id, enabled, credential_revision, issued_at,
                       issued_by_reference, revoked_at, revoked_by_reference)
        ON public.operator_identities FROM carepulse_operator
    """)

    tenant = "NULLIF(current_setting('app.organization_id', true), '')::uuid"
    for table in ("organizations", "staff_accounts", "invitations", "delivery_jobs"):
        op.execute(f"""
            CREATE POLICY {table}_sender_access ON public.{table}
            FOR SELECT TO carepulse_sender
            USING ({"id" if table == "organizations" else "organization_id"} = {tenant})
        """)
    op.execute(f"""
        CREATE POLICY delivery_jobs_sender_update ON public.delivery_jobs
        FOR UPDATE TO carepulse_sender
        USING (organization_id = {tenant}) WITH CHECK (organization_id = {tenant})
    """)
    op.execute(f"""
        CREATE POLICY delivery_material_sender_delete ON public.protected_delivery_material
        FOR DELETE TO carepulse_sender USING (
            EXISTS (SELECT 1 FROM public.delivery_jobs j WHERE j.id = job_id
                    AND j.organization_id = {tenant}
                    AND j.state IN ('SENT', 'FAILED', 'CANCELLED')))
    """)
    op.execute(f"""
        CREATE POLICY delivery_material_sender_locator ON public.protected_delivery_material
        FOR SELECT TO carepulse_sender USING (
            EXISTS (SELECT 1 FROM public.delivery_jobs j WHERE j.id = job_id
                    AND j.organization_id = {tenant}
                    AND j.state IN ('SENT', 'FAILED', 'CANCELLED')))
    """)
    op.execute("""
        GRANT SELECT (id, state) ON public.organizations TO carepulse_sender
    """)
    op.execute("""
        GRANT SELECT (id, organization_id, enabled, verified, verified_email)
        ON public.staff_accounts TO carepulse_sender
    """)
    op.execute("""
        GRANT SELECT (id, organization_id, state, expires_at, target_email, purpose,
                      proof_digest)
        ON public.invitations TO carepulse_sender
    """)
    op.execute("""
        GRANT SELECT (id, organization_id, staff_id, invitation_id, verification_id,
             recipient, proof_expires_at, state, attempts, claim_generation,
             lease_expires_at, not_before, claim_owner, proof_reference, purpose,
             template, logical_key, claimed_at), UPDATE
        ON public.delivery_jobs TO carepulse_sender
    """)
    op.execute("""
        GRANT SELECT (job_id), DELETE ON public.protected_delivery_material
        TO carepulse_sender
    """)
    op.execute("""
        GRANT SELECT (id, "expiresAt") ON public."verification" TO carepulse_sender
    """)
    for signature in (
        "is_delivery_claim_owner(uuid,uuid,bigint)",
        "claimed_delivery_material(uuid,uuid,bigint)",
    ):
        op.execute(f"GRANT EXECUTE ON FUNCTION public.{signature} TO carepulse_sender")


def downgrade() -> None:
    op.execute("""
        DO $$ BEGIN
            IF EXISTS (SELECT 1 FROM public.organizations) OR
               EXISTS (SELECT 1 FROM public.operator_auth_attempts) THEN
                RAISE EXCEPTION 'Cannot downgrade populated provisioning state'
                    USING ERRCODE = '23514';
            END IF;
        END $$
    """)
    for table in ("organizations", "staff_accounts", "invitations", "delivery_jobs"):
        op.execute(f"DROP POLICY {table}_sender_access ON public.{table}")
    op.execute("DROP POLICY delivery_jobs_sender_update ON public.delivery_jobs")
    op.execute(
        "DROP POLICY delivery_material_sender_delete ON public.protected_delivery_material"
    )
    op.execute(
        "DROP POLICY delivery_material_sender_locator ON public.protected_delivery_material"
    )
    for table in (
        "organizations",
        "staff_accounts",
        "invitations",
        "delivery_jobs",
        "protected_delivery_material",
        '"verification"',
    ):
        op.execute(f"REVOKE ALL ON public.{table} FROM carepulse_sender")
    for signature in (
        "is_delivery_claim_owner(uuid,uuid,bigint)",
        "claimed_delivery_material(uuid,uuid,bigint)",
    ):
        op.execute(
            f"REVOKE EXECUTE ON FUNCTION public.{signature} FROM carepulse_sender"
        )
    op.execute(
        "GRANT SELECT (id, branch, region, timezone, currency, state, lifecycle_revision, created_at, updated_at) ON public.organizations TO carepulse_operator"
    )
    op.execute(
        "GRANT SELECT (id, enabled, credential_revision, issued_at, issued_by_reference, revoked_at, revoked_by_reference) ON public.operator_identities TO carepulse_operator"
    )
    for signature in (
        "activate_first_owner(uuid,uuid,text,text,bigint,bigint,uuid)",
        "verify_first_owner_preparation(uuid,uuid,text)",
        "operator_list_clinics(uuid,text,text)",
        "operator_change_clinic_state(uuid,text,uuid,bigint,text)",
        "operator_revoke_owner_invitation(uuid,text,uuid,uuid,bigint,bigint)",
        "operator_get_owner_invitation(uuid,text,uuid,uuid,text)",
        "operator_issue_owner_invitation(uuid,text,uuid,uuid,text,text,timestamptz,uuid,bigint,bigint)",
        "operator_create_clinic(uuid,text,uuid,text)",
        "authenticate_operator(uuid,text,integer,integer)",
        "operator_secret_matches(uuid,text)",
    ):
        op.execute(f"DROP FUNCTION public.{signature}")
    op.execute("DROP TABLE public.operator_auth_attempts")
    op.execute("DROP TABLE public.operator_action_pending")
    op.execute("DROP TABLE public.operator_invitation_pending")
    op.execute("DROP TABLE public.operator_creation_pending")
    op.execute("DROP FUNCTION public.assert_operator_provisioning_complete()")

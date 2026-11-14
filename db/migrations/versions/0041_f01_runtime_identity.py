"""F-01: bind independently verified BFF requests to actual database transactions."""

from alembic import op

revision = "0041_f01_runtime_identity"
down_revision = "0040_t173_numeric_money"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Schema and pgcrypto are provisioned by the privileged database bootstrap.
    # No signing secrets, deployment policy or database passwords are seeded here.
    # The NOLOGIN verifier may lock authority rows; it cannot write them.
    op.execute(r"""
        GRANT USAGE, CREATE ON SCHEMA public TO carepulse_identity_owner;
        GRANT USAGE ON SCHEMA public TO carepulse_api, carepulse_gate;
        GRANT SELECT (id, state), UPDATE (id) ON public.organizations TO carepulse_identity_owner;
        GRANT SELECT (id, organization_id, auth_user_id, verified_email, role,
            enabled, verified, security_revision, reset_block_operation_id, operational_manager),
            UPDATE (id) ON public.staff_accounts TO carepulse_identity_owner;
        GRANT SELECT (id, organization_id, staff_id, purpose, target_email, proof_digest,
            state, expires_at, accepted_by_user_id), UPDATE (id)
            ON public.invitations TO carepulse_identity_owner;
        GRANT SELECT (id, email, "emailVerified"), UPDATE (id)
            ON public."user" TO carepulse_identity_owner;
        GRANT SELECT (id, "userId", "securityRevision", "expiresAt", "absoluteDeadline",
            "lastActivity"), UPDATE (id) ON public."session" TO carepulse_identity_owner;
        GRANT SELECT (id, organization_id, staff_id, auth_user_id, proof_digest,
            proof_reference, state, invalidated), UPDATE (id)
            ON public.recovery_operations TO carepulse_identity_owner;
        GRANT SELECT (id, identifier, value, "expiresAt")
            ON public.verification TO carepulse_identity_owner;
        GRANT SELECT (verification_id, organization_id, staff_id, purpose, proof_reference,
            recipient, proof_expires_at, state) ON public.delivery_jobs TO carepulse_identity_owner;
        GRANT SELECT ON public.sensitive_audit TO carepulse_identity_owner;
        CREATE POLICY organization_f01_live ON public.organizations
            FOR ALL TO carepulse_identity_owner USING (true) WITH CHECK (false);
        CREATE POLICY staff_f01_live ON public.staff_accounts
            FOR ALL TO carepulse_identity_owner USING (true) WITH CHECK (false);
        CREATE POLICY invitation_f01_live ON public.invitations
            FOR ALL TO carepulse_identity_owner USING (true) WITH CHECK (false);
        CREATE POLICY recovery_f01_live ON public.recovery_operations
            FOR ALL TO carepulse_identity_owner USING (true) WITH CHECK (false);
        CREATE POLICY delivery_f01_recovery_read ON public.delivery_jobs
            FOR SELECT TO carepulse_identity_owner USING (purpose = 'PASSWORD_RESET');
        CREATE POLICY audit_f01_security_read ON public.sensitive_audit
            FOR SELECT TO carepulse_identity_owner
            USING (action IN ('RECEPTIONIST_DISABLE', 'RECEPTIONIST_PERMISSIONS'));
        LOCK TABLE public.request_nonces IN SHARE MODE;
        GRANT SELECT (key_id, nonce) ON public.request_nonces TO carepulse_identity_owner;
        CREATE POLICY request_nonce_f01_transfer ON public.request_nonces
            FOR SELECT TO carepulse_identity_owner USING (true);
        SET LOCAL ROLE carepulse_identity_owner;
    """)
    op.execute(r"""
        REVOKE ALL ON SCHEMA f01_private FROM PUBLIC;
        CREATE TABLE f01_private.keys (
            key_id text PRIMARY KEY CHECK (key_id ~ '^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$'),
            secret bytea NOT NULL CHECK (octet_length(secret) = 32),
            status text NOT NULL CHECK (status IN ('ACTIVE', 'RETIRING', 'REVOKED')),
            issuance_cutoff_ms bigint,
            CHECK (status <> 'RETIRING' OR issuance_cutoff_ms IS NOT NULL),
            CHECK (status <> 'ACTIVE' OR issuance_cutoff_ms IS NULL)
        );
        CREATE TABLE f01_private.policy (
            singleton boolean PRIMARY KEY DEFAULT true CHECK (singleton),
            signed_request_freshness_seconds integer NOT NULL CHECK (signed_request_freshness_seconds > 0),
            signed_request_max_skew_seconds integer NOT NULL CHECK (signed_request_max_skew_seconds >= 0),
            session_idle_minutes integer NOT NULL CHECK (session_idle_minutes > 0)
        );
        CREATE TABLE f01_private.clock_state (
            singleton boolean PRIMARY KEY DEFAULT true CHECK (singleton),
            last_seen_at timestamptz NOT NULL
        );
        INSERT INTO f01_private.clock_state VALUES (true, '-infinity');
        -- Retain compact nonce tombstones even when the request registry is purged.
        -- Changing the proof-key or freshness configuration cannot revive a nonce.
        CREATE TABLE f01_private.nonces (
            http_key_id text NOT NULL,
            nonce text NOT NULL,
            PRIMARY KEY (http_key_id, nonce)
        );
        INSERT INTO f01_private.nonces (http_key_id, nonce)
            SELECT key_id, nonce FROM public.request_nonces;
        CREATE TABLE f01_private.requests (
            id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
            http_key_id text NOT NULL,
            nonce text NOT NULL,
            database_key_id text NOT NULL REFERENCES f01_private.keys(key_id),
            organization_id uuid NOT NULL,
            staff_id uuid,
            session_id text NOT NULL,
            role text NOT NULL,
            security_revision bigint NOT NULL,
            purpose text NOT NULL,
            method text NOT NULL,
            path text NOT NULL,
            query text NOT NULL,
            body_sha256 text NOT NULL,
            idempotency_key text NOT NULL,
            target_id uuid,
            proof_digest text,
            native_user_id text,
            proof_reference text,
            native_identifier_hash text,
            expected_security_revision bigint,
            expected_staff_revision bigint,
            manager_enabled boolean,
            patient_id uuid,
            source_visit_id uuid,
            target_appointment_id uuid,
            body_scope_valid boolean NOT NULL,
            issued_at_ms bigint NOT NULL,
            registered_at timestamptz NOT NULL,
            expires_at timestamptz NOT NULL,
            FOREIGN KEY (http_key_id, nonce) REFERENCES f01_private.nonces(http_key_id, nonce)
        );
        CREATE INDEX f01_request_expiry ON f01_private.requests (expires_at);
        CREATE TABLE f01_private.bindings (
            request_id bigint NOT NULL REFERENCES f01_private.requests(id) ON DELETE CASCADE,
            stage text NOT NULL CHECK (stage IN ('PRIMARY', 'RECEIPT_RECOVERY',
                'INVITATION_PREPARE', 'INVITATION_APPLY', 'SESSION_CLEANUP')),
            database_oid oid NOT NULL,
            login_oid oid NOT NULL,
            pid integer NOT NULL,
            xid xid8 NOT NULL,
            PRIMARY KEY (request_id, stage),
            UNIQUE (database_oid, xid)
        );
        CREATE TABLE f01_private.activations (
            request_id bigint NOT NULL,
            stage text NOT NULL,
            activated_at timestamptz NOT NULL,
            valid_until timestamptz NOT NULL,
            subject_staff_id uuid,
            PRIMARY KEY (request_id, stage),
            FOREIGN KEY (request_id, stage)
                REFERENCES f01_private.bindings(request_id, stage) ON DELETE CASCADE
        );
        REVOKE ALL ON ALL TABLES IN SCHEMA f01_private FROM PUBLIC;
        REVOKE ALL ON ALL SEQUENCES IN SCHEMA f01_private FROM PUBLIC;
    """)
    op.execute(r"""
        CREATE FUNCTION f01_private.gate_now() RETURNS timestamptz
        LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, pg_temp AS $$
        DECLARE previous timestamptz; observed timestamptz;
        BEGIN
            SELECT last_seen_at INTO previous FROM f01_private.clock_state
                WHERE singleton FOR UPDATE;
            observed := clock_timestamp();
            IF NOT FOUND OR observed < previous THEN
                RAISE EXCEPTION 'request clock denied' USING ERRCODE = '42501';
            END IF;
            UPDATE f01_private.clock_state SET last_seen_at = observed WHERE singleton;
            RETURN observed;
        END $$;

        CREATE FUNCTION f01_private.route_purpose(p_method text, p_path text) RETURNS text
        LANGUAGE plpgsql IMMUTABLE STRICT SET search_path = pg_catalog, pg_temp AS $$
        DECLARE uuid_pattern constant text := '[0-9a-fA-F]{8}(-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}';
        BEGIN
            RETURN CASE
                WHEN p_method = 'POST' AND p_path = '/v1/provisioning/owner-activate'
                    THEN 'OWNER_ACTIVATION'
                WHEN p_method = 'POST' AND p_path = '/v1/staff/receptionists/invite-accept'
                    THEN 'RECEPTIONIST_ACCEPT'
                WHEN p_method = 'POST' AND p_path IN ('/v1/staff/recovery/reserve',
                    '/v1/staff/recovery/finalize') THEN 'OWNER_RECOVERY'
                WHEN (p_method IN ('GET', 'POST') AND p_path = '/v1/staff/receptionists')
                    OR (p_method = 'GET' AND p_path ~ ('^/v1/staff/receptionists/' || uuid_pattern || '$'))
                    OR (p_method = 'POST' AND p_path ~ ('^/v1/staff/receptionists/' || uuid_pattern ||
                        '/(enable|disable|invite|revoke-invite)$'))
                    OR (p_method = 'PATCH' AND p_path ~ ('^/v1/staff/receptionists/' || uuid_pattern ||
                        '/permissions$')) THEN 'STAFF_MANAGEMENT'
                WHEN (p_method IN ('GET', 'POST') AND p_path = '/v1/patients')
                    OR (p_method IN ('GET', 'PATCH') AND p_path ~ ('^/v1/patients/' || uuid_pattern || '$'))
                    OR (p_method = 'POST' AND p_path ~ ('^/v1/patients/' || uuid_pattern ||
                        '/(archive|unarchive)$')) THEN 'PATIENT_REGISTRY'
                WHEN (p_method IN ('GET', 'POST') AND p_path = '/v1/appointments')
                    OR (p_method = 'GET' AND p_path ~ ('^/v1/appointments/' || uuid_pattern || '$'))
                    THEN 'APPOINTMENT_BOOKING'
                WHEN p_method IN ('GET', 'PATCH') AND p_path = '/v1/settings/pricing'
                    THEN 'SETTINGS_PRICING'
                WHEN (p_method IN ('GET', 'PATCH') AND p_path = '/v1/settings/schedule')
                    OR (p_method = 'POST' AND p_path IN ('/v1/settings/schedule/preview',
                        '/v1/settings/schedule/activate')) THEN 'SETTINGS_SCHEDULE'
                WHEN p_method = 'GET' AND p_path = '/v1/slots' THEN 'SLOTS'
                WHEN (p_method = 'PATCH' AND p_path ~ ('^/v1/visits/' || uuid_pattern || '$'))
                    OR (p_method = 'POST' AND p_path ~ ('^/v1/visits/' || uuid_pattern ||
                        '/extend-followup$')) THEN 'VISIT_FOLLOWUP'
            END;
        END $$;

        CREATE FUNCTION public.f01_register_proof(
            payload bytea, key_id text, mac bytea, body bytea) RETURNS bigint
        LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, pg_temp AS $$
        DECLARE
            signing_key f01_private.keys%ROWTYPE;
            settings f01_private.policy%ROWTYPE;
            expected bytea;
            blind bytea;
            envelope jsonb;
            command jsonb;
            req f01_private.requests%ROWTYPE;
            observed timestamptz;
            issued_at timestamptz;
            result_id bigint;
        BEGIN
            IF session_user <> 'carepulse_gate' OR payload IS NULL OR key_id IS NULL OR mac IS NULL
               OR body IS NULL OR octet_length(mac) <> 32 OR octet_length(payload) > 65536
               OR octet_length(body) > 1048576 THEN
                RAISE EXCEPTION 'proof denied' USING ERRCODE = '42501';
            END IF;
            SELECT * INTO settings FROM f01_private.policy WHERE singleton FOR SHARE;
            IF NOT FOUND THEN RAISE EXCEPTION 'proof denied' USING ERRCODE = '42501'; END IF;
            SELECT k.* INTO signing_key FROM f01_private.keys k
                WHERE k.key_id = f01_register_proof.key_id
                AND k.status IN ('ACTIVE', 'RETIRING') FOR SHARE;
            IF NOT FOUND THEN RAISE EXCEPTION 'proof denied' USING ERRCODE = '42501'; END IF;
            expected := f01_crypto.hmac(convert_to('carepulse-db-context-v1', 'UTF8')
                || decode('00', 'hex') || convert_to(key_id, 'UTF8') || decode('00', 'hex')
                || payload, signing_key.secret, 'sha256');
            blind := f01_crypto.gen_random_bytes(32);
            IF f01_crypto.hmac(mac, blind, 'sha256') <> f01_crypto.hmac(expected, blind, 'sha256') THEN
                RAISE EXCEPTION 'proof denied' USING ERRCODE = '42501';
            END IF;
            BEGIN
                envelope := convert_from(payload, 'UTF8')::jsonb;
                IF jsonb_typeof(envelope) IS DISTINCT FROM 'array' THEN
                    RAISE EXCEPTION 'invalid envelope' USING ERRCODE = '22023';
                END IF;
                IF jsonb_array_length(envelope) <> 16 OR EXISTS (
                    SELECT 1 FROM jsonb_array_elements(envelope) AS e
                    WHERE jsonb_typeof(e) <> 'string' OR length(e #>> '{}') > 4096
                        OR (e #>> '{}') ~ '[[:cntrl:]]'
                ) THEN RAISE EXCEPTION 'invalid envelope' USING ERRCODE = '22023'; END IF;
                IF envelope->>0 <> 'carepulse-request-v1'
                   OR envelope->>1 !~ '^[A-Za-z0-9_-]{1,128}$'
                   OR envelope->>2 <> 'web'
                   OR envelope->>6 IS DISTINCT FROM encode(sha256(body), 'hex')
                   OR envelope->>11 !~ '^(0|[1-9][0-9]{0,18})$'
                   OR envelope->>12 !~ '^[0-9]{13}$'
                   OR envelope->>13 !~ '^[A-Za-z0-9_-]{22,128}$'
                   OR f01_private.route_purpose(envelope->>3, envelope->>4)
                        IS DISTINCT FROM envelope->>14 THEN
                    RAISE EXCEPTION 'invalid envelope' USING ERRCODE = '22023';
                END IF;
                req.http_key_id := envelope->>1;
                req.nonce := envelope->>13;
                req.database_key_id := key_id;
                req.method := envelope->>3;
                req.path := envelope->>4;
                req.query := envelope->>5;
                req.body_sha256 := envelope->>6;
                req.organization_id := (envelope->>7)::uuid;
                req.staff_id := NULLIF(envelope->>8, '')::uuid;
                req.session_id := envelope->>9;
                req.role := envelope->>10;
                req.security_revision := (envelope->>11)::bigint;
                req.issued_at_ms := (envelope->>12)::bigint;
                req.purpose := envelope->>14;
                req.idempotency_key := envelope->>15;
                issued_at := to_timestamp(req.issued_at_ms / 1000.0);
                IF req.path ~ '^/v1/staff/receptionists/[0-9a-fA-F-]{36}(/|$)' THEN
                    req.target_id := split_part(req.path, '/', 5)::uuid;
                ELSIF req.path ~ '^/v1/(patients|appointments|visits)/[0-9a-fA-F-]{36}(/|$)' THEN
                    req.target_id := split_part(req.path, '/', 4)::uuid;
                END IF;
                IF req.purpose IN ('OWNER_ACTIVATION', 'RECEPTIONIST_ACCEPT', 'OWNER_RECOVERY') THEN
                    IF req.role <> 'BOOTSTRAP' OR req.session_id <> '' OR req.security_revision <> 0
                       OR (req.purpose = 'OWNER_RECOVERY') IS DISTINCT FROM (req.staff_id IS NOT NULL) THEN
                        RAISE EXCEPTION 'invalid envelope' USING ERRCODE = '22023';
                    END IF;
                ELSIF req.role NOT IN ('DOCTOR_OWNER', 'RECEPTIONIST')
                      OR req.staff_id IS NULL OR req.session_id = '' THEN
                    RAISE EXCEPTION 'invalid envelope' USING ERRCODE = '22023';
                END IF;
            EXCEPTION WHEN data_exception THEN
                RAISE EXCEPTION 'invalid envelope' USING ERRCODE = '22023';
            END;
            -- Keep transport replay protection even for a malformed domain command;
            -- the HTTP validator still returns its normal 422. Such a proof cannot activate.
            req.body_scope_valid := true;
            BEGIN
                IF req.purpose IN ('OWNER_ACTIVATION', 'RECEPTIONIST_ACCEPT', 'OWNER_RECOVERY') THEN
                    command := convert_from(body, 'UTF8')::jsonb;
                    IF jsonb_typeof(command) IS DISTINCT FROM 'object' THEN
                        RAISE EXCEPTION 'invalid command' USING ERRCODE = '22023';
                    END IF;
                    IF req.purpose = 'OWNER_RECOVERY' THEN
                        req.native_user_id := command->>'native_user_id';
                        req.proof_digest := command->>'proof_digest';
                        req.proof_reference := command->>'proof_reference';
                        req.native_identifier_hash := command->>'native_identifier_hash';
                        IF COALESCE(req.proof_digest, '') !~ '^[0-9a-f]{64}$'
                           OR COALESCE(req.native_identifier_hash, '') !~ '^[A-Za-z0-9_-]{43}$'
                           OR COALESCE(length(req.proof_reference), 0) NOT BETWEEN 1 AND 255 THEN
                            RAISE EXCEPTION 'invalid command' USING ERRCODE = '22023';
                        END IF;
                    ELSE
                        req.target_id := (command->>'invitation_id')::uuid;
                        req.native_user_id := command->>'prepared_user_id';
                        IF req.target_id IS NULL OR COALESCE(command->>'proof', '')
                                !~ '^[A-Za-z0-9_-]{43}$' THEN
                            RAISE EXCEPTION 'invalid command' USING ERRCODE = '22023';
                        END IF;
                        req.proof_digest := encode(sha256(convert_to(command->>'proof', 'UTF8')), 'hex');
                    END IF;
                    IF COALESCE(length(req.native_user_id), 0) NOT BETWEEN 1 AND 255 THEN
                        RAISE EXCEPTION 'invalid command' USING ERRCODE = '22023';
                    END IF;
                ELSIF req.purpose = 'STAFF_MANAGEMENT' AND req.target_id IS NOT NULL
                      AND (req.path LIKE '%/disable' OR req.path LIKE '%/permissions') THEN
                    command := convert_from(body, 'UTF8')::jsonb;
                    IF jsonb_typeof(command->'expected_security_revision') IS DISTINCT FROM 'number'
                       OR (command->>'expected_security_revision') !~ '^(0|[1-9][0-9]{0,18})$' THEN
                        RAISE EXCEPTION 'invalid command' USING ERRCODE = '22023';
                    END IF;
                    req.expected_security_revision := (command->>'expected_security_revision')::bigint;
                    IF jsonb_typeof(command->'expected_revision') IS DISTINCT FROM 'number'
                       OR (command->>'expected_revision') !~ '^(0|[1-9][0-9]{0,18})$' THEN
                        RAISE EXCEPTION 'invalid command' USING ERRCODE = '22023';
                    END IF;
                    req.expected_staff_revision := (command->>'expected_revision')::bigint;
                    IF req.path LIKE '%/permissions' THEN
                        IF jsonb_typeof(command->'operational_manager') IS DISTINCT FROM 'boolean' THEN
                            RAISE EXCEPTION 'invalid command' USING ERRCODE = '22023';
                        END IF;
                        req.manager_enabled := (command->>'operational_manager')::boolean;
                    END IF;
                ELSIF req.method = 'POST' AND req.path = '/v1/staff/receptionists' THEN
                    command := convert_from(body, 'UTF8')::jsonb;
                    req.native_user_id := command->>'prepared_user_id';
                    IF COALESCE(length(req.native_user_id), 0) NOT BETWEEN 1 AND 255 THEN
                        RAISE EXCEPTION 'invalid command' USING ERRCODE = '22023';
                    END IF;
                ELSIF req.method = 'POST' AND req.path = '/v1/appointments' THEN
                    command := convert_from(body, 'UTF8')::jsonb;
                    req.patient_id := (command->>'patient_id')::uuid;
                    req.source_visit_id := (command->>'source_visit_id')::uuid;
                    IF req.patient_id IS NULL THEN
                        RAISE EXCEPTION 'invalid command' USING ERRCODE = '22023';
                    END IF;
                ELSIF req.purpose = 'VISIT_FOLLOWUP' AND req.method = 'POST' THEN
                    command := convert_from(body, 'UTF8')::jsonb;
                    req.target_appointment_id := (command->>'appointment_id')::uuid;
                    IF req.target_appointment_id IS NULL THEN
                        RAISE EXCEPTION 'invalid command' USING ERRCODE = '22023';
                    END IF;
                END IF;
            EXCEPTION WHEN data_exception THEN
                req.body_scope_valid := false;
            END;
            observed := f01_private.gate_now();
            req.expires_at := issued_at + make_interval(secs => settings.signed_request_freshness_seconds);
            IF req.expires_at <= observed
               OR issued_at > observed + make_interval(secs => settings.signed_request_max_skew_seconds)
               OR (signing_key.status = 'RETIRING' AND req.issued_at_ms >= signing_key.issuance_cutoff_ms)
            THEN RAISE EXCEPTION 'proof denied' USING ERRCODE = '42501'; END IF;
            INSERT INTO f01_private.nonces VALUES (req.http_key_id, req.nonce);
            observed := f01_private.gate_now();
            IF req.expires_at <= observed THEN
                RAISE EXCEPTION 'proof denied' USING ERRCODE = '42501';
            END IF;
            req.registered_at := observed;
            INSERT INTO f01_private.requests
                (http_key_id, nonce, database_key_id, organization_id, staff_id, session_id,
                 role, security_revision, purpose, method, path, query, body_sha256, idempotency_key,
                 target_id, proof_digest, native_user_id, proof_reference, native_identifier_hash,
                 expected_security_revision, expected_staff_revision, manager_enabled,
                 patient_id, source_visit_id, target_appointment_id, body_scope_valid,
                 issued_at_ms, registered_at, expires_at)
            VALUES (req.http_key_id, req.nonce, req.database_key_id, req.organization_id, req.staff_id,
                    req.session_id, req.role, req.security_revision, req.purpose, req.method, req.path,
                    req.query, req.body_sha256, req.idempotency_key, req.target_id, req.proof_digest,
                    req.native_user_id, req.proof_reference, req.native_identifier_hash,
                    req.expected_security_revision, req.expected_staff_revision, req.manager_enabled,
                    req.patient_id, req.source_visit_id,
                    req.target_appointment_id, req.body_scope_valid, req.issued_at_ms, req.registered_at, req.expires_at)
            RETURNING id INTO result_id;
            RETURN result_id;
        END $$;
    """)
    op.execute(r"""
        CREATE FUNCTION f01_private.cleanup_proven(req f01_private.requests, primary_xid xid8)
        RETURNS boolean LANGUAGE sql SECURITY DEFINER SET search_path = pg_catalog, pg_temp AS $$
            SELECT req.purpose = 'STAFF_MANAGEMENT' AND req.role = 'DOCTOR_OWNER'
                AND req.expected_security_revision IS NOT NULL AND EXISTS (
                    SELECT 1 FROM public.staff_accounts s
                    JOIN public.sensitive_audit a ON a.organization_id = s.organization_id
                    WHERE s.organization_id = req.organization_id AND s.id = req.target_id
                      AND s.role = 'RECEPTIONIST'
                      AND s.security_revision::numeric = req.expected_security_revision::numeric + 1
                      AND a.xmin = primary_xid::xid
                      AND a.actor_staff_id = req.staff_id AND a.actor_role = req.role
                      AND a.before_state->>'id' = req.target_id::text
                      AND a.after_state->>'id' = req.target_id::text
                      AND ((req.path LIKE '%/disable' AND NOT s.enabled
                            AND a.action = 'RECEPTIONIST_DISABLE'
                            AND a.before_state->'enabled' = 'true'::jsonb
                            AND a.after_state->'enabled' = 'false'::jsonb)
                           OR (req.path LIKE '%/permissions' AND req.manager_enabled IS FALSE AND NOT s.operational_manager
                            AND a.action = 'RECEPTIONIST_PERMISSIONS'
                            AND a.before_state->'operational_manager' = 'true'::jsonb
                            AND a.after_state->'operational_manager' = 'false'::jsonb
                            AND a.before_state->>'security_revision' = req.expected_security_revision::text
                            AND a.after_state->>'security_revision' = s.security_revision::text))
                );
        $$;

        CREATE FUNCTION public.f01_bind(p_request_id bigint, p_stage text, p_pid integer, p_xid xid8)
        RETURNS void LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, pg_temp AS $$
        DECLARE
            req f01_private.requests%ROWTYPE;
            previous f01_private.bindings%ROWTYPE;
            allowed text;
            observed timestamptz;
            db oid := (SELECT oid FROM pg_database WHERE datname = current_database());
        BEGIN
            IF session_user <> 'carepulse_gate' OR p_request_id IS NULL OR p_stage IS NULL
                OR p_pid IS NULL OR p_xid IS NULL OR p_pid <= 0 THEN
                RAISE EXCEPTION 'binding denied' USING ERRCODE = '42501';
            END IF;
            SELECT * INTO req FROM f01_private.requests r WHERE r.id = p_request_id FOR UPDATE;
            IF NOT FOUND THEN RAISE EXCEPTION 'binding denied' USING ERRCODE = '42501'; END IF;
            observed := f01_private.gate_now();
            IF observed >= req.expires_at OR observed < req.registered_at THEN
                RAISE EXCEPTION 'binding denied' USING ERRCODE = '42501';
            END IF;
            IF NOT EXISTS (SELECT 1 FROM f01_private.bindings b WHERE b.request_id = req.id) THEN
                allowed := CASE WHEN req.purpose = 'STAFF_MANAGEMENT' AND req.path LIKE '%/invite'
                    THEN 'INVITATION_PREPARE' ELSE 'PRIMARY' END;
            ELSIF (SELECT count(*) FROM f01_private.bindings b WHERE b.request_id = req.id) = 1 THEN
                SELECT * INTO previous FROM f01_private.bindings b WHERE b.request_id = req.id;
                IF previous.stage = 'INVITATION_PREPARE'
                   AND pg_xact_status(previous.xid) = 'committed'
                   AND EXISTS (SELECT 1 FROM f01_private.activations a
                       WHERE a.request_id = req.id AND a.stage = previous.stage) THEN
                    allowed := 'INVITATION_APPLY';
                ELSIF previous.stage = 'PRIMARY' AND pg_xact_status(previous.xid) = 'aborted'
                      AND req.idempotency_key <> ''
                      AND ((req.method = 'POST' AND req.path = '/v1/appointments')
                           OR (req.method = 'PATCH' AND req.path IN (
                               '/v1/settings/pricing', '/v1/settings/schedule'))
                           OR (req.method = 'POST' AND req.path IN (
                               '/v1/settings/schedule/preview', '/v1/settings/schedule/activate'))
                           OR req.purpose = 'VISIT_FOLLOWUP') THEN
                    allowed := 'RECEIPT_RECOVERY';
                ELSIF previous.stage = 'PRIMARY' AND pg_xact_status(previous.xid) = 'committed'
                      AND EXISTS (SELECT 1 FROM f01_private.activations a
                          WHERE a.request_id = req.id AND a.stage = previous.stage)
                      AND f01_private.cleanup_proven(req, previous.xid) THEN
                    allowed := 'SESSION_CLEANUP';
                END IF;
            END IF;
            IF allowed IS NULL OR p_stage IS DISTINCT FROM allowed
               OR pg_xact_status(p_xid) IS DISTINCT FROM 'in progress'
               OR NOT EXISTS (
                    SELECT 1 FROM pg_stat_activity a
                    JOIN pg_locks l ON l.pid = a.pid AND l.locktype = 'transactionid'
                       AND l.mode = 'ExclusiveLock' AND l.granted AND l.transactionid = p_xid::xid
                    WHERE a.pid = p_pid AND a.datid = db AND a.usename = 'carepulse_api'
                ) THEN RAISE EXCEPTION 'binding denied' USING ERRCODE = '42501'; END IF;
            IF f01_private.gate_now() >= req.expires_at THEN
                RAISE EXCEPTION 'binding denied' USING ERRCODE = '42501';
            END IF;
            INSERT INTO f01_private.bindings VALUES
                (req.id, p_stage, db, 'carepulse_api'::regrole::oid, p_pid, p_xid);
        END $$;
    """)
    op.execute(r"""
        CREATE FUNCTION public.f01_activate() RETURNS void
        LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, pg_temp AS $$
        DECLARE
            b f01_private.bindings%ROWTYPE;
            req f01_private.requests%ROWTYPE;
            settings f01_private.policy%ROWTYPE;
            org record;
            staff_row record;
            invite record;
            sess record;
            live_staff uuid;
            observed timestamptz;
            horizon timestamptz;
            primary_xid xid8;
        BEGIN
            IF session_user <> 'carepulse_api' OR current_setting('transaction_isolation') <> 'read committed'
                THEN RAISE EXCEPTION 'activation denied' USING ERRCODE = '42501'; END IF;
            SELECT * INTO b FROM f01_private.bindings x
                WHERE x.database_oid = (SELECT oid FROM pg_database WHERE datname = current_database())
                AND x.login_oid = session_user::regrole::oid AND x.pid = pg_backend_pid()
                AND x.xid = pg_current_xact_id();
            IF NOT FOUND THEN RAISE EXCEPTION 'activation denied' USING ERRCODE = '42501'; END IF;
            SELECT * INTO req FROM f01_private.requests r WHERE r.id = b.request_id;
            IF NOT FOUND OR NOT req.body_scope_valid THEN
                RAISE EXCEPTION 'activation denied' USING ERRCODE = '42501';
            END IF;
            SELECT * INTO settings FROM f01_private.policy WHERE singleton FOR SHARE;
            IF NOT FOUND THEN RAISE EXCEPTION 'activation denied' USING ERRCODE = '42501'; END IF;
            PERFORM 1 FROM f01_private.keys k WHERE k.key_id = req.database_key_id
                AND (k.status = 'ACTIVE' OR
                      (k.status = 'RETIRING' AND req.issued_at_ms < k.issuance_cutoff_ms)) FOR SHARE;
            IF NOT FOUND THEN RAISE EXCEPTION 'activation denied' USING ERRCODE = '42501'; END IF;
            SELECT o.state INTO org FROM public.organizations o
                WHERE o.id = req.organization_id FOR UPDATE;
            IF NOT FOUND OR (req.purpose = 'OWNER_ACTIVATION' AND org.state NOT IN ('PROVISIONING', 'ACTIVE'))
               OR (req.purpose <> 'OWNER_ACTIVATION' AND org.state <> 'ACTIVE') THEN
                RAISE EXCEPTION 'activation denied' USING ERRCODE = '42501';
            END IF;
            horizon := LEAST(req.expires_at, to_timestamp(req.issued_at_ms / 1000.0)
                + make_interval(secs => settings.signed_request_freshness_seconds));
            IF req.purpose IN ('OWNER_ACTIVATION', 'RECEPTIONIST_ACCEPT') THEN
                IF req.purpose = 'RECEPTIONIST_ACCEPT' THEN
                    SELECT i.staff_id INTO live_staff FROM public.invitations i
                        WHERE i.id = req.target_id AND i.organization_id = req.organization_id
                        AND i.purpose = 'RECEPTIONIST';
                    SELECT s.role, s.enabled, s.verified, s.auth_user_id, s.verified_email INTO staff_row
                        FROM public.staff_accounts s
                        WHERE s.id = live_staff AND s.organization_id = req.organization_id FOR UPDATE;
                    IF NOT FOUND OR staff_row.role <> 'RECEPTIONIST' OR NOT staff_row.enabled
                       OR staff_row.verified OR staff_row.auth_user_id <> req.native_user_id THEN
                        RAISE EXCEPTION 'activation denied' USING ERRCODE = '42501';
                    END IF;
                END IF;
                SELECT i.target_email, i.state, i.expires_at, i.accepted_by_user_id INTO invite
                    FROM public.invitations i WHERE i.id = req.target_id
                    AND i.organization_id = req.organization_id AND i.proof_digest = req.proof_digest
                    AND i.purpose = CASE WHEN req.purpose = 'OWNER_ACTIVATION'
                        THEN 'FIRST_OWNER' ELSE 'RECEPTIONIST' END FOR UPDATE;
                IF NOT FOUND THEN
                    RAISE EXCEPTION 'activation denied' USING ERRCODE = '42501';
                END IF;
                IF req.purpose = 'RECEPTIONIST_ACCEPT' THEN
                    IF staff_row.verified_email IS DISTINCT FROM invite.target_email THEN
                        RAISE EXCEPTION 'activation denied' USING ERRCODE = '42501';
                    END IF;
                END IF;
                PERFORM 1 FROM public."user" u WHERE u.id = req.native_user_id
                    AND u."emailVerified" AND u.email = invite.target_email FOR SHARE;
                IF NOT FOUND THEN RAISE EXCEPTION 'activation denied' USING ERRCODE = '42501'; END IF;
                IF req.purpose = 'OWNER_ACTIVATION' AND org.state = 'ACTIVE'
                    AND invite.state = 'ACCEPTED' AND invite.accepted_by_user_id = req.native_user_id THEN
                    SELECT s.id INTO live_staff FROM public.staff_accounts s
                        WHERE s.organization_id = req.organization_id
                        AND s.auth_user_id = req.native_user_id AND s.role = 'DOCTOR_OWNER'
                        AND s.enabled AND s.verified FOR UPDATE;
                    IF NOT FOUND THEN RAISE EXCEPTION 'activation denied' USING ERRCODE = '42501'; END IF;
                ELSIF invite.state = 'ISSUED'
                    AND (req.purpose = 'RECEPTIONIST_ACCEPT' OR org.state = 'PROVISIONING') THEN
                    horizon := LEAST(horizon, invite.expires_at);
                ELSE
                    RAISE EXCEPTION 'activation denied' USING ERRCODE = '42501';
                END IF;
            ELSE
                SELECT s.enabled, s.verified, s.auth_user_id, s.verified_email, s.role,
                    s.security_revision, s.reset_block_operation_id INTO staff_row
                    FROM public.staff_accounts s WHERE s.id = req.staff_id
                    AND s.organization_id = req.organization_id FOR UPDATE;
                IF NOT FOUND THEN
                    RAISE EXCEPTION 'activation denied' USING ERRCODE = '28000';
                END IF;
                IF NOT staff_row.enabled OR NOT staff_row.verified THEN
                    RAISE EXCEPTION 'activation denied' USING ERRCODE = '42501';
                END IF;
                PERFORM 1 FROM public."user" u WHERE u.id = staff_row.auth_user_id
                    AND u."emailVerified" AND u.email = staff_row.verified_email FOR SHARE;
                IF NOT FOUND THEN RAISE EXCEPTION 'activation denied' USING ERRCODE = '42501'; END IF;
                IF req.purpose = 'OWNER_RECOVERY' THEN
                    IF staff_row.role <> 'DOCTOR_OWNER' OR staff_row.auth_user_id <> req.native_user_id THEN
                        RAISE EXCEPTION 'activation denied' USING ERRCODE = '42501';
                    END IF;
                    -- A completed native reset can consume the verification before finalize/replay.
                    PERFORM 1 FROM public.recovery_operations r
                        WHERE r.organization_id = req.organization_id AND r.staff_id = req.staff_id
                          AND r.auth_user_id = req.native_user_id AND r.proof_digest = req.proof_digest
                          AND r.proof_reference = req.proof_reference
                          AND r.state IN ('RESERVED', 'AUTH_DONE', 'FINALIZED') AND NOT r.invalidated
                        FOR UPDATE;
                    IF NOT FOUND THEN
                        IF req.path <> '/v1/staff/recovery/reserve' THEN
                            RAISE EXCEPTION 'activation denied' USING ERRCODE = '42501';
                        END IF;
                        SELECT LEAST(horizon, v."expiresAt") INTO horizon
                            FROM public.verification v JOIN public.delivery_jobs j ON j.verification_id = v.id
                            WHERE v.id = req.proof_reference AND v.identifier = req.native_identifier_hash
                              AND v.value = req.native_user_id AND j.organization_id = req.organization_id
                              AND j.staff_id = req.staff_id AND j.purpose = 'PASSWORD_RESET'
                              AND j.proof_reference = req.proof_digest AND j.recipient = staff_row.verified_email
                              AND j.proof_expires_at = v."expiresAt" AND j.state NOT IN ('FAILED', 'CANCELLED');
                        IF NOT FOUND THEN
                            RAISE EXCEPTION 'activation denied' USING ERRCODE = '42501';
                        END IF;
                    END IF;
                ELSE
                    IF staff_row.role <> req.role
                       OR staff_row.reset_block_operation_id IS NOT NULL THEN
                        RAISE EXCEPTION 'activation denied' USING ERRCODE = '42501';
                    END IF;
                    IF staff_row.security_revision <> req.security_revision THEN
                        RAISE EXCEPTION 'activation denied' USING ERRCODE = '28000';
                    END IF;
                    SELECT s."expiresAt", s."absoluteDeadline", s."lastActivity" INTO sess
                        FROM public."session" s
                        WHERE s.id = req.session_id AND s."userId" = staff_row.auth_user_id
                          AND s."securityRevision" = req.security_revision FOR SHARE;
                    IF NOT FOUND OR LEAST(sess."expiresAt", sess."absoluteDeadline",
                        sess."lastActivity" + make_interval(mins => settings.session_idle_minutes))
                        <= clock_timestamp() THEN
                        RAISE EXCEPTION 'activation denied' USING ERRCODE = '28000';
                    END IF;
                    horizon := LEAST(horizon, sess."expiresAt", sess."absoluteDeadline",
                        sess."lastActivity" + make_interval(mins => settings.session_idle_minutes));
                    IF b.stage = 'SESSION_CLEANUP' THEN
                        PERFORM 1 FROM public.staff_accounts s WHERE s.id = req.target_id
                            AND s.organization_id = req.organization_id FOR UPDATE;
                        IF NOT FOUND THEN RAISE EXCEPTION 'activation denied' USING ERRCODE = '42501'; END IF;
                        SELECT x.xid INTO primary_xid FROM f01_private.bindings x
                            WHERE x.request_id = req.id AND x.stage = 'PRIMARY';
                        IF NOT FOUND OR pg_xact_status(primary_xid) IS DISTINCT FROM 'committed'
                            OR f01_private.cleanup_proven(req, primary_xid) IS NOT TRUE THEN
                            RAISE EXCEPTION 'activation denied' USING ERRCODE = '42501';
                        END IF;
                    END IF;
                END IF;
            END IF;
            observed := clock_timestamp();
            IF horizon IS NULL OR observed >= horizon OR observed < req.registered_at
               OR observed < (SELECT last_seen_at FROM f01_private.clock_state WHERE singleton) THEN
                RAISE EXCEPTION 'activation denied' USING ERRCODE = '42501';
            END IF;
            INSERT INTO f01_private.activations VALUES
                (b.request_id, b.stage, observed, horizon, live_staff);
        END $$;

        CREATE FUNCTION public.f01_context()
        RETURNS TABLE (organization_id text, staff_id text, role text, purpose text,
            method text, path text, stage text, idempotency_key text, request_id bigint,
            session_id text, security_revision bigint, target_id uuid, proof_digest text,
            native_user_id text, expected_security_revision bigint, body_sha256 text, query text,
            subject_staff_id uuid, patient_id uuid, source_visit_id uuid, target_appointment_id uuid,
            expected_staff_revision bigint, manager_enabled boolean)
        LANGUAGE sql VOLATILE SECURITY DEFINER SET search_path = pg_catalog, pg_temp AS $$
            SELECT r.organization_id::text, r.staff_id::text, r.role, r.purpose, r.method,
                r.path, b.stage, r.idempotency_key, r.id, r.session_id, r.security_revision,
                r.target_id, r.proof_digest, r.native_user_id, r.expected_security_revision,
                r.body_sha256, r.query, a.subject_staff_id, r.patient_id, r.source_visit_id,
                r.target_appointment_id, r.expected_staff_revision, r.manager_enabled
            FROM f01_private.bindings b
            JOIN f01_private.activations a USING (request_id, stage)
            JOIN f01_private.requests r ON r.id = b.request_id
            WHERE session_user = 'carepulse_api'
                AND b.database_oid = (SELECT oid FROM pg_database WHERE datname = current_database())
                AND b.login_oid = session_user::regrole::oid AND b.pid = pg_backend_pid()
                AND b.xid = pg_current_xact_id_if_assigned()
                AND clock_timestamp() >= a.activated_at AND clock_timestamp() < a.valid_until
                AND clock_timestamp() >= (SELECT last_seen_at FROM f01_private.clock_state WHERE singleton);
        $$;

        CREATE FUNCTION public.f01_org() RETURNS text
        LANGUAGE sql VOLATILE SECURITY DEFINER SET search_path = pg_catalog, pg_temp AS $$
            SELECT c.organization_id FROM public.f01_context() c;
        $$;
        CREATE FUNCTION public.f01_staff() RETURNS text
        LANGUAGE sql VOLATILE SECURITY DEFINER SET search_path = pg_catalog, pg_temp AS $$
            SELECT c.staff_id FROM public.f01_context() c;
        $$;

        CREATE FUNCTION public.f01_cleanup() RETURNS integer
        LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, pg_temp AS $$
        DECLARE observed timestamptz; affected integer;
        BEGIN
            IF session_user <> 'carepulse_gate' THEN
                RAISE EXCEPTION 'cleanup denied' USING ERRCODE = '42501';
            END IF;
            observed := f01_private.gate_now();
            WITH expired AS (
                SELECT r.id FROM f01_private.requests r WHERE r.expires_at <= observed
                    AND NOT EXISTS (SELECT 1 FROM f01_private.bindings b
                        WHERE b.request_id = r.id AND pg_xact_status(b.xid) = 'in progress')
                    FOR UPDATE SKIP LOCKED
            )
            DELETE FROM f01_private.requests r USING expired e WHERE r.id = e.id;
            GET DIAGNOSTICS affected = ROW_COUNT;
            RETURN affected;
        END $$;

        REVOKE ALL ON ALL FUNCTIONS IN SCHEMA f01_private FROM PUBLIC;
        REVOKE ALL ON FUNCTION public.f01_register_proof(bytea,text,bytea,bytea),
            public.f01_bind(bigint,text,integer,xid8), public.f01_activate(),
            public.f01_context(), public.f01_org(), public.f01_staff(), public.f01_cleanup() FROM PUBLIC;
        GRANT EXECUTE ON FUNCTION public.f01_register_proof(bytea,text,bytea,bytea),
            public.f01_bind(bigint,text,integer,xid8), public.f01_cleanup() TO carepulse_gate;
        GRANT EXECUTE ON FUNCTION public.f01_activate(), public.f01_context(),
            public.f01_org(), public.f01_staff() TO carepulse_api;
        GRANT EXECUTE ON FUNCTION public.f01_context(), public.f01_org(), public.f01_staff()
            TO carepulse_migration;
    """)
    op.execute("RESET ROLE")
    op.execute("DROP POLICY request_nonce_f01_transfer ON public.request_nonces")
    op.execute(
        "REVOKE SELECT (key_id, nonce) ON public.request_nonces FROM carepulse_identity_owner"
    )
    op.execute("REVOKE CREATE ON SCHEMA public FROM carepulse_identity_owner")


def downgrade() -> None:
    raise RuntimeError("F-01 identity migration is forward-only")

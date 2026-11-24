"""B-12: bind check-in writes and evidence to the verified signed command."""

from alembic import op

revision = "0049_b12_checkin_provenance"
down_revision = "0048_b12_queue_scope"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # The deferred D-05 check executes after the scoped writer has returned.
    # Validate persisted evidence without exposing it through API SELECT policies.
    op.execute("""
        ALTER FUNCTION public.assert_appointment_evidence_complete() SECURITY DEFINER;
        ALTER FUNCTION public.assert_appointment_evidence_complete()
            SET search_path TO pg_catalog, pg_temp;
    """)
    op.execute("""
        GRANT CREATE ON SCHEMA public TO carepulse_identity_owner;
        SET LOCAL ROLE carepulse_identity_owner;
        ALTER TABLE f01_private.requests
            ADD COLUMN checkin_expected_revision bigint,
            ADD COLUMN checkin_queue_revision bigint,
            ADD COLUMN checkin_digest text;
        ALTER TABLE f01_private.activations ADD COLUMN checkin_applied_revision bigint;
    """)
    # Preserve the frozen F-01 registration/binding behavior for other commands.
    op.execute(r"""
        CREATE OR REPLACE FUNCTION public.f01_register_proof(
            payload bytea, key_id text, mac bytea, body bytea) RETURNS bigint
        LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, pg_temp AS $$
        DECLARE
            signing_key f01_private.keys%ROWTYPE;
            settings f01_private.policy%ROWTYPE;
            expected bytea;
            blind bytea;
            envelope jsonb;
            command jsonb;
            checkin_body json;
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
                ELSIF req.purpose = 'QUEUE_CHECK_IN' THEN
                    checkin_body := convert_from(body, 'UTF8')::json;
                    IF json_typeof(checkin_body) = 'object' AND NOT EXISTS (
                        SELECT 1 FROM json_each(checkin_body) e
                        GROUP BY e.key HAVING count(*) > 1
                    ) AND NOT EXISTS (
                        SELECT 1 FROM json_each(checkin_body) e
                        WHERE e.key NOT IN ('physical_presence_confirmed',
                            'expected_appointment_revision', 'expected_queue_day_revision')
                    ) AND json_typeof(checkin_body->'physical_presence_confirmed') = 'boolean'
                      AND json_typeof(checkin_body->'expected_appointment_revision') = 'number'
                      AND checkin_body->>'expected_appointment_revision' ~ '^(0|[1-9][0-9]{0,18})$'
                      AND (checkin_body->'expected_queue_day_revision' IS NULL
                           OR json_typeof(checkin_body->'expected_queue_day_revision') = 'null'
                           OR (json_typeof(checkin_body->'expected_queue_day_revision') = 'number'
                               AND checkin_body->>'expected_queue_day_revision' ~ '^(0|[1-9][0-9]{0,18})$'))
                      AND req.query = '' AND req.idempotency_key <> '' THEN
                        req.checkin_expected_revision := (checkin_body->>'expected_appointment_revision')::bigint;
                        req.checkin_queue_revision := (checkin_body->>'expected_queue_day_revision')::bigint;
                        IF (checkin_body->>'physical_presence_confirmed')::boolean THEN
                            -- Match the canonical typed command digest, including explicit null vs omission.
                            req.checkin_digest := encode(sha256(convert_to(
                                '{"body":{"expected_appointment_revision":' || req.checkin_expected_revision::text
                                || CASE WHEN checkin_body->'expected_queue_day_revision' IS NULL THEN ''
                                    ELSE ',"expected_queue_day_revision":'
                                        || COALESCE(req.checkin_queue_revision::text, 'null') END
                                || ',"physical_presence_confirmed":true},"operation":"appointments.check_in","target":{"appointment_id":"'
                                || req.target_id::text || '"}}', 'UTF8')), 'hex');
                        END IF;
                    ELSE
                        req.body_scope_valid := false;
                    END IF;
                ELSIF req.purpose = 'VISIT_FOLLOWUP' AND req.method = 'POST' THEN
                    command := convert_from(body, 'UTF8')::jsonb;
                    req.target_appointment_id := (command->>'appointment_id')::uuid;
                    IF req.target_appointment_id IS NULL THEN
                        RAISE EXCEPTION 'invalid command' USING ERRCODE = '22023';
                    END IF;
                END IF;
            EXCEPTION WHEN data_exception THEN
                IF req.purpose = 'QUEUE_CHECK_IN' THEN
                    req.checkin_expected_revision := NULL;
                    req.checkin_queue_revision := NULL;
                    req.checkin_digest := NULL;
                END IF;
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
                 issued_at_ms, registered_at, expires_at,
                 checkin_expected_revision, checkin_queue_revision, checkin_digest)
            VALUES (req.http_key_id, req.nonce, req.database_key_id, req.organization_id, req.staff_id,
                    req.session_id, req.role, req.security_revision, req.purpose, req.method, req.path,
                    req.query, req.body_sha256, req.idempotency_key, req.target_id, req.proof_digest,
                    req.native_user_id, req.proof_reference, req.native_identifier_hash,
                    req.expected_security_revision, req.expected_staff_revision, req.manager_enabled,
                    req.patient_id, req.source_visit_id,
                    req.target_appointment_id, req.body_scope_valid, req.issued_at_ms, req.registered_at, req.expires_at,
                    req.checkin_expected_revision, req.checkin_queue_revision, req.checkin_digest)
            RETURNING id INTO result_id;
            RETURN result_id;
        END $$;
        CREATE OR REPLACE FUNCTION public.f01_bind(p_request_id bigint, p_stage text, p_pid integer, p_xid xid8)
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
                           OR req.purpose = 'VISIT_FOLLOWUP'
                           OR (req.purpose = 'QUEUE_CHECK_IN' AND req.method = 'POST'
                               AND req.target_id IS NOT NULL AND req.checkin_digest IS NOT NULL)) THEN
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
        CREATE FUNCTION public.f01_checkin_authority()
        RETURNS TABLE (expected_revision bigint, queue_revision bigint,
                       command_digest text, applied_revision bigint)
        LANGUAGE sql VOLATILE SECURITY DEFINER SET search_path = pg_catalog, pg_temp AS $$
            SELECT r.checkin_expected_revision, r.checkin_queue_revision,
                   r.checkin_digest, a.checkin_applied_revision
            FROM public.f01_context() c
            JOIN f01_private.requests r ON r.id = c.request_id
            JOIN f01_private.activations a ON a.request_id = c.request_id AND a.stage = c.stage
            WHERE c.purpose = 'QUEUE_CHECK_IN' AND c.method = 'POST' AND c.stage = 'PRIMARY'
              AND r.checkin_digest IS NOT NULL;
        $$;

        CREATE FUNCTION public.f01_mark_checkin(p_revision bigint)
        RETURNS void LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, pg_temp AS $$
        BEGIN
            UPDATE f01_private.activations a SET checkin_applied_revision = p_revision
            FROM public.f01_context() c, f01_private.requests r
            WHERE c.request_id = a.request_id AND c.stage = a.stage AND r.id = c.request_id
              AND c.purpose = 'QUEUE_CHECK_IN' AND c.method = 'POST' AND c.stage = 'PRIMARY'
              AND r.checkin_digest IS NOT NULL AND p_revision = r.checkin_expected_revision + 1
              AND a.checkin_applied_revision IS NULL;
            IF NOT FOUND THEN
                RAISE EXCEPTION 'Check-in transition evidence denied' USING ERRCODE = '42501';
            END IF;
        END $$;
        REVOKE ALL ON FUNCTION public.f01_checkin_authority(), public.f01_mark_checkin(bigint)
            FROM PUBLIC;
        GRANT EXECUTE ON FUNCTION public.f01_checkin_authority(), public.f01_mark_checkin(bigint)
            TO carepulse_migration;
        RESET ROLE;
        REVOKE CREATE ON SCHEMA public FROM carepulse_identity_owner;
    """)
    op.execute("""
        CREATE OR REPLACE FUNCTION public.f01_check_in_appointment(
            p_tenant uuid, p_appointment_id uuid, p_expected_revision bigint)
        RETURNS bigint
        LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, pg_temp AS $$
        DECLARE
            ctx record;
            appt public.appointments%ROWTYPE;
            new_rev bigint;
        BEGIN
            IF session_user <> 'carepulse_api' THEN
                RAISE EXCEPTION 'Check-in writer scope denied' USING ERRCODE = '42501';
            END IF;
            SELECT * INTO ctx FROM public.f01_context();
            IF NOT FOUND OR NOT COALESCE(
                ctx.purpose = 'QUEUE_CHECK_IN'
                AND ctx.method = 'POST'
                AND ctx.stage = 'PRIMARY'
                AND ctx.organization_id = p_tenant::text
                AND ctx.target_id = p_appointment_id
                AND ctx.role IN ('DOCTOR_OWNER', 'RECEPTIONIST')
                AND ctx.staff_id IS NOT NULL
                AND EXISTS (
                    SELECT 1 FROM public.f01_checkin_authority() a
                    WHERE a.expected_revision = p_expected_revision AND a.applied_revision IS NULL
                ),
                false
            ) THEN
                RAISE EXCEPTION 'Check-in writer scope denied' USING ERRCODE = '42501';
            END IF;
            SELECT * INTO appt FROM public.appointments
            WHERE organization_id = p_tenant AND id = p_appointment_id
            FOR UPDATE;
            IF NOT FOUND OR appt.status <> 'Booked' OR appt.revision <> p_expected_revision THEN
                RAISE EXCEPTION 'Check-in precondition failed' USING ERRCODE = '23514';
            END IF;
            IF NOT EXISTS (
                SELECT 1 FROM public.queue_days q
                CROSS JOIN public.f01_checkin_authority() a
                WHERE q.organization_id = p_tenant
                  AND q.doctor_staff_id = appt.doctor_staff_id
                  AND q.service_date = appt.service_date AND q.lane = appt.lane
                  AND q.revision_transaction_id = pg_current_xact_id()
                  AND (a.queue_revision IS NULL OR q.revision = a.queue_revision + 1)
            ) THEN
                RAISE EXCEPTION 'Check-in queue revision scope denied' USING ERRCODE = '42501';
            END IF;
            new_rev := appt.revision + 1;
            UPDATE public.appointments
            SET status = 'Waiting', revision = new_rev
            WHERE organization_id = p_tenant AND id = p_appointment_id;
            INSERT INTO public.appointment_history
                (id, organization_id, appointment_id, appointment_revision, action,
                 old_status, new_status, old_service_date, new_service_date,
                 old_number, new_number, actor_staff_id, actor_role,
                 price_snapshot_appointment_id, reason)
            VALUES (gen_random_uuid(), p_tenant, p_appointment_id, new_rev, 'CHECKED_IN',
                    'Booked', 'Waiting', appt.service_date, appt.service_date,
                    appt.current_number, appt.current_number, ctx.staff_id::uuid, ctx.role,
                    p_appointment_id, NULL);
            PERFORM public.f01_mark_checkin(new_rev);
            RETURN new_rev;
        END $$;
    """)
    op.execute("""
        CREATE FUNCTION public.f01_guard_signed_queue_day()
        RETURNS trigger LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, pg_temp AS $$
        BEGIN
            IF session_user = 'carepulse_api' AND NOT EXISTS (
                SELECT 1 FROM public.f01_checkin_authority() a
                WHERE a.applied_revision IS NULL
                  AND (a.queue_revision IS NULL OR a.queue_revision =
                      CASE WHEN TG_OP = 'INSERT' THEN 0 ELSE OLD.revision END)
            ) THEN
                RAISE EXCEPTION 'Signed queue revision scope denied' USING ERRCODE = '42501';
            END IF;
            RETURN NEW;
        END $$;
        CREATE TRIGGER f01_signed_queue_day BEFORE INSERT OR UPDATE ON public.queue_days
            FOR EACH ROW EXECUTE FUNCTION public.f01_guard_signed_queue_day();

        CREATE FUNCTION public.f01_guard_checkin_entry()
        RETURNS trigger LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, pg_temp AS $$
        BEGIN
            IF session_user = 'carepulse_api' AND NOT EXISTS (
                SELECT 1 FROM public.f01_checkin_authority() a
                CROSS JOIN public.f01_context() c
                WHERE a.applied_revision IS NOT NULL AND c.target_id = NEW.appointment_id
                  AND c.organization_id = NEW.organization_id::text
            ) THEN
                RAISE EXCEPTION 'Check-in arrival evidence denied' USING ERRCODE = '42501';
            END IF;
            RETURN NEW;
        END $$;
        CREATE TRIGGER f01_signed_checkin_entry BEFORE INSERT ON public.queue_entries
            FOR EACH ROW EXECUTE FUNCTION public.f01_guard_checkin_entry();

        -- Only the scoped migration-owned writer may create CHECKED_IN history.
        CREATE POLICY f01_api_checkin_history_writer ON public.appointment_history
            AS RESTRICTIVE FOR INSERT TO carepulse_api
            WITH CHECK (NOT EXISTS (
                SELECT 1 FROM public.f01_context() c WHERE c.purpose = 'QUEUE_CHECK_IN'
            ));

        CREATE FUNCTION public.f01_guard_checkin_evidence()
        RETURNS trigger LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, pg_temp AS $$
        DECLARE
            ctx record;
            authority record;
            appt public.appointments%ROWTYPE;
            entry public.queue_entries%ROWTYPE;
            day public.queue_days%ROWTYPE;
            live_position bigint;
        BEGIN
            IF session_user <> 'carepulse_api' THEN RETURN NEW; END IF;
            SELECT * INTO ctx FROM public.f01_context();
            SELECT * INTO authority FROM public.f01_checkin_authority();
            IF NOT FOUND OR authority.applied_revision IS NULL
               OR ctx.organization_id IS DISTINCT FROM NEW.organization_id::text THEN
                RAISE EXCEPTION 'Check-in evidence scope denied' USING ERRCODE = '42501';
            END IF;
            SELECT * INTO appt FROM public.appointments
                WHERE organization_id = NEW.organization_id AND id = ctx.target_id
                  AND status = 'Waiting' AND revision = authority.applied_revision;
            IF NOT FOUND THEN
                RAISE EXCEPTION 'Check-in transition evidence denied' USING ERRCODE = '42501';
            END IF;
            SELECT * INTO entry FROM public.queue_entries
                WHERE organization_id = appt.organization_id AND appointment_id = appt.id
                  AND disposition = 'ACTIVE' AND attested_by_staff_id::text = ctx.staff_id;
            IF NOT FOUND THEN
                RAISE EXCEPTION 'Check-in arrival evidence denied' USING ERRCODE = '42501';
            END IF;
            SELECT * INTO day FROM public.queue_days
                WHERE organization_id = entry.organization_id AND id = entry.queue_day_id
                  AND revision_transaction_id = pg_current_xact_id()
                  AND arrival_counter = entry.arrival_sequence;
            IF NOT FOUND THEN
                RAISE EXCEPTION 'Check-in queue evidence denied' USING ERRCODE = '42501';
            END IF;

            IF TG_TABLE_NAME = 'sensitive_audit' THEN
                IF NEW.actor_staff_id::text IS DISTINCT FROM ctx.staff_id
                   OR NEW.actor_role IS DISTINCT FROM ctx.role
                   OR NEW.correlation_id IS DISTINCT FROM appt.id::text
                   OR NEW.operator_id IS NOT NULL OR NEW.reason IS NOT NULL
                   OR NEW.recovery_operation_id IS NOT NULL
                   OR NEW.before_state IS DISTINCT FROM jsonb_build_object(
                       'appointment_id', appt.id, 'status', 'Booked',
                       'revision', authority.expected_revision)
                   OR NEW.after_state IS DISTINCT FROM jsonb_build_object(
                       'appointment_id', appt.id, 'queue_entry_id', entry.id,
                       'status', 'Waiting', 'revision', appt.revision,
                       'service_date', appt.service_date, 'lane', appt.lane,
                       'arrival_sequence', entry.arrival_sequence)
                   OR EXISTS (SELECT 1 FROM public.sensitive_audit s
                       WHERE s.organization_id = appt.organization_id AND s.action = NEW.action
                         AND s.correlation_id = NEW.correlation_id AND s.after_state = NEW.after_state)
                THEN
                    RAISE EXCEPTION 'Check-in audit evidence denied' USING ERRCODE = '42501';
                END IF;
            ELSIF TG_TABLE_NAME = 'outbox_events' THEN
                IF NEW.aggregate_type IS DISTINCT FROM 'APPOINTMENT'
                   OR NEW.aggregate_id IS DISTINCT FROM appt.id::text
                   OR NEW.payload IS DISTINCT FROM jsonb_build_object(
                       'appointment_id', appt.id, 'queue_entry_id', entry.id,
                       'doctor_staff_id', appt.doctor_staff_id, 'service_date', appt.service_date,
                       'lane', appt.lane, 'classification', appt.classification,
                       'arrival_sequence', entry.arrival_sequence)
                   OR EXISTS (SELECT 1 FROM public.outbox_events e
                       WHERE e.organization_id = appt.organization_id AND e.event_type = NEW.event_type
                         AND e.aggregate_id = NEW.aggregate_id AND e.payload = NEW.payload)
                THEN
                    RAISE EXCEPTION 'Check-in outbox evidence denied' USING ERRCODE = '42501';
                END IF;
            ELSE
                SELECT count(*) INTO live_position FROM public.queue_entries e
                JOIN public.appointments a ON a.organization_id = e.organization_id
                    AND a.id = e.appointment_id
                WHERE e.organization_id = appt.organization_id AND e.queue_day_id = day.id
                  AND e.disposition = 'ACTIVE' AND a.status = 'Waiting'
                  AND (
                      (appt.lane = 'EMERGENCY' AND e.arrival_sequence <= entry.arrival_sequence)
                      OR (appt.lane = 'MAIN' AND a.classification = 'URGENT'
                          AND (appt.classification = 'ROUTINE' OR e.arrival_sequence <= entry.arrival_sequence))
                      OR (appt.classification = 'ROUTINE' AND e.routine_rank <= entry.routine_rank)
                  );
                IF NEW.staff_id::text IS DISTINCT FROM ctx.staff_id
                   OR NEW.actor_id::text IS DISTINCT FROM ctx.staff_id
                   OR NEW.idempotency_key IS DISTINCT FROM ctx.idempotency_key
                   OR NEW.request_digest IS DISTINCT FROM authority.command_digest
                   OR NEW.result_revision IS DISTINCT FROM appt.revision
                   OR jsonb_typeof(NEW.result_projection->'attested_at') IS DISTINCT FROM 'string'
                   OR (NEW.result_projection->>'attested_at')::timestamptz IS DISTINCT FROM entry.attested_at
                   OR NEW.result_projection - 'attested_at' IS DISTINCT FROM jsonb_build_object(
                       'appointment_id', appt.id, 'queue_entry_id', entry.id,
                       'queue_day_id', day.id, 'patient_id', appt.patient_id,
                       'doctor_staff_id', appt.doctor_staff_id, 'service_date', appt.service_date,
                       'lane', appt.lane, 'classification', appt.classification, 'status', appt.status,
                       'booking_number', appt.current_number, 'arrival_sequence', entry.arrival_sequence,
                       'routine_rank', entry.routine_rank, 'position', live_position,
                       'attested_by_staff_id', entry.attested_by_staff_id,
                       'appointment_revision', appt.revision, 'queue_day_revision', day.revision,
                       'queue_entry_revision', entry.revision, 'queue_policy_version', day.queue_policy_version,
                       'routine_cursor', day.routine_cursor)
                THEN
                    RAISE EXCEPTION 'Check-in receipt evidence denied' USING ERRCODE = '42501';
                END IF;
            END IF;
            RETURN NEW;
        EXCEPTION WHEN data_exception THEN
            RAISE EXCEPTION 'Check-in evidence invalid' USING ERRCODE = '42501';
        END $$;

        CREATE TRIGGER f01_checkin_audit BEFORE INSERT ON public.sensitive_audit
            FOR EACH ROW WHEN (NEW.action = 'appointments.check_in')
            EXECUTE FUNCTION public.f01_guard_checkin_evidence();
        CREATE TRIGGER f01_checkin_outbox BEFORE INSERT ON public.outbox_events
            FOR EACH ROW WHEN (NEW.event_type = 'PATIENT_CHECKED_IN')
            EXECUTE FUNCTION public.f01_guard_checkin_evidence();
        CREATE TRIGGER f01_checkin_receipt BEFORE INSERT ON public.idempotency_receipts
            FOR EACH ROW WHEN (NEW.operation = 'appointments.check_in')
            EXECUTE FUNCTION public.f01_guard_checkin_evidence();
        REVOKE ALL ON FUNCTION public.f01_guard_signed_queue_day(),
            public.f01_guard_checkin_entry(), public.f01_guard_checkin_evidence() FROM PUBLIC;
    """)


def downgrade() -> None:
    raise RuntimeError("B-12 check-in provenance migration is forward-only")

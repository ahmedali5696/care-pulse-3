-- Disposable F-01 proof only. Bootstrap on a fresh f01 database as postgres.
BEGIN;
CREATE ROLE carepulse_identity_owner NOLOGIN NOSUPERUSER NOBYPASSRLS;
CREATE ROLE f01_gate LOGIN PASSWORD 'gate-test' NOSUPERUSER NOBYPASSRLS;
CREATE ROLE f01_runtime LOGIN PASSWORD 'runtime-test' NOSUPERUSER NOBYPASSRLS;
CREATE ROLE f01_operator LOGIN PASSWORD 'operator-test' NOSUPERUSER NOBYPASSRLS;

REVOKE ALL ON DATABASE f01 FROM PUBLIC;
GRANT CONNECT ON DATABASE f01 TO f01_gate, f01_runtime, f01_operator;
REVOKE ALL ON SCHEMA public FROM PUBLIC;
CREATE SCHEMA f01_crypto AUTHORIZATION carepulse_identity_owner;
CREATE EXTENSION pgcrypto WITH SCHEMA f01_crypto;
REVOKE ALL ON ALL FUNCTIONS IN SCHEMA f01_crypto FROM PUBLIC;
GRANT EXECUTE ON FUNCTION f01_crypto.hmac(bytea, bytea, text),
    f01_crypto.gen_random_bytes(integer),
    f01_crypto.digest(bytea, text) TO carepulse_identity_owner;
CREATE SCHEMA f01_private AUTHORIZATION carepulse_identity_owner;
CREATE SCHEMA f01_api AUTHORIZATION carepulse_identity_owner;
CREATE SCHEMA f01_data AUTHORIZATION carepulse_identity_owner;
SET ROLE carepulse_identity_owner;
ALTER DEFAULT PRIVILEGES REVOKE EXECUTE ON FUNCTIONS FROM PUBLIC;

CREATE TABLE f01_private.keys (
    key_id text PRIMARY KEY,
    secret bytea NOT NULL CHECK (octet_length(secret) = 32),
    status text NOT NULL
);
CREATE TABLE f01_private.organizations (
    id text PRIMARY KEY,
    status text NOT NULL
);
CREATE TABLE f01_private.staff (
    organization_id text REFERENCES f01_private.organizations(id),
    id text,
    role text NOT NULL,
    status text NOT NULL,
    security_revision bigint NOT NULL,
    email_verified boolean NOT NULL DEFAULT true,
    reset_required boolean NOT NULL DEFAULT false,
    PRIMARY KEY (organization_id, id)
);
CREATE TABLE f01_private.sessions (
    organization_id text,
    staff_id text,
    id text,
    status text NOT NULL,
    security_revision bigint NOT NULL,
    expires_at timestamptz NOT NULL,
    idle_expires_at timestamptz NOT NULL DEFAULT clock_timestamp() + interval '1 day',
    PRIMARY KEY (organization_id, staff_id, id),
    FOREIGN KEY (organization_id, staff_id)
        REFERENCES f01_private.staff(organization_id, id)
);
CREATE TABLE f01_private.nonces (
    http_key_id text,
    nonce text,
    PRIMARY KEY (http_key_id, nonce)
);
CREATE TABLE f01_private.requests (
    id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    http_key_id text NOT NULL,
    nonce text NOT NULL,
    database_key_id text NOT NULL REFERENCES f01_private.keys(key_id),
    organization_id text NOT NULL,
    staff_id text NOT NULL,
    session_id text NOT NULL,
    role text NOT NULL,
    security_revision bigint NOT NULL,
    purpose text NOT NULL,
    target text NOT NULL,
    body_sha256 text NOT NULL,
    expires_at timestamptz NOT NULL,
    FOREIGN KEY (http_key_id, nonce) REFERENCES f01_private.nonces(http_key_id, nonce)
);
CREATE TABLE f01_private.bindings (
    request_id bigint REFERENCES f01_private.requests(id) ON DELETE CASCADE,
    stage text CHECK (stage IN ('PRIMARY', 'RECEIPT_RECOVERY', 'INVITATION_PREPARE',
                               'INVITATION_APPLY', 'SESSION_CLEANUP')),
    database_oid oid NOT NULL,
    login_oid oid NOT NULL,
    pid integer NOT NULL,
    xid xid8 NOT NULL,
    PRIMARY KEY (request_id, stage),
    UNIQUE (database_oid, xid)
);
CREATE TABLE f01_private.activations (
    request_id bigint,
    stage text,
    PRIMARY KEY (request_id, stage),
    FOREIGN KEY (request_id, stage)
        REFERENCES f01_private.bindings(request_id, stage) ON DELETE CASCADE
);
CREATE TABLE f01_private.receipts (
    organization_id text NOT NULL,
    staff_id text NOT NULL,
    target text NOT NULL,
    body_sha256 text NOT NULL,
    note text NOT NULL,
    request_id bigint PRIMARY KEY,
    UNIQUE (organization_id, staff_id, target)
);
CREATE TABLE f01_private.invitations (
    organization_id text,
    target text,
    staff_id text NOT NULL,
    credential_id text NOT NULL,
    expires_at timestamptz NOT NULL,
    request_id bigint UNIQUE,
    status text NOT NULL,
    PRIMARY KEY (organization_id, target),
    FOREIGN KEY (organization_id, staff_id)
        REFERENCES f01_private.staff(organization_id, id)
);
CREATE TABLE f01_private.security_changes (
    request_id bigint PRIMARY KEY,
    organization_id text NOT NULL,
    staff_id text NOT NULL,
    old_revision bigint NOT NULL,
    new_revision bigint NOT NULL
);
CREATE TABLE f01_data.patients (
    org text REFERENCES f01_private.organizations(id),
    id text,
    note text NOT NULL,
    PRIMARY KEY (org, id)
);

INSERT INTO f01_private.keys VALUES
    ('db-a', convert_to(repeat('A', 32), 'UTF8'), 'ACTIVE'),
    ('db-b', convert_to(repeat('B', 32), 'UTF8'), 'ACTIVE');
INSERT INTO f01_private.organizations VALUES ('A', 'ACTIVE'), ('B', 'ACTIVE'), ('C', 'PROVISIONING');
INSERT INTO f01_private.staff (organization_id, id, role, status, security_revision) VALUES
    ('A', 'doctor-a', 'DOCTOR_OWNER', 'ACTIVE', 1),
    ('A', 'reception-a', 'RECEPTIONIST', 'ACTIVE', 1),
    ('B', 'doctor-b', 'DOCTOR_OWNER', 'ACTIVE', 1);
INSERT INTO f01_private.staff (organization_id, id, role, status, security_revision, email_verified) VALUES
    ('C', 'invite-c', 'DOCTOR_OWNER', 'INVITED', 1, false);
INSERT INTO f01_private.sessions (organization_id, staff_id, id, status, security_revision, expires_at) VALUES
    ('A', 'doctor-a', 'sa', 'ACTIVE', 1, clock_timestamp() + interval '1 day'),
    ('A', 'reception-a', 'ra', 'ACTIVE', 1, clock_timestamp() + interval '1 day'),
    ('B', 'doctor-b', 'sb', 'ACTIVE', 1, clock_timestamp() + interval '1 day');
INSERT INTO f01_private.invitations (organization_id, target, staff_id, credential_id, expires_at, status) VALUES
    ('C', 'p1', 'invite-c', 'ic', clock_timestamp() + interval '1 day', 'PENDING');
INSERT INTO f01_data.patients VALUES ('A', 'p1', 'original-a'), ('B', 'p2', 'original-b');

-- The owner is the only principal permitted through these nonrecursive policies.
ALTER TABLE f01_private.organizations ENABLE ROW LEVEL SECURITY;
ALTER TABLE f01_private.organizations FORCE ROW LEVEL SECURITY;
CREATE POLICY authority_owner ON f01_private.organizations TO carepulse_identity_owner
    USING (true) WITH CHECK (true);
ALTER TABLE f01_private.staff ENABLE ROW LEVEL SECURITY;
ALTER TABLE f01_private.staff FORCE ROW LEVEL SECURITY;
CREATE POLICY authority_owner ON f01_private.staff TO carepulse_identity_owner
    USING (true) WITH CHECK (true);
ALTER TABLE f01_private.sessions ENABLE ROW LEVEL SECURITY;
ALTER TABLE f01_private.sessions FORCE ROW LEVEL SECURITY;
CREATE POLICY authority_owner ON f01_private.sessions TO carepulse_identity_owner
    USING (true) WITH CHECK (true);
ALTER TABLE f01_private.invitations ENABLE ROW LEVEL SECURITY;
ALTER TABLE f01_private.invitations FORCE ROW LEVEL SECURITY;
CREATE POLICY authority_owner ON f01_private.invitations TO carepulse_identity_owner
    USING (true) WITH CHECK (true);

CREATE FUNCTION f01_api.register_proof(payload bytea, database_key_id text, mac bytea)
RETURNS bigint LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, pg_temp AS $$
DECLARE
    key_secret bytea;
    blind bytea;
    expected bytea;
    envelope jsonb;
    issued_at timestamptz;
    result_id bigint;
BEGIN
    IF session_user <> 'f01_gate' OR payload IS NULL OR database_key_id IS NULL
       OR mac IS NULL OR octet_length(mac) <> 32 OR octet_length(payload) > 8192 THEN
        RAISE EXCEPTION 'proof denied' USING ERRCODE = '42501';
    END IF;
    SELECT k.secret INTO key_secret FROM f01_private.keys k
    WHERE k.key_id = register_proof.database_key_id AND k.status IN ('ACTIVE', 'RETIRING') FOR SHARE;
    IF NOT FOUND THEN RAISE EXCEPTION 'proof denied' USING ERRCODE = '42501'; END IF;
    expected := f01_crypto.hmac(convert_to('carepulse-db-context-v1', 'UTF8') || decode('00', 'hex')
        || convert_to(database_key_id, 'UTF8') || decode('00', 'hex') || payload, key_secret, 'sha256');
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
            SELECT 1 FROM jsonb_array_elements(envelope) e WHERE jsonb_typeof(e) <> 'string'
        ) THEN RAISE EXCEPTION 'invalid envelope' USING ERRCODE = '22023'; END IF;
        IF envelope->>0 <> 'carepulse-request-v1'
           OR envelope->>1 !~ '^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$'
           OR envelope->>2 <> 'web' OR envelope->>3 <> 'POST'
           OR envelope->>4 <> '/proof' OR envelope->>5 <> ''
           OR envelope->>6 !~ '^[0-9a-f]{64}$'
           OR envelope->>7 !~ '^[A-Za-z0-9_-]{1,80}$'
           OR envelope->>8 !~ '^[A-Za-z0-9_-]{1,80}$'
           OR envelope->>9 !~ '^[A-Za-z0-9_-]{1,80}$'
           OR envelope->>10 NOT IN ('DOCTOR_OWNER', 'RECEPTIONIST')
           OR envelope->>11 !~ '^[1-9][0-9]{0,17}$'
           OR envelope->>12 !~ '^[0-9]{13}$'
           OR envelope->>13 !~ '^[A-Za-z0-9_-]{22,128}$'
           OR envelope->>14 NOT IN ('NOTE_READ', 'NOTE_WRITE', 'INVITE', 'SECURITY_CHANGE')
           OR envelope->>15 NOT IN ('p1', 'p2') THEN
            RAISE EXCEPTION 'invalid envelope' USING ERRCODE = '22023';
        END IF;
        issued_at := to_timestamp((envelope->>12)::bigint / 1000.0);
    EXCEPTION WHEN data_exception THEN
        RAISE EXCEPTION 'invalid envelope' USING ERRCODE = '22023';
    END;
    IF issued_at <= clock_timestamp() - interval '60 seconds'
       OR issued_at > clock_timestamp() + interval '5 seconds' THEN
        RAISE EXCEPTION 'proof freshness denied' USING ERRCODE = '42501';
    END IF;
    INSERT INTO f01_private.nonces VALUES (envelope->>1, envelope->>13);
    -- The nonce insert can wait behind cleanup or another registration.
    IF issued_at <= clock_timestamp() - interval '60 seconds' THEN
        RAISE EXCEPTION 'proof freshness denied' USING ERRCODE = '42501';
    END IF;
    INSERT INTO f01_private.requests
        (http_key_id, nonce, database_key_id, organization_id, staff_id, session_id,
         role, security_revision, purpose, target, body_sha256, expires_at)
    VALUES (envelope->>1, envelope->>13, database_key_id, envelope->>7, envelope->>8,
            envelope->>9, envelope->>10, (envelope->>11)::bigint, envelope->>14,
            envelope->>15, envelope->>6, issued_at + interval '60 seconds')
    RETURNING id INTO result_id;
    RETURN result_id;
END;
$$;

-- A continuation is admissible only after the previous bound top-level transaction resolves.
CREATE FUNCTION f01_private.continuation(p_request f01_private.requests)
RETURNS text LANGUAGE sql SECURITY DEFINER SET search_path = pg_catalog, pg_temp AS $$
    SELECT CASE
      WHEN p_request.expires_at <= clock_timestamp() THEN NULL
      WHEN p_request.purpose = 'NOTE_WRITE' AND EXISTS (
        SELECT 1 FROM f01_private.bindings b
        WHERE b.request_id = p_request.id AND b.stage = 'PRIMARY'
          AND pg_xact_status(b.xid) = 'aborted'
      ) AND NOT EXISTS (
        SELECT 1 FROM f01_private.bindings b WHERE b.request_id = p_request.id AND b.stage = 'RECEIPT_RECOVERY'
      ) THEN 'RECEIPT_RECOVERY'
      WHEN p_request.purpose = 'INVITE' AND EXISTS (
        SELECT 1 FROM f01_private.bindings b
        JOIN f01_private.activations a USING (request_id, stage)
        JOIN f01_private.invitations i ON i.organization_id = p_request.organization_id
          AND i.target = p_request.target AND i.staff_id = p_request.staff_id
          AND i.credential_id = p_request.session_id AND i.status = 'PENDING'
          AND i.expires_at > clock_timestamp()
        WHERE b.request_id = p_request.id AND b.stage = 'INVITATION_PREPARE'
          AND pg_xact_status(b.xid) = 'committed'
      ) AND NOT EXISTS (
        SELECT 1 FROM f01_private.bindings b WHERE b.request_id = p_request.id AND b.stage = 'INVITATION_APPLY'
      ) THEN 'INVITATION_APPLY'
      WHEN p_request.purpose = 'SECURITY_CHANGE' AND EXISTS (
        SELECT 1 FROM f01_private.bindings b
        JOIN f01_private.security_changes c ON c.request_id = b.request_id
        WHERE b.request_id = p_request.id AND b.stage = 'PRIMARY'
          AND pg_xact_status(b.xid) = 'committed'
      ) AND NOT EXISTS (
        SELECT 1 FROM f01_private.bindings b WHERE b.request_id = p_request.id AND b.stage = 'SESSION_CLEANUP'
      ) THEN 'SESSION_CLEANUP'
    END;
$$;

CREATE FUNCTION f01_api.bind(p_request_id bigint, p_stage text, p_pid integer, p_xid xid8)
RETURNS void LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, pg_temp AS $$
DECLARE
    req f01_private.requests%ROWTYPE;
    allowed text;
BEGIN
    IF session_user <> 'f01_gate' OR p_request_id IS NULL OR p_stage IS NULL
       OR p_pid IS NULL OR p_xid IS NULL OR p_pid <= 0 THEN
        RAISE EXCEPTION 'binding denied' USING ERRCODE = '42501';
    END IF;
    SELECT * INTO req FROM f01_private.requests r WHERE r.id = p_request_id FOR UPDATE;
    IF NOT FOUND OR req.expires_at <= clock_timestamp() THEN
        RAISE EXCEPTION 'binding denied' USING ERRCODE = '42501';
    END IF;
    IF NOT EXISTS (SELECT 1 FROM f01_private.bindings b WHERE b.request_id = req.id) THEN
        allowed := CASE WHEN req.purpose = 'INVITE' THEN 'INVITATION_PREPARE'
                        WHEN req.purpose IN ('NOTE_READ', 'NOTE_WRITE', 'SECURITY_CHANGE') THEN 'PRIMARY' END;
    ELSE
        allowed := f01_private.continuation(req);
    END IF;
    IF allowed IS NULL OR p_stage IS DISTINCT FROM allowed OR
       pg_xact_status(p_xid) IS DISTINCT FROM 'in progress' OR NOT EXISTS (
          SELECT 1 FROM pg_stat_activity a
          WHERE a.pid = p_pid AND a.datname = current_database() AND a.usename = 'f01_runtime'
       ) THEN RAISE EXCEPTION 'binding denied' USING ERRCODE = '42501'; END IF;
    INSERT INTO f01_private.bindings VALUES
        (req.id, p_stage, (SELECT oid FROM pg_database WHERE datname = current_database()),
         'f01_runtime'::regrole::oid, p_pid, p_xid);
END;
$$;

CREATE FUNCTION f01_api.activate()
RETURNS void LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, pg_temp AS $$
DECLARE
    b f01_private.bindings%ROWTYPE;
    req f01_private.requests%ROWTYPE;
    staff_row f01_private.staff%ROWTYPE;
    sess f01_private.sessions%ROWTYPE;
BEGIN
    IF session_user <> 'f01_runtime' OR current_setting('transaction_isolation') <> 'read committed' THEN
        RAISE EXCEPTION 'activation denied' USING ERRCODE = '42501';
    END IF;
    SELECT * INTO b FROM f01_private.bindings x
    WHERE x.database_oid = (SELECT oid FROM pg_database WHERE datname = current_database())
      AND x.login_oid = session_user::regrole::oid AND x.pid = pg_backend_pid()
      AND x.xid = pg_current_xact_id();
    IF NOT FOUND THEN RAISE EXCEPTION 'activation denied' USING ERRCODE = '42501'; END IF;
    SELECT * INTO req FROM f01_private.requests r WHERE r.id = b.request_id;
    IF NOT FOUND OR req.expires_at <= clock_timestamp() THEN
        RAISE EXCEPTION 'activation denied' USING ERRCODE = '42501';
    END IF;
    PERFORM 1 FROM f01_private.keys k WHERE k.key_id = req.database_key_id AND k.status IN ('ACTIVE', 'RETIRING') FOR SHARE;
    IF NOT FOUND THEN RAISE EXCEPTION 'activation denied' USING ERRCODE = '42501'; END IF;
    -- Lock live authority in the same order for every stage.
    IF b.stage = 'INVITATION_APPLY' THEN
        PERFORM 1 FROM f01_private.organizations o
        WHERE o.id = req.organization_id AND o.status = 'PROVISIONING' FOR UPDATE;
    ELSIF b.stage = 'INVITATION_PREPARE' THEN
        PERFORM 1 FROM f01_private.organizations o
        WHERE o.id = req.organization_id AND o.status = 'PROVISIONING' FOR SHARE;
    ELSE
        PERFORM 1 FROM f01_private.organizations o
        WHERE o.id = req.organization_id AND o.status = 'ACTIVE' FOR SHARE;
    END IF;
    IF NOT FOUND THEN RAISE EXCEPTION 'activation denied' USING ERRCODE = '42501'; END IF;
    IF req.purpose = 'SECURITY_CHANGE' OR b.stage = 'INVITATION_APPLY' THEN
        SELECT * INTO staff_row FROM f01_private.staff s
        WHERE s.organization_id = req.organization_id AND s.id = req.staff_id FOR UPDATE;
    ELSE
        SELECT * INTO staff_row FROM f01_private.staff s
        WHERE s.organization_id = req.organization_id AND s.id = req.staff_id FOR SHARE;
    END IF;
    IF NOT FOUND OR staff_row.role IS DISTINCT FROM req.role
       OR staff_row.role NOT IN ('DOCTOR_OWNER', 'RECEPTIONIST') OR staff_row.reset_required THEN
        RAISE EXCEPTION 'activation denied' USING ERRCODE = '42501';
    END IF;
    IF b.stage IN ('INVITATION_PREPARE', 'INVITATION_APPLY') THEN
        -- The signed invitation credential substitutes for a session only here.
        IF req.purpose <> 'INVITE' OR staff_row.status <> 'INVITED' OR staff_row.email_verified
           OR staff_row.role <> 'DOCTOR_OWNER' OR staff_row.security_revision IS DISTINCT FROM req.security_revision THEN
            RAISE EXCEPTION 'activation denied' USING ERRCODE = '42501';
        END IF;
        IF b.stage = 'INVITATION_APPLY' THEN
            PERFORM 1 FROM f01_private.invitations i WHERE i.organization_id = req.organization_id
              AND i.target = req.target AND i.staff_id = req.staff_id AND i.credential_id = req.session_id
              AND i.status = 'PENDING' AND i.expires_at > clock_timestamp() FOR UPDATE;
        ELSE
            PERFORM 1 FROM f01_private.invitations i WHERE i.organization_id = req.organization_id
              AND i.target = req.target AND i.staff_id = req.staff_id AND i.credential_id = req.session_id
              AND i.status = 'PENDING' AND i.expires_at > clock_timestamp() FOR SHARE;
        END IF;
        IF NOT FOUND OR req.expires_at <= clock_timestamp() THEN
            RAISE EXCEPTION 'activation denied' USING ERRCODE = '42501';
        END IF;
        INSERT INTO f01_private.activations VALUES (b.request_id, b.stage);
        RETURN;
    END IF;
    IF staff_row.status <> 'ACTIVE' OR NOT staff_row.email_verified THEN
        RAISE EXCEPTION 'activation denied' USING ERRCODE = '42501';
    END IF;
    SELECT * INTO sess FROM f01_private.sessions s
    WHERE s.organization_id = req.organization_id AND s.staff_id = req.staff_id AND s.id = req.session_id FOR SHARE;
    IF NOT FOUND OR sess.status <> 'ACTIVE' OR sess.expires_at <= clock_timestamp()
       OR sess.idle_expires_at <= clock_timestamp()
       OR sess.security_revision IS DISTINCT FROM req.security_revision THEN
        RAISE EXCEPTION 'activation denied' USING ERRCODE = '42501';
    END IF;
    IF b.stage = 'SESSION_CLEANUP' THEN
        IF req.purpose <> 'SECURITY_CHANGE' OR staff_row.security_revision <> req.security_revision + 1
           OR NOT EXISTS (
              SELECT 1 FROM f01_private.security_changes c WHERE c.request_id = req.id
              AND c.organization_id = req.organization_id AND c.staff_id = req.staff_id
              AND c.old_revision = req.security_revision AND c.new_revision = staff_row.security_revision
           ) THEN RAISE EXCEPTION 'activation denied' USING ERRCODE = '42501'; END IF;
    ELSIF staff_row.security_revision IS DISTINCT FROM req.security_revision THEN
        RAISE EXCEPTION 'activation denied' USING ERRCODE = '42501';
    END IF;
    IF req.expires_at <= clock_timestamp() THEN
        RAISE EXCEPTION 'activation denied' USING ERRCODE = '42501';
    END IF;
    INSERT INTO f01_private.activations VALUES (b.request_id, b.stage);
END;
$$;

-- No user-controlled GUC or temp object participates in authority resolution.
CREATE FUNCTION f01_private.active_binding()
RETURNS TABLE (request_id bigint, stage text)
LANGUAGE sql SECURITY DEFINER SET search_path = pg_catalog, pg_temp AS $$
    SELECT b.request_id, b.stage FROM f01_private.bindings b
    JOIN f01_private.activations a USING (request_id, stage)
    JOIN f01_private.requests r ON r.id = b.request_id
    WHERE session_user = 'f01_runtime'
      AND b.database_oid = (SELECT oid FROM pg_database WHERE datname = current_database())
      AND b.login_oid = session_user::regrole::oid AND b.pid = pg_backend_pid()
      AND b.xid = pg_current_xact_id_if_assigned()
      AND r.expires_at > clock_timestamp()
      AND CASE WHEN b.stage IN ('INVITATION_PREPARE', 'INVITATION_APPLY') THEN EXISTS (
          SELECT 1 FROM f01_private.invitations i
          WHERE i.organization_id = r.organization_id AND i.staff_id = r.staff_id
            AND i.target = r.target AND i.credential_id = r.session_id
            AND i.status = 'PENDING' AND i.expires_at > clock_timestamp()
      ) ELSE EXISTS (
          SELECT 1 FROM f01_private.sessions s
          WHERE s.organization_id = r.organization_id AND s.staff_id = r.staff_id AND s.id = r.session_id
            AND s.status = 'ACTIVE' AND s.security_revision = r.security_revision
            AND s.expires_at > clock_timestamp() AND s.idle_expires_at > clock_timestamp()
      ) END;
$$;
CREATE FUNCTION f01_api.context()
RETURNS TABLE (organization_id text, staff_id text, role text, purpose text, stage text)
LANGUAGE sql SECURITY DEFINER SET search_path = pg_catalog, pg_temp AS $$
    SELECT r.organization_id, r.staff_id, r.role, r.purpose, b.stage
    FROM f01_private.active_binding() b JOIN f01_private.requests r ON r.id = b.request_id;
$$;

ALTER TABLE f01_data.patients ENABLE ROW LEVEL SECURITY;
ALTER TABLE f01_data.patients FORCE ROW LEVEL SECURITY;
CREATE POLICY owner_effects ON f01_data.patients TO carepulse_identity_owner
    USING (true) WITH CHECK (true);
CREATE POLICY runtime_read ON f01_data.patients FOR SELECT TO f01_runtime
    USING (EXISTS (SELECT 1 FROM f01_api.context() c WHERE c.organization_id = org
                   AND c.stage = 'PRIMARY' AND c.purpose = 'NOTE_READ' AND c.role = 'DOCTOR_OWNER'));

CREATE FUNCTION f01_private.effect(p_purpose text, p_stage text)
RETURNS f01_private.requests LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, pg_temp AS $$
DECLARE req f01_private.requests%ROWTYPE;
BEGIN
    SELECT r.* INTO req FROM f01_private.active_binding() b
    JOIN f01_private.requests r ON r.id = b.request_id
    WHERE r.purpose = p_purpose AND b.stage = p_stage AND r.role = 'DOCTOR_OWNER';
    IF NOT FOUND THEN RAISE EXCEPTION 'effect denied' USING ERRCODE = '42501'; END IF;
    RETURN req;
END;
$$;
CREATE FUNCTION f01_api.write_note(p_target text, p_note text)
RETURNS void LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, pg_temp AS $$
DECLARE req f01_private.requests := f01_private.effect('NOTE_WRITE', 'PRIMARY');
BEGIN
    IF p_target IS NULL OR p_note IS NULL OR p_target IS DISTINCT FROM req.target
       OR encode(f01_crypto.digest(convert_to(p_note, 'UTF8'), 'sha256'), 'hex') IS DISTINCT FROM req.body_sha256 THEN
        RAISE EXCEPTION 'write denied' USING ERRCODE = '42501';
    END IF;
    UPDATE f01_data.patients SET note = p_note WHERE org = req.organization_id AND id = p_target;
    IF NOT FOUND THEN RAISE EXCEPTION 'write denied' USING ERRCODE = '42501'; END IF;
    INSERT INTO f01_private.receipts (organization_id, staff_id, target, body_sha256, note, request_id)
    VALUES (req.organization_id, req.staff_id, p_target, req.body_sha256, p_note, req.id);
END;
$$;
CREATE FUNCTION f01_api.read_receipt()
RETURNS text LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, pg_temp AS $$
DECLARE req f01_private.requests := f01_private.effect('NOTE_WRITE', 'RECEIPT_RECOVERY');
BEGIN
    RETURN (SELECT r.note FROM f01_private.receipts r
            WHERE r.organization_id = req.organization_id AND r.staff_id = req.staff_id
              AND r.target = req.target AND r.body_sha256 = req.body_sha256);
END;
$$;
CREATE FUNCTION f01_api.prepare_invitation()
RETURNS text LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, pg_temp AS $$
DECLARE req f01_private.requests := f01_private.effect('INVITE', 'INVITATION_PREPARE');
BEGIN
    PERFORM 1 FROM f01_private.invitations i WHERE i.organization_id = req.organization_id
      AND i.target = req.target AND i.staff_id = req.staff_id AND i.credential_id = req.session_id
      AND i.status = 'PENDING' AND i.expires_at > clock_timestamp();
    IF NOT FOUND THEN RAISE EXCEPTION 'invitation denied' USING ERRCODE = '42501'; END IF;
    RETURN req.target;
END;
$$;
CREATE FUNCTION f01_api.apply_invitation()
RETURNS void LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, pg_temp AS $$
DECLARE req f01_private.requests := f01_private.effect('INVITE', 'INVITATION_APPLY');
BEGIN
    UPDATE f01_private.invitations SET status = 'APPLIED', request_id = req.id
    WHERE organization_id = req.organization_id AND target = req.target AND staff_id = req.staff_id
      AND credential_id = req.session_id AND status = 'PENDING' AND expires_at > clock_timestamp();
    IF NOT FOUND THEN RAISE EXCEPTION 'invitation denied' USING ERRCODE = '42501'; END IF;
    UPDATE f01_private.staff SET status = 'ACTIVE', email_verified = true
    WHERE organization_id = req.organization_id AND id = req.staff_id AND status = 'INVITED'
      AND security_revision = req.security_revision AND NOT email_verified AND NOT reset_required;
    IF NOT FOUND THEN RAISE EXCEPTION 'invitation denied' USING ERRCODE = '42501'; END IF;
    UPDATE f01_private.organizations SET status = 'ACTIVE'
    WHERE id = req.organization_id AND status = 'PROVISIONING';
    IF NOT FOUND THEN RAISE EXCEPTION 'invitation denied' USING ERRCODE = '42501'; END IF;
END;
$$;
CREATE FUNCTION f01_api.change_security()
RETURNS void LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, pg_temp AS $$
DECLARE req f01_private.requests := f01_private.effect('SECURITY_CHANGE', 'PRIMARY');
BEGIN
    UPDATE f01_private.staff SET security_revision = security_revision + 1
    WHERE organization_id = req.organization_id AND id = req.staff_id AND security_revision = req.security_revision;
    IF NOT FOUND THEN RAISE EXCEPTION 'security change denied' USING ERRCODE = '42501'; END IF;
    INSERT INTO f01_private.security_changes
    VALUES (req.id, req.organization_id, req.staff_id, req.security_revision, req.security_revision + 1);
END;
$$;
CREATE FUNCTION f01_api.cleanup_sessions()
RETURNS integer LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, pg_temp AS $$
DECLARE
    req f01_private.requests := f01_private.effect('SECURITY_CHANGE', 'SESSION_CLEANUP');
    affected integer;
BEGIN
    DELETE FROM f01_private.sessions
    WHERE organization_id = req.organization_id AND staff_id = req.staff_id
      AND security_revision < req.security_revision + 1;
    GET DIAGNOSTICS affected = ROW_COUNT;
    RETURN affected;
END;
$$;

CREATE FUNCTION f01_api.cleanup()
RETURNS integer LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, pg_temp AS $$
DECLARE req f01_private.requests%ROWTYPE; affected integer := 0;
BEGIN
    IF session_user <> 'f01_gate' THEN RAISE EXCEPTION 'cleanup denied' USING ERRCODE = '42501'; END IF;
    FOR req IN SELECT * FROM f01_private.requests r
        WHERE r.expires_at <= clock_timestamp() FOR UPDATE SKIP LOCKED
    LOOP
        IF NOT EXISTS (
            SELECT 1 FROM f01_private.bindings b WHERE b.request_id = req.id
              AND pg_xact_status(b.xid) IS DISTINCT FROM 'committed'
              AND pg_xact_status(b.xid) IS DISTINCT FROM 'aborted'
        ) AND f01_private.continuation(req) IS NULL THEN
            DELETE FROM f01_private.requests WHERE id = req.id;
            DELETE FROM f01_private.nonces WHERE http_key_id = req.http_key_id AND nonce = req.nonce;
            affected := affected + 1;
        END IF;
    END LOOP;
    RETURN affected;
END;
$$;

REVOKE ALL ON SCHEMA f01_private, f01_api, f01_data, f01_crypto FROM PUBLIC;
REVOKE ALL ON ALL TABLES IN SCHEMA f01_private, f01_data FROM PUBLIC;
REVOKE ALL ON ALL FUNCTIONS IN SCHEMA f01_private, f01_api FROM PUBLIC;
GRANT USAGE ON SCHEMA f01_api TO f01_gate, f01_runtime;
GRANT USAGE ON SCHEMA f01_data TO f01_runtime;
GRANT SELECT ON f01_data.patients TO f01_runtime;
GRANT EXECUTE ON FUNCTION f01_api.register_proof(bytea, text, bytea),
    f01_api.bind(bigint, text, integer, xid8), f01_api.cleanup() TO f01_gate;
GRANT EXECUTE ON FUNCTION f01_api.activate(), f01_api.context(),
    f01_api.write_note(text, text), f01_api.read_receipt(), f01_api.prepare_invitation(),
    f01_api.apply_invitation(), f01_api.change_security(), f01_api.cleanup_sessions() TO f01_runtime;
RESET ROLE;
COMMIT;

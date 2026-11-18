-- Private target-bound maintenance control schema for PRE-W3-COMPAT.
-- Provisioned outside the immutable Alembic revision chain by carepulse_migration.
\set ON_ERROR_STOP on

BEGIN;

CREATE SCHEMA IF NOT EXISTS compat_control AUTHORIZATION carepulse_migration;
REVOKE ALL ON SCHEMA compat_control FROM PUBLIC;

CREATE TABLE IF NOT EXISTS compat_control.target_control (
    singleton_id BOOLEAN PRIMARY KEY DEFAULT TRUE CHECK (singleton_id),
    target_id TEXT NOT NULL CHECK (length(trim(target_id)) > 0),
    cluster_id TEXT NOT NULL CHECK (length(trim(cluster_id)) > 0),
    db_name TEXT NOT NULL CHECK (length(trim(db_name)) > 0),
    phase TEXT NOT NULL DEFAULT 'CLOSED'
        CHECK (phase IN ('CLOSED', 'SOURCE_OPEN', 'DRAINING', 'MIGRATING', 'VERIFYING', 'TARGET_OPEN')),
    owner_epoch BIGINT NOT NULL DEFAULT 0 CHECK (owner_epoch >= 0),
    source_manifest_id TEXT NOT NULL CHECK (length(trim(source_manifest_id)) > 0),
    target_manifest_id TEXT NOT NULL CHECK (length(trim(target_manifest_id)) > 0),
    active_generation TEXT NOT NULL DEFAULT 'none'
        CHECK (active_generation IN ('none', 'source', 'candidate')),
    source_sealed BOOLEAN NOT NULL DEFAULT FALSE,
    drain_verified_epoch BIGINT NULL,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS compat_control.work_evidence (
    attempt_id TEXT PRIMARY KEY CHECK (length(trim(attempt_id)) > 0),
    target_id TEXT NOT NULL CHECK (length(trim(target_id)) > 0),
    owner_epoch BIGINT NOT NULL CHECK (owner_epoch >= 0),
    generation TEXT NOT NULL CHECK (generation IN ('source', 'candidate')),
    service TEXT NOT NULL CHECK (service IN ('web', 'api', 'worker', 'sender')),
    invocation_class TEXT NOT NULL CHECK (length(trim(invocation_class)) > 0),
    correlation_id TEXT NULL,
    lease_generation BIGINT NULL,
    disposition TEXT NOT NULL DEFAULT 'PENDING'
        CHECK (disposition IN ('PENDING', 'COMMITTED', 'ROLLED_BACK', 'SENT', 'NOT_SENT', 'UNKNOWN')),
    created_by_role TEXT NOT NULL CHECK (length(trim(created_by_role)) > 0),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Narrow application-scoped own-attempt interface.
-- Product roles cannot mutate target_control, owner_epoch, or another role's attempt.
CREATE OR REPLACE FUNCTION compat_control.record_own_attempt(
    p_attempt_id TEXT,
    p_target_id TEXT,
    p_owner_epoch BIGINT,
    p_generation TEXT,
    p_service TEXT,
    p_invocation_class TEXT,
    p_correlation_id TEXT,
    p_lease_generation BIGINT,
    p_disposition TEXT
) RETURNS VOID
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = compat_control, pg_catalog
AS $$
DECLARE
    v_ctrl compat_control.target_control%ROWTYPE;
    v_existing compat_control.work_evidence%ROWTYPE;
BEGIN
    SELECT * INTO v_ctrl FROM compat_control.target_control WHERE singleton_id = TRUE;
    IF NOT FOUND THEN
        RAISE EXCEPTION 'compat_control_missing' USING ERRCODE = 'P0001';
    END IF;
    IF v_ctrl.target_id <> p_target_id OR v_ctrl.db_name <> current_database() THEN
        RAISE EXCEPTION 'compat_target_mismatch' USING ERRCODE = 'P0001';
    END IF;
    IF p_owner_epoch <> v_ctrl.owner_epoch THEN
        RAISE EXCEPTION 'compat_epoch_mismatch' USING ERRCODE = 'P0001';
    END IF;

    SELECT * INTO v_existing FROM compat_control.work_evidence WHERE attempt_id = p_attempt_id FOR UPDATE;
    IF NOT FOUND THEN
        IF v_ctrl.phase NOT IN ('SOURCE_OPEN', 'TARGET_OPEN') THEN
            RAISE EXCEPTION 'compat_admission_closed' USING ERRCODE = 'P0001';
        END IF;
        IF v_ctrl.active_generation <> p_generation THEN
            RAISE EXCEPTION 'compat_generation_mismatch' USING ERRCODE = 'P0001';
        END IF;
        IF p_disposition <> 'PENDING' THEN
            RAISE EXCEPTION 'compat_initial_disposition_must_be_pending' USING ERRCODE = 'P0001';
        END IF;
        INSERT INTO compat_control.work_evidence (
            attempt_id, target_id, owner_epoch, generation, service,
            invocation_class, correlation_id, lease_generation, disposition, created_by_role
        ) VALUES (
            p_attempt_id, p_target_id, p_owner_epoch, p_generation, p_service,
            p_invocation_class, p_correlation_id, p_lease_generation, p_disposition, session_user
        );
    ELSE
        IF v_existing.created_by_role <> session_user THEN
            RAISE EXCEPTION 'compat_attempt_owner_mismatch' USING ERRCODE = '42501';
        END IF;
        IF v_existing.disposition = 'UNKNOWN' THEN
            RAISE EXCEPTION 'compat_unknown_disposition_immutable' USING ERRCODE = 'P0001';
        END IF;
        UPDATE compat_control.work_evidence
        SET correlation_id = COALESCE(p_correlation_id, correlation_id),
            lease_generation = COALESCE(p_lease_generation, lease_generation),
            disposition = p_disposition,
            updated_at = now()
        WHERE attempt_id = p_attempt_id;
    END IF;
END;
$$;

-- Narrow read-only control reader that takes FOR SHARE without granting UPDATE on target_control.
CREATE OR REPLACE FUNCTION compat_control.read_target_control()
RETURNS TABLE (
    target_id TEXT,
    cluster_id TEXT,
    db_name TEXT,
    phase TEXT,
    owner_epoch BIGINT,
    source_manifest_id TEXT,
    target_manifest_id TEXT,
    active_generation TEXT,
    source_sealed BOOLEAN
)
LANGUAGE sql
SECURITY DEFINER
SET search_path = compat_control, pg_catalog
AS $$
    SELECT tc.target_id, tc.cluster_id, tc.db_name, tc.phase, tc.owner_epoch,
           tc.source_manifest_id, tc.target_manifest_id, tc.active_generation,
           tc.source_sealed
    FROM compat_control.target_control tc
    WHERE tc.singleton_id = TRUE
    FOR SHARE;
$$;

REVOKE ALL ON TABLE compat_control.target_control FROM PUBLIC;
REVOKE ALL ON TABLE compat_control.work_evidence FROM PUBLIC;
REVOKE ALL ON FUNCTION compat_control.read_target_control() FROM PUBLIC;
REVOKE ALL ON FUNCTION compat_control.record_own_attempt(
    TEXT, TEXT, BIGINT, TEXT, TEXT, TEXT, TEXT, BIGINT, TEXT
) FROM PUBLIC;

DO $$
DECLARE
    r TEXT;
BEGIN
    FOREACH r IN ARRAY ARRAY[
        'carepulse_runtime', 'carepulse_auth', 'carepulse_sender', 'carepulse_operator',
        'carepulse_api', 'carepulse_web', 'carepulse_worker', 'carepulse_gate'
    ]
    LOOP
        IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = r) THEN
            EXECUTE format('GRANT USAGE ON SCHEMA compat_control TO %I', r);
            EXECUTE format('GRANT SELECT ON compat_control.target_control TO %I', r);
            EXECUTE format('GRANT EXECUTE ON FUNCTION compat_control.read_target_control() TO %I', r);
            EXECUTE format('REVOKE INSERT, UPDATE, DELETE, TRUNCATE ON compat_control.target_control FROM %I', r);
            EXECUTE format('REVOKE ALL ON compat_control.work_evidence FROM %I', r);
        END IF;
    END LOOP;

    FOREACH r IN ARRAY ARRAY[
        'carepulse_runtime', 'carepulse_auth', 'carepulse_sender',
        'carepulse_api', 'carepulse_web'
    ]
    LOOP
        IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = r) THEN
            EXECUTE format(
                'GRANT EXECUTE ON FUNCTION compat_control.record_own_attempt(TEXT, TEXT, BIGINT, TEXT, TEXT, TEXT, TEXT, BIGINT, TEXT) TO %I',
                r
            );
        END IF;
    END LOOP;
END
$$;

COMMIT;

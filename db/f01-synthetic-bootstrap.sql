-- Disposable Foundation CI only. Never use for a production database.
\set ON_ERROR_STOP on
\getenv key_id CP_DB_IDENTITY_KEY_ID
\getenv key_hex CP_F01_SYNTHETIC_KEY
\getenv freshness CP_F01_FRESHNESS
\getenv skew CP_F01_SKEW
\getenv idle CP_F01_IDLE
BEGIN;
SET LOCAL ROLE carepulse_identity_owner;
INSERT INTO f01_private.policy (signed_request_freshness_seconds,
    signed_request_max_skew_seconds, session_idle_minutes)
VALUES (:'freshness'::integer, :'skew'::integer, :'idle'::integer);
INSERT INTO f01_private.keys (key_id, secret, status)
VALUES (:'key_id', decode(:'key_hex','hex'), 'ACTIVE');
COMMIT;
ALTER ROLE carepulse_runtime NOLOGIN;

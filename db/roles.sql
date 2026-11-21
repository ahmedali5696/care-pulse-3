-- Controlled bootstrap only; password variables come from CI secret files.
\set ON_ERROR_STOP on
\getenv migration_password CP_MIGRATION_PASSWORD
\getenv runtime_password CP_RUNTIME_PASSWORD
\getenv operator_password CP_OPERATOR_PASSWORD
\getenv sender_password CP_SENDER_PASSWORD
\getenv auth_password CP_AUTH_DB_PASSWORD
\getenv api_password CP_API_DB_PASSWORD
\getenv web_password CP_WEB_DB_PASSWORD
\getenv worker_password CP_WORKER_DB_PASSWORD
\getenv gate_password CP_GATE_DB_PASSWORD

BEGIN;
CREATE ROLE carepulse_migration LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE
    NOINHERIT NOREPLICATION NOBYPASSRLS PASSWORD :'migration_password';
CREATE ROLE carepulse_runtime LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE
    NOINHERIT NOREPLICATION NOBYPASSRLS PASSWORD :'runtime_password';
CREATE ROLE carepulse_api LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE
    NOINHERIT NOREPLICATION NOBYPASSRLS PASSWORD :'api_password';
CREATE ROLE carepulse_web LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE
    NOINHERIT NOREPLICATION NOBYPASSRLS PASSWORD :'web_password';
CREATE ROLE carepulse_worker LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE
    NOINHERIT NOREPLICATION NOBYPASSRLS PASSWORD :'worker_password';
CREATE ROLE carepulse_gate LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE
    NOINHERIT NOREPLICATION NOBYPASSRLS PASSWORD :'gate_password';
CREATE ROLE carepulse_operator LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE
    NOINHERIT NOREPLICATION NOBYPASSRLS PASSWORD :'operator_password';
CREATE ROLE carepulse_sender LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE
    NOINHERIT NOREPLICATION NOBYPASSRLS PASSWORD :'sender_password';
CREATE ROLE carepulse_auth LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE
    NOINHERIT NOREPLICATION NOBYPASSRLS PASSWORD :'auth_password';
-- Dedicated D-04 trigger executor; it cannot log in or bypass tenant RLS.
CREATE ROLE carepulse_policy_seed NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE
    NOINHERIT NOREPLICATION NOBYPASSRLS;
GRANT carepulse_policy_seed TO carepulse_migration WITH SET TRUE, INHERIT FALSE;
CREATE ROLE carepulse_identity_owner NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE
    NOINHERIT NOREPLICATION NOBYPASSRLS;
GRANT carepulse_identity_owner TO carepulse_migration WITH SET TRUE, INHERIT FALSE;

SELECT current_database() AS database_name \gset
REVOKE ALL ON DATABASE :"database_name" FROM PUBLIC;
GRANT CONNECT ON DATABASE :"database_name"
    TO carepulse_migration, carepulse_runtime, carepulse_operator, carepulse_sender,
       carepulse_auth, carepulse_api, carepulse_web, carepulse_worker, carepulse_gate;
REVOKE ALL ON SCHEMA public FROM PUBLIC;
ALTER SCHEMA public OWNER TO carepulse_migration;
CREATE SCHEMA f01_private AUTHORIZATION carepulse_identity_owner;
CREATE SCHEMA f01_crypto AUTHORIZATION carepulse_migration;
CREATE EXTENSION pgcrypto WITH SCHEMA f01_crypto;
REVOKE ALL ON SCHEMA f01_private, f01_crypto FROM PUBLIC;
REVOKE ALL ON ALL FUNCTIONS IN SCHEMA f01_crypto FROM PUBLIC;
GRANT EXECUTE ON FUNCTION f01_crypto.hmac(bytea, bytea, text),
    f01_crypto.digest(bytea, text), f01_crypto.gen_random_bytes(integer)
    TO carepulse_identity_owner;
GRANT USAGE ON SCHEMA f01_crypto TO carepulse_identity_owner;
GRANT USAGE ON SCHEMA public TO carepulse_runtime;
GRANT USAGE ON SCHEMA public TO carepulse_sender;
GRANT USAGE ON SCHEMA public TO carepulse_auth;
-- No operator table grants or future-table grants exist in the empty baseline.
COMMIT;

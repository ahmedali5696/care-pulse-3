-- Controlled bootstrap only; password variables come from CI secret files.
\set ON_ERROR_STOP on
\getenv migration_password CP_MIGRATION_PASSWORD
\getenv runtime_password CP_RUNTIME_PASSWORD
\getenv operator_password CP_OPERATOR_PASSWORD

BEGIN;
CREATE ROLE carepulse_migration LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE
    NOINHERIT NOREPLICATION NOBYPASSRLS PASSWORD :'migration_password';
CREATE ROLE carepulse_runtime LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE
    NOINHERIT NOREPLICATION NOBYPASSRLS PASSWORD :'runtime_password';
CREATE ROLE carepulse_operator LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE
    NOINHERIT NOREPLICATION NOBYPASSRLS PASSWORD :'operator_password';

SELECT current_database() AS database_name \gset
REVOKE ALL ON DATABASE :"database_name" FROM PUBLIC;
GRANT CONNECT ON DATABASE :"database_name"
    TO carepulse_migration, carepulse_runtime, carepulse_operator;
REVOKE ALL ON SCHEMA public FROM PUBLIC;
ALTER SCHEMA public OWNER TO carepulse_migration;
GRANT USAGE ON SCHEMA public TO carepulse_runtime;
-- No operator table grants or future-table grants exist in the empty baseline.
COMMIT;

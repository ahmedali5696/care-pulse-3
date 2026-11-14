"""F-01 grant migration, with isolated disposable databases only."""

import shutil
from uuid import uuid4

import psycopg
import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from test_database import ROOT, _create_empty_database, _engine, _password
from test_database import admin as admin  # noqa: PLC0414
from test_database import disposable_project as disposable_project  # noqa: PLC0414
from test_database import empty_database as empty_database  # noqa: PLC0414

PREVIOUS = "0041_f01_runtime_identity"
REVISION = "0042_f01_effective_grants"


def _bootstrap(admin, name):
    _create_empty_database(admin, name)
    # Reproduce the per-database privileged setup; roles already exist in CI.
    with psycopg.connect(
        host="db",
        dbname=name,
        user="carepulse_bootstrap",
        password=_password("carepulse_bootstrap"),
    ) as connection:
        connection.execute("""
            CREATE SCHEMA f01_private AUTHORIZATION carepulse_identity_owner;
            CREATE SCHEMA f01_crypto AUTHORIZATION carepulse_migration;
            CREATE EXTENSION pgcrypto WITH SCHEMA f01_crypto;
            REVOKE ALL ON SCHEMA f01_private, f01_crypto FROM PUBLIC;
            REVOKE ALL ON ALL FUNCTIONS IN SCHEMA f01_crypto FROM PUBLIC;
            GRANT USAGE ON SCHEMA f01_crypto TO carepulse_identity_owner;
            GRANT EXECUTE ON FUNCTION f01_crypto.hmac(bytea,bytea,text),
                f01_crypto.digest(bytea,text), f01_crypto.gen_random_bytes(integer)
                TO carepulse_identity_owner;
        """)


def _upgrade(engine, revision, scripts=None):
    config = Config(str(ROOT / "alembic.ini"))
    if scripts is not None:
        config.set_main_option("script_location", str(scripts))
    with engine.connect() as connection:
        config.attributes["connection"] = connection
        command.upgrade(config, revision)


def _snapshot(connection):
    return (
        connection.scalar(text("SELECT version_num FROM public.alembic_version")),
        connection.execute(
            text("""
            SELECT tablename, policyname, permissive, roles::text, cmd, qual, with_check
            FROM pg_policies WHERE schemaname='public' ORDER BY tablename,policyname
        """)
        ).all(),
        connection.execute(
            text("""
            SELECT c.relname,c.relacl::text,c.relrowsecurity,c.relforcerowsecurity
            FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace
            WHERE n.nspname='public' AND c.relkind IN ('r','S') ORDER BY c.relname
        """)
        ).all(),
        connection.execute(
            text("""
            SELECT c.relname,a.attname,a.attacl::text
            FROM pg_attribute a JOIN pg_class c ON c.oid=a.attrelid
            JOIN pg_namespace n ON n.oid=c.relnamespace
            WHERE n.nspname='public' AND a.attacl IS NOT NULL
            ORDER BY c.relname,a.attname
        """)
        ).all(),
        connection.execute(
            text("""
            SELECT p.oid::regprocedure::text,p.proacl::text
            FROM pg_proc p JOIN pg_namespace n ON n.oid=p.pronamespace
            WHERE n.nspname='public' ORDER BY 1
        """)
        ).all(),
        connection.execute(text("SELECT * FROM public.verification ORDER BY id")).all(),
        connection.execute(
            text(
                "SELECT * FROM public.auth_login_attempts ORDER BY dimension,scope_digest"
            )
        ).all(),
    )


@pytest.mark.parametrize("populated", [False, True])
def test_f01_grants_cutover_is_atomic_repeatable_and_preserves_rows(
    admin, empty_database, tmp_path, populated
):
    _bootstrap(admin, empty_database)
    migration = _engine("carepulse_migration", empty_database)
    try:
        _upgrade(migration, PREVIOUS)
        if populated:
            with migration.begin() as connection:
                connection.execute(
                    text("""
                        INSERT INTO public.verification
                            (id,identifier,value,"expiresAt","createdAt","updatedAt")
                        VALUES (:id,'synthetic','synthetic-user',
                            clock_timestamp()+interval '1 hour',clock_timestamp(),clock_timestamp())
                    """),
                    {"id": str(uuid4())},
                )
                connection.execute(
                    text("""
                    INSERT INTO public.auth_login_attempts
                    VALUES ('identity',repeat('a',64),1,clock_timestamp())
                """)
                )
        with migration.connect() as connection:
            before = _snapshot(connection)
            recovery_triggers = connection.execute(
                text("""
                SELECT p.oid, p.proowner, pg_get_functiondef(p.oid),
                    t.oid, pg_get_triggerdef(t.oid), t.tgenabled
                FROM pg_proc p JOIN pg_trigger t ON t.tgfoid=p.oid
                WHERE p.oid IN ('public.guard_recovery_operator_provenance()'::regprocedure,
                    'public.check_recovery_final_audit()'::regprocedure)
                ORDER BY p.oid, t.oid
            """)
            ).all()
            assert len(recovery_triggers) == 2

        scripts = tmp_path / "migrations"
        shutil.copytree(ROOT / "db/migrations", scripts)
        target = scripts / "versions/0042_f01_effective_grants.py"
        contents = target.read_text(encoding="utf-8")
        assert contents.count("\ndef downgrade() -> None:") == 1
        target.write_text(
            contents.replace(
                "\ndef downgrade() -> None:",
                '\n    op.execute("SELECT 1 / 0")\n\ndef downgrade() -> None:',
            ),
            encoding="utf-8",
        )
        with pytest.raises(DBAPIError) as failed:
            _upgrade(migration, REVISION, scripts)
        assert failed.value.orig.sqlstate == "22012"
        with migration.connect() as connection:
            assert _snapshot(connection) == before

        _upgrade(migration, REVISION)
        with migration.connect() as connection:
            after = _snapshot(connection)
            assert after[0] == REVISION
            assert after[-2:] == before[-2:]
            assert (
                connection.execute(
                    text("""
                SELECT p.oid, p.proowner, pg_get_functiondef(p.oid),
                    t.oid, pg_get_triggerdef(t.oid), t.tgenabled
                FROM pg_proc p JOIN pg_trigger t ON t.tgfoid=p.oid
                WHERE p.oid IN ('public.guard_recovery_operator_provenance()'::regprocedure,
                    'public.check_recovery_final_audit()'::regprocedure)
                ORDER BY p.oid, t.oid
            """)
                ).all()
                == recovery_triggers
            )
            assert connection.scalar(
                text("""
                SELECT NOT EXISTS (
                    SELECT 1 FROM pg_proc p JOIN pg_namespace n ON n.oid=p.pronamespace
                    WHERE n.nspname='public'
                      AND has_function_privilege('carepulse_runtime',p.oid,'EXECUTE')
                )
                AND (SELECT proowner='carepulse_policy_seed'::regrole
                    FROM pg_proc WHERE oid='public.seed_policy_on_organization_insert()'::regprocedure)
                AND NOT has_function_privilege('carepulse_migration',
                    'public.seed_policy_on_organization_insert()','EXECUTE')
                AND has_function_privilege('carepulse_migration',
                    'public.seed_clinic_policy_defaults(uuid)','EXECUTE')
            """)
            )
            assert connection.scalar(
                text("""
                SELECT has_function_privilege('carepulse_api',
                    'public.valid_clinic_policy(text,jsonb)','EXECUTE')
                AND has_function_privilege('carepulse_api',
                    'public.t145_percent_net_matches(numeric,numeric,text)','EXECUTE')
                AND NOT has_table_privilege('carepulse_api',
                    'public.delivery_jobs','UPDATE')
                AND NOT has_column_privilege('carepulse_api',
                    'public.protected_delivery_material','ciphertext_base64url','SELECT')
                AND has_column_privilege('carepulse_sender',
                    'public.verification','expiresAt','SELECT')
            """)
            )
            assert connection.scalar(
                text("""
                SELECT EXISTS (SELECT 1 FROM pg_policies
                    WHERE schemaname='public' AND tablename='verification'
                    AND policyname='f01_verification_sender_read'
                    AND roles=ARRAY['carepulse_sender']::name[])
            """)
            )
        _upgrade(migration, REVISION)
        with migration.connect() as connection:
            assert _snapshot(connection) == after
    finally:
        migration.dispose()

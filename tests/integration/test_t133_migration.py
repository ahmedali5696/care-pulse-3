"""D-08/T133: forward-only 0033 upgrade and ungranted terminal ACL."""

import shutil
from pathlib import Path
from uuid import uuid4

import pytest
from alembic import command
from alembic.config import Config
from carepulse.db import transaction
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from test_appointment_persistence import _activate_clinic
from test_appointment_persistence import appointment_storage as appointment_storage  # noqa: PLC0414
from test_database import _create_empty_database, _engine
from test_database import admin as admin  # noqa: PLC0414
from test_database import disposable_project as disposable_project  # noqa: PLC0414
from test_database import empty_database as empty_database  # noqa: PLC0414
from test_followup_persistence import _v2_booking, _v2_command, _v2_record, _v2_source

ROOT = Path(__file__).resolve().parents[2]
BEFORE = "0032_b10_cairo_offset_guard"
AFTER = "0033_d08_terminal_writer"
CURRENT_HEAD = "0040_t173_numeric_money"
TERMINAL = "public.transition_terminal_appointment(uuid,uuid,bigint,jsonb)"
LEGACY = "public.complete_examined_visit(uuid,uuid,bigint,boolean)"
PRIVATE = "public.d07_complete_examined_visit_raw(uuid,uuid,bigint,boolean)"


def _upgrade_to(engine, revision, scripts=None):
    config = Config(str(ROOT / "alembic.ini"))
    if scripts is not None:
        config.set_main_option("script_location", str(scripts))
    with engine.connect() as connection:
        config.attributes["connection"] = connection
        command.upgrade(config, revision)


def _acl(connection):
    return connection.execute(
        text("""
            SELECT r.rolname,
                   has_function_privilege('carepulse_runtime', p.oid, 'EXECUTE'),
                   EXISTS (
                       SELECT 1 FROM aclexplode(p.proacl) acl
                       WHERE acl.grantee=0
                   ),
                   EXISTS (
                       SELECT 1 FROM aclexplode(p.proacl) acl
                       WHERE acl.is_grantable
                   )
            FROM pg_proc p
            JOIN pg_roles r ON r.oid=p.proowner
            WHERE p.oid=CAST(:signature AS regprocedure)
        """),
        {"signature": TERMINAL},
    ).one()


def test_t133_existing_head_preserves_owner_only_terminal_writer(appointment_storage):
    runtime, migration, (tenant, _), (doctor, _) = appointment_storage
    with migration.connect() as connection:
        assert (
            connection.scalar(text("SELECT version_num FROM public.alembic_version"))
            == CURRENT_HEAD
        )
        assert _acl(connection) == ("carepulse_migration", False, False, False)
        assert connection.scalar(
            text(
                "SELECT proconfig FROM pg_proc WHERE oid=CAST(:signature AS regprocedure)"
            ),
            {"signature": TERMINAL},
        ) == ["search_path=pg_catalog"]
        assert connection.scalar(
            text("""
                SELECT NOT rolbypassrls FROM pg_roles WHERE rolname='carepulse_runtime'
            """)
        )
        assert connection.scalar(
            text("""
                SELECT NOT pg_has_role('carepulse_runtime','carepulse_migration','MEMBER')
            """)
        )
        assert connection.scalar(
            text(
                "SELECT has_function_privilege('carepulse_runtime', :legacy, 'EXECUTE')"
            ),
            {"legacy": LEGACY},
        )
        assert not connection.scalar(
            text(
                "SELECT has_function_privilege('carepulse_runtime', :private, 'EXECUTE')"
            ),
            {"private": PRIVATE},
        )
    with transaction(runtime, tenant) as connection:
        source = _v2_source(connection, tenant, doctor)
        claim = _v2_command(
            connection, "create_followup_claim", source, **_v2_booking(source)
        )
        target = _v2_record(connection, claim)
        with pytest.raises(DBAPIError) as forbidden, connection.begin_nested():
            connection.execute(
                text("""
                    SELECT * FROM public.transition_terminal_appointment(
                        :tenant,:target,0,'{"operation":"CANCEL","reason":"Forbidden"}'::jsonb)
                """),
                {"tenant": tenant, "target": target["id"]},
            )
        assert forbidden.value.orig.sqlstate == "42501"
        assert (
            connection.scalar(
                text("SELECT status FROM public.appointments WHERE id=:target"),
                {"target": target["id"]},
            )
            == "Booked"
        )
        with pytest.raises(DBAPIError) as fabricated, connection.begin_nested():
            connection.execute(
                text("""
                    INSERT INTO public.sensitive_audit (
                        id, organization_id, actor_staff_id, actor_role,
                        action, reason, correlation_id)
                    VALUES (gen_random_uuid(),:tenant,:doctor,'DOCTOR_OWNER',
                            'APPOINTMENT_CANCELLED','Forged terminal action',
                            'synthetic-t133-direct-insert')
                """),
                {"tenant": tenant, "doctor": doctor},
            )
        assert fabricated.value.orig.sqlstate == "42501"


def test_t133_populated_0032_upgrade_preserves_claim_and_new_exam(
    admin, empty_database
):
    _create_empty_database(admin, empty_database)
    migration = _engine("carepulse_migration", empty_database)
    runtime = _engine("carepulse_runtime", empty_database)
    try:
        _upgrade_to(migration, BEFORE)
        operator = uuid4()
        tenant = uuid4()
        with migration.begin() as connection:
            connection.execute(
                text("""
                    INSERT INTO public.operator_identities (
                        id, credential_digest, issued_by_reference, issued_at)
                    VALUES (:id,:digest,'synthetic-t133',now())
                """),
                {"id": operator, "digest": f"synthetic:{operator}"},
            )
            connection.execute(
                text("""
                    INSERT INTO public.organizations (id, branch, created_by_operator_id)
                    VALUES (:tenant,'Synthetic T133 populated upgrade',:operator)
                """),
                {"tenant": tenant, "operator": operator},
            )
            doctor = _activate_clinic(connection, tenant, operator)
        with transaction(runtime, tenant) as connection:
            source = _v2_source(connection, tenant, doctor)
            claim = _v2_command(
                connection, "create_followup_claim", source, **_v2_booking(source)
            )
            target = _v2_record(connection, claim)["id"]
        with migration.connect() as connection:
            original = connection.execute(
                text("""
                    SELECT a.status,c.state,c.revision,
                           p.net_egp,p.entitlement_provenance
                    FROM public.followup_claims c
                    JOIN public.appointments a
                      ON (a.organization_id,a.id)=(c.organization_id,c.appointment_id)
                    JOIN public.appointment_price_snapshots p
                      ON (p.organization_id,p.appointment_id)=(a.organization_id,a.id)
                    WHERE a.id=:target
                """),
                {"target": target},
            ).one()
        _upgrade_to(migration, AFTER)
        _upgrade_to(migration, "head")
        with migration.connect() as connection:
            assert (
                connection.scalar(
                    text("SELECT version_num FROM public.alembic_version")
                )
                == CURRENT_HEAD
            )
            assert _acl(connection) == ("carepulse_migration", False, False, False)
            observed = connection.execute(
                text("""
                    SELECT a.status,c.state,c.revision,
                           p.net_egp,p.entitlement_provenance
                    FROM public.followup_claims c
                    JOIN public.appointments a
                      ON (a.organization_id,a.id)=(c.organization_id,c.appointment_id)
                    JOIN public.appointment_price_snapshots p
                      ON (p.organization_id,p.appointment_id)=(a.organization_id,a.id)
                    WHERE a.id=:target
                """),
                {"target": target},
            ).one()
            assert observed == original
        with transaction(runtime, tenant) as connection:
            new_source = _v2_source(connection, tenant, doctor)
            assert new_source["visit"] is not None
    finally:
        runtime.dispose()
        migration.dispose()


def test_t133_failed_upgrade_0032_rolls_back_every_new_object(
    admin, empty_database, tmp_path
):
    _create_empty_database(admin, empty_database)
    migration = _engine("carepulse_migration", empty_database)
    try:
        _upgrade_to(migration, BEFORE)
        scripts = tmp_path / "migrations"
        shutil.copytree(ROOT / "db/migrations", scripts)
        successor = scripts / "versions/0033_d08_terminal_writer.py"
        successor.write_text(
            'from alembic import op\nrevision="0033_d08_terminal_writer"\n'
            'down_revision="0032_b10_cairo_offset_guard"\n'
            "def upgrade():\n"
            '    op.execute("CREATE TABLE public.t133_failed_upgrade (id integer)")\n'
            '    op.execute("SELECT 1 / 0")\n',
            encoding="utf-8",
        )
        with pytest.raises(DBAPIError) as failed:
            _upgrade_to(migration, "head", scripts)
        assert failed.value.orig.sqlstate == "22012"
        with migration.connect() as connection:
            assert (
                connection.scalar(
                    text("SELECT version_num FROM public.alembic_version")
                )
                == BEFORE
            )
            assert (
                connection.scalar(
                    text("SELECT to_regclass('public.t133_failed_upgrade')")
                )
                is None
            )
            assert (
                connection.scalar(
                    text("SELECT to_regprocedure(:signature)"), {"signature": TERMINAL}
                )
                is None
            )
            assert (
                connection.scalar(
                    text("SELECT to_regprocedure(:signature)"), {"signature": LEGACY}
                )
                is not None
            )
        _upgrade_to(migration, AFTER)
    finally:
        migration.dispose()

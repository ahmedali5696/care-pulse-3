"""D-08/T133: forward-only 0033 upgrade and ungranted terminal ACL."""

from pathlib import Path
from uuid import uuid4

import pytest
from alembic import command
from alembic.config import Config
from carepulse.db import transaction
from runtime_identity_fixture import signed_test_transaction
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from test_appointment_persistence import _activate_clinic
from test_appointment_persistence import appointment_storage as appointment_storage  # noqa: PLC0414
from test_b10_writer_migration import _t177_receipt, _t177_upgrade_proof
from test_booking import booking_body, booking_request
from test_booking import booking_clinic as booking_clinic  # noqa: PLC0414
from test_database import _create_empty_database, _engine
from test_database import admin as admin  # noqa: PLC0414
from test_database import disposable_project as disposable_project  # noqa: PLC0414
from test_database import empty_database as empty_database  # noqa: PLC0414
from test_followup_persistence import _v2_booking, _v2_command, _v2_record, _v2_source
from test_patients import patient_backend as patient_backend  # noqa: PLC0414
from test_request_boundary import boundary_data as boundary_data  # noqa: PLC0414
from test_visit_migration import _prepare

ROOT = Path(__file__).resolve().parents[2]
BEFORE = "0032_b10_cairo_offset_guard"
AFTER = "0033_d08_terminal_writer"
CURRENT_HEAD = "0057_w3_queue_admit"
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


def test_t133_existing_head_preserves_owner_only_terminal_writer(booking_clinic):
    from test_booking import new_patient

    clinic = booking_clinic
    migration, tenant, doctor = clinic.migration, clinic.tenant, clinic.staff
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
        assert not connection.scalar(
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
        for role in (
            "carepulse_api",
            "carepulse_web",
            "carepulse_worker",
            "carepulse_gate",
        ):
            for signature in (TERMINAL, LEGACY, PRIVATE):
                assert not connection.scalar(
                    text("SELECT has_function_privilege(:role, :signature, 'EXECUTE')"),
                    {"role": role, "signature": signature},
                )
    body = booking_body(new_patient(clinic))
    status, response = booking_request(
        clinic, clinic.owner, body=body, key=str(uuid4())
    )
    assert status == 201, response
    target = response["data"]
    with signed_test_transaction(
        clinic,
        clinic.owner,
        method="GET",
        path=f"/v1/appointments/{target['id']}",
        purpose="APPOINTMENT_BOOKING",
    ) as (connection, _):
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
    migration, runtime, tenant, doctor = _prepare(admin, empty_database)
    try:
        _upgrade_to(migration, BEFORE)
        with transaction(runtime, tenant) as connection:
            source = _v2_source(connection, tenant, doctor)
            claim = _v2_command(
                connection, "create_followup_claim", source, **_v2_booking(source)
            )
            assert claim is not None
        _t177_receipt(migration, tenant, doctor, claim)
        _t177_upgrade_proof(migration, BEFORE, AFTER, tmp_path)
        with migration.connect() as connection:
            assert _acl(connection) == ("carepulse_migration", False, False, False)
    finally:
        runtime.dispose()
        migration.dispose()

"""M1 forward-only migration: real PostgreSQL grants, legacy rows, and authority."""

import json
import runpy
from dataclasses import replace
from hashlib import sha256
from types import SimpleNamespace
from uuid import uuid4

import pytest
from alembic import command
from alembic.config import Config
from carepulse.db import transaction
from carepulse.main import app
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from test_appointment_persistence import _activate_clinic
from test_database import ROOT, _create_empty_database, _engine
from test_database import admin as admin  # noqa: PLC0414
from test_database import disposable_project as disposable_project  # noqa: PLC0414
from test_database import empty_database as empty_database  # noqa: PLC0414
from test_request_boundary import _signed
from test_request_boundary import boundary_data as boundary_data  # noqa: PLC0414
from test_schedule import _schedule_request

MIGRATION = ROOT / "db/migrations/versions/0023_m1_operational_manager.py"
SCHEDULE = {
    "booking_mode": "OPEN",
    "duration_minutes": None,
    "holiday_booking_enabled": False,
    "routine_walk_in_enabled": False,
    "allow_overlap": False,
    "no_show_cutoff": {"enabled": False, "local_time": None},
}


def _at_revision(engine, revision):
    config = Config(str(ROOT / "alembic.ini"))
    with engine.connect() as connection:
        config.attributes["connection"] = connection
        command.upgrade(config, revision)


def _seed_staff(connection, tenant, *, enabled=True, bulk=False):
    staff, user = uuid4(), f"synthetic-m1-{uuid4()}"
    email = f"{uuid4()}@example.invalid"
    connection.execute(
        text("""
        INSERT INTO public."user"
            (id, name, email, "emailVerified", "createdAt", "updatedAt")
        VALUES (:user, 'M1 Receptionist', :email, true,
                clock_timestamp(), clock_timestamp())
    """),
        {"user": user, "email": email},
    )
    connection.execute(
        text("""
        INSERT INTO public.staff_accounts
            (id, organization_id, auth_user_id, verified_email,
             role, enabled, verified, bulk_reschedule)
        VALUES (:staff, :tenant, :user, :email, 'RECEPTIONIST',
                :enabled, true, :bulk)
    """),
        {
            "staff": staff,
            "tenant": tenant,
            "user": user,
            "email": email,
            "enabled": enabled,
            "bulk": bulk,
        },
    )
    return staff


def _call_manager(
    connection,
    tenant,
    actor,
    target,
    enabled,
    *,
    revision=0,
    security_revision=0,
    reason="Signed Owner permission change",
):
    return connection.scalar(
        text("""
        SELECT public.set_receptionist_operational_manager(
            :tenant, :actor, :staff, :enabled, :revision, :security_revision,
            :reason, :correlation)
    """),
        {
            "tenant": tenant,
            "actor": actor,
            "staff": target,
            "enabled": enabled,
            "revision": revision,
            "security_revision": security_revision,
            "reason": reason,
            "correlation": uuid4(),
        },
    )


def _as_actor(connection, actor):
    connection.execute(
        text("SELECT set_config('app.staff_id', :staff, true)"),
        {"staff": str(actor)},
    )


def _sqlstate(error):
    return error.value.orig.sqlstate


def test_populated_predecessor_and_runtime_privilege_matrix(
    admin, empty_database, boundary_data, monkeypatch
):
    migration_details = runpy.run_path(str(MIGRATION))
    predecessor, head = (
        migration_details["down_revision"],
        migration_details["revision"],
    )
    _create_empty_database(admin, empty_database)
    migration = _engine("carepulse_migration", empty_database)
    runtime = _engine("carepulse_runtime", empty_database)
    try:
        _at_revision(migration, predecessor)
        tenant_a, tenant_b, operator, preview = uuid4(), uuid4(), uuid4(), uuid4()
        with migration.begin() as connection:
            connection.execute(
                text("""
                INSERT INTO public.operator_identities
                    (id, credential_digest, issued_by_reference, issued_at)
                VALUES (:id, :digest, 'synthetic-m1', clock_timestamp())
            """),
                {"id": operator, "digest": f"synthetic:{operator}"},
            )
            for tenant in (tenant_a, tenant_b):
                connection.execute(
                    text("""
                    INSERT INTO public.organizations
                        (id, branch, created_by_operator_id)
                    VALUES (:id, :branch, :operator)
                """),
                    {"id": tenant, "branch": str(tenant), "operator": operator},
                )
            owner_a = _activate_clinic(connection, tenant_a, operator)
            owner_b = _activate_clinic(connection, tenant_b, operator)
            receptionist = _seed_staff(connection, tenant_a, bulk=True)
            disabled = _seed_staff(connection, tenant_a, enabled=False)
            invited = _seed_staff(connection, tenant_a)
            foreign_staff = _seed_staff(connection, tenant_b)
            email = connection.scalar(
                text(
                    "SELECT verified_email FROM public.staff_accounts WHERE id=:staff"
                ),
                {"staff": invited},
            )
            invitation = uuid4()
            connection.execute(
                text("""
                INSERT INTO public.invitations
                    (id, organization_id, staff_id, purpose, target_email,
                     proof_digest, issued_by_staff_id, expires_at)
                VALUES (:id, :tenant, :staff, 'RECEPTIONIST', :email,
                        :digest, :owner, clock_timestamp() + interval '1 day')
            """),
                {
                    "id": invitation,
                    "tenant": tenant_a,
                    "staff": invited,
                    "email": email,
                    "digest": sha256(str(invitation).encode()).hexdigest(),
                    "owner": owner_a,
                },
            )
            connection.execute(
                text("""
                INSERT INTO public.clinic_policy_previews
                    (id, organization_id, policy_kind, policy_version,
                     policy_revision, actor_staff_id, selected_refs, affected_refs,
                     record_revisions, candidate_payload)
                VALUES (:id, :tenant, 'SCHEDULE', 1, 1, :actor,
                        '[]'::jsonb, '[]'::jsonb, '{}'::jsonb, CAST(:payload AS jsonb))
            """),
                {
                    "id": preview,
                    "tenant": tenant_a,
                    "actor": owner_a,
                    "payload": json.dumps(SCHEDULE),
                },
            )

        _at_revision(migration, head)
        _at_revision(migration, head)
        with migration.connect() as connection:
            assert (
                connection.scalar(
                    text("SELECT version_num FROM public.alembic_version")
                )
                == head
            )
            old = connection.execute(
                text("""
                SELECT operational_manager, bulk_reschedule, revision,
                       security_revision
                FROM public.staff_accounts WHERE id=:staff
            """),
                {"staff": receptionist},
            ).one()
            assert old == (False, True, 0, 0)
            assert (
                connection.scalar(
                    text("""
                SELECT actor_security_revision FROM public.clinic_policy_previews
                WHERE id=:preview
            """),
                    {"preview": preview},
                )
                is None
            )

        # The pre-0023 preview is genuinely historical: neither its immutable
        # row nor the post-0023 INSERT guard is bypassed to manufacture NULL.
        legacy_session = f"synthetic-m1-legacy-{uuid4()}"
        with migration.begin() as connection:
            owner_user = connection.scalar(
                text("SELECT auth_user_id FROM public.staff_accounts WHERE id=:owner"),
                {"owner": owner_a},
            )
            connection.execute(
                text("""
                    INSERT INTO public."session"
                        (id, "userId", token, "expiresAt", "createdAt", "updatedAt",
                         "securityRevision", "absoluteDeadline", "lastActivity")
                    VALUES (:id, :user, :token, clock_timestamp() + interval '1 hour',
                            clock_timestamp(), clock_timestamp(), 0,
                            clock_timestamp() + interval '1 hour', clock_timestamp())
                """),
                {"id": legacy_session, "user": owner_user, "token": str(uuid4())},
            )
        legacy_actor = replace(
            _signed(boundary_data)[0],
            organization_id=str(tenant_a),
            staff_id=str(owner_a),
            session_id=legacy_session,
        )
        legacy_clinic = SimpleNamespace(config=boundary_data.config, tenant=tenant_a)
        monkeypatch.setattr(
            app.state, "service_config", boundary_data.config, raising=False
        )
        monkeypatch.setattr(app.state, "db_engine", runtime, raising=False)
        status, denied = _schedule_request(
            legacy_clinic,
            legacy_actor,
            method="POST",
            suffix="/activate",
            body={
                "preview_id": str(preview),
                "expected_schedule_revision": 1,
                "expected_capacity_revision": 1,
                "reason": "Synthetic pre-M1 preview activation",
            },
            key=str(uuid4()),
        )
        assert status == 409 and denied["error"]["code"] == "STALE_REVISION"
        with transaction(runtime, tenant_a) as connection:
            assert (
                connection.scalar(
                    text("""
                    SELECT revision FROM public.clinic_policy_heads
                    WHERE organization_id=:tenant AND kind='SCHEDULE'
                """),
                    {"tenant": tenant_a},
                )
                == 1
            )
            assert (
                connection.scalar(
                    text("""
                    SELECT count(*) FROM public.clinic_policy_versions
                    WHERE organization_id=:tenant AND kind='SCHEDULE'
                """),
                    {"tenant": tenant_a},
                )
                == 1
            )

        with migration.connect() as connection:
            assert connection.scalar(
                text("""
                SELECT relforcerowsecurity
                FROM pg_class WHERE oid='public.staff_accounts'::regclass
            """)
            )
            assert (
                connection.scalar(
                    text("""
                SELECT count(*) FROM aclexplode(
                    (SELECT relacl FROM pg_class
                     WHERE oid='public.staff_accounts'::regclass))
                WHERE grantee='carepulse_runtime'::regrole
                  AND privilege_type='UPDATE'
            """)
                )
                == 0
            )
            assert not connection.scalar(
                text("""
                SELECT has_column_privilege(
                    'carepulse_runtime','public.staff_accounts',
                    'operational_manager','UPDATE')
            """)
            )
            assert not connection.scalar(
                text("""
                SELECT has_column_privilege(
                    'carepulse_runtime','public.staff_accounts',
                    'bulk_reschedule','UPDATE')
            """)
            )
            for column in (
                "enabled",
                "verified",
                "security_revision",
                "reset_block_operation_id",
                "updated_at",
            ):
                assert connection.scalar(
                    text("""
                    SELECT has_column_privilege('carepulse_runtime',
                        'public.staff_accounts', :column, 'UPDATE')
                """),
                    {"column": column},
                ), column

        # Even the migration owner cannot bypass the security-revision guard.
        with (
            pytest.raises(DBAPIError) as missing_revision,
            migration.begin() as connection,
        ):
            connection.execute(
                text("""
                UPDATE public.staff_accounts SET operational_manager=true
                WHERE id=:staff
            """),
                {"staff": receptionist},
            )
        assert _sqlstate(missing_revision) == "23514"
        with transaction(runtime, tenant_a) as connection:
            assert (
                connection.execute(
                    text("""
                UPDATE public.staff_accounts SET updated_at=clock_timestamp()
                WHERE organization_id=:tenant AND id=:staff
            """),
                    {"tenant": tenant_a, "staff": disabled},
                ).rowcount
                == 1
            )

        with (
            pytest.raises(DBAPIError) as direct_denied,
            transaction(runtime, tenant_a) as connection,
        ):
            connection.execute(
                text("""
                UPDATE public.staff_accounts SET operational_manager=true,
                    security_revision=security_revision+1
                WHERE organization_id=:tenant AND id=:staff
            """),
                {"tenant": tenant_a, "staff": receptionist},
            )
        assert _sqlstate(direct_denied) == "42501"

        # Insertion of a pre-granted Receptionist is blocked even though
        # legacy table-wide staff INSERT is used for invitations. The runtime
        # cannot INSERT native auth users, so seed that prerequisite separately.
        user = f"synthetic-m1-new-{uuid4()}"
        email = f"synthetic-m1-{uuid4()}@example.invalid"
        with migration.begin() as connection:
            connection.execute(
                text("""
                INSERT INTO public."user"
                    (id,name,email,"emailVerified","createdAt","updatedAt")
                VALUES (:id,'M1 new',:email,true,
                        clock_timestamp(),clock_timestamp())
            """),
                {"id": user, "email": email},
            )
        with (
            pytest.raises(DBAPIError) as pregranted,
            transaction(runtime, tenant_a) as connection,
        ):
            connection.execute(
                text("""
                INSERT INTO public.staff_accounts
                    (id,organization_id,auth_user_id,verified_email,role,
                     enabled,verified,operational_manager)
                VALUES (:id,:tenant,:user,:email,
                        'RECEPTIONIST',true,true,true)
            """),
                {"id": uuid4(), "tenant": tenant_a, "user": user, "email": email},
            )
        assert _sqlstate(pregranted) == "23514"
        with migration.connect() as connection:
            assert not connection.scalar(
                text(
                    "SELECT EXISTS(SELECT 1 FROM public.staff_accounts WHERE auth_user_id=:user)"
                ),
                {"user": user},
            )

        with (
            pytest.raises(DBAPIError) as missing_preview_revision,
            transaction(runtime, tenant_a) as connection,
        ):
            connection.execute(
                text("""
                INSERT INTO public.clinic_policy_previews
                    (id, organization_id, policy_kind, policy_version,
                     policy_revision, actor_staff_id, selected_refs,
                     affected_refs, record_revisions, candidate_payload)
                VALUES (:id,:tenant,'SCHEDULE',1,1,:owner,
                        '[]'::jsonb,'[]'::jsonb,'{}'::jsonb,
                        CAST(:payload AS jsonb))
            """),
                {
                    "id": uuid4(),
                    "tenant": tenant_a,
                    "owner": owner_a,
                    "payload": json.dumps(SCHEDULE),
                },
            )
        assert _sqlstate(missing_preview_revision) == "23514"
        with transaction(runtime, tenant_a) as connection:
            connection.execute(
                text("""
                INSERT INTO public.clinic_policy_previews
                    (id, organization_id, policy_kind, policy_version,
                     policy_revision, actor_staff_id, actor_security_revision,
                     selected_refs, affected_refs, record_revisions, candidate_payload)
                VALUES (:id,:tenant,'SCHEDULE',1,1,:owner,0,
                        '[]'::jsonb,'[]'::jsonb,'{}'::jsonb,
                        CAST(:payload AS jsonb))
            """),
                {
                    "id": uuid4(),
                    "tenant": tenant_a,
                    "owner": owner_a,
                    "payload": json.dumps(SCHEDULE),
                },
            )

        # SQL defense-in-depth denies ordinary, disabled, foreign, self and
        # Owner-target changes. Signed/session verification is service-owned.
        for actor, target, clinic, expected_state in (
            (receptionist, disabled, tenant_a, "42501"),
            (owner_a, disabled, tenant_a, "42501"),
            (owner_a, invited, tenant_a, "42501"),
            (owner_a, foreign_staff, tenant_a, "42501"),
            (owner_a, owner_a, tenant_a, "42501"),
            (owner_b, receptionist, tenant_b, "42501"),
        ):
            with (
                pytest.raises(DBAPIError) as rejected,
                transaction(runtime, clinic) as connection,
            ):
                _as_actor(connection, actor)
                _call_manager(connection, clinic, actor, target, True)
            assert _sqlstate(rejected) == expected_state

        with transaction(runtime, tenant_a) as connection:
            _as_actor(connection, owner_a)
            _call_manager(connection, tenant_a, owner_a, receptionist, True)
        with migration.connect() as connection:
            state = connection.execute(
                text("""
                SELECT operational_manager, revision, security_revision
                FROM public.staff_accounts WHERE id=:staff
            """),
                {"staff": receptionist},
            ).one()
            assert state == (True, 1, 1)
        with (
            pytest.raises(DBAPIError) as stale,
            transaction(runtime, tenant_a) as connection,
        ):
            _as_actor(connection, owner_a)
            _call_manager(connection, tenant_a, owner_a, receptionist, False)
        assert _sqlstate(stale) == "40001"
        with transaction(runtime, tenant_a) as connection:
            _as_actor(connection, owner_a)
            _call_manager(
                connection,
                tenant_a,
                owner_a,
                receptionist,
                False,
                revision=1,
                security_revision=1,
            )
        with migration.connect() as connection:
            assert connection.execute(
                text("""
                SELECT operational_manager, revision, security_revision
                FROM public.staff_accounts WHERE id=:staff
            """),
                {"staff": receptionist},
            ).one() == (False, 2, 2)
            audits = connection.execute(
                text("""
                SELECT actor_staff_id, actor_role, reason,
                       before_state->>'operational_manager',
                       after_state->>'operational_manager'
                FROM public.sensitive_audit
                WHERE organization_id=:tenant AND action='RECEPTIONIST_PERMISSIONS'
                ORDER BY occurred_at, id
            """),
                {"tenant": tenant_a},
            ).all()
            assert len(audits) == 2
            assert {tuple(row[3:]) for row in audits} == {
                ("false", "true"),
                ("true", "false"),
            }
            assert all(
                row[0] == owner_a and row[1] == "DOCTOR_OWNER" and row[2]
                for row in audits
            )
            assert (
                connection.scalar(
                    text("""
                SELECT actor_security_revision FROM public.clinic_policy_previews
                WHERE id=:preview
            """),
                    {"preview": preview},
                )
                is None
            )
    finally:
        runtime.dispose()
        migration.dispose()


def test_fresh_install_repeat_and_no_direct_owner_capability(admin, empty_database):
    _create_empty_database(admin, empty_database)
    migration = _engine("carepulse_migration", empty_database)
    try:
        _at_revision(migration, "head")
        _at_revision(migration, "head")
        with migration.connect() as connection:
            assert (
                connection.scalar(
                    text("SELECT version_num FROM public.alembic_version")
                )
                == "0023_m1_operational_manager"
            )
            assert connection.scalar(
                text("""
                SELECT has_function_privilege('carepulse_runtime',
                    'public.set_receptionist_operational_manager(uuid,uuid,uuid,boolean,bigint,bigint,text,uuid)',
                    'EXECUTE')
            """)
            )
            assert not connection.scalar(
                text("""
                SELECT EXISTS (
                    SELECT 1 FROM aclexplode(
                        (SELECT proacl FROM pg_proc WHERE oid =
                            'public.set_receptionist_operational_manager(uuid,uuid,uuid,boolean,bigint,bigint,text,uuid)'::regprocedure))
                    WHERE grantee=0 AND privilege_type='EXECUTE'
                )
            """)
            )
            assert not connection.scalar(
                text("""
                SELECT has_column_privilege('carepulse_runtime',
                    'public.staff_accounts','operational_manager','UPDATE')
            """)
            )
    finally:
        migration.dispose()

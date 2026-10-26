"""D-04: versioned clinic settings on disposable PostgreSQL."""

import json
import os
from concurrent.futures import ThreadPoolExecutor
from datetime import date
from pathlib import Path
from threading import Barrier
from uuid import uuid4

import psycopg
import pytest
from alembic import command
from alembic.config import Config
from carepulse.db import transaction
from carepulse.policies.versions import StalePolicyRevision, append_version
from psycopg import sql
from sqlalchemy import create_engine, text
from sqlalchemy.exc import DBAPIError

DEFAULTS = {
    "PRICE": {
        "standard_new_exam_egp": None,
        "preferential_followup_egp": None,
        "urgent_base_egp": None,
        "urgent_enabled": False,
    },
    "SCHEDULE": {
        "booking_mode": "OPEN",
        "duration_minutes": None,
        "holiday_booking_enabled": False,
        "routine_walk_in_enabled": False,
        "allow_overlap": False,
        "no_show_cutoff": {"enabled": False, "local_time": None},
    },
    "QUEUE": {"routine_order": "BOOKED_NUMBER", "late_depth": 5},
    "ADMISSION": {"actors": "BOTH"},
    "CAPACITY": {"capacity": 1, "paused": False},
    "FOLLOWUP": {"completion_day_zero": True, "last_day_inclusive": 7},
}
ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture
def policy_storage(role_engine, tenant_factory):
    _, create_tenant = tenant_factory
    tenant_a = create_tenant("Synthetic D-04 clinic A")
    tenant_b = create_tenant("Synthetic D-04 clinic B")
    runtime = role_engine("carepulse_runtime")
    migration = role_engine("carepulse_migration")
    operator = role_engine("carepulse_operator")
    try:
        yield runtime, migration, operator, tenant_a, tenant_b
    finally:
        runtime.dispose()
        migration.dispose()
        operator.dispose()


def _versions(connection):
    return connection.execute(
        text("""
            SELECT kind, version, payload
            FROM public.clinic_policy_versions ORDER BY kind, version
        """)
    ).all()


def _heads(connection):
    return connection.execute(
        text("""
            SELECT kind, current_version, revision,
                   queue_effective_on, queue_pending
            FROM public.clinic_policy_heads ORDER BY kind
        """)
    ).all()


def test_new_organizations_get_one_complete_initial_version_per_kind(policy_storage):
    runtime, _, _, tenant_a, tenant_b = policy_storage
    initial = []
    for tenant in (tenant_a, tenant_b):
        with transaction(runtime, tenant) as connection:
            versions = _versions(connection)
            heads = _heads(connection)
            assert len(versions) == len(DEFAULTS)
            assert len(versions) == len(heads)
            assert {kind for kind, _, _ in versions} == {kind for kind, *_ in heads}
            assert all(version == 1 for _, version, _ in versions)
            assert all(
                current_version == revision == 1
                for _, current_version, revision, _, _ in heads
            )
            for kind, _, _, effective, pending in heads:
                assert (effective, pending) == (None, False)
            for table in ("clinic_schedule_intervals", "clinic_schedule_holidays"):
                assert (
                    connection.scalar(text(f"SELECT count(*) FROM public.{table}")) == 0
                )
            initial.append({kind: payload for kind, _, payload in versions})
    assert initial == [DEFAULTS, DEFAULTS]


@pytest.mark.parametrize(
    ("kind", "null_field"),
    [
        ("ADMISSION", "actors"),
        ("QUEUE", "routine_order"),
        ("SCHEDULE", "booking_mode"),
    ],
)
def test_policy_payload_null_required_fields_fail_database_check(
    policy_storage, kind, null_field
):
    runtime, _, _, tenant, _ = policy_storage
    payload = {**DEFAULTS[kind], null_field: None}
    with transaction(runtime, tenant) as connection:
        with pytest.raises(DBAPIError) as denied, connection.begin_nested():
            connection.execute(
                text("""
                    INSERT INTO public.clinic_policy_versions
                        (organization_id, kind, version, payload, pending_effective)
                    VALUES (:tenant, :kind, 2, CAST(:payload AS jsonb), :pending)
                """),
                {
                    "tenant": tenant,
                    "kind": kind,
                    "payload": json.dumps(payload),
                    "pending": kind == "QUEUE",
                },
            )
        assert denied.value.orig.sqlstate == "23514"
        assert denied.value.orig.diag.constraint_name == "clinic_policy_payload_valid"
        assert (
            connection.scalar(
                text("""
                SELECT count(*) FROM public.clinic_policy_versions
                WHERE organization_id = :tenant AND kind = :kind AND version = 2
            """),
                {"tenant": tenant, "kind": kind},
            )
            == 0
        )


def test_enabled_no_show_without_explicit_cutoff_is_accepted(policy_storage):
    runtime, _, _, tenant, _ = policy_storage
    payload = {
        **DEFAULTS["SCHEDULE"],
        "no_show_cutoff": {"enabled": True, "local_time": None},
    }
    with transaction(runtime, tenant) as connection:
        assert (
            connection.scalar(
                text("""
                SELECT public.valid_clinic_policy(
                    'SCHEDULE', CAST(:payload AS jsonb)
                )
            """),
                {"payload": json.dumps(payload)},
            )
            is True
        )
        assert (
            append_version(connection, tenant, "SCHEDULE", payload, expected_revision=1)
            == 2
        )
        assert (
            connection.execute(
                text("""
                SELECT payload FROM public.clinic_policy_versions
                WHERE organization_id = :tenant AND kind = 'SCHEDULE'
                  AND version = 2
            """),
                {"tenant": tenant},
            ).scalar_one()
            == payload
        )
        assert next(row for row in _heads(connection) if row.kind == "SCHEDULE")[
            1:3
        ] == (2, 2)


def test_policy_rls_tenant_separation_and_operator_denial(policy_storage):
    runtime, _, operator, tenant_a, tenant_b = policy_storage
    for table in (
        "clinic_policy_versions",
        "clinic_policy_heads",
        "clinic_policy_previews",
    ):
        with transaction(runtime) as connection:
            assert connection.execute(text(f"SELECT * FROM public.{table}")).all() == []
        with pytest.raises(DBAPIError) as denied, operator.connect() as connection:
            connection.execute(text(f"SELECT * FROM public.{table}"))
        assert denied.value.orig.sqlstate == "42501"
    with pytest.raises(DBAPIError) as denied_insert, operator.begin() as connection:
        connection.execute(
            text("""
            INSERT INTO public.clinic_policy_versions
                (organization_id, kind, version, payload)
            VALUES (:tenant, 'QUEUE', 99, CAST(:payload AS jsonb))
        """),
            {"tenant": tenant_a, "payload": json.dumps(DEFAULTS["QUEUE"])},
        )
    assert denied_insert.value.orig.sqlstate == "42501"

    with transaction(runtime, tenant_a) as connection:
        assert _versions(connection)
        assert (
            connection.scalar(
                text("""
            SELECT count(*) FROM public.clinic_policy_versions
            WHERE organization_id = :other
        """),
                {"other": tenant_b},
            )
            == 0
        )
    with transaction(runtime, tenant_b) as connection:
        assert _versions(connection)
        assert (
            connection.scalar(
                text("""
            SELECT count(*) FROM public.clinic_policy_heads
            WHERE organization_id = :other
        """),
                {"other": tenant_a},
            )
            == 0
        )


def test_policy_seed_role_function_and_trigger_are_privilege_isolated(policy_storage):
    _, migration, _, _, _ = policy_storage
    with migration.connect() as connection:
        assert connection.execute(
            text("""
                SELECT rolcanlogin, rolbypassrls, rolsuper
                FROM pg_roles WHERE rolname = 'carepulse_policy_seed'
            """)
        ).one() == (False, False, False)

        functions = {
            row.proname: row
            for row in connection.execute(
                text("""
                    SELECT p.proname, owner.rolname AS function_owner,
                           p.prosecdef, p.proconfig,
                           has_function_privilege('carepulse_runtime', p.oid, 'EXECUTE')
                               AS runtime_execute,
                           has_function_privilege('carepulse_operator', p.oid, 'EXECUTE')
                               AS operator_execute
                    FROM pg_proc p JOIN pg_roles owner ON owner.oid = p.proowner
                    WHERE p.oid IN (
                        'public.seed_clinic_policy_defaults(uuid)'::regprocedure,
                        'public.seed_policy_on_organization_insert()'::regprocedure
                    )
                """)
            )
        }
        assert set(functions) == {
            "seed_clinic_policy_defaults",
            "seed_policy_on_organization_insert",
        }
        for function in functions.values():
            assert function.function_owner == "carepulse_policy_seed"
            assert function.prosecdef is True
            assert function.proconfig == ["search_path=pg_catalog"]
            assert function.runtime_execute is False
            assert function.operator_execute is False
        assert (
            connection.scalar(
                text("""
            SELECT has_function_privilege(
                'carepulse_migration',
                'public.seed_clinic_policy_defaults(uuid)', 'EXECUTE'
            )
        """)
            )
            is True
        )

        assert connection.execute(
            text("""
                SELECT t.tgtype, t.tgenabled, t.tgisinternal,
                       t.tgfoid =
                         'public.seed_policy_on_organization_insert()'::regprocedure
                FROM pg_trigger t
                WHERE t.tgrelid = 'public.organizations'::regclass
                  AND t.tgname = 'organizations_policy_defaults'
            """)
        ).one() == (5, "O", False, True)  # AFTER INSERT FOR EACH ROW

        operator_grants = dict(
            connection.execute(
                text("""
                    SELECT c.relname,
                           has_table_privilege(
                               'carepulse_operator', c.oid,
                               'SELECT,INSERT,UPDATE,DELETE,TRUNCATE,REFERENCES,TRIGGER'
                           )
                    FROM pg_class c
                    WHERE c.oid IN (
                        'public.clinic_policy_versions'::regclass,
                        'public.clinic_policy_heads'::regclass,
                        'public.clinic_schedule_intervals'::regclass,
                        'public.clinic_schedule_holidays'::regclass,
                        'public.clinic_policy_previews'::regclass
                    )
                """)
            ).all()
        )
        assert set(operator_grants) == {
            "clinic_policy_versions",
            "clinic_policy_heads",
            "clinic_schedule_intervals",
            "clinic_schedule_holidays",
            "clinic_policy_previews",
        }
        assert not any(operator_grants.values())


def test_historical_policy_versions_cannot_be_mutated(policy_storage):
    runtime, migration, _, tenant_a, _ = policy_storage
    with transaction(runtime, tenant_a) as connection:
        kind, version, _ = _versions(connection)[0]
        with pytest.raises(DBAPIError) as denied, connection.begin_nested():
            connection.execute(
                text("""
                UPDATE public.clinic_policy_versions SET payload = '{}'::jsonb
                WHERE organization_id = :tenant AND kind = :kind AND version = :version
            """),
                {"tenant": tenant_a, "kind": kind, "version": version},
            )
        assert denied.value.orig.sqlstate in {"42501", "23514"}

    # A table owner cannot rewrite historical settings through a bypassed app role.
    with migration.begin() as connection:
        connection.execute(
            text("SELECT set_config('app.organization_id', :tenant, true)"),
            {"tenant": str(tenant_a)},
        )
        with pytest.raises(DBAPIError) as denied, connection.begin_nested():
            connection.execute(
                text("""
                UPDATE public.clinic_policy_versions SET payload = '{}'::jsonb
                WHERE organization_id = :tenant AND kind = :kind AND version = :version
            """),
                {"tenant": tenant_a, "kind": kind, "version": version},
            )
        assert denied.value.orig.sqlstate in {"42501", "23514"}


def test_clinical_policy_tables_enforce_rls_even_for_owner(policy_storage):
    _, migration, _, _, _ = policy_storage
    with migration.connect() as connection:
        flags = dict(
            connection.execute(
                text("""
            SELECT relname, relforcerowsecurity
            FROM pg_class
            WHERE oid IN (
                'public.clinic_policy_versions'::regclass,
                'public.clinic_policy_heads'::regclass,
                'public.clinic_policy_previews'::regclass
            )
        """)
            ).all()
        )
    assert flags == {
        "clinic_policy_versions": True,
        "clinic_policy_heads": True,
        "clinic_policy_previews": True,
    }


@pytest.mark.parametrize(
    "column, invalid",
    [("region", "OTHER"), ("currency", "USD"), ("timezone", "UTC")],
)
def test_clinic_currency_timezone_region_remain_fixed(policy_storage, column, invalid):
    _, migration, _, tenant, _ = policy_storage
    with migration.begin() as connection:
        with pytest.raises(DBAPIError) as denied, connection.begin_nested():
            connection.execute(
                text(
                    f"UPDATE public.organizations SET {column} = :value WHERE id = :id"
                ),
                {"id": tenant, "value": invalid},
            )
        assert denied.value.orig.sqlstate == "23514"
        assert connection.execute(
            text("""
                SELECT region, currency, timezone
                FROM public.organizations WHERE id = :id
            """),
            {"id": tenant},
        ).one() == ("EGYPT", "EGP", "Africa/Cairo")


def test_queue_version_pending_without_calendar_preserves_prior_history(policy_storage):
    runtime, migration, _, tenant, _ = policy_storage
    with transaction(runtime, tenant) as connection:
        assert (
            append_version(
                connection,
                tenant,
                "QUEUE",
                {"routine_order": "ARRIVAL_FIFO", "late_depth": 3},
                expected_revision=1,
                today=date(2026, 10, 26),
            )
            == 2
        )
        versions = [row for row in _versions(connection) if row.kind == "QUEUE"]
        assert [(row.version, row.payload) for row in versions] == [
            (1, DEFAULTS["QUEUE"]),
            (2, {"routine_order": "ARRIVAL_FIFO", "late_depth": 3}),
        ]
        head = next(row for row in _heads(connection) if row.kind == "QUEUE")
        assert (
            head.current_version,
            head.revision,
            head.queue_effective_on,
            head.queue_pending,
        ) == (2, 2, None, True)
        with pytest.raises(StalePolicyRevision):
            append_version(
                connection,
                tenant,
                "QUEUE",
                DEFAULTS["QUEUE"],
                expected_revision=1,
                today=date(2026, 10, 26),
            )
        assert len([row for row in _versions(connection) if row.kind == "QUEUE"]) == 2

    # Repeated initialization cannot revert a revised head or replace version 1.
    with migration.begin() as connection:
        connection.execute(
            text("SELECT public.seed_clinic_policy_defaults(:tenant)"),
            {"tenant": tenant},
        )
    with transaction(runtime, tenant) as connection:
        assert len([row for row in _versions(connection) if row.kind == "QUEUE"]) == 2
        head = next(row for row in _heads(connection) if row.kind == "QUEUE")
        assert (head.current_version, head.revision, head.queue_pending) == (2, 2, True)


def test_queue_effective_date_uses_working_calendar_and_skips_holidays(policy_storage):
    runtime, _, _, tenant, _ = policy_storage
    with transaction(runtime, tenant) as connection:
        assert (
            append_version(
                connection,
                tenant,
                "SCHEDULE",
                DEFAULTS["SCHEDULE"],
                expected_revision=1,
                intervals=[
                    {
                        "interval_kind": "WORK",
                        "weekday": 3,
                        "service_date": None,
                        "starts_at": "09:00",
                        "ends_at": "17:00",
                    }
                ],
                holidays=[date(2026, 10, 28)],
            )
            == 2
        )
        assert (
            append_version(
                connection,
                tenant,
                "QUEUE",
                DEFAULTS["QUEUE"],
                expected_revision=1,
                today=date(2026, 10, 26),
            )
            == 2
        )
        head = next(row for row in _heads(connection) if row.kind == "QUEUE")
        assert (head.queue_effective_on, head.queue_pending) == (
            date(2026, 11, 4),
            False,
        )
        assert (
            next(
                row
                for row in _versions(connection)
                if row.kind == "QUEUE" and row.version == 1
            ).payload
            == DEFAULTS["QUEUE"]
        )


def test_schedule_rejects_nonpositive_and_wrong_tenant_periods(policy_storage):
    runtime, _, _, tenant_a, tenant_b = policy_storage
    with transaction(runtime, tenant_a) as connection:
        with pytest.raises(DBAPIError) as denied, connection.begin_nested():
            append_version(
                connection,
                tenant_a,
                "SCHEDULE",
                DEFAULTS["SCHEDULE"],
                expected_revision=1,
                intervals=[
                    {
                        "interval_kind": "WORK",
                        "weekday": 1,
                        "service_date": None,
                        "starts_at": "17:00",
                        "ends_at": "09:00",
                    }
                ],
            )
        assert denied.value.orig.sqlstate == "23514"
        with pytest.raises(DBAPIError) as missing, connection.begin_nested():
            connection.execute(
                text("""
                INSERT INTO public.clinic_schedule_intervals
                    (organization_id, policy_version, interval_kind,
                     weekday, starts_at, ends_at)
                VALUES (:tenant, 9000, 'WORK', 1, '09:00', '17:00')
            """),
                {"tenant": tenant_a},
            )
        assert missing.value.orig.sqlstate == "23514"
        with pytest.raises(DBAPIError) as cross_tenant, connection.begin_nested():
            connection.execute(
                text("""
                INSERT INTO public.clinic_schedule_holidays
                    (organization_id, policy_version, service_date)
                VALUES (:tenant, 1, DATE '2026-10-28')
            """),
                {"tenant": tenant_b},
            )
        # The unpublished-schedule guard may reject before RLS's INSERT check.
        assert cross_tenant.value.orig.sqlstate in {"42501", "23514"}


def test_parallel_writers_commit_one_expected_revision(policy_storage):
    runtime, _, _, tenant, _ = policy_storage
    barrier = Barrier(2)

    def attempt():
        with transaction(runtime, tenant) as connection:
            barrier.wait(timeout=10)
            try:
                append_version(
                    connection,
                    tenant,
                    "FOLLOWUP",
                    DEFAULTS["FOLLOWUP"],
                    expected_revision=1,
                )
                return "updated"
            except StalePolicyRevision:
                return "stale"

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = [
            task.result() for task in [executor.submit(attempt) for _ in range(2)]
        ]
    assert sorted(results) == ["stale", "updated"]
    with transaction(runtime, tenant) as connection:
        assert [
            (row.version, row.payload)
            for row in _versions(connection)
            if row.kind == "FOLLOWUP"
        ] == [
            (1, DEFAULTS["FOLLOWUP"]),
            (2, DEFAULTS["FOLLOWUP"]),
        ]
        head = next(row for row in _heads(connection) if row.kind == "FOLLOWUP")
        assert (head.current_version, head.revision) == (2, 2)


def test_organization_rollback_discards_policy_initialization(
    role_engine, tenant_factory
):
    operator_id, _ = tenant_factory
    migration = role_engine("carepulse_migration")
    runtime = role_engine("carepulse_runtime")
    tenant = uuid4()
    try:
        with (
            pytest.raises(RuntimeError, match="synthetic rollback"),
            migration.begin() as connection,
        ):
            connection.execute(
                text("""
                    INSERT INTO public.organizations
                        (id, branch, created_by_operator_id)
                    VALUES (:tenant, 'Synthetic D-04 rollback', :operator)
                """),
                {"tenant": tenant, "operator": operator_id},
            )
            assert connection.scalar(
                text("""
                    SELECT count(*) FROM public.clinic_policy_versions
                    WHERE organization_id = :tenant
                """),
                {"tenant": tenant},
            ) == len(DEFAULTS)
            raise RuntimeError("synthetic rollback")
        with migration.connect() as connection:
            assert (
                connection.scalar(
                    text("""
                SELECT count(*) FROM public.organizations WHERE id = :tenant
            """),
                    {"tenant": tenant},
                )
                == 0
            )
        with transaction(runtime, tenant) as connection:
            assert _versions(connection) == []
    finally:
        runtime.dispose()
        migration.dispose()


def _actor(connection, tenant):
    """Create a synthetic owner solely to satisfy preview's tenant-qualified FK."""
    staff = uuid4()
    user = f"synthetic-d04-{uuid4()}"
    email = f"{uuid4()}@example.invalid"
    connection.execute(
        text("""
        INSERT INTO public."user"
            (id, name, email, "emailVerified", "createdAt", "updatedAt")
        VALUES (:user, 'Synthetic policy actor', :email, true,
                clock_timestamp(), clock_timestamp())
    """),
        {"user": user, "email": email},
    )
    connection.execute(
        text("""
        INSERT INTO public.staff_accounts
            (id, organization_id, auth_user_id, verified_email,
             role, enabled, verified)
        VALUES (:staff, :tenant, :user, :email, 'DOCTOR_OWNER', true, true)
    """),
        {"staff": staff, "tenant": tenant, "user": user, "email": email},
    )
    return staff


def _preview(connection, tenant, staff, *, preview_id=None):
    preview_id = preview_id or uuid4()
    selected = [str(uuid4())]
    affected = [str(uuid4())]
    captured = {selected[0]: 2}
    connection.execute(
        text("""
        INSERT INTO public.clinic_policy_previews
            (id, organization_id, policy_kind, policy_version,
             policy_revision, service_day_revision, actor_staff_id, service_date,
             selected_refs, affected_refs, record_revisions, candidate_payload)
        VALUES (:id, :tenant, 'SCHEDULE', 1, 1, 3, :staff, DATE '2026-10-30',
                CAST(:selected AS jsonb), CAST(:affected AS jsonb),
                CAST(:captured AS jsonb), CAST(:candidate AS jsonb))
    """),
        {
            "id": preview_id,
            "tenant": tenant,
            "staff": staff,
            "selected": json.dumps(selected),
            "affected": json.dumps(affected),
            "captured": json.dumps(captured),
            "candidate": json.dumps(DEFAULTS["SCHEDULE"]),
        },
    )
    return preview_id, selected, affected, captured


def test_preview_retains_actor_refs_and_captured_revisions_immutably(policy_storage):
    runtime, migration, _, tenant_a, tenant_b = policy_storage
    with migration.begin() as connection:
        actor_a = _actor(connection, tenant_a)
        actor_b = _actor(connection, tenant_b)
    with transaction(runtime, tenant_a) as connection:
        preview, selected, affected, captured = _preview(connection, tenant_a, actor_a)
        record = connection.execute(
            text("""
            SELECT actor_staff_id, policy_kind, policy_version, policy_revision,
                   service_day_revision, service_date,
                   selected_refs, affected_refs, record_revisions,
                   candidate_payload
            FROM public.clinic_policy_previews WHERE id = :id
        """),
            {"id": preview},
        ).one()
        assert record == (
            actor_a,
            "SCHEDULE",
            1,
            1,
            3,
            date(2026, 10, 30),
            selected,
            affected,
            captured,
            DEFAULTS["SCHEDULE"],
        )
        with pytest.raises(DBAPIError) as foreign_actor, connection.begin_nested():
            _preview(connection, tenant_a, actor_b)
        assert foreign_actor.value.orig.sqlstate == "23503"
        with pytest.raises(DBAPIError) as invalid_captured, connection.begin_nested():
            connection.execute(
                text("""
                INSERT INTO public.clinic_policy_previews
                    (id, organization_id, policy_kind, policy_version,
                     policy_revision, actor_staff_id, selected_refs,
                     affected_refs, record_revisions, candidate_payload)
                VALUES (:id, :tenant, 'SCHEDULE', 1, 1, :actor,
                        '{}'::jsonb, '[]'::jsonb, '{}'::jsonb, '{}'::jsonb)
            """),
                {"id": uuid4(), "tenant": tenant_a, "actor": actor_a},
            )
        assert invalid_captured.value.orig.sqlstate == "23514"
    with migration.begin() as connection:
        with pytest.raises(DBAPIError) as immutable, connection.begin_nested():
            connection.execute(
                text("""
                UPDATE public.clinic_policy_previews
                SET record_revisions = '{}'::jsonb WHERE id = :id
            """),
                {"id": preview},
            )
        assert immutable.value.orig.sqlstate == "23514"
    with transaction(runtime, tenant_a) as connection:
        assert (
            connection.scalar(
                text("""
            SELECT count(*) FROM public.clinic_policy_previews WHERE id = :id
        """),
                {"id": preview},
            )
            == 1
        )


def test_older_clinics_backfilled_and_new_clinics_triggered_in_both_orders(
    role_engine,
):
    """Install D-04 after real old tenants; then insert a tenant after install."""
    db_name = os.environ["CP_CI_PROJECT"].replace("-", "_") + "_d04_" + uuid4().hex[:8]
    migration_source = role_engine("carepulse_migration")
    runtime_source = role_engine("carepulse_runtime")
    admin_password = Path("/run/secrets/postgres_password").read_text().strip()
    admin = psycopg.connect(
        host="db",
        dbname="postgres",
        user="carepulse_bootstrap",
        password=admin_password,
        autocommit=True,
    )
    migration = None
    runtime = None

    def upgrade(revision):
        configuration = Config(str(ROOT / "alembic.ini"))
        with migration.connect() as connection:
            configuration.attributes["connection"] = connection
            command.upgrade(configuration, revision)

    try:
        admin.execute(
            sql.SQL("CREATE DATABASE {} OWNER carepulse_migration").format(
                sql.Identifier(db_name),
            )
        )
        migration = create_engine(
            migration_source.url.set(database=db_name),
            hide_parameters=True,
        )
        runtime = create_engine(
            runtime_source.url.set(database=db_name),
            hide_parameters=True,
        )
        with migration.begin() as connection:
            connection.execute(text("REVOKE ALL ON SCHEMA public FROM PUBLIC"))
            connection.execute(
                text("GRANT USAGE ON SCHEMA public TO carepulse_runtime")
            )
            connection.execute(text("GRANT USAGE ON SCHEMA public TO carepulse_auth"))
        with psycopg.connect(
            host="db",
            dbname=db_name,
            user="carepulse_migration",
            password=Path("/run/secrets/migration_password").read_text().strip(),
        ) as connection:
            connection.execute(
                sql.SQL("GRANT CONNECT ON DATABASE {} TO carepulse_runtime").format(
                    sql.Identifier(db_name),
                )
            )
            connection.execute(
                sql.SQL("GRANT CONNECT ON DATABASE {} TO carepulse_auth").format(
                    sql.Identifier(db_name),
                )
            )

        upgrade("0016_d03_patient_name_bounds")
        operator = uuid4()
        existing = {state: uuid4() for state in ("PROVISIONING", "ACTIVE", "SUSPENDED")}
        with migration.begin() as connection:
            connection.execute(
                text("""
                INSERT INTO public.operator_identities
                    (id, credential_digest, issued_by_reference, issued_at)
                VALUES (:id, :digest, 'synthetic-ci', clock_timestamp())
            """),
                {"id": operator, "digest": f"synthetic:{operator}"},
            )
            for state, tenant in existing.items():
                connection.execute(
                    text("""
                    INSERT INTO public.organizations
                        (id, branch, created_by_operator_id)
                    VALUES (:id, :branch, :operator)
                """),
                    {
                        "id": tenant,
                        "branch": f"Synthetic D-04 {state}",
                        "operator": operator,
                    },
                )
            owner_staff = _actor(connection, existing["ACTIVE"])
            owner_user, owner_email = connection.execute(
                text("""
                    SELECT auth_user_id, verified_email
                    FROM public.staff_accounts WHERE id = :staff
                """),
                {"staff": owner_staff},
            ).one()
            invitation = uuid4()
            connection.execute(
                text("""
                    INSERT INTO public.invitations
                        (id, organization_id, purpose, target_email,
                         proof_digest, issued_by_operator_id, expires_at)
                    VALUES (:id, :tenant, 'FIRST_OWNER', :email,
                            :digest, :operator, clock_timestamp() + interval '5 minutes')
                """),
                {
                    "id": invitation,
                    "tenant": existing["ACTIVE"],
                    "email": owner_email,
                    "digest": uuid4().hex + uuid4().hex,
                    "operator": operator,
                },
            )
            connection.execute(
                text("""
                    UPDATE public.invitations
                    SET state = 'ACCEPTED', accepted_by_user_id = :user
                    WHERE id = :id
                """),
                {"user": owner_user, "id": invitation},
            )
            connection.execute(
                text("""
                UPDATE public.organizations SET state = 'ACTIVE'
                WHERE id = :tenant
            """),
                {"tenant": existing["ACTIVE"]},
            )
            connection.execute(
                text("""
                UPDATE public.organizations SET state = 'SUSPENDED'
                WHERE id = :tenant
            """),
                {"tenant": existing["SUSPENDED"]},
            )

        upgrade("head")
        upgrade("head")
        with migration.begin() as connection:
            new_tenant = uuid4()
            connection.execute(
                text("""
                INSERT INTO public.organizations
                    (id, branch, created_by_operator_id)
                VALUES (:tenant, 'Synthetic D-04 created after migration', :operator)
            """),
                {"tenant": new_tenant, "operator": operator},
            )
        for tenant in [*existing.values(), new_tenant]:
            with transaction(runtime, tenant) as connection:
                assert {
                    kind: payload for kind, _, payload in _versions(connection)
                } == DEFAULTS
                assert len(_heads(connection)) == len(DEFAULTS)
                assert all(
                    row.current_version == row.revision == 1
                    for row in _heads(connection)
                )
    finally:
        if migration is not None:
            migration.dispose()
        if runtime is not None:
            runtime.dispose()
        migration_source.dispose()
        runtime_source.dispose()
        try:
            admin.execute(
                sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(
                    sql.Identifier(db_name),
                )
            )
        finally:
            admin.close()

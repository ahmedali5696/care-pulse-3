"""D-08 predecessor, refusal, and atomic Alembic upgrade verification."""

import runpy
import shutil
from datetime import timedelta
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from carepulse.appointments.persistence import persist_appointment
from carepulse.db import transaction
from carepulse.policies.versions import append_version
from carepulse.visits.eligibility import extend_followup
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from test_appointment_persistence import _activate_clinic, _draft, _patient, _query
from test_database import _create_empty_database, _engine
from test_database import admin as admin  # noqa: PLC0414
from test_database import disposable_project as disposable_project  # noqa: PLC0414
from test_database import empty_database as empty_database  # noqa: PLC0414
from test_followup_decisions import _actor, _doctor_context, _extension
from test_followup_persistence import _claim, _outcome, _provenance, _source
from test_visit_migration import _prepare, _upgrade
from test_visit_persistence import _seed

ROOT = Path(__file__).resolve().parents[2]
MIGRATION = ROOT / "db/migrations/versions/0022_d08_followup_claims.py"
BEFORE = "0021_d07_visits"
AFTER = "0022_d08_followup_claims"


def _predecessor(admin, database):
    migration, runtime, tenant, doctor = _prepare(admin, database)
    _upgrade(migration, BEFORE)
    return migration, runtime, tenant, doctor


def test_predecessor_data_and_security_survive_compatible_upgrade(
    admin, empty_database
):
    assert runpy.run_path(str(MIGRATION))["down_revision"] == BEFORE
    migration, runtime, tenant, doctor = _predecessor(admin, empty_database)
    try:
        with transaction(runtime, tenant) as connection:
            source = _seed(connection, tenant, doctor, complete=True)
            cancelled = _seed(connection, tenant, doctor)
            connection.execute(
                text("""
                    UPDATE public.appointments SET status='Cancelled', revision=3
                    WHERE organization_id=:tenant AND id=:id
                """),
                {"tenant": tenant, "id": cancelled},
            )
            connection.execute(
                text("""
                    INSERT INTO public.appointment_history (
                        id, organization_id, appointment_id, appointment_revision,
                        action, old_status, new_status, old_service_date,
                        new_service_date, old_number, new_number, actor_staff_id,
                        actor_role, reason, price_snapshot_appointment_id
                    )
                    SELECT :history, organization_id, id, 3,
                           'DOCTOR_EXCEPTION', 'In Examination', 'Cancelled',
                           service_date, service_date, current_number,
                           current_number, :doctor, 'DOCTOR_OWNER',
                           'Synthetic exception retained', id
                    FROM public.appointments WHERE organization_id=:tenant AND id=:id
                """),
                {
                    "history": uuid4(),
                    "doctor": doctor,
                    "tenant": tenant,
                    "id": cancelled,
                },
            )
            saved = persist_appointment(
                connection,
                _draft(_patient(connection, tenant), doctor),
                _query(tenant, doctor),
            )
            pending = UUID(saved["id"])
            before = connection.execute(
                text("""
                    SELECT a.id, a.status, a.service_date, a.current_number,
                           s.net_egp, v.id AS visit_id
                    FROM public.appointments a
                    JOIN public.appointment_price_snapshots s
                      ON s.organization_id=a.organization_id AND s.appointment_id=a.id
                    LEFT JOIN public.visits v
                      ON v.organization_id=a.organization_id AND v.appointment_id=a.id
                    WHERE a.id IN (:source, :cancelled, :pending) ORDER BY a.id
                """),
                {"source": source, "cancelled": cancelled, "pending": pending},
            ).all()
            assert any(
                row.id == cancelled and row.status == "Cancelled" and row.visit_id
                for row in before
            )
        with migration.connect() as connection:
            predecessor_security = connection.execute(
                text("""
                    SELECT c.relname, c.relrowsecurity, c.relforcerowsecurity,
                           has_table_privilege('carepulse_runtime', c.oid, 'SELECT'),
                           has_table_privilege('carepulse_operator', c.oid, 'SELECT')
                    FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace
                    WHERE n.nspname='public' AND c.relname IN
                        ('appointments','appointment_history','visits','clinical_revisions')
                    ORDER BY c.relname
                """)
            ).all()
        _upgrade(migration, AFTER)
        _upgrade(migration, AFTER)
        with transaction(runtime, tenant) as connection:
            after = connection.execute(
                text("""
                    SELECT a.id, a.status, a.service_date, a.current_number,
                           s.net_egp, v.id AS visit_id
                    FROM public.appointments a
                    JOIN public.appointment_price_snapshots s
                      ON s.organization_id=a.organization_id AND s.appointment_id=a.id
                    LEFT JOIN public.visits v
                      ON v.organization_id=a.organization_id AND v.appointment_id=a.id
                    WHERE a.id IN (:source, :cancelled, :pending) ORDER BY a.id
                """),
                {"source": source, "cancelled": cancelled, "pending": pending},
            ).all()
            assert after == before
            assert (
                connection.scalar(text("SELECT count(*) FROM public.followup_claims"))
                == 0
            )
        with migration.connect() as connection:
            assert (
                connection.execute(
                    text("""
                    SELECT c.relname, c.relrowsecurity, c.relforcerowsecurity,
                           has_table_privilege('carepulse_runtime', c.oid, 'SELECT'),
                           has_table_privilege('carepulse_operator', c.oid, 'SELECT')
                    FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace
                    WHERE n.nspname='public' AND c.relname IN
                        ('appointments','appointment_history','visits','clinical_revisions')
                    ORDER BY c.relname
                """)
                ).all()
                == predecessor_security
            )
            assert (
                connection.scalar(
                    text("SELECT version_num FROM public.alembic_version")
                )
                == AFTER
            )
            for relation in ("followup_claims", "followup_extensions"):
                assert connection.execute(
                    text("""
                        SELECT relrowsecurity, relforcerowsecurity FROM pg_class
                        WHERE oid=to_regclass(:relation)
                    """),
                    {"relation": f"public.{relation}"},
                ).one() == (True, True)
                assert not connection.scalar(
                    text(
                        "SELECT has_table_privilege('carepulse_operator', :table, 'SELECT')"
                    ),
                    {"table": f"public.{relation}"},
                )
    finally:
        runtime.dispose()
        migration.dispose()


def test_incompatible_unpaired_predecessor_claim_refuses_atomically(
    admin, empty_database
):
    migration, runtime, tenant, doctor = _predecessor(admin, empty_database)
    try:
        with transaction(runtime, tenant) as connection:
            followup = persist_appointment(
                connection,
                _draft(
                    _patient(connection, tenant),
                    doctor,
                    service_type="FOLLOW_UP",
                    price_snapshot={
                        "entitlement_provenance": {"synthetic": "unpaired"}
                    },
                ),
                _query(tenant, doctor),
            )
        with pytest.raises(DBAPIError) as refused:
            _upgrade(migration, AFTER)
        assert refused.value.orig.sqlstate == "23514"
        with migration.connect() as connection:
            assert (
                connection.scalar(
                    text("SELECT version_num FROM public.alembic_version")
                )
                == BEFORE
            )
            assert (
                connection.scalar(text("SELECT to_regclass('public.followup_claims')"))
                is None
            )
        with transaction(runtime, tenant) as connection:
            assert (
                connection.scalar(
                    text("SELECT status FROM public.appointments WHERE id=:id"),
                    {"id": UUID(followup["id"])},
                )
                == "Booked"
            )
    finally:
        runtime.dispose()
        migration.dispose()


def test_fresh_and_failed_following_migration_keep_one_head(
    admin, empty_database, tmp_path
):
    _create_empty_database(admin, empty_database)
    migration = _engine("carepulse_migration", empty_database)
    try:
        _upgrade(migration, AFTER)
        scripts = tmp_path / "migrations"
        shutil.copytree(ROOT / "db/migrations", scripts)
        # Isolate D-08 from later M1/B-06 heads for this rollback proof.
        (scripts / "versions/0023_m1_operational_manager.py").unlink()
        (scripts / "versions/0024_b06_followup_window.py").unlink()
        (scripts / "versions/0023_d08_failure.py").write_text(
            'from alembic import op\nrevision = "0023_d08_failure"\n'
            f'down_revision = "{AFTER}"\n'
            "def upgrade():\n"
            '    op.execute("CREATE TABLE public.synthetic_d08_partial (x integer)")\n'
            '    op.execute("SELECT 1 / 0")\n',
            encoding="utf-8",
        )
        with pytest.raises(DBAPIError) as failure:
            _upgrade(migration, "head", scripts)
        assert failure.value.orig.sqlstate == "22012"
        with migration.connect() as connection:
            assert (
                connection.scalar(
                    text("SELECT version_num FROM public.alembic_version")
                )
                == AFTER
            )
            assert (
                connection.scalar(
                    text("SELECT to_regclass('public.synthetic_d08_partial')")
                )
                is None
            )
    finally:
        migration.dispose()


def test_populated_0023_followup_evidence_survives_0024_and_new_claims_use_live_policy(
    admin, empty_database
):
    predecessor = "0023_m1_operational_manager"
    successor = "0024_b06_followup_window"
    assert (
        runpy.run_path(
            str(ROOT / "db/migrations/versions/0024_b06_followup_window.py")
        )["down_revision"]
        == predecessor
    )
    migration, runtime, tenant, doctor = _predecessor(admin, empty_database)
    operator_engine = _engine("carepulse_operator", empty_database)

    def set_doctor_deadline(connection, source):
        connection.execute(
            text(
                "SELECT set_config('app.clinical_change_kind', 'DECISION_EXCEPTION', true)"
            )
        )
        connection.execute(
            text("SELECT set_config('app.clinical_reason', 'Synthetic deadline', true)")
        )
        connection.execute(
            text("""
                UPDATE public.visits SET followup_decision = jsonb_build_object(
                    'kind', 'DEADLINE',
                    'deadline_date', CAST(:deadline AS text),
                    'reason', 'Synthetic deadline')
                WHERE organization_id=:tenant AND id=:visit
            """),
            {
                "deadline": (source["day"] + timedelta(days=14)).isoformat(),
                "tenant": tenant,
                "visit": source["visit"],
            },
        )
        assert (
            connection.scalar(
                text("SELECT clinical_revision FROM public.visits WHERE id=:visit"),
                {"visit": source["visit"]},
            )
            == 2
        )

    tables = (
        "appointments",
        "appointment_history",
        "appointment_number_assignments",
        "appointment_day_counters",
        "appointment_price_snapshots",
        "visits",
        "clinical_revisions",
        "clinical_audit",
        "followup_claims",
        "followup_extensions",
        "idempotency_receipts",
        "clinic_policy_heads",
        "clinic_policy_versions",
    )
    secured_tables = (
        "appointments",
        "visits",
        "followup_claims",
        "followup_extensions",
        "idempotency_receipts",
    )

    def snapshot(connection):
        return {
            table: connection.execute(
                text(
                    f"SELECT to_jsonb(t) FROM public.{table} t ORDER BY to_jsonb(t)::text"
                )
            )
            .scalars()
            .all()
            for table in tables
        }

    def security(connection):
        relations = connection.execute(
            text("""
                SELECT c.relname, c.relrowsecurity, c.relforcerowsecurity,
                       r.rolname, p.privilege,
                       has_table_privilege(r.oid, c.oid, p.privilege),
                       CASE WHEN p.privilege IN ('SELECT', 'INSERT', 'UPDATE')
                            THEN has_any_column_privilege(r.oid, c.oid, p.privilege)
                       END
                FROM pg_class c
                CROSS JOIN pg_roles r
                CROSS JOIN (VALUES ('SELECT'), ('INSERT'), ('UPDATE'),
                                   ('DELETE')) p(privilege)
                WHERE c.relnamespace='public'::regnamespace
                  AND c.relname=ANY(:tables)
                  AND r.rolname IN ('carepulse_runtime','carepulse_operator')
                ORDER BY c.relname, r.rolname, p.privilege
            """),
            {"tables": list(secured_tables)},
        ).all()
        policies = connection.execute(
            text("""
                SELECT tablename, policyname, permissive, roles, cmd, qual, with_check
                FROM pg_policies WHERE schemaname='public' AND tablename=ANY(:tables)
                ORDER BY tablename, policyname
            """),
            {"tables": list(secured_tables)},
        ).all()
        triggers = connection.execute(
            text("""
                SELECT tgrelid::regclass::text, tgname, tgfoid::regprocedure::text,
                       tgenabled, tgdeferrable, tginitdeferred, pg_get_triggerdef(t.oid)
                FROM pg_trigger t
                WHERE tgrelid IN ('public.appointments'::regclass,
                                  'public.followup_claims'::regclass,
                                  'public.followup_extensions'::regclass)
                  AND NOT tgisinternal
                ORDER BY tgrelid::regclass::text, tgname
            """)
        ).all()
        functions = {
            row.proname: row
            for row in connection.execute(
                text("""
                    SELECT p.proname, pg_get_userbyid(p.proowner) AS owner,
                           p.prosecdef, p.proacl::text AS acl,
                           pg_get_functiondef(p.oid) AS definition
                    FROM pg_proc p
                    WHERE p.oid IN (
                        'public.guard_followup_claim()'::regprocedure,
                        'public.guard_followup_extension()'::regprocedure,
                        'public.assert_followup_claim_consistency()'::regprocedure)
                """)
            )
        }
        return relations, policies, triggers, functions

    try:
        _upgrade(migration, predecessor)
        with migration.connect() as connection:
            assert connection.execute(
                text("SELECT version_num FROM public.alembic_version")
            ).scalars().all() == [predecessor]
        with migration.begin() as connection:
            operator = connection.scalar(
                text(
                    "SELECT created_by_operator_id FROM public.organizations WHERE id=:id"
                ),
                {"id": tenant},
            )
            other = uuid4()
            connection.execute(
                text("""
                    INSERT INTO public.organizations (id, branch, created_by_operator_id)
                    VALUES (:id, 'Other synthetic migration tenant', :operator)
                """),
                {"id": other, "operator": operator},
            )
            other_doctor = _activate_clinic(connection, other, operator)

        with transaction(runtime, tenant) as connection:
            active_source = _source(connection, tenant, doctor)
            active = _claim(connection, tenant, active_source, price="80.00")
            extension_command = _extension(active_source, active["id"])
            extension_key = str(uuid4())
            _doctor_context(connection, doctor)
            extension_result = extend_followup(
                connection,
                _actor(tenant, doctor),
                active_source["visit"],
                extension_command,
                extension_key,
            )
            assert extension_result["revision"] == 2

            released_source = _source(connection, tenant, doctor)
            released = _claim(connection, tenant, released_source, price="95.00")
            _outcome(connection, tenant, doctor, released, status="Cancelled")
            consumed_source = _source(connection, tenant, doctor)
            consumed = _claim(connection, tenant, consumed_source, price="90.00")
            _outcome(connection, tenant, doctor, consumed, status="Completed")
            doctor_source = _source(connection, tenant, doctor)
            set_doctor_deadline(connection, doctor_source)
            doctor_claim = _claim(
                connection,
                tenant,
                doctor_source,
                day_offset=14,
                deadline=doctor_source["day"] + timedelta(days=14),
                provenance={**_provenance(doctor_source), "source_revision": 2},
                price="91.00",
            )
            fresh_source = _source(connection, tenant, doctor)
            fresh_doctor_source = _source(connection, tenant, doctor)
            set_doctor_deadline(connection, fresh_doctor_source)
            assert (
                append_version(
                    connection,
                    tenant,
                    "FOLLOWUP",
                    {"completion_day_zero": True, "last_day_inclusive": 10},
                    1,
                    actor_staff_id=str(doctor),
                )
                == 2
            )

        with transaction(runtime, other) as connection:
            other_source = _source(connection, other, other_doctor)
            other_claim = _claim(connection, other, other_source, price="72.00")

        with migration.connect() as connection:
            before = snapshot(connection)
            before_security = security(connection)
            assert len(before["followup_claims"]) == 5
            assert len(before["followup_extensions"]) == 1
            assert before["idempotency_receipts"]
            assert {row["state"] for row in before["followup_claims"]} == {
                "ACTIVE",
                "RELEASED",
                "CONSUMED",
            }
            by_claim = {row["id"]: row for row in before["followup_claims"]}
            assert (
                by_claim[str(active["claim"])]["approved_deadline"]
                == (active_source["day"] + timedelta(days=7)).isoformat()
            )
            assert (
                by_claim[str(doctor_claim["claim"])]["approved_deadline"]
                == (doctor_source["day"] + timedelta(days=14)).isoformat()
            )
            assert before["followup_extensions"][0]["claim_id"] == str(active["claim"])

        _upgrade(migration, successor)
        _upgrade(migration, successor)

        with migration.connect() as connection:
            assert connection.execute(
                text("SELECT version_num FROM public.alembic_version")
            ).scalars().all() == [successor]
            assert snapshot(connection) == before
            after_relations, after_policies, after_triggers, after_functions = security(
                connection
            )
            old_relations, old_policies, old_triggers, old_functions = before_security
            assert after_relations == old_relations
            assert after_policies == old_policies
            assert after_triggers == old_triggers
            assert set(after_functions) == set(old_functions)
            for function, original in old_functions.items():
                updated = after_functions[function]
                assert updated[:4] == original[:4]
                assert updated.owner == "carepulse_migration"
                assert updated.prosecdef is True
                if function == "guard_followup_claim":
                    assert updated.definition != original.definition
                else:
                    assert updated.definition == original.definition

        with transaction(runtime, other) as connection:
            assert (
                connection.scalar(text("SELECT count(*) FROM public.followup_claims"))
                == 1
            )
            assert (
                connection.scalar(
                    text("SELECT count(*) FROM public.followup_extensions")
                )
                == 0
            )
            assert connection.scalar(
                text(
                    "SELECT approved_deadline FROM public.followup_claims WHERE id=:id"
                ),
                {"id": other_claim["claim"]},
            ) == other_source["day"] + timedelta(days=7)
        with transaction(runtime, None) as connection:
            assert (
                connection.scalar(text("SELECT count(*) FROM public.followup_claims"))
                == 0
            )
        with (
            pytest.raises(DBAPIError) as forbidden,
            operator_engine.connect() as connection,
        ):
            connection.execute(text("SELECT * FROM public.followup_claims")).all()
        assert forbidden.value.orig.sqlstate == "42501"

        with transaction(runtime, tenant) as connection:
            assert (
                connection.scalar(text("SELECT count(*) FROM public.followup_claims"))
                == 4
            )
            _doctor_context(connection, doctor)
            assert (
                extend_followup(
                    connection,
                    _actor(tenant, doctor),
                    active_source["visit"],
                    extension_command,
                    extension_key,
                )
                == extension_result
            )
            assert (
                connection.scalar(
                    text(
                        "SELECT count(*) FROM public.followup_extensions WHERE claim_id=:id"
                    ),
                    {"id": active["claim"]},
                )
                == 1
            )
            for source, deadline, provenance in (
                (fresh_source, 10, _provenance(fresh_source, version=1)),
                (
                    fresh_doctor_source,
                    14,
                    {
                        **_provenance(fresh_doctor_source, version=1),
                        "source_revision": 2,
                    },
                ),
            ):
                with pytest.raises(DBAPIError) as stale, connection.begin_nested():
                    _claim(
                        connection,
                        tenant,
                        source,
                        day_offset=deadline,
                        deadline=source["day"] + timedelta(days=deadline),
                        provenance=provenance,
                    )
                assert stale.value.orig.sqlstate == "23514"
            with pytest.raises(DBAPIError) as forged, connection.begin_nested():
                _claim(
                    connection,
                    tenant,
                    fresh_source,
                    day_offset=11,
                    deadline=fresh_source["day"] + timedelta(days=11),
                    provenance=_provenance(fresh_source, version=2),
                )
            assert forged.value.orig.sqlstate == "23514"
            fresh_default = _claim(
                connection,
                tenant,
                fresh_source,
                day_offset=10,
                deadline=fresh_source["day"] + timedelta(days=10),
                provenance=_provenance(fresh_source, version=2),
                price="125.00",
            )
            fresh_deadline = _claim(
                connection,
                tenant,
                fresh_doctor_source,
                day_offset=14,
                deadline=fresh_doctor_source["day"] + timedelta(days=14),
                provenance={
                    **_provenance(fresh_doctor_source, version=2),
                    "source_revision": 2,
                },
                price="115.00",
            )
            _outcome(connection, tenant, doctor, active, status="Cancelled")
            _outcome(connection, tenant, doctor, doctor_claim, status="Completed")
            rebooked = _claim(
                connection,
                tenant,
                active_source,
                day_offset=10,
                deadline=active_source["day"] + timedelta(days=10),
                provenance={
                    **_provenance(active_source, version=2),
                    "source_revision": 2,
                },
                price="130.00",
            )
            assert connection.execute(
                text("""
                    SELECT c.state, c.revision, c.approved_deadline,
                           c.policy_snapshot, s.net_egp
                    FROM public.followup_claims c
                    JOIN public.appointment_price_snapshots s
                      ON (s.organization_id, s.appointment_id)=
                         (c.organization_id, c.appointment_id)
                    WHERE c.id IN (:old, :new)
                    ORDER BY c.approved_deadline
                """),
                {"old": active["claim"], "new": rebooked["claim"]},
            ).all() == [
                (
                    "RELEASED",
                    2,
                    active_source["day"] + timedelta(days=7),
                    _provenance(active_source),
                    80,
                ),
                (
                    "ACTIVE",
                    1,
                    active_source["day"] + timedelta(days=10),
                    {**_provenance(active_source, version=2), "source_revision": 2},
                    130,
                ),
            ]
            assert {fresh_default["claim"], fresh_deadline["claim"]} <= set(
                connection.execute(
                    text("SELECT id FROM public.followup_claims WHERE state='ACTIVE'")
                ).scalars()
            )
            preserved_claims = {
                row["id"]: row
                for row in connection.execute(
                    text("""
                        SELECT to_jsonb(c) FROM public.followup_claims c
                    WHERE id IN (:released, :consumed)
                    """),
                    {
                        "released": released["claim"],
                        "consumed": consumed["claim"],
                    },
                ).scalars()
            }
            assert preserved_claims == {
                str(record["claim"]): by_claim[str(record["claim"])]
                for record in (released, consumed)
            }
            completed_doctor = connection.execute(
                text("SELECT to_jsonb(c) FROM public.followup_claims c WHERE id=:id"),
                {"id": doctor_claim["claim"]},
            ).scalar_one()
            assert (completed_doctor["state"], completed_doctor["revision"]) == (
                "CONSUMED",
                2,
            )
            assert completed_doctor["terminal_history_revision"] == 3
            assert completed_doctor["terminal_at"] is not None
            for field, value in by_claim[str(doctor_claim["claim"])].items():
                if field not in (
                    "state",
                    "revision",
                    "terminal_history_revision",
                    "terminal_at",
                ):
                    assert completed_doctor[field] == value
    finally:
        operator_engine.dispose()
        runtime.dispose()
        migration.dispose()

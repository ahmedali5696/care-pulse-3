"""F-01 grant migration, with isolated disposable databases only."""

import shutil
from pathlib import Path
from uuid import uuid4

import psycopg
import pytest
from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory
from carepulse.appointments.persistence import persist_appointment
from carepulse.db import transaction
from sqlalchemy import URL, create_engine, text
from sqlalchemy.exc import DBAPIError
from test_appointment_persistence import (
    _activate_clinic,
    _admit_saved,
    _draft,
    _patient,
    _query,
    _seed_current_completion,
)
from test_database import ROOT, _create_empty_database, _engine, _password
from test_database import admin as admin  # noqa: PLC0414
from test_database import disposable_project as disposable_project  # noqa: PLC0414
from test_database import empty_database as empty_database  # noqa: PLC0414
from test_evidence_persistence import _new_delivery
from test_followup_persistence import _claim, _source
from test_queue_persistence import _arrive

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
            SELECT p.oid::regprocedure::text,p.proowner,p.proacl::text,
                p.prosecdef,p.proconfig,pg_get_functiondef(p.oid)
            FROM pg_proc p JOIN pg_namespace n ON n.oid=p.pronamespace
            WHERE n.nspname='public' AND p.prokind IN ('f','p') ORDER BY 1
        """)
        ).all(),
        connection.scalar(
            text("SELECT pg_get_functiondef(to_regprocedure('public.f01_cleanup()'))")
        ),
        connection.execute(
            text("""
            SELECT c.relname, a.attname, format_type(a.atttypid,a.atttypmod), a.attnotnull, a.attacl::text
            FROM pg_attribute a JOIN pg_class c ON c.oid=a.attrelid
            JOIN pg_namespace n ON n.oid=c.relnamespace
            WHERE n.nspname='f01_private' AND a.attnum>0 AND NOT a.attisdropped
            ORDER BY c.relname,a.attnum
        """)
        ).all(),
        connection.execute(
            text("""
            SELECT p.proname, p.proowner, p.proacl::text, pg_get_functiondef(p.oid)
            FROM pg_proc p JOIN pg_namespace n ON n.oid=p.pronamespace
            WHERE n.nspname='f01_private' ORDER BY p.proname
        """)
        ).all(),
        connection.execute(
            text("""
            SELECT t.tgname, pg_get_triggerdef(t.oid), t.tgenabled
            FROM pg_trigger t JOIN pg_proc p ON p.oid=t.tgfoid
            JOIN pg_namespace n ON n.oid=p.pronamespace
            WHERE n.nspname IN ('public','f01_private') ORDER BY t.tgrelid,t.tgname
        """)
        ).all(),
        _constraints(connection, ("public", "f01_private")),
        connection.execute(
            text("""
            SELECT n.nspname,c.relname,c.relowner,c.relkind,c.relacl::text,
                   c.relrowsecurity,c.relforcerowsecurity
            FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace
            WHERE n.nspname IN ('public','f01_private')
            ORDER BY n.nspname,c.relname
        """)
        ).all(),
        connection.execute(text("SELECT * FROM public.verification ORDER BY id")).all(),
        connection.execute(
            text(
                "SELECT * FROM public.auth_login_attempts ORDER BY dimension,scope_digest"
            )
        ).all(),
        connection.execute(
            text("SELECT * FROM public.delivery_jobs ORDER BY id")
        ).all(),
        connection.execute(
            text("SELECT * FROM public.protected_delivery_material ORDER BY job_id")
        ).all(),
    )


def _constraints(connection, schemas):
    return connection.execute(
        text("""
        SELECT n.nspname,c.relname,k.conname,k.convalidated,
               k.condeferrable,k.condeferred,pg_get_constraintdef(k.oid)
        FROM pg_constraint k JOIN pg_class c ON c.oid=k.conrelid
        JOIN pg_namespace n ON n.oid=c.relnamespace
        WHERE n.nspname::text=ANY(:schemas) ORDER BY n.nspname,c.relname,k.conname
    """),
        {"schemas": list(schemas)},
    ).all()


def _historic_rows(connection):
    return {
        table: connection.execute(
            text(f"SELECT to_jsonb(t) FROM public.{table} t ORDER BY to_jsonb(t)::text")
        )
        .scalars()
        .all()
        for table in (
            "operator_identities",
            "organizations",
            '"user"',
            '"session"',
            "staff_accounts",
            "invitations",
            "patients",
            "appointments",
            "appointment_day_counters",
            "appointment_number_assignments",
            "appointment_history",
            "appointment_price_snapshots",
            "visits",
            "clinical_revisions",
            "clinical_audit",
            "completion_attestations",
            "followup_source_state",
            "followup_claims",
            "followup_extensions",
            "followup_source_decision_events",
            "followup_authorization_events",
            "clinic_policy_versions",
            "clinic_policy_heads",
            "queue_days",
            "queue_entries",
            "doctor_controls",
            "sensitive_audit",
            "outbox_events",
            "idempotency_receipts",
            "request_nonces",
            "verification",
            "auth_login_attempts",
            "delivery_jobs",
            "protected_delivery_material",
        )
    }


def _private_nonces(connection):
    connection.execute(text("SET LOCAL ROLE carepulse_identity_owner"))
    try:
        if connection.scalar(text("SELECT to_regclass('f01_private.nonces')")) is None:
            return None
        return connection.execute(
            text("SELECT http_key_id,nonce FROM f01_private.nonces ORDER BY 1,2")
        ).all()
    finally:
        connection.execute(text("RESET ROLE"))


def test_f01_populated_0040_cutover_preserves_history_and_rolls_back_each_correction(
    admin, empty_database, tmp_path
):
    _bootstrap(admin, empty_database)
    migration = _engine("carepulse_migration", empty_database)
    baseline = "0040_t173_numeric_money"
    head = ScriptDirectory(str(ROOT / "db/migrations")).get_current_head()
    try:
        # Create genuine predecessor claim evidence before its v2 schema exists.
        # All setup uses maintenance; the retired runtime login stays retired.
        _upgrade(migration, "0024_b06_followup_window")
        operator, tenant = uuid4(), uuid4()
        with migration.begin() as connection:
            connection.execute(
                text("""
                    INSERT INTO public.operator_identities
                        (id, credential_digest, issued_by_reference, issued_at)
                    VALUES (:operator, :digest, 'synthetic-f01-cutover', clock_timestamp())
                """),
                {"operator": operator, "digest": f"synthetic:{operator}"},
            )
            connection.execute(
                text("""
                    INSERT INTO public.organizations (id, branch, created_by_operator_id)
                    VALUES (:tenant, 'Synthetic F01 cutover', :operator)
                """),
                {"tenant": tenant, "operator": operator},
            )
            doctor = _activate_clinic(connection, tenant, operator)
        with transaction(migration, tenant) as connection:
            source = _source(connection, tenant, doctor)
            claim = _claim(connection, tenant, source)

        _upgrade(migration, baseline)
        with transaction(migration, tenant) as connection:
            connection.execute(
                text("""
                    INSERT INTO public."session"
                        (id, "userId", token, "expiresAt", "createdAt", "updatedAt",
                         "securityRevision", "absoluteDeadline", "lastActivity")
                    SELECT :session, auth_user_id, :token, now()+interval '1 hour',
                           now(), now(), security_revision, now()+interval '1 hour', now()
                    FROM public.staff_accounts WHERE id=:doctor
                """),
                {"session": str(uuid4()), "token": str(uuid4()), "doctor": doctor},
            )
            saved = persist_appointment(
                connection,
                _draft(_patient(connection, tenant), doctor),
                _query(tenant, doctor),
            )
            _admit_saved(connection, tenant, doctor, saved)
            visit = _seed_current_completion(connection, tenant, saved["id"], doctor)
            connection.execute(
                text(
                    "SELECT set_config('app.clinical_reason','Synthetic retained correction',true)"
                )
            )
            connection.execute(
                text(
                    "UPDATE public.visits SET diagnosis='Retained diagnosis' WHERE id=:visit"
                ),
                {"visit": visit},
            )
            connection.execute(
                text("""
                    INSERT INTO public.request_nonces (key_id, nonce, signed_at)
                    VALUES ('legacy-http-key', 'synthetic-f01-retained-nonce', clock_timestamp())
                """)
            )
            _new_delivery(connection, operator)
            waiting = persist_appointment(
                connection,
                _draft(_patient(connection, tenant), doctor, status="Waiting"),
                _query(tenant, doctor),
            )
            _arrive(connection, tenant, doctor, waiting)
        with migration.connect() as connection:
            retained = _historic_rows(connection)
            original_constraints = _constraints(connection, ("public",))
            original_rls = connection.execute(
                text("""
                    SELECT relname,relrowsecurity,relforcerowsecurity FROM pg_class
                    WHERE relnamespace='public'::regnamespace AND relkind='r'
                    ORDER BY relname
                """)
            ).all()
            assert all(
                retained[table]
                for table in (
                    "appointments",
                    "appointment_day_counters",
                    "appointment_number_assignments",
                    "appointment_history",
                    "appointment_price_snapshots",
                    "visits",
                    "clinical_revisions",
                    "completion_attestations",
                    "followup_claims",
                    "sensitive_audit",
                    "outbox_events",
                    "idempotency_receipts",
                    "request_nonces",
                    "delivery_jobs",
                    "protected_delivery_material",
                    '"session"',
                )
            )
            assert any(
                row["id"] == str(claim["claim"]) for row in retained["followup_claims"]
            )
            assert any(row["id"] == str(visit) for row in retained["visits"])

        graph = ScriptDirectory(str(ROOT / "db/migrations"))
        revisions = list(reversed(list(graph.iterate_revisions(head, baseline))))
        assert revisions[0].revision == PREVIOUS
        assert revisions[-1].revision == head
        revision_order = [revision.revision for revision in revisions]
        queue_grants_index = revision_order.index("0047_b12_queue_checkin_grants")
        assert revision_order[queue_grants_index - 1 : queue_grants_index + 19] == [
            "0046_f01_audit_integrity",
            "0047_b12_queue_checkin_grants",
            "0048_b12_queue_scope",
            "0049_b12_checkin_provenance",
            "0050_b12_checkin_completeness",
            "0051_b11_intake",
            "0052_b11_routine_scope",
            "0053_b11_receipt_conflict",
            "0054_b12_receipt_scope",
            "0055_b14_admission_control",
            "0056_b14_target_binding",
            "0057_w3_queue_admit",
            "0058_b15_admission_guards",
            "0059_b13_queue_policy_placement",
            "0060_b16_visits",
            "0061_b17_cancel_replace",
            "0062_b17_typed_body",
            "0063_b18_reschedule",
            "0064_b18_source_alias",
            "0065_b18_followup_fence",
            "0066_b19_bulk_reschedule",
        ]
        assert any(row["disposition"] == "ACTIVE" for row in retained["queue_entries"])
        for revision in revisions:
            target = revision.revision
            with migration.connect() as connection:
                before = (
                    _snapshot(connection),
                    _historic_rows(connection),
                    _private_nonces(connection),
                )
            # Inject at the end of each migration, including 0041/0043 and every
            # corrective successor, after every normal DDL/data statement ran.
            scripts = tmp_path / target
            shutil.copytree(ROOT / "db/migrations", scripts)
            script = scripts / f"versions/{target}.py"
            contents = script.read_text(encoding="utf-8")
            marker = "\ndef downgrade() -> None:"
            assert contents.count(marker) == 1
            script.write_text(
                contents.replace(marker, '\n    op.execute("SELECT 1 / 0")\n' + marker),
                encoding="utf-8",
            )
            with pytest.raises(DBAPIError) as failed:
                _upgrade(migration, target, scripts)
            assert failed.value.orig.sqlstate == "22012"
            with migration.connect() as connection:
                assert (
                    _snapshot(connection),
                    _historic_rows(connection),
                    _private_nonces(connection),
                ) == before

            _upgrade(migration, target)
            if target == "0060_b16_visits":
                retained["visits"] = [
                    row | {"recommendation": None} for row in retained["visits"]
                ]
            with migration.connect() as connection:
                assert _historic_rows(connection) == retained
                assert _private_nonces(connection) == [
                    ("legacy-http-key", "synthetic-f01-retained-nonce")
                ]
                assert set(original_constraints) <= set(
                    _constraints(connection, ("public",))
                )
                after = _snapshot(connection)
                assert after[0] == target
            _upgrade(migration, target)
            with migration.connect() as connection:
                assert _snapshot(connection) == after
                assert _historic_rows(connection) == retained
                assert _private_nonces(connection) == [
                    ("legacy-http-key", "synthetic-f01-retained-nonce")
                ]

        with migration.connect() as connection:
            assert connection.execute(
                text("""
                    SELECT relname,relrowsecurity,relforcerowsecurity FROM pg_class
                    WHERE relnamespace='public'::regnamespace AND relkind='r'
                    ORDER BY relname
                """)
            ).all() == [
                (name, True, True)
                if name in ("user", "verification")
                else (name, rls, force)
                for name, rls, force in original_rls
            ]
            assert (
                connection.scalar(
                    text("""
                SELECT NOT has_column_privilege('carepulse_api','public.visits','diagnosis','UPDATE')
                   AND NOT has_column_privilege('carepulse_api','public.visits','diagnosis','SELECT')
                   AND NOT has_table_privilege('carepulse_api','public.clinical_revisions','INSERT')
                   AND NOT has_table_privilege('carepulse_runtime','public.idempotency_receipts','INSERT')
                   AND NOT has_column_privilege('carepulse_api','public.delivery_jobs','claim_owner','SELECT')
                   AND has_column_privilege('carepulse_sender','public.delivery_jobs','claim_owner','SELECT')
                   AND (SELECT prosecdef AND proowner = 'carepulse_migration'::regrole
                        FROM pg_proc WHERE oid = 'public.f01_intake_key_conflict()'::regprocedure)
                   AND has_function_privilege('carepulse_api','public.f01_intake_key_conflict()','EXECUTE')
                   AND NOT EXISTS (
                       SELECT 1 FROM unnest(ARRAY['public','carepulse_runtime','carepulse_gate',
                           'carepulse_web','carepulse_worker','carepulse_operator','carepulse_sender',
                           'carepulse_auth','carepulse_identity_owner']) AS role_name
                       WHERE has_function_privilege(role_name,'public.f01_intake_key_conflict()','EXECUTE')
                   )
            """)
                )
                is True
            )
        admin.execute(
            psycopg.sql.SQL("GRANT CONNECT ON DATABASE {} TO carepulse_api").format(
                psycopg.sql.Identifier(empty_database)
            )
        )
        api = create_engine(
            URL.create(
                "postgresql+psycopg",
                username="carepulse_api",
                password=Path("/run/secrets/api_db_password").read_text().strip(),
                host="db",
                database=empty_database,
            ),
            hide_parameters=True,
        )
        try:
            with api.connect() as api_conn:
                for sql in (
                    'SELECT count(*) FROM public."user"',
                    "SELECT count(*) FROM public.patients",
                    "SELECT count(*) FROM public.appointments",
                    "SELECT count(*) FROM public.visits",
                    "SELECT count(*) FROM public.followup_claims",
                ):
                    assert api_conn.scalar(text(sql)) == 0
                for fn_sql in (
                    "SELECT * FROM public.f01_counted_appointment_intervals(:tenant, CURRENT_DATE)",
                    "SELECT * FROM public.f01_schedule_review_appointments(:tenant, CURRENT_DATE)",
                ):
                    with (
                        pytest.raises(DBAPIError) as denied,
                        api_conn.begin_nested(),
                    ):
                        api_conn.execute(text(fn_sql), {"tenant": tenant})
                    assert denied.value.orig.sqlstate == "42501"
        finally:
            api.dispose()
    finally:
        migration.dispose()


@pytest.mark.parametrize("populated", [False, True])
@pytest.mark.parametrize(
    "predecessor,revision",
    [
        (PREVIOUS, REVISION),
        ("0043_f01_writer_boundary", "0044_f01_delivery_acl"),
        ("0044_f01_delivery_acl", "0045_f01_revocation_evidence"),
        ("0045_f01_revocation_evidence", "0046_f01_audit_integrity"),
    ],
)
def test_f01_grants_cutover_is_atomic_repeatable_and_preserves_rows(
    admin, empty_database, tmp_path, populated, predecessor, revision
):
    _bootstrap(admin, empty_database)
    migration = _engine("carepulse_migration", empty_database)
    try:
        _upgrade(migration, predecessor)
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
                operator = uuid4()
                connection.execute(
                    text(
                        "INSERT INTO public.operator_identities (id, credential_digest, issued_by_reference, issued_at) VALUES (:id, :digest, 'synthetic-acl-migration', clock_timestamp())"
                    ),
                    {"id": operator, "digest": f"synthetic:{operator}"},
                )
                _new_delivery(connection, operator)
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
        target = scripts / f"versions/{revision}.py"
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
            _upgrade(migration, revision, scripts)
        assert failed.value.orig.sqlstate == "22012"
        with migration.connect() as connection:
            assert _snapshot(connection) == before

        _upgrade(migration, revision)
        with migration.connect() as connection:
            after = _snapshot(connection)
            assert after[0] == revision
            assert after[-4:] == before[-4:]
            assert connection.scalar(
                text(
                    "SELECT has_column_privilege('carepulse_api','public.delivery_jobs','claim_owner','SELECT')"
                )
            ) is (revision == REVISION)
            assert (
                connection.scalar(
                    text(
                        "SELECT has_column_privilege('carepulse_sender','public.delivery_jobs','claim_owner','SELECT')"
                    )
                )
                is True
            )
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
            if revision in (
                "0045_f01_revocation_evidence",
                "0046_f01_audit_integrity",
            ):
                expected_triggers = (
                    2 if revision == "0045_f01_revocation_evidence" else 9
                )
                assert (
                    connection.scalar(
                        text("""
                    SELECT (SELECT count(*) FROM pg_trigger t JOIN pg_proc p ON p.oid=t.tgfoid
                        JOIN pg_namespace n ON n.oid=p.pronamespace
                        WHERE n.nspname='f01_private' AND p.proowner='carepulse_identity_owner'::regrole
                            AND p.prosecdef AND t.tgenabled='O'
                            AND NOT has_function_privilege('carepulse_api',p.oid,'EXECUTE')
                            AND NOT has_function_privilege('carepulse_gate',p.oid,'EXECUTE')) = :expected
                        AND NOT has_table_privilege('carepulse_identity_owner','public.patients','TRIGGER')
                        AND NOT has_table_privilege('carepulse_identity_owner','public.appointments','TRIGGER')
                        AND NOT has_table_privilege('carepulse_identity_owner','public.appointment_history','TRIGGER')
                        AND NOT has_table_privilege('carepulse_identity_owner','public.staff_accounts','TRIGGER')
                        AND NOT has_table_privilege('carepulse_identity_owner','public.invitations','TRIGGER')
                        AND NOT has_table_privilege('carepulse_identity_owner','public.sensitive_audit','TRIGGER')
                """),
                        {"expected": expected_triggers},
                    )
                    is True
                )
            if revision == "0046_f01_audit_integrity":
                assert (
                    connection.scalar(
                        text("""
                    SELECT has_function_privilege('carepulse_api',
                        'public.f01_counted_appointment_intervals(uuid,date)','EXECUTE')
                        AND has_function_privilege('carepulse_api',
                            'public.f01_schedule_review_appointments(uuid,date)','EXECUTE')
                        AND has_function_privilege('carepulse_api',
                            'public.f01_audit_targets()','EXECUTE')
                        AND has_function_privilege('carepulse_identity_owner',
                            'public.native_account_unbound_for_receptionist(uuid,text)','EXECUTE')
                        AND NOT has_function_privilege('public',
                            'public.f01_counted_appointment_intervals(uuid,date)','EXECUTE')
                        AND NOT has_function_privilege('public',
                            'public.f01_schedule_review_appointments(uuid,date)','EXECUTE')
                        AND NOT has_function_privilege('public',
                            'public.f01_audit_targets()','EXECUTE')
                        AND NOT has_schema_privilege('carepulse_identity_owner','public','CREATE')
                        AND NOT has_schema_privilege('carepulse_api','f01_private','USAGE')
                """)
                    )
                    is True
                )
        _upgrade(migration, revision)
        with migration.connect() as connection:
            assert _snapshot(connection) == after
    finally:
        migration.dispose()

"""B-06 predecessor upgrade, legacy price data and PostgreSQL arithmetic contract."""

import json
import runpy
import shutil
from decimal import Decimal
from uuid import uuid4

import pytest
from alembic import command
from alembic.config import Config
from carepulse.appointments.persistence import persist_appointment
from carepulse.appointments.snapshots import validate_price_snapshot
from carepulse.db import transaction
from carepulse.policies.pricing import quote_price, validate_discount_reason
from carepulse.policies.versions import append_version
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from test_appointment_persistence import (
    _activate_clinic,
    _draft,
    _patient,
    _query,
)
from test_appointment_persistence import (
    appointment_storage as appointment_storage,  # noqa: PLC0414 - fixture export
)
from test_database import (
    ROOT,
    _create_empty_database,
    _engine,
)
from test_database import (
    admin as admin,  # noqa: PLC0414 - fixture export
)
from test_database import (
    disposable_project as disposable_project,  # noqa: PLC0414 - fixture export
)
from test_database import (
    empty_database as empty_database,  # noqa: PLC0414 - fixture export
)
from test_followup_persistence import _v2_priced_followup

MIGRATION = ROOT / "db/migrations/versions/0019_b06_price_policy_strings.py"
T145_BEFORE = "0033_d08_terminal_writer"
T145_AFTER = "0034_t145_exact_percentage"
T173_BEFORE = "0039_t160_capacity_numeric"
T173_AFTER = "0040_t173_numeric_money"


def _revisions():
    info = runpy.run_path(str(MIGRATION))
    assert info["revision"] == MIGRATION.stem
    return info["down_revision"], info["revision"]


def _at_revision(engine, revision, scripts=None):
    config = Config(str(ROOT / "alembic.ini"))
    if scripts is not None:
        config.set_main_option("script_location", str(scripts))
    with engine.connect() as conn:
        config.attributes["connection"] = conn
        command.upgrade(config, revision)


def _database_fingerprint(conn):
    return {
        "tenants": conn.execute(
            text("""
            SELECT id, branch, state FROM public.organizations ORDER BY id
        """)
        ).all(),
        "versions": conn.execute(
            text("""
            SELECT organization_id, kind, version, payload::text, created_by_staff_id
            FROM public.clinic_policy_versions
            ORDER BY organization_id, kind, version
        """)
        ).all(),
        "heads": conn.execute(
            text("""
            SELECT organization_id, kind, current_version, revision
            FROM public.clinic_policy_heads ORDER BY organization_id, kind
        """)
        ).all(),
        "snapshots": conn.execute(
            text("""
            SELECT organization_id, appointment_id, row_to_json(s)::text
            FROM public.appointment_price_snapshots s ORDER BY organization_id, appointment_id
        """)
        ).all(),
        "receipts": conn.execute(
            text("""
            SELECT id, organization_id, operation, request_digest, result_projection::text
            FROM public.idempotency_receipts ORDER BY organization_id, id
        """)
        ).all(),
        "row_security": conn.execute(
            text("""
            SELECT relname, relrowsecurity, relforcerowsecurity, relacl::text
            FROM pg_class WHERE relnamespace = 'public'::regnamespace
              AND relkind = 'r' ORDER BY relname
        """)
        ).all(),
        "privileges": conn.execute(
            text("""
            SELECT role.rolname, table_name, privilege_type
            FROM information_schema.role_table_grants g
            JOIN pg_roles role ON role.rolname = g.grantee
            WHERE table_schema = 'public' AND role.rolname IN
                ('carepulse_runtime', 'carepulse_operator')
            ORDER BY role.rolname, table_name, privilege_type
        """)
        ).all(),
    }


def test_predecessor_legacy_numeric_null_snapshots_receipts_survive_upgrade(
    admin, empty_database
):
    predecessor, head = _revisions()
    _create_empty_database(admin, empty_database)
    migration = _engine("carepulse_migration", empty_database)
    runtime = _engine("carepulse_runtime", empty_database)
    try:
        _at_revision(migration, predecessor)
        operator, tenants, doctors = uuid4(), (uuid4(), uuid4()), []
        with migration.begin() as conn:
            conn.execute(
                text("""
                INSERT INTO public.operator_identities
                    (id, credential_digest, issued_by_reference, issued_at)
                VALUES (:operator, :digest, 'synthetic-ci', clock_timestamp())
            """),
                {"operator": operator, "digest": f"synthetic:{operator}"},
            )
            for tenant in tenants:
                conn.execute(
                    text("""
                    INSERT INTO public.organizations
                        (id, branch, created_by_operator_id)
                    VALUES (:tenant, :branch, :operator)
                """),
                    {"tenant": tenant, "branch": str(tenant), "operator": operator},
                )
                doctors.append(_activate_clinic(conn, tenant, operator))
        for tenant, doctor in zip(tenants, doctors, strict=True):
            with transaction(runtime, tenant) as conn:
                saved = persist_appointment(
                    conn,
                    _draft(_patient(conn, tenant), doctor),
                    _query(tenant, doctor),
                )
                assert saved["net_egp"] == "100.00"
                if tenant == tenants[0]:
                    append_version(
                        conn,
                        str(tenant),
                        "PRICE",
                        '{"standard_new_exam_egp":100.123,'
                        '"preferential_followup_egp":30.123,'
                        '"urgent_base_egp":null,"urgent_enabled":false}',
                        1,
                    )
        with migration.connect() as conn:
            before = _database_fingerprint(conn)
            assert len(before["tenants"]) == 2
            assert len(before["snapshots"]) == len(before["receipts"]) == 2
            assert conn.execute(
                text("""
                    SELECT payload->>'standard_new_exam_egp',
                           payload->>'preferential_followup_egp'
                    FROM public.clinic_policy_versions
                    WHERE organization_id = :tenant AND kind = 'PRICE' AND version = 2
                """),
                {"tenant": tenants[0]},
            ).one() == ("100.123", "30.123")
        _at_revision(migration, head)
        _at_revision(migration, head)
        with migration.connect() as conn:
            assert (
                conn.scalar(text("SELECT version_num FROM public.alembic_version"))
                == head
            )
            assert _database_fingerprint(conn) == before
            assert (
                conn.scalar(
                    text("""
                    SELECT public.valid_clinic_policy('PRICE', payload)
                    FROM public.clinic_policy_versions
                    WHERE organization_id = :tenant AND kind = 'PRICE' AND version = 2
                """),
                    {"tenant": tenants[0]},
                )
                is True
            )
        with transaction(runtime, tenants[0]) as conn:
            assert (
                append_version(
                    conn,
                    str(tenants[0]),
                    "PRICE",
                    '{"standard_new_exam_egp":"100.12",'
                    '"preferential_followup_egp":30.123,'
                    '"urgent_base_egp":null,"urgent_enabled":false}',
                    2,
                )
                == 3
            )
        with migration.connect() as conn:
            values = conn.execute(
                text("""
                    SELECT version, payload->>'standard_new_exam_egp',
                           payload->>'preferential_followup_egp',
                           jsonb_typeof(payload->'preferential_followup_egp')
                    FROM public.clinic_policy_versions
                    WHERE organization_id = :tenant AND kind = 'PRICE'
                    ORDER BY version
                """),
                {"tenant": tenants[0]},
            ).all()
            assert values == [
                (1, None, None, "null"),
                (2, "100.123", "30.123", "number"),
                (3, "100.12", "30.123", "number"),
            ]
            after = _database_fingerprint(conn)
            assert after["snapshots"] == before["snapshots"]
            assert after["receipts"] == before["receipts"]
            assert after["row_security"] == before["row_security"]
            assert after["privileges"] == before["privileges"]
        with pytest.raises(DBAPIError) as bad, transaction(runtime, tenants[1]) as conn:
            append_version(
                conn,
                str(tenants[1]),
                "PRICE",
                {
                    "standard_new_exam_egp": "1e2",
                    "preferential_followup_egp": None,
                    "urgent_base_egp": None,
                    "urgent_enabled": False,
                },
                1,
            )
        assert bad.value.orig.sqlstate == "23514"
    finally:
        runtime.dispose()
        migration.dispose()


def test_fresh_install_and_repeated_upgrade_are_no_ops(admin, empty_database):
    _, head = _revisions()
    _create_empty_database(admin, empty_database)
    migration = _engine("carepulse_migration", empty_database)
    try:
        _at_revision(migration, head)
        with migration.connect() as conn:
            first = conn.scalar(
                text("""
                SELECT oid::text, prosrc, provolatile, proconfig
                FROM pg_proc WHERE oid = 'public.valid_clinic_policy(text,jsonb)'::regprocedure
            """)
            )
        _at_revision(migration, head)
        with migration.connect() as conn:
            assert (
                conn.scalar(text("SELECT version_num FROM public.alembic_version"))
                == head
            )
            assert (
                conn.scalar(
                    text("""
                SELECT oid::text, prosrc, provolatile, proconfig
                FROM pg_proc WHERE oid = 'public.valid_clinic_policy(text,jsonb)'::regprocedure
            """)
                )
                == first
            )
            assert (
                conn.scalar(
                    text("""
                SELECT public.valid_clinic_policy('PRICE', CAST(:price AS jsonb))
            """),
                    {
                        "price": json.dumps(
                            {
                                "standard_new_exam_egp": "100.00",
                                "preferential_followup_egp": None,
                                "urgent_base_egp": None,
                                "urgent_enabled": False,
                            }
                        )
                    },
                )
                is True
            )
    finally:
        migration.dispose()


def test_failed_following_revision_keeps_head_and_ddl_atomic(
    admin, empty_database, tmp_path
):
    _, head = _revisions()
    _create_empty_database(admin, empty_database)
    migration = _engine("carepulse_migration", empty_database)
    scripts = tmp_path / "migrations"
    shutil.copytree(ROOT / "db/migrations", scripts)
    (scripts / "versions/9999_pricing_failure.py").write_text(
        'from alembic import op\nrevision = "9999_pricing_failure"\n'
        f'down_revision = "{head}"\n'
        "def upgrade():\n"
        '    op.execute("CREATE TABLE public.synthetic_pricing_partial (id integer)")\n'
        '    op.execute("SELECT 1 / 0")\n'
    )
    try:
        _at_revision(migration, head)
        with pytest.raises(DBAPIError) as error:
            _at_revision(migration, "9999_pricing_failure", scripts)
        assert error.value.orig.sqlstate == "22012"
        with migration.connect() as conn:
            assert (
                conn.scalar(text("SELECT version_num FROM public.alembic_version"))
                == head
            )
            assert (
                conn.scalar(
                    text("SELECT to_regclass('public.synthetic_pricing_partial')")
                )
                is None
            )
    finally:
        migration.dispose()


@pytest.mark.parametrize(
    ("service", "classification", "base", "standard", "kind", "discount", "reason"),
    [
        ("NEW_EXAMINATION", "ROUTINE", "100.00", "100.00", "NONE", "0", None),
        (
            "NEW_EXAMINATION",
            "ROUTINE",
            "100.00",
            "100.00",
            "FIXED",
            "10.00",
            "Approved",
        ),
        (
            "NEW_EXAMINATION",
            "ROUTINE",
            "100.00",
            "100.00",
            "PERCENT",
            "12.345",
            "Approved",
        ),
        ("NEW_EXAMINATION", "ROUTINE", "10.05", "10.05", "PERCENT", "50", "Approved"),
        (
            "NEW_EXAMINATION",
            "ROUTINE",
            "1.00",
            "1.00",
            "PERCENT",
            "0.500000000000000000000000000001",
            "Approved",
        ),
        ("NEW_EXAMINATION", "URGENT", "120.00", "100.00", "FIXED", "19.99", "Approved"),
        ("NEW_EXAMINATION", "ROUTINE", "0.00", "0.00", "NONE", "0", None),
        ("NEW_EXAMINATION", "ROUTINE", "0.00", "0.00", "PERCENT", "10", "Approved"),
        ("FOLLOW_UP", "ROUTINE", "30.00", "100.00", "PERCENT", "100", "Approved"),
        (
            "NEW_EXAMINATION",
            "ROUTINE",
            "1234567890123456789012345678901.00",
            "1234567890123456789012345678901.00",
            "PERCENT",
            "0.000000000000000000000000000000000000000000000001",
            "Approved",
        ),
    ],
)
def test_postgresql_final_check_accepts_python_confirmed_corpus(
    appointment_storage, service, classification, base, standard, kind, discount, reason
):
    runtime, _, (tenant, _), (doctor, _) = appointment_storage
    quote = quote_price(base, standard, service, classification, kind, discount)
    validate_discount_reason(kind, reason)
    frozen = {
        "service_type": service,
        "classification": classification,
        "base_egp": base,
        "discount_type": kind,
        "discount_value": discount,
        "discount_reason": reason,
        "is_free": quote.is_free,
        "net_egp": format(quote.net_egp, ".2f"),
        "pricing_version": 1,
        "creation_standard_base_egp": standard,
        "entitlement_provenance": None,
    }
    # If PostgreSQL's unchanged D-05 CHECK diverges, surface this exact corpus value.
    with transaction(runtime, tenant) as conn:
        if service == "FOLLOW_UP":
            saved = _v2_priced_followup(conn, tenant, doctor, frozen)
            frozen["entitlement_provenance"] = saved["entitlement_provenance"]
        else:
            saved = persist_appointment(
                conn,
                _draft(
                    _patient(conn, tenant),
                    doctor,
                    service_type=service,
                    classification=classification,
                    price_snapshot=frozen,
                ),
                _query(tenant, doctor),
            )
        assert validate_price_snapshot(frozen)["net_egp"] == quote.net_egp
        assert saved["net_egp"] == format(quote.net_egp, ".2f"), frozen


def test_t145_upgrade_preserves_historical_numeric_percentage_and_receipt(
    admin, empty_database
):
    _create_empty_database(admin, empty_database)
    migration = _engine("carepulse_migration", empty_database)
    runtime = _engine("carepulse_runtime", empty_database)
    try:
        _at_revision(migration, T145_BEFORE)
        operator, tenant = uuid4(), uuid4()
        appointment, assignment, patient, receipt = uuid4(), uuid4(), uuid4(), uuid4()
        with migration.begin() as conn:
            conn.execute(
                text("""
                    INSERT INTO public.operator_identities
                        (id, credential_digest, issued_by_reference, issued_at)
                    VALUES (:operator, :digest, 'synthetic-t145', clock_timestamp())
                """),
                {"operator": operator, "digest": f"synthetic:{operator}"},
            )
            conn.execute(
                text("""
                    INSERT INTO public.organizations (id, branch, created_by_operator_id)
                    VALUES (:tenant, 'Synthetic T145 legacy clinic', :operator)
                """),
                {"tenant": tenant, "operator": operator},
            )
            doctor = _activate_clinic(conn, tenant, operator)
            conn.execute(
                text("""
                    INSERT INTO public.patients
                        (id, organization_id, name_parts, date_of_birth)
                    VALUES (:patient, :tenant, ARRAY['Legacy','Percent','Patient'],
                            DATE '2001-01-01')
                """),
                {"patient": patient, "tenant": tenant},
            )
            conn.execute(
                text("""
                    INSERT INTO public.appointment_day_counters
                        (organization_id, service_date, last_number)
                    VALUES (:tenant, DATE '2026-11-20', 1)
                """),
                {"tenant": tenant},
            )
            conn.execute(
                text("""
                    INSERT INTO public.appointments
                        (id, organization_id, patient_id, doctor_staff_id, service_date,
                         status, service_type, classification, lane, booking_mode,
                         current_assignment_id, current_number)
                    VALUES (:appointment, :tenant, :patient, :doctor, DATE '2026-11-20',
                            'Booked', 'NEW_EXAMINATION', 'ROUTINE', 'MAIN', 'OPEN',
                            :assignment, 1)
                """),
                {
                    "appointment": appointment,
                    "tenant": tenant,
                    "patient": patient,
                    "doctor": doctor,
                    "assignment": assignment,
                },
            )
            conn.execute(
                text("""
                    INSERT INTO public.appointment_number_assignments
                        (id, organization_id, appointment_id, service_date, number)
                    VALUES (:assignment, :tenant, :appointment, DATE '2026-11-20', 1)
                """),
                {
                    "assignment": assignment,
                    "tenant": tenant,
                    "appointment": appointment,
                },
            )
            conn.execute(
                text("""
                    INSERT INTO public.appointment_price_snapshots
                        (organization_id, appointment_id, service_type, classification,
                         base_egp, discount_type, discount_value, discount_reason,
                         is_free, net_egp, pricing_version, creation_standard_base_egp)
                    VALUES (:tenant, :appointment, 'NEW_EXAMINATION', 'ROUTINE',
                            100.00, 'PERCENT', 12.345, 'Legacy approved discount',
                            false, 87.66, 1, 100.00)
                """),
                {"tenant": tenant, "appointment": appointment},
            )
            conn.execute(
                text("""
                    INSERT INTO public.appointment_history
                        (id, organization_id, appointment_id, appointment_revision,
                         action, new_status, new_service_date, new_number,
                         actor_staff_id, actor_role, price_snapshot_appointment_id)
                    VALUES (:id, :tenant, :appointment, 0, 'CREATED', 'Booked',
                            DATE '2026-11-20', 1, :doctor, 'DOCTOR_OWNER', :appointment)
                """),
                {
                    "id": uuid4(),
                    "tenant": tenant,
                    "appointment": appointment,
                    "doctor": doctor,
                },
            )
            conn.execute(
                text("""
                    INSERT INTO public.idempotency_receipts
                        (id, scope_kind, scope_id, actor_kind, actor_id,
                         organization_id, staff_id, operation, idempotency_key,
                         request_digest, result_projection, result_revision)
                    VALUES (:id, 'TENANT', :tenant, 'STAFF', :doctor,
                            :tenant, :doctor, 'appointments.book', 'legacy-t145',
                            :digest, CAST(:projection AS jsonb), 0)
                """),
                {
                    "id": receipt,
                    "tenant": tenant,
                    "doctor": doctor,
                    "digest": "0" * 64,
                    "projection": json.dumps(
                        {"discount_value": "12.345", "net_egp": "87.66"}
                    ),
                },
            )
        _at_revision(migration, T145_AFTER)
        _at_revision(migration, T145_AFTER)
        with migration.connect() as conn:
            assert conn.execute(
                text("""
                    SELECT discount_value, discount_percent_text, net_egp
                    FROM public.appointment_price_snapshots
                    WHERE appointment_id=:appointment
                """),
                {"appointment": appointment},
            ).one() == (Decimal("12.345"), None, Decimal("87.66"))
            assert conn.scalar(
                text("""
                    SELECT result_projection FROM public.idempotency_receipts WHERE id=:id
                """),
                {"id": receipt},
            ) == {"discount_value": "12.345", "net_egp": "87.66"}
            assert (
                conn.scalar(text("SELECT version_num FROM public.alembic_version"))
                == T145_AFTER
            )
        with (
            pytest.raises(DBAPIError) as direct_runtime,
            transaction(runtime, tenant) as conn,
        ):
            conn.execute(
                text("""
                    UPDATE public.appointment_price_snapshots SET discount_value=12.34
                    WHERE appointment_id=:appointment
                """),
                {"appointment": appointment},
            )
        assert direct_runtime.value.orig.sqlstate == "42501"
        with pytest.raises(DBAPIError) as immutable, migration.begin() as conn:
            conn.execute(
                text("""
                    UPDATE public.appointment_price_snapshots SET discount_value=12.34
                    WHERE appointment_id=:appointment
                """),
                {"appointment": appointment},
            )
        assert immutable.value.orig.sqlstate == "23514"
    finally:
        runtime.dispose()
        migration.dispose()


def test_t173_populated_0039_money_successor_and_atomic_failed_upgrade(
    admin, empty_database, tmp_path
):
    assert (
        runpy.run_path(str(ROOT / "db/migrations/versions/0040_t173_numeric_money.py"))[
            "down_revision"
        ]
        == T173_BEFORE
    )
    _create_empty_database(admin, empty_database)
    migration = _engine("carepulse_migration", empty_database)
    runtime = _engine("carepulse_runtime", empty_database)
    functions = (
        "public.t145_decimal_compare_rational(text,numeric,numeric)",
        "public.t145_percent_net_matches(numeric,numeric,text)",
    )
    definition = text("""
        SELECT pg_get_functiondef(p.oid), p.oid, p.proowner, p.prosecdef,
               p.proconfig, p.proacl::text,
               has_function_privilege('carepulse_runtime',p.oid,'EXECUTE'),
               has_function_privilege('carepulse_operator',p.oid,'EXECUTE')
        FROM pg_proc p WHERE p.oid=CAST(:signature AS regprocedure)
    """)
    try:
        _at_revision(migration, T173_BEFORE)
        operator, tenant = uuid4(), uuid4()
        with migration.begin() as connection:
            connection.execute(
                text("""
                    INSERT INTO public.operator_identities
                        (id, credential_digest, issued_by_reference, issued_at)
                    VALUES (:operator, :digest, 'synthetic-t173', clock_timestamp())
                """),
                {"operator": operator, "digest": f"synthetic:{operator}"},
            )
            connection.execute(
                text("""
                    INSERT INTO public.organizations (id, branch, created_by_operator_id)
                    VALUES (:tenant, 'Synthetic T173 clinic', :operator)
                """),
                {"tenant": tenant, "operator": operator},
            )
            doctor = _activate_clinic(connection, tenant, operator)
        with transaction(runtime, tenant) as connection:
            saved = persist_appointment(
                connection,
                _draft(
                    _patient(connection, tenant),
                    doctor,
                    price_snapshot={
                        "discount_type": "PERCENT",
                        "discount_value": "12.345",
                        "discount_reason": "Historical approved percentage",
                        "net_egp": "87.66",
                    },
                ),
                _query(tenant, doctor),
            )
            assert saved["net_egp"] == "87.66"
            assert (
                append_version(
                    connection,
                    str(tenant),
                    "PRICE",
                    {
                        "standard_new_exam_egp": "120.00",
                        "preferential_followup_egp": "50.00",
                        "urgent_base_egp": None,
                        "urgent_enabled": False,
                    },
                    1,
                )
                == 2
            )
        with migration.connect() as connection:
            before = _database_fingerprint(connection)
            before_history = connection.execute(
                text("""
                    SELECT id, appointment_id, appointment_revision, action,
                           row_to_json(h)::text
                    FROM public.appointment_history h
                    WHERE organization_id=:tenant ORDER BY id
                """),
                {"tenant": tenant},
            ).all()
            before_functions = {
                signature: connection.execute(
                    definition, {"signature": signature}
                ).one()
                for signature in functions
            }
            assert len(before["snapshots"]) == len(before["receipts"]) == 1
            assert before_history
            assert all(
                row[1] and row[2]
                for row in before["row_security"]
                if row[0]
                in (
                    "appointment_price_snapshots",
                    "idempotency_receipts",
                    "clinic_policy_versions",
                )
            )

        scripts = tmp_path / "migrations"
        shutil.copytree(ROOT / "db/migrations", scripts)
        successor = scripts / "versions/0040_t173_numeric_money.py"
        source_text = successor.read_text(encoding="utf-8")
        assert source_text.count("def downgrade()") == 1
        successor.write_text(
            source_text.replace(
                "def downgrade()",
                '    op.execute("SELECT 1 / 0")\n\n\ndef downgrade()',
            ),
            encoding="utf-8",
        )
        with pytest.raises(DBAPIError) as failed:
            _at_revision(migration, T173_AFTER, scripts)
        assert failed.value.orig.sqlstate == "22012"
        with migration.connect() as connection:
            assert (
                connection.scalar(
                    text("SELECT version_num FROM public.alembic_version")
                )
                == T173_BEFORE
            )
            assert _database_fingerprint(connection) == before
            assert {
                signature: connection.execute(
                    definition, {"signature": signature}
                ).one()
                for signature in functions
            } == before_functions

        # 131071 integer digits fit NUMERIC; cent-scaling at 0039 overflows.
        huge = "1" + "0" * 131070 + ".00"
        huge_check = text("""
            SELECT public.t145_percent_net_matches(
                CAST(:amount AS numeric), CAST(:amount AS numeric), '0.0')
        """)
        with pytest.raises(DBAPIError) as legacy, migration.connect() as connection:
            connection.scalar(huge_check, {"amount": huge})
        assert legacy.value.orig.sqlstate == "22003"

        _at_revision(migration, T173_AFTER)
        _at_revision(migration, T173_AFTER)
        with migration.connect() as connection:
            assert (
                connection.scalar(
                    text("SELECT version_num FROM public.alembic_version")
                )
                == T173_AFTER
            )
            assert _database_fingerprint(connection) == before
            assert (
                connection.execute(
                    text("""
                    SELECT id, appointment_id, appointment_revision, action,
                           row_to_json(h)::text
                    FROM public.appointment_history h
                    WHERE organization_id=:tenant ORDER BY id
                """),
                    {"tenant": tenant},
                ).all()
                == before_history
            )
            for signature in functions:
                upgraded = connection.execute(
                    definition, {"signature": signature}
                ).one()
                assert upgraded[1:] == before_functions[signature][1:]
                assert upgraded[0] != before_functions[signature][0]
            assert connection.scalar(huge_check, {"amount": huge}) is True
            assert (
                connection.scalar(
                    text("""
                    SELECT public.t145_percent_net_matches(100.00, 87.66, '12.345')
                """)
                )
                is True
            )
    finally:
        runtime.dispose()
        migration.dispose()

"""B-06 predecessor upgrade, legacy price data and PostgreSQL arithmetic contract."""

import json
import runpy
import shutil
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

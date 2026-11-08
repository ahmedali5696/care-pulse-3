"""B-10 successor upgrade and real PostgreSQL writer boundaries."""

import shutil
from uuid import uuid4

import pytest
from carepulse.db import transaction
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from test_database import admin as admin  # noqa: PLC0414
from test_database import disposable_project as disposable_project  # noqa: PLC0414
from test_database import empty_database as empty_database  # noqa: PLC0414
from test_followup_persistence import _v2_booking, _v2_command, _v2_source
from test_visit_migration import ROOT, _prepare, _upgrade

BEFORE = "0028_d08_followup_provenance"
AFTER = "0029_b10_followup_writers"
PROOF_AFTER = "0030_b10_source_proof"


def test_operational_proof_successor_preserves_claim_and_limits_runtime_access(
    admin, empty_database
):
    migration, runtime, tenant, doctor = _prepare(admin, empty_database)
    try:
        _upgrade(migration, AFTER)
        with transaction(runtime, tenant) as connection:
            source = _v2_source(connection, tenant, doctor)
            claim = _v2_command(
                connection, "create_followup_claim", source, **_v2_booking(source)
            )
        with migration.connect() as connection:
            old_claim = connection.scalar(
                text("SELECT to_jsonb(c) FROM public.followup_claims c WHERE id=:id"),
                {"id": claim},
            )
        _upgrade(migration, PROOF_AFTER)
        _upgrade(migration, PROOF_AFTER)
        with migration.connect() as connection:
            assert (
                connection.scalar(
                    text(
                        "SELECT to_jsonb(c) FROM public.followup_claims c WHERE id=:id"
                    ),
                    {"id": claim},
                )
                == old_claim
            )
            assert (
                connection.scalar(
                    text("""
                    SELECT has_function_privilege('carepulse_runtime',
                        'public.d08_legacy_source(uuid,uuid)', 'EXECUTE')
                """)
                )
                is False
            )
        with transaction(runtime, tenant) as connection:
            connection.execute(
                text("SELECT set_config('app.staff_id', :staff, true)"),
                {"staff": str(doctor)},
            )
            with pytest.raises(DBAPIError) as denied, connection.begin_nested():
                connection.scalar(
                    text("SELECT public.b10_operational_source_proof(:org,:visit)"),
                    {"org": uuid4(), "visit": source["visit"]},
                )
            assert denied.value.orig.sqlstate == "42501"
            assert connection.scalar(
                text("SELECT public.b10_operational_source_proof(:org,:visit)"),
                {"org": tenant, "visit": source["visit"]},
            ) == {
                "state": "VERIFIED",
                "decision": None,
                "actor_staff_id": None,
                "actor_role": None,
            }
    finally:
        runtime.dispose()
        migration.dispose()


def test_operational_proof_failure_rolls_back_to_immediate_predecessor(
    admin, empty_database, tmp_path
):
    migration, runtime, tenant, doctor = _prepare(admin, empty_database)
    try:
        _upgrade(migration, AFTER)
        with transaction(runtime, tenant) as connection:
            source = _v2_source(connection, tenant, doctor)
            claim = _v2_command(
                connection, "create_followup_claim", source, **_v2_booking(source)
            )
        with migration.connect() as connection:
            previous = connection.scalar(
                text("SELECT to_jsonb(c) FROM public.followup_claims c WHERE id=:id"),
                {"id": claim},
            )
        scripts = tmp_path / "migrations"
        shutil.copytree(ROOT / "db/migrations", scripts)
        successor = scripts / "versions/0030_b10_operational_source_proof.py"
        original = successor.read_text(encoding="utf-8")
        marker = '    """)\n\n\ndef downgrade()'
        assert original.count(marker) == 1
        successor.write_text(
            original.replace(
                marker, '    """)\n    op.execute("SELECT 1 / 0")\n\n\ndef downgrade()'
            ),
            encoding="utf-8",
        )
        with pytest.raises(DBAPIError) as failure:
            _upgrade(migration, PROOF_AFTER, scripts)
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
                    text(
                        "SELECT to_regprocedure('public.b10_operational_source_proof(uuid,uuid)')"
                    )
                )
                is None
            )
            assert (
                connection.scalar(
                    text(
                        "SELECT to_jsonb(c) FROM public.followup_claims c WHERE id=:id"
                    ),
                    {"id": claim},
                )
                == previous
            )
        _upgrade(migration, PROOF_AFTER)
    finally:
        runtime.dispose()
        migration.dispose()


def test_populated_successor_retains_claim_and_fences_legacy_sql(admin, empty_database):
    migration, runtime, tenant, doctor = _prepare(admin, empty_database)
    try:
        _upgrade(migration, BEFORE)
        with transaction(runtime, tenant) as connection:
            source = _v2_source(connection, tenant, doctor)
            claim = _v2_command(
                connection, "create_followup_claim", source, **_v2_booking(source)
            )
        with migration.connect() as connection:
            saved = connection.scalar(
                text(
                    "SELECT to_jsonb(c) FROM public.followup_claims c WHERE id=:claim"
                ),
                {"claim": claim},
            )
        _upgrade(migration, AFTER)
        _upgrade(migration, AFTER)
        with migration.connect() as connection:
            assert (
                connection.scalar(
                    text(
                        "SELECT to_jsonb(c) FROM public.followup_claims c WHERE id=:claim"
                    ),
                    {"claim": claim},
                )
                == saved
            )
            for name, allowed in (
                ("create_followup_claim", False),
                ("extend_followup_claim", False),
                ("create_followup_claim_ordered", True),
                ("extend_followup_claim_ordered", True),
            ):
                assert (
                    connection.scalar(
                        text("""
                        SELECT has_function_privilege('carepulse_runtime',
                            CAST(:signature AS regprocedure), 'EXECUTE')
                    """),
                        {"signature": f"public.{name}(uuid,uuid,jsonb,jsonb)"},
                    )
                    is allowed
                )
            assert (
                connection.scalar(
                    text("""
                    SELECT has_column_privilege('carepulse_runtime',
                        'public.followup_claims', 'state', 'UPDATE')
                """)
                )
                is False
            )
        with transaction(runtime, tenant) as connection:
            for name in ("create_followup_claim", "extend_followup_claim"):
                with pytest.raises(DBAPIError) as denied, connection.begin_nested():
                    _v2_command(
                        connection,
                        name,
                        source,
                        legacy_entrypoint=True,
                        **_v2_booking(source),
                    )
                assert denied.value.orig.sqlstate == "42501"
            with pytest.raises(DBAPIError) as denied, connection.begin_nested():
                connection.execute(
                    text("""
                        UPDATE public.appointments
                        SET service_date = service_date + 1
                        WHERE id=(SELECT appointment_id FROM public.followup_claims
                                  WHERE id=:claim)
                    """),
                    {"claim": claim},
                )
            assert denied.value.orig.sqlstate == "42501"
            with pytest.raises(DBAPIError) as denied, connection.begin_nested():
                connection.execute(
                    text(
                        "UPDATE public.followup_claims SET state='RELEASED' WHERE id=:claim"
                    ),
                    {"claim": claim},
                )
            assert denied.value.orig.sqlstate == "42501"
            with pytest.raises(DBAPIError) as denied, connection.begin_nested():
                connection.execute(
                    text(
                        "INSERT INTO public.followup_authorization_events DEFAULT VALUES"
                    )
                )
            assert denied.value.orig.sqlstate == "42501"
    finally:
        runtime.dispose()
        migration.dispose()


def test_ordered_writer_requires_declaration_and_consumes_one_reserved_number(
    admin, empty_database
):
    migration, runtime, tenant, doctor = _prepare(admin, empty_database)
    try:
        _upgrade(migration, AFTER)
        with transaction(runtime, tenant) as connection:
            source = _v2_source(connection, tenant, doctor)
            booking = _v2_booking(source, offset=10)
            day = booking["service_date"]
            allocator = text("""
                INSERT INTO public.appointment_day_counters
                    (organization_id, service_date, last_number)
                VALUES (:tenant, CAST(:day AS date), 1)
                ON CONFLICT (organization_id, service_date)
                DO UPDATE SET last_number = public.appointment_day_counters.last_number + 1
                RETURNING last_number
            """)
            with pytest.raises(DBAPIError) as denied, connection.begin_nested():
                number = connection.scalar(allocator, {"tenant": tenant, "day": day})
                _v2_command(
                    connection,
                    "create_followup_claim_ordered",
                    source,
                    **booking,
                    allocated_number=number,
                )
            assert denied.value.orig.sqlstate == "23514"
            assert (
                connection.scalar(
                    text("""
                    SELECT count(*) FROM public.appointment_day_counters
                    WHERE organization_id=:tenant AND service_date=CAST(:day AS date)
                """),
                    {"tenant": tenant, "day": day},
                )
                == 0
            )
            number = connection.scalar(allocator, {"tenant": tenant, "day": day})
            claim = _v2_command(
                connection,
                "create_followup_claim_ordered",
                source,
                **booking,
                allocated_number=number,
                new_booking_date_exception={
                    "selected_service_date": day,
                    "reason": "Synthetic reviewed exception",
                },
            )
            snapshot, event_id, reason = connection.execute(
                text("""
                    SELECT c.policy_snapshot, e.id, e.reason
                    FROM public.followup_claims c
                    JOIN public.followup_authorization_events e
                        ON e.organization_id=c.organization_id AND e.claim_id=c.id
                    WHERE c.organization_id=:tenant AND c.id=:claim
                """),
                {"tenant": tenant, "claim": claim},
            ).one()
            assert snapshot["new_date_event_id"] == str(event_id)
            assert snapshot["new_booking_date_exception"] == {
                "selected_service_date": day,
                "reason": "Synthetic reviewed exception",
            }
            assert reason == "Synthetic reviewed exception"
            # Issue #26 / G3: raw runtime SQL succeeds without signed HMAC/nonce
            # or an operation-scoped receipt; keep this evidence until fenced.
            assert (
                connection.scalar(
                    text("""
                    SELECT count(*) FROM public.idempotency_receipts
                    WHERE organization_id=:tenant AND operation='appointments.book'
                """),
                    {"tenant": tenant},
                )
                == 0
            )
            assert (
                connection.scalar(
                    text("""
                    SELECT last_number FROM public.appointment_day_counters
                    WHERE organization_id=:tenant AND service_date=CAST(:day AS date)
                """),
                    {"tenant": tenant, "day": day},
                )
                == number
            )
    finally:
        runtime.dispose()
        migration.dispose()

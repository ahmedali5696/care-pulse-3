"""B-10 successor upgrade and real PostgreSQL writer boundaries."""

import json
import shutil
from datetime import UTC, date, datetime, time, timedelta, timezone
from uuid import uuid4
from zoneinfo import ZoneInfo

import pytest
from carepulse.db import transaction
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from test_database import admin as admin  # noqa: PLC0414
from test_database import disposable_project as disposable_project  # noqa: PLC0414
from test_database import empty_database as empty_database  # noqa: PLC0414
from test_followup_persistence import _v2_booking, _v2_command, _v2_source
from test_queue_capacity import _policy_capacity
from test_visit_migration import ROOT, _prepare, _upgrade

BEFORE = "0028_d08_followup_provenance"
AFTER = "0029_b10_followup_writers"
PROOF_AFTER = "0030_b10_source_proof"
GUARD_AFTER = "0031_b10_writer_guard"
OFFSET_AFTER = "0032_b10_cairo_offset_guard"
EXACT_AFTER = "0035_t146_cairo_exact_schedule"
PARITY_AFTER = "0036_b10_confirmation_parity"
UNICODE_AFTER = "0037_t155_followup_reason"
DURATION_AFTER = "0038_t159_duration_numeric"
CAPACITY_AFTER = "0039_t160_capacity_numeric"


def _t177_rows(connection, tables, *, legacy_price=False):
    rows = {}
    for table in tables:
        projection = "to_jsonb(t)"
        if legacy_price and table == "appointment_price_snapshots":
            projection += " - 'discount_percent_text'"
        rows[table] = (
            connection.execute(
                text(f"SELECT ({projection})::text FROM public.{table} t ORDER BY 1")
            )
            .scalars()
            .all()
        )
    return rows


def _t177_catalog(connection):
    queries = {
        "functions": """
            SELECT p.oid, p.oid::regprocedure::text, pg_get_functiondef(p.oid),
                   p.proowner, p.proacl::text,
                   has_function_privilege('carepulse_runtime',p.oid,'EXECUTE'),
                   has_function_privilege('carepulse_operator',p.oid,'EXECUTE'),
                   EXISTS (SELECT 1 FROM aclexplode(coalesce(p.proacl,
                       acldefault('f',p.proowner))) a WHERE a.grantee=0)
            FROM pg_proc p WHERE p.pronamespace='public'::regnamespace
              AND p.prokind IN ('f','p') ORDER BY p.oid
        """,
        "tables": """
            SELECT c.relname, c.relowner, c.relacl::text,
                   c.relrowsecurity, c.relforcerowsecurity,
                   r.rolname, p.privilege, has_table_privilege(r.oid,c.oid,p.privilege)
            FROM pg_class c CROSS JOIN pg_roles r
            CROSS JOIN (VALUES ('SELECT'), ('INSERT'), ('UPDATE'), ('DELETE'),
                               ('TRUNCATE'), ('REFERENCES'), ('TRIGGER')) p(privilege)
            WHERE c.relnamespace='public'::regnamespace AND c.relkind='r'
              AND r.rolname IN ('carepulse_runtime','carepulse_operator')
            ORDER BY c.relname,r.rolname,p.privilege
        """,
        "columns": """
            SELECT c.relname, a.attname, r.rolname, p.privilege, a.attacl::text,
                   has_column_privilege(r.oid,c.oid,a.attnum,p.privilege)
            FROM pg_class c JOIN pg_attribute a ON a.attrelid=c.oid
            CROSS JOIN pg_roles r
            CROSS JOIN (VALUES ('SELECT'), ('INSERT'), ('UPDATE'), ('REFERENCES')) p(privilege)
            WHERE c.relnamespace='public'::regnamespace AND c.relkind='r'
              AND a.attnum>0 AND NOT a.attisdropped
              AND r.rolname IN ('carepulse_runtime','carepulse_operator')
            ORDER BY c.relname,a.attname,r.rolname,p.privilege
        """,
        "column_definitions": """
            SELECT c.relname,a.attname,a.attnotnull,format_type(a.atttypid,a.atttypmod),
                   pg_get_expr(d.adbin,d.adrelid)
            FROM pg_class c JOIN pg_attribute a ON a.attrelid=c.oid
            LEFT JOIN pg_attrdef d ON (d.adrelid,d.adnum)=(a.attrelid,a.attnum)
            WHERE c.relnamespace='public'::regnamespace AND c.relkind='r'
              AND a.attnum>0 AND NOT a.attisdropped ORDER BY c.relname,a.attname
        """,
        "constraints": """
            SELECT oid,conname,conrelid,convalidated,pg_get_constraintdef(oid)
            FROM pg_constraint WHERE connamespace='public'::regnamespace ORDER BY oid
        """,
        "triggers": """
            SELECT t.oid,t.tgenabled,pg_get_triggerdef(t.oid)
            FROM pg_trigger t JOIN pg_class c ON c.oid=t.tgrelid
            WHERE c.relnamespace='public'::regnamespace ORDER BY t.oid
        """,
        "policies": """
            SELECT tablename,policyname,permissive,roles,cmd,qual,with_check
            FROM pg_policies WHERE schemaname='public' ORDER BY tablename,policyname
        """,
    }
    return {name: connection.execute(text(sql)).all() for name, sql in queries.items()}


def _t177_upgrade_proof(migration, predecessor, successor, tmp_path):
    revision = text("SELECT version_num FROM public.alembic_version")
    tables = (
        "organizations",
        "staff_accounts",
        "patients",
        "appointments",
        "appointment_history",
        "appointment_price_snapshots",
        "appointment_number_assignments",
        "appointment_day_counters",
        "clinic_policy_versions",
        "clinic_policy_heads",
        "clinic_schedule_intervals",
        "clinic_schedule_holidays",
        "followup_claims",
        "followup_extensions",
        "followup_source_state",
        "followup_source_decision_events",
        "followup_authorization_events",
        "completion_attestations",
        "visits",
        "clinical_revisions",
        "queue_entries",
        "queue_days",
        "sensitive_audit",
        "outbox_events",
        "idempotency_receipts",
    )
    with migration.connect() as connection:
        assert connection.scalar(revision) == predecessor
        catalog = _t177_catalog(connection)
        retained = _t177_rows(connection, tables)
        assert retained["appointments"] and retained["appointment_history"]
        assert (
            retained["appointment_price_snapshots"] and retained["idempotency_receipts"]
        )
        assert retained["clinic_policy_versions"]
        if successor != "0034_t145_exact_percentage":
            assert retained["followup_claims"] and retained["completion_attestations"]

    scripts = tmp_path / successor
    shutil.copytree(ROOT / "db/migrations", scripts)
    migration_file = scripts / "versions" / f"{successor}.py"
    original = migration_file.read_text(encoding="utf-8")
    migration_file.write_text(
        original
        + "\n\n_t177_original_upgrade = upgrade\n\ndef upgrade():\n"
        + '    _t177_original_upgrade()\n    op.execute("SELECT 1 / 0")\n',
        encoding="utf-8",
    )
    with pytest.raises(DBAPIError) as failed:
        _upgrade(migration, successor, scripts)
    assert failed.value.orig.sqlstate == "22012"
    with migration.connect() as connection:
        assert connection.scalar(revision) == predecessor
        assert _t177_catalog(connection) == catalog
        assert _t177_rows(connection, tables) == retained

    _upgrade(migration, successor)
    with migration.connect() as connection:
        assert connection.scalar(revision) == successor
        upgraded = _t177_catalog(connection)
        assert upgraded != catalog
        assert upgraded["tables"] == catalog["tables"]
        assert upgraded["policies"] == catalog["policies"]
        previous_functions = {row[0]: row for row in catalog["functions"]}
        functions = {row[0]: row for row in upgraded["functions"]}
        assert previous_functions.keys() <= functions.keys()
        for oid, previous in previous_functions.items():
            current = functions[oid]
            assert current[3] == previous[3]
            if current[1] != previous[1]:
                assert current[5:] == (False, False, False)
            else:
                assert current[4:] == previous[4:]
        runtime_entrypoints = {
            "create_followup_claim_ordered",
            "complete_examined_visit",
            "t145_percent_valid",
            "t145_decimal_compare_rational",
            "t145_percent_net_matches",
        }
        for oid in functions.keys() - previous_functions.keys():
            current = functions[oid]
            name = current[1].split("(")[0].removeprefix("public.")
            assert current[5:] == (name in runtime_entrypoints, False, False)
        legacy_price = successor == "0034_t145_exact_percentage"
        columns = upgraded["columns"]
        if legacy_price:
            assert (
                connection.scalar(
                    text(
                        "SELECT count(*) FROM public.appointment_price_snapshots WHERE discount_percent_text IS NOT NULL"
                    )
                )
                == 0
            )
            columns = [
                row
                for row in columns
                if row[:2] != ("appointment_price_snapshots", "discount_percent_text")
            ]
        assert columns == catalog["columns"]
        assert _t177_rows(connection, tables, legacy_price=legacy_price) == retained
        after_rows = _t177_rows(connection, tables)
    _upgrade(migration, successor)
    with migration.connect() as connection:
        assert connection.scalar(revision) == successor
        assert _t177_catalog(connection) == upgraded
        assert _t177_rows(connection, tables) == after_rows


def _t177_receipt(migration, tenant, doctor, claim):
    with migration.begin() as connection:
        connection.execute(
            text("""
                INSERT INTO public.idempotency_receipts
                    (id,scope_kind,scope_id,actor_kind,actor_id,organization_id,
                     staff_id,operation,idempotency_key,request_digest,result_projection,
                     result_revision)
                VALUES (:id,'TENANT',:tenant,'STAFF',:doctor,:tenant,:doctor,
                        'appointments.book',:key,:digest,CAST(:projection AS jsonb),0)
            """),
            {
                "id": uuid4(),
                "tenant": tenant,
                "doctor": doctor,
                "key": str(uuid4()),
                "digest": "0" * 64,
                "projection": json.dumps({"claim_id": str(claim)}),
            },
        )


def test_t177_populated_writer_and_offset_successors_are_atomic(
    admin, empty_database, tmp_path
):
    migration, runtime, tenant, doctor = _prepare(admin, empty_database)
    try:
        _upgrade(migration, PROOF_AFTER)
        with transaction(runtime, tenant) as connection:
            source = _v2_source(connection, tenant, doctor)
            claim = _v2_command(
                connection, "create_followup_claim", source, **_v2_booking(source)
            )
            assert claim is not None
        _t177_receipt(migration, tenant, doctor, claim)
        _t177_upgrade_proof(migration, PROOF_AFTER, GUARD_AFTER, tmp_path)
        day = source["day"] + timedelta(days=1)
        _publish_bookable_policies(migration, tenant, timed_day=day)
        with transaction(runtime, tenant) as connection:
            timed_source = _v2_source(connection, tenant, doctor)
            start = datetime.combine(day, time(9), ZoneInfo("Africa/Cairo"))
            timed_claim = _v2_command(
                connection,
                "create_followup_claim_ordered",
                timed_source,
                **_priced_booking(
                    timed_source,
                    service_date=day.isoformat(),
                    booking_mode="EXACT_TIME",
                    starts_at=start.isoformat(),
                    ends_at=(start + timedelta(hours=1)).isoformat(),
                ),
            )
            assert timed_claim is not None
        _t177_receipt(migration, tenant, doctor, timed_claim)
        _t177_upgrade_proof(migration, GUARD_AFTER, OFFSET_AFTER, tmp_path)
    finally:
        runtime.dispose()
        migration.dispose()


def _publish_bookable_policies(
    migration,
    tenant,
    timed_day=None,
    *,
    work_period=("09:00", "12:00"),
    breaks=(),
    manual_slots=(),
):
    with migration.begin() as connection:
        price = {
            "standard_new_exam_egp": "100.00",
            "preferential_followup_egp": "100.00",
            "urgent_base_egp": None,
            "urgent_enabled": False,
        }
        schedule = connection.scalar(
            text("""
                SELECT payload FROM public.clinic_policy_versions
                WHERE organization_id=:tenant AND kind='SCHEDULE' AND version=1
            """),
            {"tenant": tenant},
        )
        schedule["booking_mode"] = "EXACT_TIME" if timed_day else "OPEN"
        schedule["duration_minutes"] = 60 if timed_day else None
        schedule["holiday_booking_enabled"] = timed_day is None
        for kind, payload in (("PRICE", price), ("SCHEDULE", schedule)):
            connection.execute(
                text("""
                    INSERT INTO public.clinic_policy_versions
                        (organization_id,kind,version,payload)
                    VALUES (:tenant,:kind,2,CAST(:payload AS jsonb))
                """),
                {"tenant": tenant, "kind": kind, "payload": json.dumps(payload)},
            )
            if kind == "SCHEDULE" and timed_day:
                intervals = [
                    ("OVERRIDE_WORK", work_period),
                    *(("BREAK", interval) for interval in breaks),
                    *(("MANUAL_SLOT", interval) for interval in manual_slots),
                ]
                for interval_kind, (starts, ends) in intervals:
                    connection.execute(
                        text("""
                            INSERT INTO public.clinic_schedule_intervals
                                (organization_id,policy_version,interval_kind,
                                 weekday,service_date,starts_at,ends_at)
                            VALUES (:tenant,2,:kind,:weekday,CAST(:day AS date),
                                    CAST(:starts AS time),CAST(:ends AS time))
                        """),
                        {
                            "tenant": tenant,
                            "kind": interval_kind,
                            "weekday": timed_day.isoweekday()
                            if interval_kind == "BREAK"
                            else None,
                            "day": timed_day if interval_kind != "BREAK" else None,
                            "starts": starts,
                            "ends": ends,
                        },
                    )
            connection.execute(
                text("""
                    UPDATE public.clinic_policy_heads
                    SET current_version=2, revision=revision+1
                    WHERE organization_id=:tenant AND kind=:kind
                """),
                {"tenant": tenant, "kind": kind},
            )


def _priced_booking(source, **changes):
    booking = _v2_booking(source)
    booking["price_snapshot"] = {
        key: booking["price_snapshot"][key]
        for key in (
            "base_egp",
            "discount_type",
            "discount_value",
            "discount_reason",
            "is_free",
            "net_egp",
            "pricing_version",
            "creation_standard_base_egp",
        )
    }
    booking["price_snapshot"]["pricing_version"] = 2
    return {**booking, **changes}


def _assert_no_guarded_booking_effects(connection, tenant, day):
    assert (
        connection.scalar(
            text("""
            SELECT count(*) FROM public.appointments
            WHERE organization_id=:tenant AND service_type='FOLLOW_UP'
        """),
            {"tenant": tenant},
        )
        == 0
    )
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
    assert (
        connection.scalar(
            text(
                "SELECT count(*) FROM public.followup_claims WHERE organization_id=:tenant"
            ),
            {"tenant": tenant},
        )
        == 0
    )


def _writer_effect_counts(connection, tenant):
    tables = (
        "appointment_day_counters",
        "appointments",
        "appointment_number_assignments",
        "appointment_history",
        "appointment_price_snapshots",
        "followup_claims",
        "followup_authorization_events",
        "sensitive_audit",
    )
    return tuple(
        connection.scalar(
            text(f"SELECT count(*) FROM public.{table} WHERE organization_id=:tenant"),
            {"tenant": tenant},
        )
        for table in tables
    )


def test_t159_duration_successor_preserves_saved_policy_and_private_guard(
    admin, empty_database, tmp_path
):
    migration, runtime, tenant, doctor = _prepare(admin, empty_database)
    try:
        _upgrade(migration, UNICODE_AFTER)
        day = date(2030, 5, 6)
        _publish_bookable_policies(migration, tenant, timed_day=day)
        with transaction(runtime, tenant) as connection:
            historical = _v2_source(connection, tenant, doctor)
            start = datetime.combine(day, time(10), ZoneInfo("Africa/Cairo"))
            claim = _v2_command(
                connection,
                "create_followup_claim_ordered",
                historical,
                **_priced_booking(
                    historical,
                    service_date=day.isoformat(),
                    booking_mode="EXACT_TIME",
                    starts_at=start.isoformat(),
                    ends_at=(start + timedelta(hours=1)).isoformat(),
                    new_booking_date_exception={
                        "selected_service_date": day.isoformat(),
                        "reason": "Reviewed historical booking",
                    },
                ),
            )
            assert claim is not None
        _t177_receipt(migration, tenant, doctor, claim)
        with migration.begin() as connection:
            connection.execute(
                text("""
                    INSERT INTO public.clinic_policy_versions
                        (organization_id,kind,version,payload)
                    SELECT organization_id,kind,3,
                           jsonb_set(payload,'{duration_minutes}','30.0'::jsonb)
                    FROM public.clinic_policy_versions
                    WHERE organization_id=:tenant AND kind='SCHEDULE' AND version=2
                """),
                {"tenant": tenant},
            )
            connection.execute(
                text("""
                    INSERT INTO public.clinic_schedule_intervals
                        (organization_id,policy_version,interval_kind,weekday,
                         service_date,starts_at,ends_at)
                    SELECT organization_id,3,interval_kind,weekday,
                           service_date,starts_at,ends_at
                    FROM public.clinic_schedule_intervals
                    WHERE organization_id=:tenant AND policy_version=2
                """),
                {"tenant": tenant},
            )
            connection.execute(
                text("""
                    UPDATE public.clinic_policy_heads
                    SET current_version=3,revision=revision+1
                    WHERE organization_id=:tenant AND kind='SCHEDULE'
                """),
                {"tenant": tenant},
            )
        with transaction(runtime, tenant) as connection:
            source = _v2_source(connection, tenant, doctor)
        start = datetime.combine(day, time(9), ZoneInfo("Africa/Cairo"))
        booking = _priced_booking(
            source,
            service_date=day.isoformat(),
            booking_mode="EXACT_TIME",
            starts_at=start.isoformat(),
            ends_at=(start + timedelta(minutes=30)).isoformat(),
            new_booking_date_exception={
                "selected_service_date": day.isoformat(),
                "reason": "Doctor reviewed future booking date",
            },
        )
        with migration.connect() as connection:
            saved = connection.execute(
                text("""
                    SELECT version,payload FROM public.clinic_policy_versions
                    WHERE organization_id=:tenant AND kind='SCHEDULE' ORDER BY version
                """),
                {"tenant": tenant},
            ).all()
            signature = "public.b10_schedule_without_offset_guard(uuid,jsonb)"
            identity = connection.execute(
                text("""
                    SELECT oid,proowner,prosecdef,
                           has_function_privilege('carepulse_runtime',oid,'EXECUTE')
                    FROM pg_proc WHERE oid=CAST(:signature AS regprocedure)
                """),
                {"signature": signature},
            ).one()
            assert identity.prosecdef and identity[3] is False
        with transaction(runtime, tenant) as connection:
            with pytest.raises(DBAPIError) as denied, connection.begin_nested():
                _v2_command(
                    connection, "create_followup_claim_ordered", source, **booking
                )
            assert denied.value.orig.sqlstate == "22P02"

        _t177_upgrade_proof(migration, UNICODE_AFTER, DURATION_AFTER, tmp_path)
        with migration.connect() as connection:
            assert (
                connection.execute(
                    text("""
                    SELECT version,payload FROM public.clinic_policy_versions
                    WHERE organization_id=:tenant AND kind='SCHEDULE' ORDER BY version
                """),
                    {"tenant": tenant},
                ).all()
                == saved
            )
            assert (
                connection.execute(
                    text("""
                    SELECT oid,proowner,prosecdef,
                           has_function_privilege('carepulse_runtime',oid,'EXECUTE')
                    FROM pg_proc WHERE oid=CAST(:signature AS regprocedure)
                """),
                    {"signature": signature},
                ).one()
                == identity
            )
        with transaction(runtime, tenant) as connection:
            claim = _v2_command(
                connection, "create_followup_claim_ordered", source, **booking
            )
            assert claim is not None
    finally:
        runtime.dispose()
        migration.dispose()


def test_t160_populated_schedule_guard_and_failed_successor_are_atomic(
    admin, empty_database, tmp_path
):
    migration, runtime, tenant, doctor = _prepare(admin, empty_database)
    try:
        _upgrade(migration, DURATION_AFTER)
        day = date(2030, 5, 6)
        _publish_bookable_policies(migration, tenant, timed_day=day)
        with transaction(runtime, tenant) as connection:
            source = _v2_source(connection, tenant, doctor)
        with transaction(runtime, tenant) as connection:
            _policy_capacity(connection, tenant, capacity=2.0)
        with migration.begin() as connection:
            connection.execute(
                text("""
                    INSERT INTO public.clinic_policy_versions
                        (organization_id,kind,version,payload)
                    SELECT organization_id,kind,3,
                           jsonb_set(payload,'{allow_overlap}','true'::jsonb)
                    FROM public.clinic_policy_versions
                    WHERE organization_id=:tenant AND kind='SCHEDULE' AND version=2
                """),
                {"tenant": tenant},
            )
            connection.execute(
                text("""
                    INSERT INTO public.clinic_schedule_intervals
                        (organization_id,policy_version,interval_kind,weekday,
                         service_date,starts_at,ends_at)
                    SELECT organization_id,3,interval_kind,weekday,
                           service_date,starts_at,ends_at
                    FROM public.clinic_schedule_intervals
                    WHERE organization_id=:tenant AND policy_version=2
                """),
                {"tenant": tenant},
            )
            connection.execute(
                text("""
                    UPDATE public.clinic_policy_heads
                    SET current_version=3,revision=revision+1
                    WHERE organization_id=:tenant AND kind='SCHEDULE'
                """),
                {"tenant": tenant},
            )
        starts = datetime.combine(day, time(9), ZoneInfo("Africa/Cairo"))
        booking = _priced_booking(
            source,
            service_date=day.isoformat(),
            booking_mode="EXACT_TIME",
            starts_at=starts.isoformat(),
            ends_at=(starts + timedelta(minutes=60)).isoformat(),
            new_booking_date_exception={
                "selected_service_date": day.isoformat(),
                "reason": "Approved future schedule",
            },
        )
        signature = "public.b10_schedule_without_offset_guard(uuid,jsonb)"
        signatures = (
            "public.lock_doctor_admission_capacity(uuid,uuid,date,text)",
            "public.assert_confirmation_issued_scope(public.doctor_admission_confirmations)",
            "public.assert_admission_confirmation_final()",
            signature,
        )
        function = text("""
            SELECT pg_get_functiondef(p.oid), p.oid, p.proowner, p.prosecdef,
                   p.proconfig, p.proacl::text,
                   has_function_privilege('carepulse_runtime', p.oid, 'EXECUTE'),
                   has_function_privilege('carepulse_operator', p.oid, 'EXECUTE')
            FROM pg_proc p WHERE p.oid=CAST(:signature AS regprocedure)
        """)
        with migration.connect() as connection:
            before_functions = {
                name: connection.execute(function, {"signature": name}).one()
                for name in signatures
            }
            before_function = before_functions[signature]
            before_policies = connection.execute(
                text("""
                    SELECT kind,version,payload::text FROM public.clinic_policy_versions
                    WHERE organization_id=:tenant ORDER BY kind,version
                """),
                {"tenant": tenant},
            ).all()
            before_receipts = connection.execute(
                text("""
                    SELECT id,operation,result_projection::text FROM public.idempotency_receipts
                    WHERE organization_id=:tenant ORDER BY id
                """),
                {"tenant": tenant},
            ).all()
            assert before_receipts
        with transaction(runtime, tenant) as connection:
            with pytest.raises(DBAPIError) as old_guard, connection.begin_nested():
                _v2_command(
                    connection, "create_followup_claim_ordered", source, **booking
                )
            assert old_guard.value.orig.sqlstate == "22P02"
            before_effects = _writer_effect_counts(connection, tenant)

        scripts = tmp_path / "migrations"
        shutil.copytree(ROOT / "db/migrations", scripts)
        successor = scripts / "versions/0039_t160_capacity_numeric.py"
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
            _upgrade(migration, CAPACITY_AFTER, scripts)
        assert failed.value.orig.sqlstate == "22012"
        with migration.connect() as connection:
            assert (
                connection.scalar(
                    text("SELECT version_num FROM public.alembic_version")
                )
                == DURATION_AFTER
            )
            assert {
                name: connection.execute(function, {"signature": name}).one()
                for name in signatures
            } == before_functions
            assert (
                connection.execute(
                    text("""
                    SELECT kind,version,payload::text FROM public.clinic_policy_versions
                    WHERE organization_id=:tenant ORDER BY kind,version
                """),
                    {"tenant": tenant},
                ).all()
                == before_policies
            )
            assert (
                connection.execute(
                    text("""
                    SELECT id,operation,result_projection::text FROM public.idempotency_receipts
                    WHERE organization_id=:tenant ORDER BY id
                """),
                    {"tenant": tenant},
                ).all()
                == before_receipts
            )
        with transaction(runtime, tenant) as connection:
            assert _writer_effect_counts(connection, tenant) == before_effects

        _upgrade(migration, CAPACITY_AFTER)
        _upgrade(migration, CAPACITY_AFTER)
        with migration.connect() as connection:
            after_function = connection.execute(
                function, {"signature": signature}
            ).one()
            assert after_function[1:] == before_function[1:]
            assert "::numeric)::integer" in after_function[0]
        with transaction(runtime, tenant) as connection:
            claim = _v2_command(
                connection, "create_followup_claim_ordered", source, **booking
            )
            assert claim is not None
    finally:
        runtime.dispose()
        migration.dispose()


def test_t155_unicode_blank_reasons_deny_new_evidence_and_preserve_history(
    admin, empty_database, tmp_path
):
    migration, runtime, tenant, doctor = _prepare(admin, empty_database)
    expected = {"source_revision": 0, "clinical_revision": 0, "policy_version": 1}

    def decision(connection, source, reason):
        connection.execute(
            text("SELECT set_config('app.staff_id', :actor, true)"),
            {"actor": str(doctor)},
        )
        return connection.scalar(
            text("""
                SELECT public.record_followup_source_decision(
                    :tenant, :visit, CAST(:expected AS jsonb), CAST(:decision AS jsonb))
            """),
            {
                "tenant": tenant,
                "visit": source["visit"],
                "expected": json.dumps(expected),
                "decision": json.dumps({"kind": "OPTED_OUT", "reason": reason}),
            },
        )

    try:
        _upgrade(migration, PARITY_AFTER)
        _publish_bookable_policies(migration, tenant)
        with transaction(runtime, tenant) as connection:
            historical_source = _v2_source(connection, tenant, doctor)
            assert decision(connection, historical_source, "\u3000") == 1
            booked_source = _v2_source(connection, tenant, doctor)
            claim = _v2_command(
                connection,
                "create_followup_claim_ordered",
                booked_source,
                **_priced_booking(booked_source),
            )
            assert claim is not None
        _t177_receipt(migration, tenant, doctor, claim)
        with migration.connect() as connection:
            old_evidence = connection.scalar(
                text("""
                    SELECT to_jsonb(e) FROM public.followup_source_decision_events e
                    WHERE organization_id=:tenant AND source_visit_id=:visit
                """),
                {"tenant": tenant, "visit": historical_source["visit"]},
            )
        _t177_upgrade_proof(migration, PARITY_AFTER, UNICODE_AFTER, tmp_path)
        with migration.connect() as connection:
            assert (
                connection.scalar(
                    text("""
                    SELECT to_jsonb(e) FROM public.followup_source_decision_events e
                    WHERE organization_id=:tenant AND source_visit_id=:visit
                """),
                    {"tenant": tenant, "visit": historical_source["visit"]},
                )
                == old_evidence
            )
            assert (
                connection.scalar(
                    text("""
                SELECT count(*) FROM pg_constraint
                WHERE conname LIKE 'followup_%_t155_reason_nonblank'
                  AND NOT convalidated
            """)
                )
                == 2
            )
        with transaction(runtime, tenant) as connection:
            source = _v2_source(connection, tenant, doctor)
            before = _writer_effect_counts(connection, tenant)
            for kind in ("OPTED_OUT", "DEADLINE"):
                connection.execute(
                    text("SELECT set_config('app.staff_id', :actor, true)"),
                    {"actor": str(doctor)},
                )
                body = {"kind": kind, "reason": "\u3000"}
                if kind == "DEADLINE":
                    body["deadline_date"] = (
                        source["day"] + timedelta(days=8)
                    ).isoformat()
                with pytest.raises(DBAPIError) as denied, connection.begin_nested():
                    connection.scalar(
                        text("""
                        SELECT public.record_followup_source_decision(
                            :tenant, :visit, CAST(:expected AS jsonb),
                            CAST(:decision AS jsonb))
                    """),
                        {
                            "tenant": tenant,
                            "visit": source["visit"],
                            "expected": json.dumps(expected),
                            "decision": json.dumps(body),
                        },
                    )
                assert denied.value.orig.sqlstate == "23514"
                assert _writer_effect_counts(connection, tenant) == before
                assert (
                    connection.scalar(
                        text("""
                    SELECT count(*) FROM public.followup_source_decision_events
                    WHERE organization_id=:tenant AND source_visit_id=:visit
                """),
                        {"tenant": tenant, "visit": source["visit"]},
                    )
                    == 0
                )

            day = (source["day"] + timedelta(days=10)).isoformat()
            booking = _priced_booking(source, service_date=day)
            with pytest.raises(DBAPIError) as denied, connection.begin_nested():
                _v2_command(
                    connection,
                    "create_followup_claim_ordered",
                    source,
                    **booking,
                    new_booking_date_exception={
                        "selected_service_date": day,
                        "reason": "\u3000",
                    },
                )
            assert denied.value.orig.sqlstate == "23514"
            assert _writer_effect_counts(connection, tenant) == before

            claim = _v2_command(
                connection,
                "create_followup_claim_ordered",
                source,
                **booking,
                new_booking_date_exception={
                    "selected_service_date": day,
                    "reason": "Doctor-approved date",
                },
            )
            approved = _writer_effect_counts(connection, tenant)
            extension_expected = {
                **expected,
                "claim_revision": 1,
                "appointment_revision": 0,
                "authorization_revision": 1,
            }
            with pytest.raises(DBAPIError) as denied, connection.begin_nested():
                _v2_command(
                    connection,
                    "extend_followup_claim_ordered",
                    source,
                    reference=claim,
                    expected=extension_expected,
                    deadline_date=(source["day"] + timedelta(days=12)).isoformat(),
                    reason="\u3000",
                )
            assert denied.value.orig.sqlstate == "23514"
            assert _writer_effect_counts(connection, tenant) == approved
            assert (
                _v2_command(
                    connection,
                    "extend_followup_claim_ordered",
                    source,
                    reference=claim,
                    expected=extension_expected,
                    deadline_date=(source["day"] + timedelta(days=12)).isoformat(),
                    reason="Doctor-approved extension",
                )
                == 2
            )
    finally:
        runtime.dispose()
        migration.dispose()


def test_ordered_writer_guard_rejects_unset_policy_without_effects(
    admin, empty_database
):
    migration, runtime, tenant, doctor = _prepare(admin, empty_database)
    try:
        _upgrade(migration, PROOF_AFTER)
        with transaction(runtime, tenant) as connection:
            source = _v2_source(connection, tenant, doctor)
        _upgrade(migration, GUARD_AFTER)
        with transaction(runtime, tenant) as connection:
            booking = _priced_booking(source)
            with pytest.raises(DBAPIError) as denied, connection.begin_nested():
                _v2_command(
                    connection, "create_followup_claim_ordered", source, **booking
                )
            assert denied.value.orig.sqlstate == "23514"
            _assert_no_guarded_booking_effects(
                connection, tenant, booking["service_date"]
            )
    finally:
        runtime.dispose()
        migration.dispose()


def test_ordered_guard_rejects_forged_price_and_interval_then_books_valid_quote(
    admin, empty_database
):
    migration, runtime, tenant, doctor = _prepare(admin, empty_database)
    try:
        _upgrade(migration, GUARD_AFTER)
        _publish_bookable_policies(migration, tenant)
        with transaction(runtime, tenant) as connection:
            source = _v2_source(connection, tenant, doctor)
            booking = _priced_booking(source)
            for bad_booking in (
                {
                    **booking,
                    "price_snapshot": {**booking["price_snapshot"], "base_egp": "1.00"},
                },
                {
                    **booking,
                    "price_snapshot": {
                        **booking["price_snapshot"],
                        "discount_type": "FIXED",
                        "discount_value": "10.00",
                        "discount_reason": "Invalid\ncontrol",
                        "net_egp": "90.00",
                    },
                },
                {**booking, "starts_at": "2030-01-01T09:00:00+02:00"},
            ):
                with pytest.raises(DBAPIError) as denied, connection.begin_nested():
                    _v2_command(
                        connection,
                        "create_followup_claim_ordered",
                        source,
                        **bad_booking,
                    )
                assert denied.value.orig.sqlstate == "23514"
                _assert_no_guarded_booking_effects(
                    connection, tenant, booking["service_date"]
                )
            claim = _v2_command(
                connection, "create_followup_claim_ordered", source, **booking
            )
            version, base, net, number = connection.execute(
                text("""
                    SELECT p.pricing_version, p.base_egp, p.net_egp, a.current_number
                    FROM public.followup_claims c
                    JOIN public.appointments a
                      ON (a.organization_id,a.id)=(c.organization_id,c.appointment_id)
                    JOIN public.appointment_price_snapshots p
                      ON (p.organization_id,p.appointment_id)=(c.organization_id,c.appointment_id)
                    WHERE c.organization_id=:tenant AND c.id=:claim
                """),
                {"tenant": tenant, "claim": claim},
            ).one()
            assert (version, str(base), str(net), number) == (2, "100.00", "100.00", 1)
    finally:
        runtime.dispose()
        migration.dispose()


def test_timed_guard_rejects_offered_slot_bypass_and_over_capacity(
    admin, empty_database
):
    migration, runtime, tenant, doctor = _prepare(admin, empty_database)
    try:
        _upgrade(migration, PROOF_AFTER)
        with transaction(runtime, tenant) as connection:
            source = _v2_source(connection, tenant, doctor)
        day = source["day"] + timedelta(days=1)
        _upgrade(migration, GUARD_AFTER)
        _publish_bookable_policies(migration, tenant, timed_day=day)
        cairo = ZoneInfo("Africa/Cairo")
        starts = datetime.combine(day, time(9), cairo).isoformat()
        ends = datetime.combine(day, time(10), cairo).isoformat()
        with transaction(runtime, tenant) as connection:
            booking = _priced_booking(
                source, booking_mode="EXACT_TIME", starts_at=starts, ends_at=ends
            )
            with pytest.raises(DBAPIError) as denied, connection.begin_nested():
                _v2_command(
                    connection,
                    "create_followup_claim_ordered",
                    source,
                    **{
                        **booking,
                        "starts_at": datetime.combine(
                            day, time(9, 30), cairo
                        ).isoformat(),
                        "ends_at": datetime.combine(
                            day, time(10, 30), cairo
                        ).isoformat(),
                    },
                )
            assert denied.value.orig.sqlstate == "23514"
            _assert_no_guarded_booking_effects(
                connection, tenant, booking["service_date"]
            )
            _v2_command(connection, "create_followup_claim_ordered", source, **booking)
            second = _v2_source(connection, tenant, doctor)
            with pytest.raises(DBAPIError) as denied, connection.begin_nested():
                _v2_command(
                    connection,
                    "create_followup_claim_ordered",
                    second,
                    **_priced_booking(
                        second,
                        booking_mode="EXACT_TIME",
                        starts_at=starts,
                        ends_at=ends,
                    ),
                )
            assert denied.value.orig.sqlstate == "23514"
            assert (
                connection.scalar(
                    text(
                        "SELECT count(*) FROM public.followup_claims WHERE organization_id=:tenant"
                    ),
                    {"tenant": tenant},
                )
                == 1
            )
    finally:
        runtime.dispose()
        migration.dispose()


def test_cairo_offset_guard_rejects_false_offset_and_accepts_cairo_and_utc(
    admin, empty_database
):
    migration, runtime, tenant, doctor = _prepare(admin, empty_database)
    try:
        _upgrade(migration, PROOF_AFTER)
        with transaction(runtime, tenant) as connection:
            first = _v2_source(connection, tenant, doctor)
            second = _v2_source(connection, tenant, doctor)
        day = first["day"] + timedelta(days=1)
        _upgrade(migration, GUARD_AFTER)
        _publish_bookable_policies(migration, tenant, timed_day=day)
        _upgrade(migration, OFFSET_AFTER)
        with migration.connect() as connection:
            for signature in (
                "b10_assert_followup_schedule(uuid,jsonb)",
                "b10_schedule_without_offset_guard(uuid,jsonb)",
            ):
                can_execute, definer = connection.execute(
                    text("""
                        SELECT has_function_privilege(
                            'carepulse_runtime', CAST(:function AS regprocedure), 'EXECUTE'
                        ), prosecdef
                        FROM pg_proc WHERE oid=CAST(:function AS regprocedure)
                    """),
                    {"function": f"public.{signature}"},
                ).one()
                assert can_execute is False
                assert definer is True

        cairo = ZoneInfo("Africa/Cairo")
        start = datetime.combine(day, time(9), cairo)
        end = datetime.combine(day, time(10), cairo)
        mismatched = timezone(start.utcoffset() + timedelta(hours=1))
        booking = _priced_booking(
            first,
            booking_mode="EXACT_TIME",
            starts_at=start.isoformat(),
            ends_at=end.isoformat(),
        )
        with transaction(runtime, tenant) as connection:
            for wrong in (
                {**booking, "starts_at": start.astimezone(mismatched).isoformat()},
                {**booking, "ends_at": end.astimezone(mismatched).isoformat()},
            ):
                with pytest.raises(DBAPIError) as denied, connection.begin_nested():
                    _v2_command(
                        connection, "create_followup_claim_ordered", first, **wrong
                    )
                assert denied.value.orig.sqlstate == "23514"
                _assert_no_guarded_booking_effects(
                    connection, tenant, booking["service_date"]
                )
            _v2_command(connection, "create_followup_claim_ordered", first, **booking)
            utc_start = datetime.combine(day, time(10), cairo).astimezone(UTC)
            utc_end = datetime.combine(day, time(11), cairo).astimezone(UTC)
            _v2_command(
                connection,
                "create_followup_claim_ordered",
                second,
                **_priced_booking(
                    second,
                    booking_mode="EXACT_TIME",
                    starts_at=utc_start.isoformat().replace("+00:00", "Z"),
                    ends_at=utc_end.isoformat(),
                ),
            )
            assert (
                connection.scalar(
                    text("""
                    SELECT count(*) FROM public.followup_claims
                    WHERE organization_id=:tenant
                """),
                    {"tenant": tenant},
                )
                == 2
            )
    finally:
        runtime.dispose()
        migration.dispose()


def test_exact_writer_rejects_submicrosecond_rounding_without_effects(
    admin, empty_database, tmp_path
):
    migration, runtime, tenant, doctor = _prepare(admin, empty_database)
    try:
        _upgrade(migration, PROOF_AFTER)
        with transaction(runtime, tenant) as connection:
            historical = _v2_source(connection, tenant, doctor)
            first = _v2_source(connection, tenant, doctor)
            second = _v2_source(connection, tenant, doctor)
        day = first["day"] + timedelta(days=1)
        _upgrade(migration, "0034_t145_exact_percentage")
        _publish_bookable_policies(migration, tenant, timed_day=day)
        cairo = ZoneInfo("Africa/Cairo")
        start = datetime.combine(day, time(9), cairo)
        end = datetime.combine(day, time(10), cairo)
        booking = _priced_booking(
            first,
            booking_mode="EXACT_TIME",
            starts_at=start.isoformat(),
            ends_at=end.isoformat(),
        )
        with transaction(runtime, tenant) as connection:
            previous_claim = _v2_command(
                connection,
                "create_followup_claim_ordered",
                historical,
                **_priced_booking(
                    historical,
                    booking_mode="EXACT_TIME",
                    starts_at=datetime.combine(day, time(11), cairo).isoformat(),
                    ends_at=datetime.combine(day, time(12), cairo).isoformat(),
                ),
            )
            with connection.begin_nested() as savepoint:
                assert (
                    _v2_command(
                        connection,
                        "create_followup_claim_ordered",
                        first,
                        **{
                            **booking,
                            "starts_at": start.isoformat(
                                timespec="microseconds"
                            ).replace(".000000", ".0000004"),
                        },
                    )
                    is not None
                )
                savepoint.rollback()
        _t177_receipt(migration, tenant, doctor, previous_claim)
        with migration.connect() as connection:
            saved = connection.execute(
                text("""
                    SELECT to_jsonb(c), to_jsonb(a), to_jsonb(p)
                    FROM public.followup_claims c
                    JOIN public.appointments a ON a.id=c.appointment_id
                    JOIN public.appointment_price_snapshots p ON p.appointment_id=a.id
                    WHERE c.organization_id=:tenant AND c.id=:claim
                """),
                {"tenant": tenant, "claim": previous_claim},
            ).one()
        _t177_upgrade_proof(
            migration, "0034_t145_exact_percentage", EXACT_AFTER, tmp_path
        )
        _t177_upgrade_proof(migration, EXACT_AFTER, PARITY_AFTER, tmp_path)
        with migration.connect() as connection:
            assert (
                connection.execute(
                    text("""
                    SELECT to_jsonb(c), to_jsonb(a), to_jsonb(p)
                    FROM public.followup_claims c
                    JOIN public.appointments a ON a.id=c.appointment_id
                    JOIN public.appointment_price_snapshots p ON p.appointment_id=a.id
                    WHERE c.organization_id=:tenant AND c.id=:claim
                """),
                    {"tenant": tenant, "claim": previous_claim},
                ).one()
                == saved
            )
            for signature, allowed in (
                ("create_followup_claim_ordered(uuid,uuid,jsonb,jsonb)", True),
                ("extend_followup_claim_ordered(uuid,uuid,jsonb,jsonb)", True),
                ("b10_assert_followup_schedule(uuid,jsonb)", False),
                ("b10_schedule_without_exact_guard(uuid,jsonb)", False),
                ("b10_schedule_without_offset_guard(uuid,jsonb)", False),
                ("b10_unchecked_claim_creation(uuid,uuid,jsonb,jsonb)", False),
            ):
                assert (
                    connection.scalar(
                        text("""
                        SELECT has_function_privilege('carepulse_runtime',
                            CAST(:signature AS regprocedure), 'EXECUTE')
                    """),
                        {"signature": f"public.{signature}"},
                    )
                    is allowed
                )

        with transaction(runtime, tenant) as connection:
            before = _writer_effect_counts(connection, tenant)
            for endpoint, valid in (("starts_at", start), ("ends_at", end)):
                rounded_to_valid_slot = valid.isoformat(
                    timespec="microseconds"
                ).replace(".000000", ".0000004")
                with pytest.raises(DBAPIError) as denied, connection.begin_nested():
                    _v2_command(
                        connection,
                        "create_followup_claim_ordered",
                        first,
                        **{**booking, endpoint: rounded_to_valid_slot},
                    )
                assert denied.value.orig.sqlstate == "23514"
                assert _writer_effect_counts(connection, tenant) == before

            # Excess trailing zeros preserve the exact instant; both offset forms work.
            claim_one = _v2_command(
                connection,
                "create_followup_claim_ordered",
                first,
                **{
                    **booking,
                    "starts_at": start.isoformat(timespec="microseconds").replace(
                        ".000000", ".000000000"
                    ),
                },
            )
            utc_start = datetime.combine(day, time(10), cairo).astimezone(UTC)
            utc_end = datetime.combine(day, time(11), cairo).astimezone(UTC)
            claim_two = _v2_command(
                connection,
                "create_followup_claim_ordered",
                second,
                **_priced_booking(
                    second,
                    booking_mode="EXACT_TIME",
                    starts_at=utc_start.isoformat().replace("+00:00", "Z"),
                    ends_at=utc_end.isoformat(),
                ),
            )
            assert claim_one != claim_two
            assert (
                connection.scalar(
                    text("""
                    SELECT count(*) FROM public.followup_claims
                    WHERE organization_id=:tenant AND id IN (:one, :two)
                """),
                    {"tenant": tenant, "one": claim_one, "two": claim_two},
                )
                == 2
            )
    finally:
        runtime.dispose()
        migration.dispose()


@pytest.mark.parametrize(
    "work_period,breaks",
    [
        (("23:20", "23:40"), ()),
        (("22:00", "23:59"), (("23:40", "23:50"),)),
        (("22:00", "23:59"), ()),
    ],
    ids=("crosses-work-period", "crosses-break", "crosses-2359-boundary"),
)
def test_exact_writer_enforces_resolved_cairo_fold_work_and_breaks(
    admin, empty_database, work_period, breaks
):
    migration, runtime, tenant, doctor = _prepare(admin, empty_database)
    try:
        _upgrade(migration, "0034_t145_exact_percentage")
        fold_day = date(2030, 10, 31)
        _publish_bookable_policies(
            migration,
            tenant,
            timed_day=fold_day,
            work_period=work_period,
            breaks=breaks,
            manual_slots=(("23:25", "23:35"),),
        )
        with transaction(runtime, tenant) as connection:
            sources = [_v2_source(connection, tenant, doctor) for _ in range(3)]
        _upgrade(migration, PARITY_AFTER)

        cairo = ZoneInfo("Africa/Cairo")
        first_start = datetime.combine(fold_day, time(23, 25), cairo).replace(fold=0)
        first_end = datetime.combine(fold_day, time(23, 35), cairo).replace(fold=0)
        second_start = first_start.replace(fold=1)
        second_end = first_end.replace(fold=1)
        assert first_start.utcoffset() != second_start.utcoffset()
        assert second_end.astimezone(UTC) - first_start.astimezone(UTC) == timedelta(
            minutes=70
        )
        declaration = {
            "selected_service_date": fold_day.isoformat(),
            "reason": "Reviewed Cairo DST booking",
        }

        def priced(source, starts_at, ends_at):
            return _priced_booking(
                source,
                service_date=fold_day.isoformat(),
                booking_mode="EXACT_TIME",
                starts_at=starts_at,
                ends_at=ends_at,
                new_booking_date_exception=declaration,
            )

        with transaction(runtime, tenant) as connection:
            before = _writer_effect_counts(connection, tenant)
            mixed = priced(sources[0], first_start.isoformat(), second_end.isoformat())
            # Even the widest representable HH:MM work interval ends at
            # 23:59 exclusively: the crossed 23:59 minute is not covered.
            with pytest.raises(DBAPIError) as denied, connection.begin_nested():
                _v2_command(
                    connection,
                    "create_followup_claim_ordered",
                    sources[0],
                    **mixed,
                )
            assert denied.value.orig.sqlstate == "23514"
            assert _writer_effect_counts(connection, tenant) == before

            # Both explicitly resolved same-fold alternatives remain bookable.
            first_claim = _v2_command(
                connection,
                "create_followup_claim_ordered",
                sources[1],
                **priced(sources[1], first_start.isoformat(), first_end.isoformat()),
            )
            second_claim = _v2_command(
                connection,
                "create_followup_claim_ordered",
                sources[2],
                **priced(
                    sources[2],
                    second_start.astimezone(UTC).isoformat().replace("+00:00", "Z"),
                    second_end.astimezone(UTC).isoformat(),
                ),
            )
            assert first_claim != second_claim
    finally:
        runtime.dispose()
        migration.dispose()


def test_exact_writer_rejects_normalized_clock_components_without_effects(
    admin, empty_database
):
    migration, runtime, tenant, doctor = _prepare(admin, empty_database)
    try:
        _upgrade(migration, PROOF_AFTER)
        with transaction(runtime, tenant) as connection:
            cairo_source = _v2_source(connection, tenant, doctor)
            utc_source = _v2_source(connection, tenant, doctor)
        day = date(2030, 1, 1)
        _upgrade(migration, EXACT_AFTER)
        _publish_bookable_policies(
            migration, tenant, timed_day=day, work_period=("01:00", "10:00")
        )
        _upgrade(migration, PARITY_AFTER)

        def timed(source, starts_at, ends_at):
            return _priced_booking(
                source,
                service_date=day.isoformat(),
                booking_mode="EXACT_TIME",
                starts_at=starts_at,
                ends_at=ends_at,
                new_booking_date_exception={
                    "selected_service_date": day.isoformat(),
                    "reason": "Reviewed future service date",
                },
            )

        cairo_cases = (
            ("2030-01-01T08:59:60+02:00", "2030-01-01T10:00:00+02:00"),
            ("2030-01-01T08:00:00+02:00", "2030-01-01T08:59:60+02:00"),
        )
        utc_cases = (
            ("2029-12-31T24:00:00Z", "2030-01-01T01:00:00Z"),
            ("2029-12-31T23:00:00Z", "2029-12-31T24:00:00Z"),
        )
        with migration.connect() as connection:
            before = _writer_effect_counts(connection, tenant)
        with transaction(runtime, tenant) as connection:
            for starts_at, ends_at in (*cairo_cases, *utc_cases):
                with pytest.raises(DBAPIError) as denied, connection.begin_nested():
                    _v2_command(
                        connection,
                        "create_followup_claim_ordered",
                        cairo_source,
                        **timed(cairo_source, starts_at, ends_at),
                    )
                assert denied.value.orig.sqlstate == "23514"
                assert "explicit RFC3339 offsets" in str(denied.value.orig)
        with migration.connect() as connection:
            assert _writer_effect_counts(connection, tenant) == before
        with transaction(runtime, tenant) as connection:
            cairo_claim = _v2_command(
                connection,
                "create_followup_claim_ordered",
                cairo_source,
                **timed(
                    cairo_source,
                    "2030-01-01T09:00:00+02:00",
                    "2030-01-01T10:00:00+02:00",
                ),
            )
            utc_claim = _v2_command(
                connection,
                "create_followup_claim_ordered",
                utc_source,
                **timed(utc_source, "2029-12-31T23:00:00Z", "2030-01-01T00:00:00Z"),
            )
            assert cairo_claim is not None and utc_claim is not None
            assert cairo_claim != utc_claim
    finally:
        runtime.dispose()
        migration.dispose()


def test_guard_effective_runtime_grants_preserve_ordered_operations(
    admin, empty_database
):
    migration, runtime, _tenant, _doctor = _prepare(admin, empty_database)
    try:
        _upgrade(migration, PARITY_AFTER)
        with migration.connect() as connection:
            for signature, allowed in (
                ("create_followup_claim_ordered(uuid,uuid,jsonb,jsonb)", True),
                ("extend_followup_claim_ordered(uuid,uuid,jsonb,jsonb)", True),
                ("b10_unchecked_claim_creation(uuid,uuid,jsonb,jsonb)", False),
                ("b10_assert_followup_price(uuid,jsonb)", False),
                ("b10_assert_followup_schedule(uuid,jsonb)", False),
                ("b10_schedule_without_exact_guard(uuid,jsonb)", False),
                ("b10_schedule_without_offset_guard(uuid,jsonb)", False),
                ("b10_operational_source_proof(uuid,uuid)", True),
                ("d08_legacy_source(uuid,uuid)", False),
            ):
                assert (
                    connection.scalar(
                        text("""
                        SELECT has_function_privilege('carepulse_runtime',
                            CAST(:signature AS regprocedure), 'EXECUTE')
                    """),
                        {"signature": f"public.{signature}"},
                    )
                    is allowed
                )
    finally:
        runtime.dispose()
        migration.dispose()


def test_0029_failed_upgrade_rolls_back_function_fences_and_data(
    admin, empty_database, tmp_path
):
    migration, runtime, tenant, doctor = _prepare(admin, empty_database)
    try:
        _upgrade(migration, BEFORE)
        with transaction(runtime, tenant) as connection:
            source = _v2_source(connection, tenant, doctor)
            claim = _v2_command(
                connection, "create_followup_claim", source, **_v2_booking(source)
            )
        with migration.connect() as connection:
            previous = connection.scalar(
                text(
                    "SELECT to_jsonb(c) FROM public.followup_claims c WHERE id=:claim"
                ),
                {"claim": claim},
            )
        scripts = tmp_path / "migrations"
        shutil.copytree(ROOT / "db/migrations", scripts)
        successor = scripts / "versions/0029_b10_ordered_followup_writers.py"
        original = successor.read_text(encoding="utf-8")
        marker = "    _privileges()\n"
        assert original.count(marker) == 1
        successor.write_text(
            original.replace(marker, marker + '    op.execute("SELECT 1 / 0")\n'),
            encoding="utf-8",
        )
        with pytest.raises(DBAPIError) as failure:
            _upgrade(migration, AFTER, scripts)
        assert failure.value.orig.sqlstate == "22012"
        with migration.connect() as connection:
            assert (
                connection.scalar(
                    text("SELECT version_num FROM public.alembic_version")
                )
                == BEFORE
            )
            assert (
                connection.scalar(
                    text(
                        "SELECT to_regprocedure('public.create_followup_claim_ordered(uuid,uuid,jsonb,jsonb)')"
                    )
                )
                is None
            )
            assert (
                connection.scalar(
                    text(
                        "SELECT to_jsonb(c) FROM public.followup_claims c WHERE id=:claim"
                    ),
                    {"claim": claim},
                )
                == previous
            )
            assert (
                connection.scalar(
                    text("""
                    SELECT has_function_privilege('carepulse_runtime',
                        'public.create_followup_claim(uuid,uuid,jsonb,jsonb)', 'EXECUTE')
                """)
                )
                is True
            )
        _upgrade(migration, AFTER)
    finally:
        runtime.dispose()
        migration.dispose()


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


def test_final_writer_new_date_denials_rollback_and_evidence_links(
    admin, empty_database
):
    migration, runtime, tenant, doctor = _prepare(admin, empty_database)
    try:
        _upgrade(migration, OFFSET_AFTER)
        _publish_bookable_policies(migration, tenant)
        with transaction(runtime, tenant) as connection:
            source = _v2_source(connection, tenant, doctor)
            day = (source["day"] + timedelta(days=10)).isoformat()
            booking = _priced_booking(source, service_date=day)
            connection.execute(
                text("SELECT set_config('app.staff_id', :doctor, true)"),
                {"doctor": str(doctor)},
            )
            tables = (
                "appointments",
                "appointment_number_assignments",
                "appointment_history",
                "appointment_price_snapshots",
                "followup_claims",
                "followup_authorization_events",
                "sensitive_audit",
                "idempotency_receipts",
                "outbox_events",
            )

            def effect_counts():
                return tuple(
                    connection.scalar(
                        text(
                            f"SELECT count(*) FROM public.{table} WHERE organization_id=:tenant"
                        ),
                        {"tenant": tenant},
                    )
                    for table in tables
                )

            before = effect_counts()
            for declaration in (
                {},
                {"new_booking_date_exception": None},
                {
                    "new_booking_date_exception": {
                        "selected_service_date": day,
                        "reason": None,
                    }
                },
                {
                    "new_booking_date_exception": {
                        "selected_service_date": source["day"].isoformat(),
                        "reason": "Wrong date",
                    }
                },
                {
                    "new_booking_date_exception": {
                        "selected_service_date": day,
                        "reason": "Valid",
                        "extra": True,
                    }
                },
                {"new_booking_date_exception": {"selected_service_date": day}},
            ):
                with pytest.raises(DBAPIError) as denied, connection.begin_nested():
                    _v2_command(
                        connection,
                        "create_followup_claim_ordered",
                        source,
                        **{**booking, **declaration},
                    )
                assert denied.value.orig.sqlstate == "23514"
                assert effect_counts() == before
                _assert_no_guarded_booking_effects(connection, tenant, day)

            declaration = {
                "selected_service_date": day,
                "reason": "Doctor-approved visit",
            }
            claim = _v2_command(
                connection,
                "create_followup_claim_ordered",
                source,
                **booking,
                new_booking_date_exception=declaration,
            )
        with migration.connect() as connection:
            joined = (
                connection.execute(
                    text("""
                    SELECT c.source_visit_id, c.source_appointment_id,
                           c.patient_id, c.doctor_staff_id, c.state,
                           c.approved_deadline, c.policy_snapshot,
                           a.id AS appointment_id, a.service_date, a.current_number,
                           n.number AS assigned_number, h.action, h.new_service_date,
                           h.new_number, h.actor_staff_id AS history_actor,
                           h.actor_role AS history_role, h.reason AS history_reason,
                           h.price_snapshot_appointment_id AS history_price,
                           p.service_type, p.base_egp, p.net_egp,
                           p.pricing_version, p.entitlement_provenance,
                           e.id AS event_id, e.kind, e.source_visit_id AS event_source,
                           e.selected_date, e.prior_deadline, e.new_deadline,
                           e.actor_staff_id AS event_actor, e.actor_role AS event_role,
                           e.reason AS event_reason, e.occurred_at AS event_at,
                           e.policy_version, e.source_revision, e.clinical_revision,
                           e.authorization_revision, e.claim_revision, e.appointment_revision,
                           au.actor_staff_id AS audit_actor, au.actor_role AS audit_role,
                           au.action AS audit_action, au.correlation_id,
                           au.occurred_at AS audit_at,
                           v.appointment_id AS visit_appointment
                    FROM public.followup_claims c
                    JOIN public.visits v ON (v.organization_id,v.id)=(c.organization_id,c.source_visit_id)
                    JOIN public.appointments a ON (a.organization_id,a.id)=(c.organization_id,c.appointment_id)
                    JOIN public.appointment_number_assignments n
                      ON (n.organization_id,n.id)=(a.organization_id,a.current_assignment_id)
                    JOIN public.appointment_history h
                      ON (h.organization_id,h.appointment_id)=(a.organization_id,a.id)
                     AND h.action='CREATED'
                    JOIN public.appointment_price_snapshots p
                      ON (p.organization_id,p.appointment_id)=(a.organization_id,a.id)
                    JOIN public.followup_authorization_events e
                      ON (e.organization_id,e.claim_id)=(c.organization_id,c.id)
                    JOIN public.sensitive_audit au
                      ON (au.organization_id,au.id)=(e.organization_id,e.sensitive_audit_id)
                    WHERE c.organization_id=:tenant AND c.id=:claim
                """),
                    {"tenant": tenant, "claim": claim},
                )
                .mappings()
                .one()
            )
            assert (joined["source_visit_id"], joined["event_source"]) == (
                source["visit"],
                source["visit"],
            )
            assert (joined["source_appointment_id"], joined["visit_appointment"]) == (
                source["appointment"],
                source["appointment"],
            )
            assert (
                joined["patient_id"],
                joined["doctor_staff_id"],
                joined["state"],
            ) == (source["patient"], doctor, "ACTIVE")
            assert (
                joined["service_date"].isoformat(),
                joined["selected_date"].isoformat(),
                joined["new_deadline"].isoformat(),
                joined["new_service_date"].isoformat(),
            ) == (day, day, day, day)
            assert (
                joined["prior_deadline"]
                == joined["approved_deadline"]
                == (source["day"] + timedelta(days=7))
            )
            assert (
                joined["current_number"],
                joined["assigned_number"],
                joined["new_number"],
            ) == (1, 1, 1)
            assert (
                joined["action"],
                joined["history_actor"],
                joined["history_role"],
                joined["history_reason"],
                joined["history_price"],
            ) == ("CREATED", doctor, "DOCTOR_OWNER", None, joined["appointment_id"])
            assert (
                joined["service_type"],
                str(joined["base_egp"]),
                str(joined["net_egp"]),
                joined["pricing_version"],
            ) == ("FOLLOW_UP", "100.00", "100.00", 2)
            assert (
                joined["kind"],
                joined["event_actor"],
                joined["event_role"],
                joined["event_reason"],
                joined["policy_version"],
                joined["source_revision"],
                joined["clinical_revision"],
                joined["authorization_revision"],
                joined["claim_revision"],
                joined["appointment_revision"],
            ) == (
                "NEW_DATE",
                doctor,
                "DOCTOR_OWNER",
                declaration["reason"],
                1,
                0,
                0,
                1,
                1,
                0,
            )
            assert (
                joined["audit_actor"],
                joined["audit_role"],
                joined["audit_action"],
                joined["correlation_id"],
            ) == (doctor, "DOCTOR_OWNER", "FOLLOWUP_NEW_DATE", str(joined["event_id"]))
            assert joined["audit_at"] == joined["event_at"]
            assert joined["policy_snapshot"]["new_date_event_id"] == str(
                joined["event_id"]
            )
            assert (
                joined["policy_snapshot"]["new_booking_date_exception"] == declaration
            )
            assert joined["entitlement_provenance"] == joined["policy_snapshot"]
            assert (
                connection.scalar(
                    text("""
                    SELECT last_number FROM public.appointment_day_counters
                    WHERE organization_id=:tenant AND service_date=CAST(:day AS date)
                """),
                    {"tenant": tenant, "day": day},
                )
                == 1
            )
    finally:
        runtime.dispose()
        migration.dispose()

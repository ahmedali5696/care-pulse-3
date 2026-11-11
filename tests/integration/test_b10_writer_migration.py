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
from test_visit_migration import ROOT, _prepare, _upgrade

BEFORE = "0028_d08_followup_provenance"
AFTER = "0029_b10_followup_writers"
PROOF_AFTER = "0030_b10_source_proof"
GUARD_AFTER = "0031_b10_writer_guard"
OFFSET_AFTER = "0032_b10_cairo_offset_guard"
EXACT_AFTER = "0035_t146_cairo_exact_schedule"


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
    admin, empty_database
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
        _upgrade(migration, EXACT_AFTER)
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
        _upgrade(migration, EXACT_AFTER)

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


def test_guard_effective_runtime_grants_preserve_ordered_operations(
    admin, empty_database
):
    migration, runtime, _tenant, _doctor = _prepare(admin, empty_database)
    try:
        _upgrade(migration, GUARD_AFTER)
        with migration.connect() as connection:
            for signature, allowed in (
                ("create_followup_claim_ordered(uuid,uuid,jsonb,jsonb)", True),
                ("extend_followup_claim_ordered(uuid,uuid,jsonb,jsonb)", True),
                ("b10_unchecked_claim_creation(uuid,uuid,jsonb,jsonb)", False),
                ("b10_assert_followup_price(uuid,jsonb)", False),
                ("b10_assert_followup_schedule(uuid,jsonb)", False),
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

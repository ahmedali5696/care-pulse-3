"""D-08 durable preferential claims, lifecycle, tenant isolation and grants."""

import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import UUID, uuid4

import pytest
from carepulse.appointments.followups import qualify_followup
from carepulse.appointments.persistence import persist_appointment
from carepulse.db import transaction
from carepulse.policies.versions import append_version
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from test_appointment_persistence import (
    _activate_clinic,
    _admit_saved,
    _draft,
    _patient,
    _query,
)
from test_appointment_persistence import appointment_storage as appointment_storage  # noqa: PLC0414
from test_database import _engine
from test_database import admin as admin  # noqa: PLC0414
from test_database import disposable_project as disposable_project  # noqa: PLC0414
from test_database import empty_database as empty_database  # noqa: PLC0414
from test_visit_migration import _prepare, _upgrade
from test_visit_persistence import _receptionist
from test_visit_persistence import _seed as _admitted_source


@pytest.fixture
def legacy_followup_storage(admin, empty_database):
    """Keep pre-cutover regressions on their real predecessor, with its old writers."""
    migration, runtime, tenant, doctor = _prepare(admin, empty_database)
    try:
        _upgrade(migration, "0024_b06_followup_window")
        with migration.begin() as connection:
            operator = connection.scalar(
                text(
                    "SELECT created_by_operator_id FROM public.organizations WHERE id=:tenant"
                ),
                {"tenant": tenant},
            )
            other = uuid4()
            connection.execute(
                text(
                    "INSERT INTO public.organizations(id,branch,created_by_operator_id) VALUES(:id,'Synthetic legacy D-08',:operator)"
                ),
                {"id": other, "operator": operator},
            )
            foreign_doctor = _activate_clinic(connection, other, operator)
        yield runtime, migration, (tenant, other), (doctor, foreign_doctor)
    finally:
        runtime.dispose()
        migration.dispose()


@pytest.fixture
def provenance_followup_storage(admin, empty_database):
    """Exercise historical 0028 terminal and writer behavior before B-10 fencing."""
    migration, runtime, tenant, doctor = _prepare(admin, empty_database)
    try:
        _upgrade(migration, "0028_d08_followup_provenance")
        yield runtime, migration, (tenant, None), (doctor, None)
    finally:
        runtime.dispose()
        migration.dispose()


def _v2_source(connection, tenant, doctor):
    appointment = _admitted_source(connection, tenant, doctor, complete=True)
    visit, completion, patient = connection.execute(
        text("""
            SELECT c.visit_id, (c.completed_at AT TIME ZONE 'Africa/Cairo')::date,
                   v.patient_id
            FROM public.completion_attestations c
            JOIN public.visits v ON (v.organization_id,v.id)=(c.organization_id,c.visit_id)
            WHERE c.appointment_id=:appointment
        """),
        {"appointment": appointment},
    ).one()
    return {
        "tenant": tenant,
        "doctor": doctor,
        "appointment": appointment,
        "visit": visit,
        "day": completion,
        "patient": patient,
    }


def _v2_command(connection, routine, source, **command):
    connection.execute(text("SET LOCAL lock_timeout = '5s'"))
    connection.execute(text("SET LOCAL statement_timeout = '15s'"))
    connection.execute(
        text("SELECT set_config('app.staff_id', :actor, true)"),
        {"actor": str(command.pop("actor", source["doctor"]))},
    )
    expected = command.pop(
        "expected", {"source_revision": 0, "clinical_revision": 0, "policy_version": 1}
    )
    reference = command.pop("reference", source["visit"])
    legacy_entrypoint = command.pop("legacy_entrypoint", False)
    ordered = connection.scalar(
        text("""
            SELECT to_regprocedure(
                'public.create_followup_claim_ordered(uuid,uuid,jsonb,jsonb)')
                IS NOT NULL
        """)
    )
    if (
        ordered
        and not legacy_entrypoint
        and routine in ("create_followup_claim", "extend_followup_claim")
    ):
        routine += "_ordered"
    if routine == "create_followup_claim_ordered" and "allocated_number" not in command:
        command["allocated_number"] = connection.scalar(
            text("""
                INSERT INTO public.appointment_day_counters
                    (organization_id, service_date, last_number)
                VALUES (:tenant, CAST(:day AS date), 1)
                ON CONFLICT (organization_id, service_date)
                DO UPDATE SET last_number = public.appointment_day_counters.last_number + 1
                RETURNING last_number
            """),
            {"tenant": source["tenant"], "day": command["service_date"]},
        )
    return connection.scalar(
        text(
            f"SELECT public.{routine}(:org, :reference, CAST(:expected AS jsonb), CAST(:body AS jsonb))"
        ),
        {
            "org": source["tenant"],
            "reference": reference,
            "expected": json.dumps(expected),
            "body": json.dumps(command),
        },
    )


def _v2_booking(source, offset=1):
    return {
        "service_date": (source["day"] + timedelta(days=offset)).isoformat(),
        "booking_mode": "OPEN",
        "price_snapshot": dict(_draft(uuid4(), source["doctor"]).price_snapshot),
    }


def _v2_priced_followup(connection, tenant, doctor, price_snapshot):
    """Create a priced Follow-up through the post-cutover SQL writer for test fixtures."""
    source = _v2_source(connection, tenant, doctor)
    claim = _v2_command(
        connection,
        "create_followup_claim",
        source,
        **{**_v2_booking(source), "price_snapshot": price_snapshot},
    )
    appointment, net, provenance = connection.execute(
        text("""
            SELECT c.appointment_id, p.net_egp, p.entitlement_provenance
            FROM public.followup_claims c
            JOIN public.appointment_price_snapshots p
              ON (p.organization_id, p.appointment_id) =
                 (c.organization_id, c.appointment_id)
            WHERE c.organization_id=:tenant AND c.id=:claim
        """),
        {"tenant": tenant, "claim": claim},
    ).one()
    return {
        "id": str(appointment),
        "net_egp": f"{net:.2f}",
        "entitlement_provenance": provenance,
    }


def _v2_record(connection, claim):
    appointment, day, number = connection.execute(
        text("""
            SELECT a.id, a.service_date, a.current_number FROM public.followup_claims c
            JOIN public.appointments a ON (a.organization_id,a.id)=(c.organization_id,c.appointment_id)
            WHERE c.id=:claim
        """),
        {"claim": claim},
    ).one()
    return {
        "id": appointment,
        "claim": claim,
        "saved": {
            "id": str(appointment),
            "service_date": day.isoformat(),
            "number": number,
        },
    }


@pytest.mark.parametrize("actor_kind", ["doctor", "receptionist", "manager"])
def test_v2_no_decision_and_actual_staff_events_preserve_clinical_content(
    appointment_storage, actor_kind
):
    runtime, migration, (tenant, _), (doctor, _) = appointment_storage
    staff = doctor if actor_kind == "doctor" else _receptionist(migration, tenant)
    if actor_kind == "manager":
        with migration.begin() as connection:
            connection.execute(
                text(
                    "UPDATE public.staff_accounts SET operational_manager=true, security_revision=security_revision+1 WHERE id=:id"
                ),
                {"id": staff},
            )
    with transaction(runtime, tenant) as connection:
        source = _v2_source(connection, tenant, doctor)
        claim = _v2_command(
            connection,
            "create_followup_claim",
            source,
            actor=staff,
            **_v2_booking(source),
        )
        assert connection.execute(
            text("""
            SELECT source_revision, decision, decision_actor_staff_id
            FROM public.followup_source_state WHERE visit_id=:visit
        """),
            {"visit": source["visit"]},
        ).one() == (0, None, None)
        for revision, decision in enumerate(
            (
                {
                    "kind": "DEADLINE",
                    "deadline_date": (source["day"] + timedelta(days=12)).isoformat(),
                    "reason": "Synthetic staff decision",
                },
                {"kind": "DEFAULT"},
            ),
            start=1,
        ):
            assert (
                _v2_command(
                    connection,
                    "record_followup_source_decision",
                    source,
                    actor=staff,
                    expected={
                        "source_revision": revision - 1,
                        "clinical_revision": 0,
                        "policy_version": 1,
                    },
                    **decision,
                )
                == revision
            )
        connection.execute(text("SET CONSTRAINTS ALL IMMEDIATE"))
        assert connection.execute(
            text(
                "SELECT clinical_revision, followup_decision FROM public.visits WHERE id=:visit"
            ),
            {"visit": source["visit"]},
        ).one() == (0, None)
        assert (
            connection.scalar(
                text(
                    "SELECT count(*) FROM public.clinical_revisions WHERE visit_id=:visit"
                ),
                {"visit": source["visit"]},
            )
            == 0
        )
        assert connection.scalar(
            text("SELECT approved_deadline FROM public.followup_claims WHERE id=:id"),
            {"id": claim},
        ) == source["day"] + timedelta(days=7)
    # The Receptionist may write operational evidence but cannot read sensitive audit.
    with migration.connect() as connection:
        events = connection.execute(
            text("""
                SELECT e.source_revision, e.actor_staff_id, e.actor_role,
                       e.before_decision, e.decision, a.before_state, a.after_state
                FROM public.followup_source_decision_events e
                JOIN public.sensitive_audit a
                  ON (a.organization_id,a.id)=(e.organization_id,e.sensitive_audit_id)
                WHERE e.source_visit_id=:visit ORDER BY e.source_revision
            """),
            {"visit": source["visit"]},
        ).all()
    role = "DOCTOR_OWNER" if staff == doctor else "RECEPTIONIST"
    assert [
        (row.source_revision, row.actor_staff_id, row.actor_role) for row in events
    ] == [(1, staff, role), (2, staff, role)]
    assert events[0].before_decision is None
    assert events[1].before_decision == events[0].decision
    assert events[1].before_state["actor_staff_id"] == str(staff)
    assert events[1].after_state["decision"] == {"kind": "DEFAULT"}


@pytest.mark.parametrize("restriction", ["OPTED_OUT", "DEADLINE"])
def test_v2_doctor_priority_blocks_receptionist_reset_exception_and_extension(
    provenance_followup_storage, restriction
):
    runtime, migration, (tenant, _), (doctor, _) = provenance_followup_storage
    staff = _receptionist(migration, tenant)
    with transaction(runtime, tenant) as connection:
        source = _v2_source(connection, tenant, doctor)
        claim = _v2_command(
            connection, "create_followup_claim", source, **_v2_booking(source)
        )
        decision = {"kind": restriction, "reason": "Synthetic Doctor restriction"}
        if restriction == "DEADLINE":
            decision["deadline_date"] = (source["day"] + timedelta(days=20)).isoformat()
        _v2_command(connection, "record_followup_source_decision", source, **decision)
        expected = {"source_revision": 1, "clinical_revision": 0, "policy_version": 1}
        with pytest.raises(DBAPIError) as denied, connection.begin_nested():
            _v2_command(
                connection,
                "record_followup_source_decision",
                source,
                actor=staff,
                expected=expected,
                kind="DEFAULT",
            )
        assert denied.value.orig.sqlstate == "42501"
        with pytest.raises(DBAPIError) as denied, connection.begin_nested():
            _v2_command(
                connection,
                "extend_followup_claim",
                source,
                actor=staff,
                reference=claim,
                expected={
                    **expected,
                    "claim_revision": 1,
                    "appointment_revision": 0,
                    "authorization_revision": 0,
                },
                deadline_date=(source["day"] + timedelta(days=21)).isoformat(),
                reason="Cannot bypass Doctor",
            )
        assert denied.value.orig.sqlstate == "23514"
        _outcome(
            connection,
            tenant,
            doctor,
            _v2_record(connection, claim),
            status="Cancelled",
        )
        with pytest.raises(DBAPIError) as denied, connection.begin_nested():
            _v2_command(
                connection,
                "create_followup_claim",
                source,
                actor=staff,
                expected=expected,
                **_v2_booking(source, 21),
                exception_reason="Cannot bypass Doctor",
            )
        assert denied.value.orig.sqlstate == "23514"
        if restriction == "DEADLINE":
            fresh = _v2_command(
                connection,
                "create_followup_claim",
                source,
                actor=staff,
                expected=expected,
                **_v2_booking(source, 12),
            )
            assert connection.scalar(
                text(
                    "SELECT approved_deadline FROM public.followup_claims WHERE id=:id"
                ),
                {"id": fresh},
            ) == source["day"] + timedelta(days=20)
        else:
            _v2_command(
                connection,
                "record_followup_source_decision",
                source,
                expected=expected,
                kind="DEFAULT",
            )
        connection.execute(text("SET CONSTRAINTS ALL IMMEDIATE"))


@pytest.mark.parametrize("window", [0, 365])
def test_v2_current_policy_boundaries_are_derived_without_doctor_default(
    appointment_storage, window
):
    runtime, _, (tenant, _), (doctor, _) = appointment_storage
    with transaction(runtime, tenant) as connection:
        source = _v2_source(connection, tenant, doctor)
        append_version(
            connection,
            tenant,
            "FOLLOWUP",
            {"completion_day_zero": True, "last_day_inclusive": window},
            1,
            actor_staff_id=str(doctor),
        )
        with pytest.raises(DBAPIError) as stale, connection.begin_nested():
            _v2_command(
                connection,
                "create_followup_claim",
                source,
                **_v2_booking(source, window),
            )
        assert stale.value.orig.sqlstate == "40001"
        claim = _v2_command(
            connection,
            "create_followup_claim",
            source,
            expected={
                "source_revision": 0,
                "clinical_revision": 0,
                "policy_version": 2,
            },
            **_v2_booking(source, window),
        )
        assert connection.scalar(
            text("SELECT approved_deadline FROM public.followup_claims WHERE id=:id"),
            {"id": claim},
        ) == source["day"] + timedelta(days=window)
        assert (
            connection.scalar(
                text(
                    "SELECT count(*) FROM public.followup_source_decision_events WHERE source_visit_id=:visit"
                ),
                {"visit": source["visit"]},
            )
            == 0
        )


@pytest.mark.parametrize(
    ("window", "eligible_offset", "ineligible_offset"),
    [(10, 10, 11), (2, 2, 10), (0, 0, 1), ("7.00", 7, 8)],
)
def test_current_followup_qualification_uses_live_policy_window(
    appointment_storage, window, eligible_offset, ineligible_offset
):
    runtime, _, (tenant, _), (doctor, _) = appointment_storage
    with transaction(runtime, tenant) as connection:
        source = _v2_source(connection, tenant, doctor)
        with pytest.raises(DBAPIError) as denied, connection.begin_nested():
            connection.execute(text("SELECT set_config('app.staff_id', '', true)"))
            connection.scalar(
                text("SELECT public.b10_operational_source_proof(:tenant, :visit)"),
                {"tenant": tenant, "visit": source["visit"]},
            )
        assert denied.value.orig.sqlstate == "42501"
        connection.execute(
            text("SELECT set_config('app.staff_id', :staff, true)"),
            {"staff": str(doctor)},
        )
        assert connection.scalar(
            text("SELECT public.b10_operational_source_proof(:tenant, :visit)"),
            {"tenant": tenant, "visit": source["visit"]},
        ) == {
            "state": "VERIFIED",
            "decision": None,
            "actor_staff_id": None,
            "actor_role": None,
        }
        payload = (
            '{"completion_day_zero":true,"last_day_inclusive":7.00}'
            if window == "7.00"
            else {"completion_day_zero": True, "last_day_inclusive": window}
        )
        append_version(
            connection, tenant, "FOLLOWUP", payload, 1, actor_staff_id=str(doctor)
        )
        now = datetime.now(UTC) + timedelta(seconds=2)

        def qualification(offset):
            return qualify_followup(
                connection,
                tenant,
                source["patient"],
                source["day"] + timedelta(days=offset),
                source["visit"],
                0,
                now,
                expected_visit_revision=0,
            )

        allowed = qualification(eligible_offset)
        assert (allowed.outcome, allowed.approved_deadline) == (
            "PREFERENTIAL",
            source["day"] + timedelta(days=eligible_offset),
        )
        assert qualification(ineligible_offset).outcome == "FALLBACK"


def test_v2_ordered_new_date_extension_and_direct_terminal_fence(
    appointment_storage,
):
    runtime, migration, (tenant, _), (doctor, _) = appointment_storage
    staff = _receptionist(migration, tenant)
    with transaction(runtime, tenant) as connection:
        source = _v2_source(connection, tenant, doctor)
        with pytest.raises(DBAPIError) as reason, connection.begin_nested():
            _v2_command(
                connection, "create_followup_claim", source, **_v2_booking(source, 10)
            )
        assert reason.value.orig.sqlstate == "23514"
        claim = _v2_command(
            connection,
            "create_followup_claim",
            source,
            actor=staff,
            **_v2_booking(source, 10),
            new_booking_date_exception={
                "selected_service_date": (
                    source["day"] + timedelta(days=10)
                ).isoformat(),
            },
        )
        record = _v2_record(connection, claim)
        frozen = connection.scalar(
            text(
                "SELECT to_jsonb(p) FROM public.appointment_price_snapshots p WHERE appointment_id=:id"
            ),
            {"id": record["id"]},
        )
        assert (
            frozen["entitlement_provenance"]["selected_date"]
            == (source["day"] + timedelta(days=10)).isoformat()
        )
        assert frozen["entitlement_provenance"]["new_date_event_id"]
        expected = {
            "source_revision": 0,
            "clinical_revision": 0,
            "policy_version": 1,
            "claim_revision": 1,
            "appointment_revision": 0,
            "authorization_revision": 1,
        }
        for extension in (
            {
                "deadline_date": (source["day"] + timedelta(days=9)).isoformat(),
                "reason": "Shortens",
            },
            {
                "deadline_date": (source["day"] + timedelta(days=12)).isoformat(),
                "reason": " ",
            },
        ):
            with pytest.raises(DBAPIError) as denied, connection.begin_nested():
                _v2_command(
                    connection,
                    "extend_followup_claim",
                    source,
                    actor=staff,
                    reference=claim,
                    expected=expected,
                    **extension,
                )
            assert denied.value.orig.sqlstate == "23514"
        assert (
            _v2_command(
                connection,
                "extend_followup_claim",
                source,
                actor=staff,
                reference=claim,
                expected=expected,
                deadline_date=(source["day"] + timedelta(days=12)).isoformat(),
                reason="Synthetic confirmed extension",
            )
            == 2
        )
        connection.execute(text("SET CONSTRAINTS ALL IMMEDIATE"))
        assert (
            connection.scalar(
                text(
                    "SELECT to_jsonb(p) FROM public.appointment_price_snapshots p WHERE appointment_id=:id"
                ),
                {"id": record["id"]},
            )
            == frozen
        )
        connection.execute(text("SET CONSTRAINTS ALL DEFERRED"))
        with pytest.raises(DBAPIError) as terminal, connection.begin_nested():
            _outcome(connection, tenant, doctor, record, status="Cancelled")
        assert terminal.value.orig.sqlstate == "42501"
        assert connection.execute(
            text("SELECT state, revision FROM public.followup_claims WHERE id=:claim"),
            {"claim": claim},
        ).one() == ("ACTIVE", 1)
        assert (
            connection.scalar(
                text(
                    "SELECT count(*) FROM public.followup_authorization_events WHERE claim_id=:claim"
                ),
                {"claim": claim},
            )
            == 2
        )
        with pytest.raises(DBAPIError) as competing, connection.begin_nested():
            _v2_command(
                connection, "create_followup_claim", source, **_v2_booking(source, 2)
            )
        assert competing.value.orig.sqlstate == "23505"


@pytest.mark.parametrize(
    "bad_actor", ["foreign", "disabled", "missing", "forged_expected"]
)
def test_v2_writer_rejects_invalid_context_without_evidence(
    appointment_storage, bad_actor
):
    runtime, migration, (tenant, _), (doctor, foreign_doctor) = appointment_storage
    staff = _receptionist(migration, tenant)
    if bad_actor == "disabled":
        with migration.begin() as connection:
            connection.execute(
                text(
                    "UPDATE public.staff_accounts SET enabled=false, security_revision=security_revision+1 WHERE id=:id"
                ),
                {"id": staff},
            )
    with transaction(runtime, tenant) as connection:
        source = _v2_source(connection, tenant, doctor)
        actor = {
            "foreign": foreign_doctor,
            "disabled": staff,
            "missing": "",
            "forged_expected": staff,
        }[bad_actor]
        expected = {"source_revision": 0, "clinical_revision": 0, "policy_version": 1}
        if bad_actor == "forged_expected":
            expected["actor_staff_id"] = str(doctor)
        with pytest.raises(DBAPIError) as denied, connection.begin_nested():
            _v2_command(
                connection,
                "record_followup_source_decision",
                source,
                actor=actor,
                expected=expected,
                kind="DEFAULT",
            )
        assert denied.value.orig.sqlstate == "42501"
        assert (
            connection.scalar(
                text(
                    "SELECT count(*) FROM public.followup_source_decision_events WHERE source_visit_id=:id"
                ),
                {"id": source["visit"]},
            )
            == 0
        )


def test_v2_effective_privileges_rls_reserved_audit_and_legacy_fences(
    appointment_storage, role_engine
):
    runtime, migration, (tenant, other), (doctor, _) = appointment_storage
    relations = (
        "followup_source_state",
        "followup_source_decision_events",
        "followup_authorization_events",
    )
    with transaction(runtime, tenant) as connection:
        source = _v2_source(connection, tenant, doctor)
        _v2_command(
            connection, "record_followup_source_decision", source, kind="DEFAULT"
        )
        for sql in (
            "UPDATE public.visits SET followup_decision='{}'::jsonb WHERE id=:visit",
            "INSERT INTO public.followup_source_decision_events DEFAULT VALUES",
            "INSERT INTO public.followup_authorization_events DEFAULT VALUES",
            "INSERT INTO public.followup_claims DEFAULT VALUES",
            "INSERT INTO public.followup_extensions DEFAULT VALUES",
            "TRUNCATE public.followup_source_decision_events",
            "SELECT public.d08_context(:tenant,:visit,'{}'::jsonb)",
            "INSERT INTO public.sensitive_audit(id,organization_id,actor_staff_id,actor_role,action,correlation_id) VALUES(gen_random_uuid(),:tenant,:doctor,'DOCTOR_OWNER','FOLLOWUP_EXTENSION','forged')",
        ):
            with pytest.raises(DBAPIError) as denied, connection.begin_nested():
                connection.execute(
                    text(sql),
                    {"tenant": tenant, "visit": source["visit"], "doctor": doctor},
                )
            assert denied.value.orig.sqlstate == "42501"
        # Permitted Doctor narrative correction still uses the original clinical guard.
        connection.execute(
            text("SELECT set_config('app.clinical_reason','Synthetic correction',true)")
        )
        connection.execute(
            text(
                "UPDATE public.visits SET diagnosis='Synthetic correction' WHERE id=:visit"
            ),
            {"visit": source["visit"]},
        )
    for scope in (other, None):
        with transaction(runtime, scope) as connection:
            for relation in relations:
                assert (
                    connection.scalar(text(f"SELECT count(*) FROM public.{relation}"))
                    == 0
                )
    with migration.connect() as connection:
        for relation in relations:
            assert connection.execute(
                text(
                    "SELECT relrowsecurity,relforcerowsecurity FROM pg_class WHERE oid=CAST(:relation AS regclass)"
                ),
                {"relation": f"public.{relation}"},
            ).one() == (True, True)
            for privilege in ("INSERT", "UPDATE", "DELETE", "TRUNCATE"):
                assert not connection.scalar(
                    text(
                        "SELECT has_table_privilege('carepulse_runtime',:relation,:privilege)"
                    ),
                    {"relation": f"public.{relation}", "privilege": privilege},
                )
                if privilege in ("INSERT", "UPDATE"):
                    assert not connection.scalar(
                        text(
                            "SELECT has_any_column_privilege('carepulse_runtime',:relation,:privilege)"
                        ),
                        {"relation": f"public.{relation}", "privilege": privilege},
                    )
        functions = connection.execute(
            text("""
            SELECT p.proname, pg_get_userbyid(p.proowner), p.prosecdef, p.proconfig,
                   has_function_privilege('carepulse_operator',p.oid,'EXECUTE')
            FROM pg_proc p WHERE p.proname IN ('create_followup_claim','extend_followup_claim','record_followup_source_decision')
        """)
        ).all()
        assert len(functions) == 3
        assert all(
            row[1] == "carepulse_migration"
            and row[2]
            and row[3] == ["search_path=pg_catalog"]
            and not row[4]
            for row in functions
        )
        assert connection.execute(
            text("""
            SELECT rolsuper,rolbypassrls,rolcreaterole,
                   has_schema_privilege('carepulse_runtime','public','CREATE'),
                   pg_has_role('carepulse_runtime','carepulse_migration','MEMBER')
            FROM pg_roles WHERE rolname='carepulse_runtime'
        """)
        ).one() == (False, False, False, False, False)
        assert (
            connection.scalar(
                text("""
            SELECT count(*) FROM pg_proc p,
                LATERAL aclexplode(COALESCE(p.proacl,acldefault('f',p.proowner))) permission
            WHERE p.proname IN ('create_followup_claim','extend_followup_claim','record_followup_source_decision')
              AND permission.grantee=0 AND permission.privilege_type='EXECUTE'
        """)
            )
            == 0
        )
    for role in ("carepulse_operator", "carepulse_auth", "carepulse_sender"):
        denied_role = role_engine(role)
        try:
            for relation in relations:
                with (
                    pytest.raises(DBAPIError) as denied,
                    denied_role.connect() as connection,
                ):
                    connection.execute(text(f"SELECT * FROM public.{relation}"))
                assert denied.value.orig.sqlstate == "42501"
        finally:
            denied_role.dispose()


def test_runtime_direct_sql_can_spoof_tenant_and_doctor_context(appointment_storage):
    """Issue #26: GUC-based DB checks do not authenticate an arbitrary SQL caller."""
    runtime, migration, (tenant, other), (doctor, foreign_doctor) = appointment_storage
    receptionist = _receptionist(migration, tenant)
    with transaction(runtime, tenant) as connection:
        source = _v2_source(connection, tenant, doctor)
        _v2_command(
            connection,
            "record_followup_source_decision",
            source,
            kind="OPTED_OUT",
            reason="Doctor restriction",
        )
        assert (
            connection.scalar(
                text("SELECT count(*) FROM public.staff_accounts WHERE id=:id"),
                {"id": foreign_doctor},
            )
            == 0
        )
        with connection.begin_nested() as spoofed_sql:
            connection.execute(
                text("SELECT set_config('app.organization_id', :org, true)"),
                {"org": str(other)},
            )
            assert (
                connection.scalar(
                    text("SELECT count(*) FROM public.staff_accounts WHERE id=:id"),
                    {"id": foreign_doctor},
                )
                == 1
            )
            connection.execute(
                text("SELECT set_config('app.organization_id', :org, true)"),
                {"org": str(tenant)},
            )
            connection.execute(
                text("SELECT set_config('app.staff_id', :actor, true)"),
                {"actor": str(receptionist)},
            )
            with pytest.raises(DBAPIError) as denied, connection.begin_nested():
                connection.execute(
                    text("""
                        SELECT public.record_followup_source_decision(
                            :tenant, :visit, CAST(:expected AS jsonb), '{"kind":"DEFAULT"}'::jsonb)
                    """),
                    {
                        "tenant": tenant,
                        "visit": source["visit"],
                        "expected": '{"source_revision":1,"clinical_revision":0,"policy_version":1}',
                    },
                )
            assert denied.value.orig.sqlstate == "42501"
            connection.execute(
                text("SELECT set_config('app.staff_id', :actor, true)"),
                {"actor": str(doctor)},
            )
            assert (
                connection.scalar(
                    text("""
                    SELECT public.record_followup_source_decision(
                        :tenant, :visit, CAST(:expected AS jsonb), CAST(:decision AS jsonb))
                """),
                    {
                        "tenant": tenant,
                        "visit": source["visit"],
                        "expected": '{"source_revision":1,"clinical_revision":0,"policy_version":1}',
                        "decision": '{"kind":"DEFAULT"}',
                    },
                )
                == 2
            )
            assert connection.execute(
                text("""
                    SELECT actor_staff_id, actor_role FROM public.followup_source_decision_events
                    WHERE source_visit_id=:visit AND source_revision=2
                """),
                {"visit": source["visit"]},
            ).one() == (doctor, "DOCTOR_OWNER")
            spoofed_sql.rollback()
        assert (
            connection.scalar(
                text(
                    "SELECT count(*) FROM public.followup_source_decision_events WHERE source_visit_id=:visit"
                ),
                {"visit": source["visit"]},
            )
            == 1
        )


def test_v2_creation_exception_cannot_authorize_same_date_interval_change(
    appointment_storage,
):
    runtime, migration, (tenant, _), (doctor, _) = appointment_storage
    staff = _receptionist(migration, tenant)
    with transaction(runtime, tenant) as connection:
        source = _v2_source(connection, tenant, doctor)
        claim = _v2_command(
            connection,
            "create_followup_claim",
            source,
            actor=staff,
            **_v2_booking(source, 10),
            new_booking_date_exception={
                "selected_service_date": (
                    source["day"] + timedelta(days=10)
                ).isoformat(),
            },
        )
        record = _v2_record(connection, claim)
        starts = datetime.combine(
            source["day"] + timedelta(days=10), datetime.min.time(), UTC
        ) + timedelta(hours=12)
        with pytest.raises(DBAPIError) as denied, connection.begin_nested():
            connection.execute(
                text("""
                UPDATE public.appointments SET booking_mode='EXACT_TIME',starts_at=:starts,
                    ends_at=:ends,revision=revision+1 WHERE id=:id
            """),
                {
                    "id": record["id"],
                    "starts": starts,
                    "ends": starts + timedelta(hours=1),
                },
            )
        assert denied.value.orig.sqlstate == "42501"
        with pytest.raises(DBAPIError) as late, connection.begin_nested():
            _v2_command(
                connection,
                "extend_followup_claim",
                source,
                reference=claim,
                expected={
                    "source_revision": 0,
                    "clinical_revision": 0,
                    "policy_version": 1,
                    "claim_revision": 1,
                    "appointment_revision": 0,
                    "authorization_revision": 1,
                },
                kind="NEW_DATE",
                deadline_date=(source["day"] + timedelta(days=12)).isoformat(),
                reason="Late attachment",
            )
        assert late.value.orig.sqlstate == "23514"


def test_v2_declared_new_date_without_event_rolls_back_all_creation_evidence(
    provenance_followup_storage,
):
    runtime, migration, (tenant, _), (doctor, _) = provenance_followup_storage
    with transaction(runtime, tenant) as connection:
        source = _v2_source(connection, tenant, doctor)
        ordinary = _v2_command(
            connection, "create_followup_claim", source, **_v2_booking(source)
        )
        snapshot = connection.scalar(
            text("SELECT policy_snapshot FROM public.followup_claims WHERE id=:id"),
            {"id": ordinary},
        )
        _outcome(
            connection,
            tenant,
            doctor,
            _v2_record(connection, ordinary),
            status="Cancelled",
        )
    with transaction(migration, tenant) as connection:
        tables = (
            "appointments",
            "appointment_history",
            "appointment_price_snapshots",
            "appointment_day_counters",
            "followup_claims",
            "sensitive_audit",
            "idempotency_receipts",
            "outbox_events",
        )
        counts = [
            connection.scalar(
                text(
                    f"SELECT count(*) FROM public.{table} WHERE organization_id=:tenant"
                ),
                {"tenant": tenant},
            )
            for table in tables
        ]
        with pytest.raises(DBAPIError) as missing, connection.begin_nested():
            _claim(
                connection,
                tenant,
                source,
                day_offset=10,
                provenance={
                    **snapshot,
                    "selected_date": (source["day"] + timedelta(days=10)).isoformat(),
                    "new_date_event_id": str(uuid4()),
                },
            )
            connection.execute(text("SET CONSTRAINTS ALL IMMEDIATE"))
        assert missing.value.orig.sqlstate == "23514"
        assert [
            connection.scalar(
                text(
                    f"SELECT count(*) FROM public.{table} WHERE organization_id=:tenant"
                ),
                {"tenant": tenant},
            )
            for table in tables
        ] == counts


@pytest.mark.parametrize(
    "tamper",
    ["head_only", "event_only", "skip", "before", "rewind", "delete", "truncate"],
)
def test_v2_source_requires_contiguous_matching_head_and_immutable_events(
    appointment_storage, tamper
):
    runtime, migration, (tenant, _), (doctor, _) = appointment_storage
    with transaction(runtime, tenant) as connection:
        source = _v2_source(connection, tenant, doctor)
        _v2_command(
            connection, "record_followup_source_decision", source, kind="DEFAULT"
        )
    with transaction(migration, tenant) as connection:
        with pytest.raises(DBAPIError) as invalid, connection.begin_nested():
            if tamper in ("head_only", "rewind"):
                connection.execute(
                    text(
                        "UPDATE public.followup_source_state SET source_revision=:revision WHERE visit_id=:visit"
                    ),
                    {
                        "visit": source["visit"],
                        "revision": 2 if tamper == "head_only" else 0,
                    },
                )
            elif tamper in ("delete", "truncate"):
                connection.execute(
                    text(
                        "DELETE FROM public.followup_source_decision_events WHERE source_visit_id=:visit"
                        if tamper == "delete"
                        else "TRUNCATE public.followup_source_decision_events"
                    ),
                    {"visit": source["visit"]},
                )
            else:
                event, audit = uuid4(), uuid4()
                connection.execute(
                    text("""
                    INSERT INTO public.sensitive_audit (
                        id,organization_id,actor_staff_id,actor_role,action,reason,
                        correlation_id,occurred_at,before_state,after_state)
                    SELECT :audit,organization_id,decision_actor_staff_id,decision_actor_role,
                        'FOLLOWUP_SOURCE_DECISION',NULL,CAST(:event AS text),date_trunc('second',clock_timestamp()),
                        jsonb_build_object('source_visit_id',visit_id,'source_revision',1,
                            'decision',decision,'actor_staff_id',decision_actor_staff_id,'actor_role',decision_actor_role),
                        jsonb_build_object('source_visit_id',visit_id,'source_revision',2,
                            'decision',decision,'actor_staff_id',decision_actor_staff_id,'actor_role',decision_actor_role)
                    FROM public.followup_source_state WHERE visit_id=:visit
                """),
                    {"audit": audit, "event": event, "visit": source["visit"]},
                )
                connection.execute(
                    text("""
                    INSERT INTO public.followup_source_decision_events
                    SELECT (jsonb_populate_record(NULL::public.followup_source_decision_events,
                        to_jsonb(e) || jsonb_build_object('id',CAST(:event AS text),
                            'sensitive_audit_id',CAST(:audit AS text),'source_revision',:revision,
                            'before_decision',CASE WHEN :bad_before THEN NULL ELSE e.decision END,
                            'before_actor_staff_id',e.actor_staff_id,'before_actor_role',e.actor_role,
                            'occurred_at',(SELECT occurred_at FROM public.sensitive_audit WHERE id=:audit)))).*
                    FROM public.followup_source_decision_events e
                    WHERE e.source_visit_id=:visit AND e.source_revision=1
                """),
                    {
                        "audit": audit,
                        "event": event,
                        "visit": source["visit"],
                        "revision": 3 if tamper == "skip" else 2,
                        "bad_before": tamper == "before",
                    },
                )
            connection.execute(text("SET CONSTRAINTS ALL IMMEDIATE"))
        assert invalid.value.orig.sqlstate == (
            "42501" if tamper == "delete" else "23514"
        )
        assert (
            connection.scalar(
                text(
                    "SELECT source_revision FROM public.followup_source_state WHERE visit_id=:visit"
                ),
                {"visit": source["visit"]},
            )
            == 1
        )


def _source(connection, tenant, doctor, *, classification="ROUTINE", complete=True):
    """Create a completed source; preserve Doctor decisions on legacy predecessors."""
    patient = _patient(connection, tenant)
    saved = persist_appointment(
        connection,
        _draft(
            patient,
            doctor,
            classification=classification,
            lane="EMERGENCY" if classification == "EMERGENCY" else "MAIN",
            price_snapshot=(
                {"base_egp": "120.00", "net_egp": "120.00"}
                if classification == "URGENT"
                else {}
            ),
        ),
        _query(tenant, doctor),
    )
    _admit_saved(connection, tenant, doctor, saved, complete=complete)
    appointment = UUID(saved["id"])
    visit = connection.scalar(
        text("SELECT id FROM public.visits WHERE appointment_id=:id"),
        {"id": appointment},
    )
    if (
        connection.scalar(
            text("""
            SELECT to_regprocedure(
                'public.record_followup_source_decision(uuid,uuid,jsonb,jsonb)')
        """)
        )
        is None
    ):
        connection.execute(
            text(
                "SELECT set_config('app.clinical_change_kind', 'DECISION_DEFAULT', true)"
            )
        )
        connection.execute(text("SELECT set_config('app.clinical_reason', '', true)"))
        connection.execute(
            text("""
                UPDATE public.visits SET followup_decision=CAST(:decision AS jsonb)
                WHERE id=:visit
            """),
            {"decision": '{"kind":"DEFAULT","eligible":true}', "visit": visit},
        )
    completion_day = connection.scalar(
        text("""
            SELECT (h.occurred_at AT TIME ZONE 'Africa/Cairo')::date
            FROM public.appointment_history h
            WHERE h.appointment_id=:id AND h.new_status='Completed'
        """),
        {"id": appointment},
    )
    if completion_day is None:
        completion_day = connection.scalar(
            text("SELECT (clock_timestamp() AT TIME ZONE 'Africa/Cairo')::date")
        )
    return {
        "patient": patient,
        "doctor": doctor,
        "appointment": appointment,
        "visit": visit,
        "day": completion_day,
    }


def _provenance(source, *, version=1):
    return {
        "source_visit_id": str(source["visit"]),
        "source_revision": 1,
        "doctor_staff_id": str(source["doctor"]),
        "actor_staff_id": str(source["doctor"]),
        "authorized": True,
        "policy_revision": version,
    }


def _insert_claim(
    connection,
    tenant,
    source,
    appointment,
    provenance,
    *,
    patient=None,
    doctor=None,
    source_visit=None,
    source_appointment=None,
    state="ACTIVE",
    completion_day=None,
    deadline=None,
):
    claim_id = uuid4()
    connection.execute(
        text("""
            INSERT INTO public.followup_claims
                (id, organization_id, appointment_id, patient_id, doctor_staff_id,
                 source_visit_id, source_appointment_id, policy_snapshot,
                 completion_local_date, approved_deadline, state)
            VALUES (:id, :tenant, :appointment, :patient, :doctor, :visit,
                    :source_appointment, CAST(:policy AS jsonb), :completed,
                    :deadline, :state)
        """),
        {
            "id": claim_id,
            "tenant": tenant,
            "appointment": appointment,
            "patient": patient or source["patient"],
            "doctor": doctor or source["doctor"],
            "visit": source_visit or source["visit"],
            "source_appointment": source_appointment or source["appointment"],
            "policy": json.dumps(provenance),
            "completed": completion_day or source["day"],
            "deadline": deadline or source["day"] + timedelta(days=7),
            "state": state,
        },
    )
    return claim_id


def _claim(
    connection,
    tenant,
    source,
    *,
    day_offset=1,
    patient=None,
    doctor=None,
    provenance=None,
    price="100.00",
    source_visit=None,
    source_appointment=None,
    completion_day=None,
    deadline=None,
):
    """Confirm an actual D-05 priced Appointment and its D-08 claim atomically."""
    patient = patient or source["patient"]
    doctor = doctor or source["doctor"]
    provenance = provenance or _provenance(source)
    saved = persist_appointment(
        connection,
        _draft(
            patient,
            doctor,
            service_date=source["day"] + timedelta(days=day_offset),
            service_type="FOLLOW_UP",
            price_snapshot={
                "base_egp": price,
                "net_egp": price,
                "entitlement_provenance": provenance,
            },
        ),
        _query(tenant, doctor),
    )
    claim_id = _insert_claim(
        connection,
        tenant,
        source,
        UUID(saved["id"]),
        provenance,
        patient=patient,
        doctor=doctor,
        source_visit=source_visit,
        source_appointment=source_appointment,
        completion_day=completion_day,
        deadline=deadline,
    )
    return {"id": UUID(saved["id"]), "saved": saved, "claim": claim_id}


def _outcome(
    connection,
    tenant,
    doctor,
    record,
    *,
    status,
    after_admission=False,
    update_claim=True,
    pre_admitted=False,
):
    """Write a real terminal history; claim transition shares the transaction."""
    if after_admission or (status == "Completed" and not pre_admitted):
        _admit_saved(
            connection,
            tenant,
            doctor,
            record["saved"],
            complete=status == "Completed",
        )
    previous = (
        "In Examination" if (after_admission or status == "Completed") else "Booked"
    )
    protected_completion = (
        status == "Completed"
        and pre_admitted
        and connection.scalar(
            text(
                "SELECT to_regprocedure('public.complete_examined_visit(uuid,uuid,bigint,boolean)') IS NOT NULL"
            )
        )
    )
    if protected_completion:
        connection.execute(
            text("SELECT set_config('app.staff_id', :actor, true)"),
            {"actor": str(doctor)},
        )
        connection.execute(
            text("""
                SELECT * FROM public.complete_examined_visit(:tenant, :id, 2, true)
            """),
            {"tenant": tenant, "id": record["id"]},
        ).one()
    if status == "Completed" and (not pre_admitted or protected_completion):
        revision = 3
    else:
        revision = 3 if (after_admission or pre_admitted) else 1
        connection.execute(
            text("""
                UPDATE public.appointments
                SET status=:status, revision=:revision
                WHERE organization_id=:tenant AND id=:id
            """),
            {
                "status": status,
                "revision": revision,
                "tenant": tenant,
                "id": record["id"],
            },
        )
        connection.execute(
            text("""
                INSERT INTO public.appointment_history
                    (id, organization_id, appointment_id, appointment_revision,
                     action, old_status, new_status, old_service_date, new_service_date,
                     old_number, new_number, actor_staff_id, actor_role,
                     reason, price_snapshot_appointment_id)
                SELECT :history, organization_id, id, :revision, :action,
                       :previous, :status, service_date, service_date,
                       current_number, current_number, :doctor, 'DOCTOR_OWNER',
                       'Synthetic D-08 outcome', id
                FROM public.appointments WHERE organization_id=:tenant AND id=:id
            """),
            {
                "history": uuid4(),
                "revision": revision,
                "action": "DOCTOR_EXCEPTION" if after_admission else "STATUS",
                "previous": previous,
                "status": status,
                "doctor": doctor,
                "tenant": tenant,
                "id": record["id"],
            },
        )
    terminal_at = connection.scalar(
        text("""
            SELECT occurred_at FROM public.appointment_history
            WHERE organization_id=:tenant AND appointment_id=:id
              AND appointment_revision=:revision
        """),
        {"tenant": tenant, "id": record["id"], "revision": revision},
    )
    if update_claim:
        connection.execute(
            text("""
                UPDATE public.followup_claims
                SET state=:state, revision=revision+1,
                    terminal_history_revision=:revision, terminal_at=:at
                WHERE organization_id=:tenant AND id=:claim
            """),
            {
                "state": "CONSUMED" if status == "Completed" else "RELEASED",
                "revision": revision,
                "at": terminal_at,
                "tenant": tenant,
                "claim": record["claim"],
            },
        )
    return terminal_at, revision


@pytest.mark.parametrize("classification", ["ROUTINE", "URGENT", "EMERGENCY"])
def test_completed_new_examination_sources_create_active_claim(
    legacy_followup_storage, classification
):
    runtime, _, (tenant, _), (doctor, _) = legacy_followup_storage
    with transaction(runtime, tenant) as connection:
        source = _source(connection, tenant, doctor, classification=classification)
        record = _claim(connection, tenant, source)
        stored = connection.execute(
            text("""
                SELECT c.state, c.revision, c.patient_id, c.doctor_staff_id,
                       c.source_visit_id, c.source_appointment_id,
                       c.policy_snapshot, c.completion_local_date, c.approved_deadline,
                       a.service_type, a.classification, p.entitlement_provenance
                FROM public.followup_claims c
                JOIN public.appointments a ON (a.organization_id, a.id) =
                     (c.organization_id, c.appointment_id)
                JOIN public.appointment_price_snapshots p
                  ON (p.organization_id, p.appointment_id) =
                     (c.organization_id, c.appointment_id)
                WHERE c.id=:claim
            """),
            {"claim": record["claim"]},
        ).one()
        assert stored[:6] == (
            "ACTIVE",
            1,
            source["patient"],
            doctor,
            source["visit"],
            source["appointment"],
        )
        assert stored[6] == _provenance(source)
        assert stored[7:11] == (
            source["day"],
            source["day"] + timedelta(days=7),
            "FOLLOW_UP",
            "ROUTINE",
        )
        assert stored[11] == stored[6]
    with transaction(runtime, tenant) as connection:
        assert (
            connection.scalar(
                text("SELECT count(*) FROM public.followup_claims WHERE id=:id"),
                {"id": record["claim"]},
            )
            == 1
        )


@pytest.mark.parametrize(
    "bad_source", ["uncompleted", "exceptional_cancel", "followup"]
)
def test_nonqualifying_source_cannot_claim(legacy_followup_storage, bad_source):
    runtime, _, (tenant, _), (doctor, _) = legacy_followup_storage
    with transaction(runtime, tenant) as connection:
        if bad_source == "followup":
            original = _source(connection, tenant, doctor)
            prior = _claim(connection, tenant, original)
            _outcome(connection, tenant, doctor, prior, status="Completed")
            visit_id = connection.scalar(
                text("SELECT id FROM public.visits WHERE appointment_id=:id"),
                {"id": prior["id"]},
            )
            source = {
                "patient": original["patient"],
                "doctor": doctor,
                "appointment": prior["id"],
                "visit": visit_id,
                "day": connection.scalar(
                    text("""
                        SELECT (occurred_at AT TIME ZONE 'Africa/Cairo')::date
                        FROM public.appointment_history
                        WHERE appointment_id=:id AND new_status='Completed'
                    """),
                    {"id": prior["id"]},
                ),
            }
        else:
            source = _source(connection, tenant, doctor, complete=False)
            if bad_source == "exceptional_cancel":
                connection.execute(
                    text("""
                        UPDATE public.appointments
                        SET status='Cancelled', revision=3 WHERE id=:id
                    """),
                    {"id": source["appointment"]},
                )
                connection.execute(
                    text("""
                        INSERT INTO public.appointment_history
                            (id, organization_id, appointment_id, appointment_revision,
                             action, old_status, new_status, old_service_date,
                             new_service_date, old_number, new_number, actor_staff_id,
                             actor_role, reason, price_snapshot_appointment_id)
                        SELECT :history, organization_id, id, 3, 'DOCTOR_EXCEPTION',
                               'In Examination', 'Cancelled', service_date, service_date,
                               current_number, current_number, :doctor, 'DOCTOR_OWNER',
                               'Synthetic exceptional cancellation', id
                        FROM public.appointments WHERE id=:id
                    """),
                    {"history": uuid4(), "doctor": doctor, "id": source["appointment"]},
                )
        with pytest.raises(DBAPIError) as denied, connection.begin_nested():
            _claim(
                connection,
                tenant,
                source,
                day_offset=2 if bad_source == "followup" else 1,
            )
        assert denied.value.orig.sqlstate in ("23514", "23503")
        assert (
            connection.scalar(
                text(
                    "SELECT count(*) FROM public.followup_claims WHERE source_visit_id=:visit"
                ),
                {"visit": source["visit"]},
            )
            == 0
        )


@pytest.mark.parametrize("wrong_relation", ["patient", "doctor", "clinic"])
def test_followup_target_must_match_source_tenant_patient_doctor(
    legacy_followup_storage, wrong_relation
):
    runtime, _, (tenant, other), (doctor, foreign_doctor) = legacy_followup_storage
    # A second same-tenant Owner violates D-02. Use the foreign tenant's
    # existing Doctor to prove that a forged target Doctor cannot be linked.
    second_doctor = foreign_doctor if wrong_relation == "doctor" else None
    with transaction(runtime, tenant) as connection:
        source = _source(connection, tenant, doctor)
    if wrong_relation == "clinic":
        with transaction(runtime, other) as connection:
            foreign_patient = _patient(connection, other)
            with pytest.raises(DBAPIError) as denied, connection.begin_nested():
                _claim(
                    connection,
                    other,
                    source,
                    patient=foreign_patient,
                    doctor=foreign_doctor,
                )
    else:
        with transaction(runtime, tenant) as connection:
            alternate_patient = (
                _patient(connection, tenant) if wrong_relation == "patient" else None
            )
            with pytest.raises(DBAPIError) as denied, connection.begin_nested():
                _claim(
                    connection,
                    tenant,
                    source,
                    patient=alternate_patient,
                    doctor=second_doctor,
                )
    assert denied.value.orig.sqlstate in ("23514", "23503", "42501")


def test_one_active_source_across_different_service_dates(legacy_followup_storage):
    runtime, _, (tenant, _), (doctor, _) = legacy_followup_storage
    with transaction(runtime, tenant) as connection:
        source = _source(connection, tenant, doctor)
        first = _claim(connection, tenant, source)
        with pytest.raises(DBAPIError) as denied, connection.begin_nested():
            _claim(connection, tenant, source, day_offset=2)
        assert denied.value.orig.sqlstate == "23505"
        assert connection.execute(
            text("""
                SELECT id, state FROM public.followup_claims
                WHERE source_visit_id=:visit
            """),
            {"visit": source["visit"]},
        ).all() == [(first["claim"], "ACTIVE")]


@pytest.mark.parametrize("tamper", ["actor", "approval", "policy_revision"])
def test_claim_cannot_forge_frozen_authorization_evidence(
    legacy_followup_storage, tamper
):
    runtime, _, (tenant, _), (doctor, _) = legacy_followup_storage
    with transaction(runtime, tenant) as connection:
        source = _source(connection, tenant, doctor)
        provenance = _provenance(source)
        if tamper == "actor":
            provenance["actor_staff_id"] = str(uuid4())
        elif tamper == "approval":
            provenance["authorized"] = False
        else:
            provenance["policy_revision"] = "unapproved"
        with pytest.raises(DBAPIError) as denied, connection.begin_nested():
            _claim(connection, tenant, source, provenance=provenance)
        assert denied.value.orig.sqlstate == "23514"
        assert (
            connection.scalar(
                text(
                    "SELECT count(*) FROM public.followup_claims WHERE source_visit_id=:id"
                ),
                {"id": source["visit"]},
            )
            == 0
        )


def test_consumed_source_cannot_regain_active_or_second_consumed_claim(
    legacy_followup_storage,
):
    runtime, _, (tenant, _), (doctor, _) = legacy_followup_storage
    with transaction(runtime, tenant) as connection:
        source = _source(connection, tenant, doctor)
        first = _claim(connection, tenant, source)
        _outcome(connection, tenant, doctor, first, status="Completed")
    with transaction(runtime, tenant) as connection:
        with pytest.raises(DBAPIError) as denied, connection.begin_nested():
            _claim(connection, tenant, source, day_offset=2)
        assert denied.value.orig.sqlstate == "23505"
        assert connection.execute(
            text("""
                SELECT state, count(*) FROM public.followup_claims
                WHERE source_visit_id=:visit GROUP BY state
            """),
            {"visit": source["visit"]},
        ).all() == [("CONSUMED", 1)]


def test_released_prior_claim_cannot_become_second_consumption(legacy_followup_storage):
    runtime, _, (tenant, _), (doctor, _) = legacy_followup_storage
    with transaction(runtime, tenant) as connection:
        source = _source(connection, tenant, doctor)
        first = _claim(connection, tenant, source)
        _outcome(connection, tenant, doctor, first, status="Cancelled")
        second = _claim(connection, tenant, source, day_offset=2)
        completed_at, completed_revision = _outcome(
            connection, tenant, doctor, second, status="Completed"
        )
        with pytest.raises(DBAPIError) as denied, connection.begin_nested():
            connection.execute(
                text("""
                    UPDATE public.followup_claims
                    SET state='CONSUMED', revision=3,
                        terminal_history_revision=:revision, terminal_at=:at
                    WHERE id=:id
                """),
                {
                    "revision": completed_revision,
                    "at": completed_at,
                    "id": first["claim"],
                },
            )
        assert denied.value.orig.sqlstate == "23514"
        assert connection.execute(
            text("""
                SELECT state, count(*) FROM public.followup_claims
                WHERE source_visit_id=:visit GROUP BY state ORDER BY state
            """),
            {"visit": source["visit"]},
        ).all() == [("CONSUMED", 1), ("RELEASED", 1)]


@pytest.mark.parametrize("failure", ["missing_decision", "stale_source_revision"])
def test_source_authorization_and_revision_required(legacy_followup_storage, failure):
    runtime, _, (tenant, _), (doctor, _) = legacy_followup_storage
    with transaction(runtime, tenant) as connection:
        source = _source(connection, tenant, doctor)
        provenance = _provenance(source)
        if failure == "missing_decision":
            connection.execute(
                text(
                    "SELECT set_config('app.clinical_change_kind', 'CORRECTION', true)"
                )
            )
            connection.execute(
                text(
                    "SELECT set_config('app.clinical_reason', 'Revoked approval', true)"
                )
            )
            connection.execute(
                text("UPDATE public.visits SET followup_decision=NULL WHERE id=:visit"),
                {"visit": source["visit"]},
            )
            provenance["source_revision"] = 2
        else:
            provenance["source_revision"] = 0
        with pytest.raises(DBAPIError) as denied, connection.begin_nested():
            _claim(connection, tenant, source, provenance=provenance)
        assert denied.value.orig.sqlstate == "23514"
        assert (
            connection.scalar(
                text(
                    "SELECT count(*) FROM public.followup_claims WHERE source_visit_id=:visit"
                ),
                {"visit": source["visit"]},
            )
            == 0
        )


def test_released_history_allows_fresh_eligibility_and_price_snapshot(
    legacy_followup_storage,
):
    runtime, _, (tenant, _), (doctor, _) = legacy_followup_storage
    with transaction(runtime, tenant) as connection:
        source = _source(connection, tenant, doctor)
        released = _claim(connection, tenant, source, price="100.00")
        _outcome(connection, tenant, doctor, released, status="Cancelled")
        connection.execute(
            text(
                "SELECT set_config('app.clinical_change_kind', 'DECISION_EXCEPTION', true)"
            )
        )
        connection.execute(
            text(
                "SELECT set_config('app.clinical_reason', 'Synthetic reviewed exception', true)"
            )
        )
        connection.execute(
            text("""
                UPDATE public.visits
                SET followup_decision=CAST(:decision AS jsonb)
                WHERE id=:visit
            """),
            {
                "decision": json.dumps(
                    {
                        "kind": "DEADLINE",
                        "deadline_date": (
                            source["day"] + timedelta(days=7)
                        ).isoformat(),
                        "reason": "Synthetic reviewed exception",
                    }
                ),
                "visit": source["visit"],
            },
        )
        assert (
            append_version(
                connection,
                tenant,
                "FOLLOWUP",
                {"completion_day_zero": True, "last_day_inclusive": 7},
                1,
                actor_staff_id=str(doctor),
            )
            == 2
        )
        fresh_provenance = {**_provenance(source, version=2), "source_revision": 2}
        with pytest.raises(DBAPIError) as stale_policy, connection.begin_nested():
            _claim(
                connection,
                tenant,
                source,
                day_offset=2,
                provenance={**fresh_provenance, "policy_revision": 1},
                price="125.00",
            )
        assert stale_policy.value.orig.sqlstate == "23514"
        fresh = _claim(
            connection,
            tenant,
            source,
            day_offset=2,
            provenance=fresh_provenance,
            price="125.00",
        )
        rows = connection.execute(
            text("""
                SELECT c.id, c.state, c.policy_snapshot, s.net_egp
                FROM public.followup_claims c
                JOIN public.appointment_price_snapshots s
                  ON (s.organization_id, s.appointment_id) =
                     (c.organization_id, c.appointment_id)
                WHERE c.source_visit_id=:visit ORDER BY c.revision DESC, c.id
            """),
            {"visit": source["visit"]},
        ).all()
        assert len(rows) == 2
        assert {row.id: row.state for row in rows} == {
            released["claim"]: "RELEASED",
            fresh["claim"]: "ACTIVE",
        }
        assert next(row.net_egp for row in rows if row.id == released["claim"]) == 100
        assert next(row.net_egp for row in rows if row.id == fresh["claim"]) == 125
        assert (
            next(row.policy_snapshot for row in rows if row.id == fresh["claim"])
            == fresh_provenance
        )


def test_default_claim_uses_live_followup_window_and_rebook_freezes_new_policy(
    legacy_followup_storage,
):
    runtime, _, (tenant, _), (doctor, _) = legacy_followup_storage
    with transaction(runtime, tenant) as connection:
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
        source = _source(connection, tenant, doctor)
        original_provenance = _provenance(source, version=2)
        # Even a current ten-day deadline cannot use a superseded policy head.
        with pytest.raises(DBAPIError) as mismatched, connection.begin_nested():
            _claim(
                connection,
                tenant,
                source,
                day_offset=10,
                deadline=source["day"] + timedelta(days=10),
                provenance=_provenance(source, version=1),
            )
        assert mismatched.value.orig.sqlstate == "23514"
        # A forged deadline cannot authorize day eleven under the ten-day policy.
        with pytest.raises(DBAPIError) as denied, connection.begin_nested():
            _claim(
                connection,
                tenant,
                source,
                day_offset=11,
                deadline=source["day"] + timedelta(days=11),
                provenance=original_provenance,
            )
        assert denied.value.orig.sqlstate == "23514"
        assert (
            connection.scalar(
                text(
                    "SELECT count(*) FROM public.followup_claims WHERE source_visit_id=:visit"
                ),
                {"visit": source["visit"]},
            )
            == 0
        )

        original = _claim(
            connection,
            tenant,
            source,
            day_offset=10,
            deadline=source["day"] + timedelta(days=10),
            provenance=original_provenance,
            price="100.00",
        )
        _outcome(connection, tenant, doctor, original, status="Cancelled")

    with transaction(runtime, tenant) as connection:
        assert (
            append_version(
                connection,
                tenant,
                "FOLLOWUP",
                {"completion_day_zero": True, "last_day_inclusive": 2},
                2,
                actor_staff_id=str(doctor),
            )
            == 3
        )
        fresh_provenance = _provenance(source, version=3)
        fresh = _claim(
            connection,
            tenant,
            source,
            day_offset=2,
            deadline=source["day"] + timedelta(days=2),
            provenance=fresh_provenance,
            price="125.00",
        )
        rows = connection.execute(
            text("""
                SELECT c.id, c.state, c.approved_deadline, c.policy_snapshot,
                       s.net_egp
                FROM public.followup_claims c
                JOIN public.appointment_price_snapshots s
                  ON (s.organization_id, s.appointment_id) =
                     (c.organization_id, c.appointment_id)
                WHERE c.organization_id=:tenant AND c.source_visit_id=:visit
            """),
            {"tenant": tenant, "visit": source["visit"]},
        ).all()
        by_id = {row.id: row for row in rows}
        assert set(by_id) == {original["claim"], fresh["claim"]}
        old = by_id[original["claim"]]
        new = by_id[fresh["claim"]]
        assert (old.state, old.approved_deadline, old.policy_snapshot, old.net_egp) == (
            "RELEASED",
            source["day"] + timedelta(days=10),
            original_provenance,
            100,
        )
        assert (new.state, new.approved_deadline, new.policy_snapshot, new.net_egp) == (
            "ACTIVE",
            source["day"] + timedelta(days=2),
            fresh_provenance,
            125,
        )


def test_zero_day_followup_policy_qualifies_and_confirms_only_completion_day(
    legacy_followup_storage,
):
    runtime, _, (tenant, _), (doctor, _) = legacy_followup_storage
    with transaction(runtime, tenant) as connection:
        assert (
            append_version(
                connection,
                tenant,
                "FOLLOWUP",
                {"completion_day_zero": True, "last_day_inclusive": 0},
                1,
                actor_staff_id=str(doctor),
            )
            == 2
        )
        source = _source(connection, tenant, doctor)
        provenance = _provenance(source, version=2)
        with pytest.raises(DBAPIError) as rejected, connection.begin_nested():
            _claim(
                connection,
                tenant,
                source,
                day_offset=1,
                deadline=source["day"] + timedelta(days=1),
                provenance=provenance,
            )
        assert rejected.value.orig.sqlstate == "23514"
        accepted = _claim(
            connection,
            tenant,
            source,
            day_offset=0,
            deadline=source["day"],
            provenance=provenance,
            price="45.00",
        )
        saved = connection.execute(
            text("""
                SELECT c.approved_deadline, c.completion_local_date,
                       c.policy_snapshot, s.net_egp
                FROM public.followup_claims c
                JOIN public.appointment_price_snapshots s
                  ON (s.organization_id, s.appointment_id) =
                     (c.organization_id, c.appointment_id)
                WHERE c.id=:claim AND c.organization_id=:tenant
            """),
            {"claim": accepted["claim"], "tenant": tenant},
        ).one()
        assert tuple(saved) == (
            source["day"],
            source["day"],
            provenance,
            Decimal("45.00"),
        )


def test_integral_decimal_followup_policy_payload_is_accepted(legacy_followup_storage):
    runtime, _, (tenant, _), (doctor, _) = legacy_followup_storage
    with transaction(runtime, tenant) as connection:
        assert (
            append_version(
                connection,
                tenant,
                "FOLLOWUP",
                '{"completion_day_zero":true,"last_day_inclusive":7.00}',
                1,
                actor_staff_id=str(doctor),
            )
            == 2
        )
        source = _source(connection, tenant, doctor)
        record = _claim(
            connection,
            tenant,
            source,
            day_offset=7,
            provenance=_provenance(source, version=2),
        )
        assert connection.scalar(
            text("SELECT approved_deadline FROM public.followup_claims WHERE id=:id"),
            {"id": record["claim"]},
        ) == source["day"] + timedelta(days=7)


def test_claim_and_preferential_appointment_must_both_exist_at_commit(
    legacy_followup_storage,
):
    runtime, _, (tenant, _), (doctor, _) = legacy_followup_storage
    with transaction(runtime, tenant) as connection:
        source = _source(connection, tenant, doctor)
    with (
        pytest.raises(DBAPIError) as missing_claim,
        transaction(runtime, tenant) as connection,
    ):
        persist_appointment(
            connection,
            _draft(
                source["patient"],
                doctor,
                service_type="FOLLOW_UP",
                service_date=source["day"] + timedelta(days=1),
                price_snapshot={"entitlement_provenance": _provenance(source)},
            ),
            _query(tenant, doctor),
        )
    assert missing_claim.value.orig.sqlstate == "23514"
    with transaction(runtime, tenant) as connection:
        with pytest.raises(DBAPIError) as missing_target, connection.begin_nested():
            _insert_claim(connection, tenant, source, uuid4(), _provenance(source))
        assert missing_target.value.orig.sqlstate in ("23503", "23514")
        assert (
            connection.scalar(
                text(
                    "SELECT count(*) FROM public.followup_claims WHERE source_visit_id=:visit"
                ),
                {"visit": source["visit"]},
            )
            == 0
        )


@pytest.mark.parametrize(
    ("terminal", "admit"),
    [("Cancelled", False), ("No-show", False), ("Cancelled", True)],
)
def test_cancel_no_show_and_exceptional_admission_release_in_same_commit(
    legacy_followup_storage, terminal, admit
):
    runtime, _, (tenant, _), (doctor, _) = legacy_followup_storage
    with transaction(runtime, tenant) as connection:
        source = _source(connection, tenant, doctor)
        record = _claim(connection, tenant, source)
        history_at, revision = _outcome(
            connection,
            tenant,
            doctor,
            record,
            status=terminal,
            after_admission=admit,
        )
    with transaction(runtime, tenant) as connection:
        assert connection.execute(
            text("""
                SELECT a.status, a.revision, c.state, c.revision,
                       c.terminal_history_revision, c.terminal_at, h.occurred_at
                FROM public.followup_claims c
                JOIN public.appointments a ON (a.organization_id, a.id) =
                     (c.organization_id, c.appointment_id)
                JOIN public.appointment_history h ON (h.organization_id,
                     h.appointment_id, h.appointment_revision) =
                     (c.organization_id, c.appointment_id, c.terminal_history_revision)
                WHERE c.id=:claim
            """),
            {"claim": record["claim"]},
        ).one() == (terminal, revision, "RELEASED", 2, revision, history_at, history_at)
        visit_count = connection.scalar(
            text("SELECT count(*) FROM public.visits WHERE appointment_id=:id"),
            {"id": record["id"]},
        )
        assert visit_count == int(admit)


def test_completed_followup_consumes_once_and_retains_immutable_history(
    legacy_followup_storage,
):
    runtime, migration, (tenant, _), (doctor, _) = legacy_followup_storage
    with transaction(runtime, tenant) as connection:
        source = _source(connection, tenant, doctor)
        record = _claim(connection, tenant, source)
        history_at, revision = _outcome(
            connection, tenant, doctor, record, status="Completed"
        )
    with transaction(runtime, tenant) as connection:
        assert connection.execute(
            text("""
                SELECT c.state, c.revision, c.terminal_history_revision,
                       c.terminal_at, a.status, h.action, h.new_status
                FROM public.followup_claims c
                JOIN public.appointments a ON (a.organization_id, a.id) =
                     (c.organization_id, c.appointment_id)
                JOIN public.appointment_history h ON (h.organization_id,
                     h.appointment_id, h.appointment_revision) =
                     (c.organization_id, c.appointment_id, c.terminal_history_revision)
                WHERE c.id=:claim
            """),
            {"claim": record["claim"]},
        ).one() == (
            "CONSUMED",
            2,
            revision,
            history_at,
            "Completed",
            "STATUS",
            "Completed",
        )
        assert (
            connection.scalar(
                text("SELECT count(*) FROM public.visits WHERE appointment_id=:id"),
                {"id": record["id"]},
            )
            == 1
        )
        for mutation in (
            "UPDATE public.followup_claims SET state='RELEASED' WHERE id=:id",
            "UPDATE public.followup_claims SET policy_snapshot='{}'::jsonb WHERE id=:id",
            "DELETE FROM public.followup_claims WHERE id=:id",
        ):
            with pytest.raises(DBAPIError) as denied, connection.begin_nested():
                connection.execute(text(mutation), {"id": record["claim"]})
            assert denied.value.orig.sqlstate in ("42501", "23514", "23505")
        with pytest.raises(DBAPIError) as denied, connection.begin_nested():
            connection.execute(
                text("""
                    UPDATE public.appointment_history
                    SET reason='Changed completion evidence'
                    WHERE appointment_id=:id AND appointment_revision=:revision
                """),
                {"id": record["id"], "revision": revision},
            )
        # Runtime cannot mutate immutable history even before its guard fires.
        assert denied.value.orig.sqlstate in ("23514", "42501")
    with migration.connect() as connection:
        assert (
            connection.scalar(
                text(
                    "SELECT count(*) FROM public.followup_claims WHERE id=:id AND state='CONSUMED'"
                ),
                {"id": record["claim"]},
            )
            == 1
        )


def test_injected_failure_after_appointment_write_rolls_back_both_sides(
    legacy_followup_storage,
):
    runtime, _, (tenant, _), (doctor, _) = legacy_followup_storage
    with transaction(runtime, tenant) as connection:
        source = _source(connection, tenant, doctor)
        record = _claim(connection, tenant, source)
    with transaction(runtime, tenant) as connection:
        with (
            pytest.raises(RuntimeError, match="synthetic write failure"),
            connection.begin_nested(),
        ):
            _outcome(
                connection,
                tenant,
                doctor,
                record,
                status="Cancelled",
                update_claim=False,
            )
            raise RuntimeError("synthetic write failure")
        assert connection.execute(
            text("""
                SELECT a.status, a.revision, c.state, c.revision
                FROM public.appointments a JOIN public.followup_claims c
                  ON (a.organization_id, a.id) = (c.organization_id, c.appointment_id)
                WHERE a.id=:id
            """),
            {"id": record["id"]},
        ).one() == ("Booked", 0, "ACTIVE", 1)
        assert (
            connection.scalar(
                text(
                    "SELECT count(*) FROM public.appointment_history WHERE appointment_id=:id"
                ),
                {"id": record["id"]},
            )
            == 1
        )


def test_unpaired_terminal_transition_cannot_commit(legacy_followup_storage):
    runtime, _, (tenant, _), (doctor, _) = legacy_followup_storage
    with transaction(runtime, tenant) as connection:
        source = _source(connection, tenant, doctor)
        record = _claim(connection, tenant, source)
    with (
        pytest.raises(DBAPIError) as denied,
        transaction(runtime, tenant) as connection,
    ):
        _outcome(
            connection,
            tenant,
            doctor,
            record,
            status="No-show",
            update_claim=False,
        )
    assert denied.value.orig.sqlstate == "23514"
    with transaction(runtime, tenant) as connection:
        assert connection.execute(
            text("""
                SELECT a.status, c.state FROM public.appointments a
                JOIN public.followup_claims c ON (c.organization_id, c.appointment_id) =
                     (a.organization_id, a.id)
                WHERE a.id=:id
            """),
            {"id": record["id"]},
        ).one() == ("Booked", "ACTIVE")


def test_tenant_rls_no_context_operator_denied_and_narrow_runtime_grants(
    legacy_followup_storage,
):
    runtime, migration, (tenant, other), (doctor, _) = legacy_followup_storage
    with transaction(runtime, tenant) as connection:
        source = _source(connection, tenant, doctor)
        record = _claim(connection, tenant, source)
        _outcome(connection, tenant, doctor, record, status="Completed")
    for scope in (other, None):
        with transaction(runtime, scope) as connection:
            for relation in ("followup_claims", "followup_extensions"):
                assert (
                    connection.scalar(text(f"SELECT count(*) FROM public.{relation}"))
                    == 0
                )
            assert (
                connection.execute(
                    text("""
                    UPDATE public.followup_claims SET state='RELEASED',
                           revision=revision+1
                    WHERE id=:claim RETURNING id
                """),
                    {"claim": record["claim"]},
                ).all()
                == []
            )
    operator = _engine("carepulse_operator", migration.url.database)
    try:
        for relation in ("followup_claims", "followup_extensions"):
            with pytest.raises(DBAPIError) as denied, operator.connect() as connection:
                connection.execute(text(f"SELECT * FROM public.{relation}")).all()
            assert denied.value.orig.sqlstate == "42501"
    finally:
        operator.dispose()
    with migration.connect() as connection:
        for relation in ("followup_claims", "followup_extensions"):
            relation_name = f"public.{relation}"
            assert connection.execute(
                text("""
                    SELECT relrowsecurity, relforcerowsecurity
                    FROM pg_class WHERE oid=CAST(:relation AS regclass)
                """),
                {"relation": relation_name},
            ).one() == (True, True)
            for privilege in ("SELECT", "INSERT", "UPDATE", "DELETE"):
                assert (
                    connection.scalar(
                        text("""
                        SELECT has_table_privilege('carepulse_operator', :relation, :privilege)
                    """),
                        {"relation": relation_name, "privilege": privilege},
                    )
                    is False
                )
                if privilege != "DELETE":
                    assert (
                        connection.scalar(
                            text("""
                            SELECT has_any_column_privilege(
                                'carepulse_operator', :relation, :privilege
                            )
                        """),
                            {"relation": relation_name, "privilege": privilege},
                        )
                        is False
                    )
            assert (
                connection.scalar(
                    text("""
                    SELECT has_table_privilege('carepulse_runtime', :relation, 'DELETE')
                """),
                    {"relation": relation_name},
                )
                is False
            )
        for col in ("state", "revision", "terminal_at", "terminal_history_revision"):
            assert (
                connection.scalar(
                    text("""
                    SELECT has_column_privilege('carepulse_runtime',
                        'public.followup_claims', :column, 'UPDATE')
                """),
                    {"column": col},
                )
                is True
            )
        for col in (
            "policy_snapshot",
            "patient_id",
            "approved_deadline",
            "source_visit_id",
        ):
            assert (
                connection.scalar(
                    text("""
                    SELECT has_column_privilege('carepulse_runtime',
                        'public.followup_claims', :column, 'UPDATE')
                """),
                    {"column": col},
                )
                is False
            )
        assert (
            connection.scalar(
                text("""
                SELECT has_any_column_privilege(
                    'carepulse_runtime', 'public.followup_extensions', 'UPDATE'
                )
            """)
            )
            is False
        )


def test_d08_adds_no_lifecycle_or_fixture_api_routes():
    from carepulse.main import app

    route_set = {
        (method, route.path)
        for included in app.routes
        for route in getattr(
            getattr(included, "original_router", None), "routes", (included,)
        )
        if hasattr(route, "path")
        for method in getattr(route, "methods", set())
    }
    assert ("POST", "/v1/appointments") in route_set
    assert not any(
        path.startswith(("/v1/followups", "/v1/followup-claims", "/v1/fixtures"))
        or (
            path.startswith("/v1/appointments/")
            and method in ("PATCH", "POST", "DELETE")
        )
        for method, path in route_set
    )

"""Real PostgreSQL T133 runtime acceptance under a temporary CI-only grant.

This process has ONLY a carepulse_runtime password. Its SQL connections are
genuine runtime logins, including for the accepted synthetic GUC-forgery proof.
"""

import json
import os
from concurrent.futures import ThreadPoolExecutor
from datetime import date, timedelta
from pathlib import Path
from threading import Event
from time import monotonic, sleep
from uuid import UUID

import psycopg
import pytest
from carepulse.db import transaction
from sqlalchemy import URL, create_engine, text
from sqlalchemy.exc import DBAPIError
from test_followup_persistence import _v2_booking, _v2_command, _v2_record


@pytest.fixture(scope="module")
def fixture_data():
    assert os.getenv("CI") == "true"
    assert os.getenv("CP_T133_ACTIVE") == "true"
    path = Path(os.environ["CP_T133_FIXTURE_PATH"])
    assert path.is_file()
    return json.loads(path.read_text(encoding="utf-8"))


def _login():
    password = Path("/run/secrets/runtime_db_password").read_text().strip()
    return psycopg.connect(
        host="db",
        dbname="carepulse",
        user="carepulse_runtime",
        password=password,
        connect_timeout=5,
        options="-c statement_timeout=20000 -c lock_timeout=12000 -c application_name=t133-runtime",
    )


def _runtime_engine():
    return create_engine(
        URL.create(
            "postgresql+psycopg",
            username="carepulse_runtime",
            password=Path("/run/secrets/runtime_db_password").read_text().strip(),
            host="db",
            database="carepulse",
        ),
        hide_parameters=True,
        connect_args={"options": "-c statement_timeout=20000 -c lock_timeout=12000"},
    )


def _context(connection, organization, actor):
    connection.execute(
        "SELECT set_config('app.organization_id', %s, true)", (organization,)
    )
    connection.execute("SELECT set_config('app.staff_id', %s, true)", (actor,))


def _command(case, operation, **params):
    command = {"operation": operation, **params}
    if case["claim"] is not None:
        command.update(
            expected_claim_revision=case["claim_revision"],
            expected_source_revision=case["source_revision"],
            expected_authorization_revision=case["authorization_revision"],
        )
    if case.get("queue_day_revision") is not None:
        command["expected_queue_day_revision"] = case["queue_day_revision"]
    return command


def _call(connection, data, case, command, *, revision=None, target=None):
    return connection.execute(
        """
        SELECT terminal_history_revision, terminal_at, attestation_id
        FROM public.transition_terminal_appointment(
            %s::uuid, %s::uuid, %s::bigint, %s::jsonb)
        """,
        (
            data["organization"],
            target or case["target"],
            case["revision"] if revision is None else revision,
            json.dumps(command),
        ),
    ).fetchone()


def _state(connection, case):
    return connection.execute(
        """
        SELECT a.status, a.revision,
               (SELECT c.state FROM public.followup_claims c
                WHERE c.organization_id=a.organization_id
                  AND c.appointment_id=a.id),
               (SELECT c.revision FROM public.followup_claims c
                WHERE c.organization_id=a.organization_id
                  AND c.appointment_id=a.id),
               (SELECT count(*) FROM public.appointment_history h
                WHERE h.organization_id=a.organization_id AND h.appointment_id=a.id)
        FROM public.appointments a WHERE a.id=%s::uuid
        """,
        (case["target"],),
    ).fetchone()


def _queue_revision(connection, case):
    return connection.execute(
        """
        SELECT q.revision FROM public.queue_entries e
        JOIN public.queue_days q
          ON (q.organization_id,q.id)=(e.organization_id,e.queue_day_id)
        WHERE e.appointment_id=%s::uuid AND e.disposition='ACTIVE'
        """,
        (case["target"],),
    ).fetchone()[0]


@pytest.mark.parametrize(
    ("name", "operation", "actor", "result"),
    [
        ("cancel_booked_doctor", "CANCEL", "doctor", "Cancelled"),
        ("cancel_booked_receptionist", "CANCEL", "receptionist", "Cancelled"),
        ("cancel_waiting_doctor", "CANCEL", "doctor", "Cancelled"),
        ("cancel_waiting_receptionist", "CANCEL", "receptionist", "Cancelled"),
        ("cancel_in_exam_doctor", "CANCEL", "doctor", "Cancelled"),
        ("complete_in_exam_doctor", "COMPLETE", "doctor", "Completed"),
        ("complete_in_exam_receptionist", "COMPLETE", "receptionist", "Completed"),
        ("no_show_old_doctor", "MARK_NO_SHOW", "doctor", "No-show"),
        ("no_show_old_receptionist", "MARK_NO_SHOW", "receptionist", "No-show"),
    ],
)
def test_terminal_outcome_commits_exact_claim_and_history(
    fixture_data, name, operation, actor, result
):
    case = {**fixture_data["cases"][name]}
    recorded_actor = fixture_data[actor]
    with _login() as connection:
        _context(connection, fixture_data["organization"], recorded_actor)
        if case["status"] == "Waiting":
            case["queue_day_revision"] = _queue_revision(connection, case)
        assert connection.execute(
            """
            SELECT session_user, current_user,
                   has_function_privilege(current_user,
                       'public.transition_terminal_appointment(uuid,uuid,bigint,jsonb)',
                       'EXECUTE')
            """
        ).fetchone() == ("carepulse_runtime", "carepulse_runtime", True)
        before = _state(connection, case)
        assert before is not None
        assert case["claim"] is not None
        assert before[2:4] == ("ACTIVE", case["claim_revision"])
        if operation == "MARK_NO_SHOW":
            claim_source = connection.execute(
                """
                SELECT c.organization_id, c.appointment_id, c.source_visit_id,
                       c.source_appointment_id, c.state,
                       p.entitlement_provenance = c.policy_snapshot,
                       a.service_type, a.classification,
                       ca.examination_occurred,
                       (ca.completed_at AT TIME ZONE 'Africa/Cairo')::date,
                       a.service_date
                FROM public.followup_claims c
                JOIN public.appointments a ON
                    (a.organization_id,a.id)=(c.organization_id,c.appointment_id)
                JOIN public.appointment_price_snapshots p ON
                    (p.organization_id,p.appointment_id)=(a.organization_id,a.id)
                JOIN public.completion_attestations ca ON
                    (ca.organization_id,ca.visit_id)=
                    (c.organization_id,c.source_visit_id)
                WHERE c.organization_id=%s::uuid AND c.id=%s::uuid
                """,
                (fixture_data["organization"], case["claim"]),
            ).fetchone()
            assert claim_source == (
                UUID(fixture_data["organization"]),
                UUID(case["target"]),
                UUID(case["source_visit"]),
                UUID(case["source_appointment"]),
                "ACTIVE",
                True,
                "FOLLOW_UP",
                "ROUTINE",
                True,
                date.fromisoformat(case["source_date"]),
                date.fromisoformat(case["source_date"]) + timedelta(days=1),
            )
            cairo_today = connection.execute(
                "SELECT (clock_timestamp() AT TIME ZONE 'Africa/Cairo')::date"
            ).fetchone()[0]
            assert claim_source[-1] < cairo_today
        waiting_before = None
        if before[0] == "Waiting":
            waiting_before = connection.execute(
                """
                SELECT e.arrival_sequence, e.routine_rank, e.attested_at,
                       e.disposition, q.revision, q.routine_cursor
                FROM public.queue_entries e
                JOIN public.queue_days q
                  ON (q.organization_id,q.id)=(e.organization_id,e.queue_day_id)
                WHERE e.appointment_id=%s::uuid AND e.disposition='ACTIVE'
                """,
                (case["target"],),
            ).fetchone()
            assert waiting_before is not None
        visit_before = connection.execute(
            "SELECT count(*) FROM public.visits WHERE appointment_id=%s::uuid",
            (case["target"],),
        ).fetchone()[0]
        command = _command(case, operation)
        if operation == "CANCEL":
            command["reason"] = "Verified intentional cancellation"
        elif operation == "MARK_NO_SHOW":
            command.update(
                reason="Verified absence past clinic day", absence_confirmed=True
            )
        else:
            command["examination_occurred"] = True
        revision, committed_at, attestation = _call(
            connection, fixture_data, case, command
        )
        connection.execute("SET CONSTRAINTS ALL IMMEDIATE")
        assert revision == case["revision"] + 1
        assert committed_at is not None
        assert (attestation is not None) == (operation == "COMPLETE")
    # A fresh runtime connection sees only the committed terminal state.
    with _login() as connection:
        _context(connection, fixture_data["organization"], recorded_actor)
        observed = _state(connection, case)
        assert observed == (
            result,
            revision,
            "CONSUMED" if result == "Completed" else "RELEASED",
            case["claim_revision"] + 1,
            before[4] + 1,
        )
        assert (
            connection.execute(
                "SELECT count(*) FROM public.visits WHERE appointment_id=%s::uuid",
                (case["target"],),
            ).fetchone()[0]
            == visit_before
        )
        history = connection.execute(
            """
            SELECT old_status, new_status, actor_staff_id, actor_role, occurred_at,
                   old_service_date = new_service_date, old_number = new_number
            FROM public.appointment_history
            WHERE appointment_id=%s::uuid AND appointment_revision=%s
            """,
            (case["target"], revision),
        ).fetchone()
        assert history == (
            before[0],
            result,
            UUID(recorded_actor),
            "DOCTOR_OWNER" if actor == "doctor" else "RECEPTIONIST",
            committed_at,
            True,
            True,
        )
        claim = connection.execute(
            """
            SELECT terminal_history_revision, terminal_at
            FROM public.followup_claims WHERE organization_id=%s::uuid AND id=%s::uuid
            """,
            (fixture_data["organization"], case["claim"]),
        ).fetchone()
        assert claim == (revision, committed_at)
        if waiting_before is not None:
            closed = connection.execute(
                """
                SELECT e.arrival_sequence, e.routine_rank, e.attested_at,
                       e.disposition, q.revision, q.routine_cursor,
                       e.closure_history_revision, e.closure_queue_revision
                FROM public.queue_entries e
                JOIN public.queue_days q
                  ON (q.organization_id,q.id)=(e.organization_id,e.queue_day_id)
                WHERE e.appointment_id=%s::uuid AND e.disposition='CLOSED'
                """,
                (case["target"],),
            ).fetchone()
            assert closed == (
                *waiting_before[:3],
                "CLOSED",
                waiting_before[4] + 1,
                waiting_before[5],
                revision,
                waiting_before[4],
            )


@pytest.mark.parametrize(
    ("name", "operation", "payload"),
    [
        ("deny_non_followup_cancel", "CANCEL", {"reason": "Not Follow-up"}),
        (
            "deny_non_followup_no_show",
            "MARK_NO_SHOW",
            {"reason": "Verified absence", "absence_confirmed": True},
        ),
        (
            "deny_non_followup_complete",
            "COMPLETE",
            {"examination_occurred": True},
        ),
    ],
)
def test_new_terminal_writer_rejects_non_followup_without_effects(
    fixture_data, name, operation, payload
):
    case = fixture_data["cases"][name]
    with _login() as connection:
        _context(connection, fixture_data["organization"], fixture_data["doctor"])
        before = _state(connection, case)
        assert before[2:4] == (None, None)
        with pytest.raises(psycopg.Error) as rejected:
            _call(connection, fixture_data, case, _command(case, operation, **payload))
        assert rejected.value.sqlstate == "42501"
        connection.rollback()
    with _login() as connection:
        _context(connection, fixture_data["organization"], fixture_data["doctor"])
        assert _state(connection, case) == before


def test_new_examination_completion_keeps_legacy_signature(fixture_data):
    case = fixture_data["cases"]["legacy_new_exam_complete"]
    with _login() as connection:
        _context(connection, fixture_data["organization"], fixture_data["doctor"])
        before = _state(connection, case)
        assert before[:4] == ("In Examination", 2, None, None)
        attestation, revision, committed_at = connection.execute(
            """
            SELECT * FROM public.complete_examined_visit(
                %s::uuid, %s::uuid, %s::bigint, true)
            """,
            (fixture_data["organization"], case["target"], case["revision"]),
        ).fetchone()
        connection.execute("SET CONSTRAINTS ALL IMMEDIATE")
        assert attestation is not None and revision == 3 and committed_at is not None
    with _login() as connection:
        _context(connection, fixture_data["organization"], fixture_data["doctor"])
        assert _state(connection, case) == ("Completed", 3, None, None, before[4] + 1)
        assert connection.execute(
            """
            SELECT c.completed_at, h.occurred_at, c.examination_occurred
            FROM public.completion_attestations c
            JOIN public.appointment_history h ON
                (h.organization_id,h.appointment_id,h.appointment_revision)=
                (c.organization_id,c.appointment_id,c.completion_history_revision)
            WHERE c.organization_id=%s::uuid AND c.id=%s::uuid
            """,
            (fixture_data["organization"], attestation),
        ).fetchone() == (committed_at, committed_at, True)


@pytest.mark.parametrize(
    ("name", "actor", "operation", "payload", "revision", "sqlstate"),
    [
        (
            "deny_in_exam_receptionist",
            "receptionist",
            "CANCEL",
            {"reason": "Not authorized"},
            None,
            "42501",
        ),
        (
            "deny_stale_revision",
            "doctor",
            "CANCEL",
            {"reason": "Stale revision"},
            99,
            "40001",
        ),
        (
            "deny_blank_reason",
            "doctor",
            "CANCEL",
            {"reason": "  "},
            None,
            "23514",
        ),
        (
            "deny_missing_attestation",
            "doctor",
            "COMPLETE",
            {"examination_occurred": False},
            None,
            "23514",
        ),
        (
            "deny_absent_attestation",
            "doctor",
            "COMPLETE",
            {},
            None,
            "23514",
        ),
        (
            "deny_missing_reason",
            "doctor",
            "CANCEL",
            {},
            None,
            "23514",
        ),
        (
            "deny_no_show_future",
            "receptionist",
            "MARK_NO_SHOW",
            {"reason": "Confirmed", "absence_confirmed": True},
            None,
            "23514",
        ),
        (
            "deny_false_absence",
            "doctor",
            "MARK_NO_SHOW",
            {"reason": "Absence not confirmed", "absence_confirmed": False},
            None,
            "23514",
        ),
        (
            "deny_stale_claim",
            "doctor",
            "CANCEL",
            {"reason": "Claim changed", "expected_claim_revision": 99},
            None,
            "40001",
        ),
        (
            "deny_stale_source",
            "doctor",
            "CANCEL",
            {"reason": "Source changed", "expected_source_revision": 99},
            None,
            "40001",
        ),
        (
            "deny_stale_authorization",
            "doctor",
            "CANCEL",
            {"reason": "Authorization changed", "expected_authorization_revision": 99},
            None,
            "40001",
        ),
        (
            "deny_stale_queue",
            "doctor",
            "CANCEL",
            {"reason": "Queue revision changed", "expected_queue_day_revision": 999},
            None,
            "40001",
        ),
        (
            "deny_extra_field",
            "doctor",
            "CANCEL",
            {"reason": "Unknown contract field", "unexpected": True},
            None,
            "42501",
        ),
        (
            "deny_unsupported",
            "doctor",
            "RESCHEDULE",
            {"reason": "Unsupported operation"},
            None,
            "42501",
        ),
        (
            "deny_disabled",
            "disabled_receptionist",
            "CANCEL",
            {"reason": "Former employee"},
            None,
            "42501",
        ),
        (
            "deny_foreign",
            "foreign_doctor",
            "CANCEL",
            {"reason": "Foreign actor"},
            None,
            "42501",
        ),
    ],
)
def test_denial_leaves_original_claim_and_history(
    fixture_data, name, actor, operation, payload, revision, sqlstate
):
    case = fixture_data["cases"][name]
    with _login() as connection:
        _context(connection, fixture_data["organization"], fixture_data[actor])
        before = _state(connection, case)
        with pytest.raises(psycopg.Error) as error:
            _call(
                connection,
                fixture_data,
                case,
                {**_command(case, operation), **payload},
                revision=revision,
            )
        assert error.value.sqlstate == sqlstate
        connection.rollback()
    with _login() as connection:
        _context(connection, fixture_data["organization"], fixture_data["doctor"])
        assert _state(connection, case) == before


def test_wrong_tenant_context_denies_without_terminal_effect(fixture_data):
    case = fixture_data["cases"]["deny_wrong_tenant"]
    with _login() as connection:
        _context(
            connection,
            fixture_data["foreign_organization"],
            fixture_data["foreign_doctor"],
        )
        with pytest.raises(psycopg.Error) as error:
            _call(
                connection,
                fixture_data,
                case,
                _command(case, "CANCEL", reason="Wrong tenant"),
            )
        assert error.value.sqlstate == "42501"
        connection.rollback()
    with _login() as connection:
        _context(connection, fixture_data["organization"], fixture_data["doctor"])
        assert _state(connection, case)[:4] == (
            "Booked",
            0,
            "ACTIVE",
            case["claim_revision"],
        )


@pytest.mark.parametrize("operation", ["CANCEL", "COMPLETE"])
def test_post_writer_transaction_failure_rolls_back_every_terminal_effect(
    fixture_data, operation
):
    case = fixture_data["cases"][
        "rollback_after_terminal_cancel"
        if operation == "CANCEL"
        else "rollback_after_terminal_complete"
    ]
    with _login() as connection:
        _context(connection, fixture_data["organization"], fixture_data["doctor"])
        before = _state(connection, case)
        payload = (
            {"reason": "Verified rollback after terminal writer"}
            if operation == "CANCEL"
            else {"examination_occurred": True}
        )
        assert (
            _call(connection, fixture_data, case, _command(case, operation, **payload))[
                0
            ]
            == case["revision"] + 1
        )
        connection.execute("SET CONSTRAINTS ALL IMMEDIATE")
        with pytest.raises(psycopg.Error) as failed:
            connection.execute("SELECT 1 / 0")
        assert failed.value.sqlstate == "22012"
        connection.rollback()
    with _login() as connection:
        _context(connection, fixture_data["organization"], fixture_data["doctor"])
        assert _state(connection, case) == before


@pytest.mark.parametrize(
    "action",
    [
        "APPOINTMENT_CANCELLED",
        "APPOINTMENT_MARKED_NO_SHOW",
        "VISIT_EXAMINATION_CANCELLED",
    ],
)
def test_runtime_cannot_forge_reserved_terminal_audit_even_during_grant(
    fixture_data, action
):
    with _login() as connection:
        _context(connection, fixture_data["organization"], fixture_data["doctor"])
        with pytest.raises(psycopg.Error) as error:
            connection.execute(
                """
                INSERT INTO public.sensitive_audit (
                    id, organization_id, actor_staff_id, actor_role,
                    action, reason, correlation_id)
                VALUES (gen_random_uuid(),%s::uuid,%s::uuid,'DOCTOR_OWNER',
                        %s,'Forged direct terminal audit','synthetic-forgery')
                """,
                (fixture_data["organization"], fixture_data["doctor"], action),
            )
        assert error.value.sqlstate == "42501"
        connection.rollback()


def test_forged_runtime_gucs_have_known_accepted_ci_only_vulnerability(fixture_data):
    """SEC-26-CI-01: runtime credentials plus forged GUCs impersonate Doctor.

    This is a security finding, not proof of independently authenticated staff.
    """
    case = fixture_data["cases"]["deny_foreign"]
    with _login() as connection:
        with pytest.raises(psycopg.Error) as denied:
            _call(
                connection,
                fixture_data,
                case,
                _command(case, "CANCEL", reason="No tenant context"),
            )
        assert denied.value.sqlstate == "42501"
        connection.rollback()
    # The same SQL login can impersonate the live Doctor through SET LOCAL.
    with _login() as connection:
        _context(connection, fixture_data["organization"], fixture_data["doctor"])
        revision, completed_at, _ = _call(
            connection,
            fixture_data,
            case,
            _command(case, "CANCEL", reason="Forged doctor GUC proof"),
        )
        connection.execute("SET CONSTRAINTS ALL IMMEDIATE")
        assert revision == 1 and completed_at is not None
    with _login() as connection:
        _context(connection, fixture_data["organization"], fixture_data["doctor"])
        assert _state(connection, case)[:4] == ("Cancelled", 1, "RELEASED", 2)


def _fresh_claim_after_terminal(data, case, *, must_succeed=True):
    runtime = _runtime_engine()
    try:
        with transaction(runtime, UUID(data["organization"])) as connection:
            policy = connection.execute(
                text("""
                    SELECT h.current_version, v.payload
                    FROM public.clinic_policy_heads h
                    JOIN public.clinic_policy_versions v
                      ON (v.organization_id,v.kind,v.version)=
                         (h.organization_id,h.kind,h.current_version)
                    WHERE h.organization_id=:org AND h.kind='PRICE'
                """),
                {"org": UUID(data["organization"])},
            ).one()
            source = {
                "tenant": UUID(data["organization"]),
                "doctor": UUID(data["doctor"]),
                "visit": UUID(case["source_visit"]),
                "day": date.fromisoformat(case["source_date"]),
                "strict_quote": {
                    "base_egp": str(policy.payload["preferential_followup_egp"]),
                    "net_egp": str(policy.payload["preferential_followup_egp"]),
                    "creation_standard_base_egp": str(
                        policy.payload["standard_new_exam_egp"]
                    ),
                    "pricing_version": policy.current_version,
                },
            }
            body = _v2_booking(source)
            expected = {
                "source_revision": case["source_revision"],
                "clinical_revision": 0,
                "policy_version": 1,
            }
            if not must_succeed:
                with pytest.raises(DBAPIError) as denied, connection.begin_nested():
                    _v2_command(
                        connection,
                        "create_followup_claim",
                        source,
                        expected=expected,
                        **body,
                    )
                assert denied.value.orig.sqlstate == "23505"
                return None
            claim = _v2_command(
                connection,
                "create_followup_claim",
                source,
                expected=expected,
                **body,
            )
            record = _v2_record(connection, claim)
            return tuple(
                connection.execute(
                    text("""
                        SELECT p.net_egp, p.pricing_version,
                               c.approved_deadline,
                               p.entitlement_provenance,
                               c.state
                        FROM public.followup_claims c
                        JOIN public.appointment_price_snapshots p
                          ON (p.organization_id,p.appointment_id)=
                             (c.organization_id,c.appointment_id)
                        WHERE c.organization_id=:org AND c.id=:claim
                    """),
                    {"org": UUID(data["organization"]), "claim": claim},
                ).one()
            ) + (record["id"],)
    finally:
        runtime.dispose()


@pytest.mark.parametrize("with_extension", [False, True])
def test_released_claim_fresh_qualification_uses_current_price_not_old_extension(
    fixture_data, with_extension
):
    case = fixture_data["cases"][
        "release_after_extension" if with_extension else "release_requalify"
    ]
    if with_extension:
        with _login() as connection:
            _context(connection, fixture_data["organization"], fixture_data["doctor"])
            assert _extend(connection, fixture_data, case) == (
                case["authorization_revision"] + 1
            )
        case = {
            **case,
            "authorization_revision": case["authorization_revision"] + 1,
        }
    with _login() as connection:
        _context(connection, fixture_data["organization"], fixture_data["doctor"])
        assert (
            _call(
                connection,
                fixture_data,
                case,
                _command(case, "CANCEL", reason="Documented released claim"),
            )[0]
            == case["revision"] + 1
        )
        connection.execute("SET CONSTRAINTS ALL IMMEDIATE")
    new_price, version, deadline, snapshot, state, target = _fresh_claim_after_terminal(
        fixture_data, case
    )
    assert str(new_price) == "75.00"
    assert version >= 2
    assert state == "ACTIVE"
    assert target != UUID(case["target"])
    assert deadline == date.fromisoformat(case["source_date"]) + timedelta(days=7)
    assert snapshot["source_visit_id"] == case["source_visit"]
    assert not snapshot.get("new_date_event_id")
    assert "extended_deadline" not in snapshot


def test_consumed_claim_cannot_be_rebooked_even_after_price_change(fixture_data):
    case = fixture_data["cases"]["consume_denies_second"]
    with _login() as connection:
        _context(connection, fixture_data["organization"], fixture_data["doctor"])
        assert (
            _call(
                connection,
                fixture_data,
                case,
                _command(case, "COMPLETE", examination_occurred=True),
            )[0]
            == case["revision"] + 1
        )
        connection.execute("SET CONSTRAINTS ALL IMMEDIATE")
    assert _fresh_claim_after_terminal(fixture_data, case, must_succeed=False) is None
    with _login() as connection:
        _context(connection, fixture_data["organization"], fixture_data["doctor"])
        assert _state(connection, case)[:4] == (
            "Completed",
            3,
            "CONSUMED",
            case["claim_revision"] + 1,
        )


@pytest.mark.parametrize("leader_action", ["attendance", "terminal"])
def test_waiting_cancel_serializes_with_real_admission_and_queue_day(
    fixture_data, leader_action
):
    case = {**fixture_data["cases"][f"race_waiting_{leader_action}_first"]}
    tenant = fixture_data["organization"]
    doctor = fixture_data["doctor"]
    ready, release, waiter_ready = Event(), Event(), Event()
    pids = {}
    with _login() as connection:
        _context(connection, tenant, doctor)
        case["queue_day_revision"] = _queue_revision(connection, case)
    terminal_command = _command(
        case, "CANCEL", reason="Verified synthetic Waiting cancellation"
    )

    def action(kind, leading):
        if kind == "terminal":
            with _login() as connection:
                _context(connection, tenant, doctor)
                pids["leader" if leading else "waiter"] = connection.info.backend_pid
                if not leading:
                    waiter_ready.set()
                try:
                    result = _call(connection, fixture_data, case, terminal_command)
                    connection.execute("SET CONSTRAINTS ALL IMMEDIATE")
                    if leading:
                        ready.set()
                        assert release.wait(timeout=20)
                    return result
                except psycopg.Error as error:
                    if leading:
                        ready.set()
                    return error.sqlstate
        engine = _runtime_engine()
        try:
            with transaction(engine, UUID(tenant)) as connection:
                pids["leader" if leading else "waiter"] = connection.scalar(
                    text("SELECT pg_backend_pid()")
                )
                saved = connection.execute(
                    text("""
                        SELECT id, service_date, lane
                        FROM public.appointments
                        WHERE organization_id=:tenant AND id=:target
                    """),
                    {"tenant": UUID(tenant), "target": UUID(case["target"])},
                ).one()
                if not leading:
                    waiter_ready.set()
                try:
                    connection.execute(
                        text("SELECT set_config('app.staff_id', :staff, true)"),
                        {"staff": doctor},
                    )
                    # Witness the accepted DoctorControl barrier before any
                    # attendance status write. Both contenders use this row.
                    connection.execute(
                        text("""
                            SELECT doctor_staff_id FROM public.doctor_controls
                            WHERE organization_id=:tenant AND doctor_staff_id=:doctor
                            FOR UPDATE
                        """),
                        {"tenant": UUID(tenant), "doctor": UUID(doctor)},
                    ).one()
                    connection.execute(
                        text("""
                            SELECT * FROM public.lock_doctor_admission_capacity(
                                :tenant,:doctor,:day,:lane)
                        """),
                        {
                            "tenant": UUID(tenant),
                            "doctor": UUID(doctor),
                            "day": saved.service_date,
                            "lane": saved.lane,
                        },
                    ).one()
                    connection.execute(
                        text("""
                            UPDATE public.appointments
                            SET status='In Examination',revision=revision+1
                            WHERE organization_id=:tenant AND id=:target
                        """),
                        {"tenant": UUID(tenant), "target": saved.id},
                    )
                    connection.execute(
                        text("""
                            INSERT INTO public.appointment_history (
                                id, organization_id, appointment_id,
                                appointment_revision, action, old_status, new_status,
                                old_service_date,new_service_date,old_number,new_number,
                                actor_staff_id,actor_role,reason,
                                price_snapshot_appointment_id)
                            SELECT gen_random_uuid(),organization_id,id,2,'STATUS',
                                   'Waiting','In Examination',service_date,service_date,
                                   current_number,current_number,:doctor,'DOCTOR_OWNER',
                                   'Synthetic admission',id
                            FROM public.appointments
                            WHERE organization_id=:tenant AND id=:target
                        """),
                        {
                            "doctor": UUID(doctor),
                            "tenant": UUID(tenant),
                            "target": saved.id,
                        },
                    )
                    connection.execute(
                        text("""
                            UPDATE public.queue_entries
                            SET disposition='CLOSED',closure_history_revision=2,
                                revision=revision+1
                            WHERE organization_id=:tenant AND appointment_id=:target
                              AND disposition='ACTIVE'
                        """),
                        {"tenant": UUID(tenant), "target": saved.id},
                    )
                    connection.execute(
                        text("""
                            UPDATE public.queue_days SET revision=revision+1
                            WHERE organization_id=:tenant
                              AND id=(SELECT queue_day_id FROM public.queue_entries
                                      WHERE organization_id=:tenant
                                        AND appointment_id=:target)
                        """),
                        {"tenant": UUID(tenant), "target": saved.id},
                    )
                    connection.execute(
                        text("""
                            INSERT INTO public.visits (
                                id,organization_id,appointment_id,patient_id,
                                doctor_staff_id,service_date,
                                admission_history_revision,admitted_at)
                            SELECT gen_random_uuid(),organization_id,id,patient_id,
                                   doctor_staff_id,service_date,2,clock_timestamp()
                            FROM public.appointments
                            WHERE organization_id=:tenant AND id=:target
                        """),
                        {"tenant": UUID(tenant), "target": saved.id},
                    )
                    connection.execute(text("SET CONSTRAINTS ALL IMMEDIATE"))
                    if leading:
                        ready.set()
                        assert release.wait(timeout=20)
                    return "ATTENDED"
                except DBAPIError as error:
                    if leading:
                        ready.set()
                    return error.orig.sqlstate
        finally:
            engine.dispose()

    other = "terminal" if leader_action == "attendance" else "attendance"
    with ThreadPoolExecutor(max_workers=2) as pool:
        leader = pool.submit(action, leader_action, True)
        assert ready.wait(timeout=15)
        waiter = pool.submit(action, other, False)
        try:
            assert waiter_ready.wait(timeout=15)
            with _login() as observer:
                until = monotonic() + 7
                witnessed = False
                while monotonic() < until:
                    blockers = observer.execute(
                        "SELECT pg_blocking_pids(%s::integer)",
                        (pids["waiter"],),
                    ).fetchone()[0]
                    if pids["leader"] in blockers:
                        witnessed = True
                        break
                    sleep(0.02)
                assert witnessed, (
                    "Attendance/terminal failed to reach observed row wait"
                )
        finally:
            release.set()
        leader_result = leader.result(timeout=25)
        waiter_result = waiter.result(timeout=25)
    with _login() as connection:
        _context(connection, tenant, doctor)
        status, revision, claim_state, claim_revision, _ = _state(connection, case)
        if leader_action == "attendance":
            assert leader_result == "ATTENDED"
            assert waiter_result == "40001"
            assert (status, revision, claim_state, claim_revision) == (
                "In Examination",
                2,
                "ACTIVE",
                case["claim_revision"],
            )
        else:
            assert isinstance(leader_result, tuple)
            assert waiter_result == "23514"
            assert (status, revision, claim_state, claim_revision) == (
                "Cancelled",
                2,
                "RELEASED",
                case["claim_revision"] + 1,
            )


@pytest.mark.parametrize("first", ["CANCEL", "COMPLETE"])
def test_exceptional_cancellation_and_completion_share_one_terminal_winner(
    fixture_data, first
):
    name = "race_exam_cancel_first" if first == "CANCEL" else "race_exam_complete_first"
    case = fixture_data["cases"][name]
    second = "COMPLETE" if first == "CANCEL" else "CANCEL"
    started, released, contender_started = Event(), Event(), Event()
    pids = {}

    def attempt(operation, leading):
        with _login() as connection:
            _context(connection, fixture_data["organization"], fixture_data["doctor"])
            pids["leader" if leading else "waiter"] = connection.info.backend_pid
            if not leading:
                contender_started.set()
            payload = (
                {"examination_occurred": True}
                if operation == "COMPLETE"
                else {"reason": "Verified exceptional clinical cancellation"}
            )
            try:
                result = _call(
                    connection, fixture_data, case, _command(case, operation, **payload)
                )
                connection.execute("SET CONSTRAINTS ALL IMMEDIATE")
                if leading:
                    started.set()
                    assert released.wait(timeout=20)
                return result
            except psycopg.Error as error:
                if leading:
                    started.set()
                return error.sqlstate

    with ThreadPoolExecutor(max_workers=2) as pool:
        leader = pool.submit(attempt, first, True)
        assert started.wait(timeout=15)
        waiter = pool.submit(attempt, second, False)
        try:
            assert contender_started.wait(timeout=15)
            with _login() as observer:
                until = monotonic() + 7
                witnessed = False
                while monotonic() < until:
                    blocker_ids = observer.execute(
                        "SELECT pg_blocking_pids(%s::integer)",
                        (pids["waiter"],),
                    ).fetchone()[0]
                    if pids["leader"] in blocker_ids:
                        witnessed = True
                        break
                    sleep(0.02)
                assert witnessed, "No exceptional-cancel/completion lock witness"
        finally:
            released.set()
        winner = leader.result(timeout=25)
        loser = waiter.result(timeout=25)
    assert isinstance(winner, tuple)
    assert loser == "40001"
    with _login() as connection:
        _context(connection, fixture_data["organization"], fixture_data["doctor"])
        assert _state(connection, case)[:4] == (
            "Cancelled" if first == "CANCEL" else "Completed",
            3,
            "RELEASED" if first == "CANCEL" else "CONSUMED",
            case["claim_revision"] + 1,
        )


def _extend(connection, data, case):
    expected = {
        "source_revision": case["source_revision"],
        "clinical_revision": 0,
        "policy_version": 1,
        "claim_revision": case["claim_revision"],
        "authorization_revision": case["authorization_revision"],
        "appointment_revision": case["revision"],
    }
    extension = {
        "deadline_date": (
            date.fromisoformat(case["source_date"]) + timedelta(days=9)
        ).isoformat(),
        "reason": "Verified synthetic concurrent extension",
    }
    return connection.execute(
        """
        SELECT public.extend_followup_claim_ordered(
            %s::uuid, %s::uuid, %s::jsonb, %s::jsonb)
        """,
        (
            data["organization"],
            case["claim"],
            json.dumps(expected),
            json.dumps(extension),
        ),
    ).fetchone()[0]


@pytest.mark.parametrize("operation", ["CANCEL", "COMPLETE"])
@pytest.mark.parametrize("winner", ["extension", "terminal"])
@pytest.mark.parametrize("source_first", [True, False])
def test_extension_and_terminal_race_observed_blocker_and_committed_winner(
    fixture_data, operation, winner, source_first
):
    case = fixture_data["cases"][
        f"race_{operation.lower()}_{winner}_"
        f"{'source_first' if source_first else 'target_first'}"
    ]
    ready, release, waiter_ready = Event(), Event(), Event()
    current_pids = {}
    data = fixture_data
    terminal_body = _command(
        case,
        operation,
        **(
            {"examination_occurred": True}
            if operation == "COMPLETE"
            else {"reason": "Verified concurrent cancellation"}
        ),
    )

    def ordered_leader():
        with _login() as connection:
            _context(connection, data["organization"], data["doctor"])
            current_pids["leader"] = connection.info.backend_pid
            try:
                if winner == "extension":
                    result = _extend(connection, data, case)
                else:
                    result = _call(connection, data, case, terminal_body)
                ready.set()
                assert release.wait(timeout=20), "Observed waiter never released leader"
                return result
            finally:
                ready.set()

    def ordered_waiter():
        with _login() as connection:
            _context(connection, data["organization"], data["doctor"])
            current_pids["waiter"] = connection.info.backend_pid
            waiter_ready.set()
            try:
                if winner == "extension":
                    _call(connection, data, case, terminal_body)
                else:
                    _extend(connection, data, case)
            except psycopg.Error as error:
                return error.sqlstate
            return "UNEXPECTED_SUCCESS"

    with ThreadPoolExecutor(max_workers=2) as pool:
        leader = pool.submit(ordered_leader)
        assert ready.wait(timeout=15)
        waiter = pool.submit(ordered_waiter)
        try:
            assert waiter_ready.wait(timeout=15)
            with _login() as observer:
                until = monotonic() + 7
                blocked = False
                while monotonic() < until:
                    blockers = observer.execute(
                        "SELECT pg_blocking_pids(%s::integer)",
                        (current_pids["waiter"],),
                    ).fetchone()[0]
                    if current_pids["leader"] in blockers:
                        blocked = True
                        break
                    sleep(0.02)
                assert blocked, "No PostgreSQL pg_blocking_pids lock witness"
        finally:
            release.set()
        leader.result(timeout=25)
        assert waiter.result(timeout=25) == "40001"

    with _login() as connection:
        _context(connection, data["organization"], data["doctor"])
        status, revision, claim_state, claim_revision, _ = _state(connection, case)
        if winner == "extension":
            assert (status, revision, claim_state, claim_revision) == (
                case["status"],
                case["revision"],
                "ACTIVE",
                case["claim_revision"],
            )
            tail = connection.execute(
                """
                SELECT max(authorization_revision)
                FROM public.followup_authorization_events
                WHERE claim_id=%s::uuid
                """,
                (case["claim"],),
            ).fetchone()[0]
            assert tail == case["authorization_revision"] + 1
        else:
            assert (status, revision, claim_state, claim_revision) == (
                "Cancelled" if operation == "CANCEL" else "Completed",
                case["revision"] + 1,
                "RELEASED" if operation == "CANCEL" else "CONSUMED",
                case["claim_revision"] + 1,
            )
            tail = connection.execute(
                """
                SELECT coalesce(max(authorization_revision),0)
                FROM public.followup_authorization_events
                WHERE claim_id=%s::uuid
                """,
                (case["claim"],),
            ).fetchone()[0]
            assert tail == case["authorization_revision"]

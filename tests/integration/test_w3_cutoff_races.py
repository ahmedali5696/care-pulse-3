"""T068: scoped cutoff execution using the actual worker and staff PostgreSQL logins."""

import json
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from datetime import UTC, date, datetime, time, timedelta
from threading import Event
from uuid import UUID, uuid4

import pytest
from carepulse.appointments.cutoff import apply_selected_cutoff
from carepulse.identity.contracts import ContractError
from carepulse.queue.contracts import CheckInAppointment
from postgres_locks import wait_for_postgres_lock
from runtime_identity_fixture import signed_test_transaction
from sqlalchemy import event, text
from sqlalchemy.exc import DBAPIError
from test_booking import booking_clinic as booking_clinic  # noqa: PLC0414
from test_followups import followup_clinic as followup_clinic  # noqa: PLC0414
from test_patients import patient_backend as patient_backend  # noqa: PLC0414
from test_request_boundary import boundary_data as boundary_data  # noqa: PLC0414
from test_schedule import CAIRO, _config, publish_signed_schedule
from test_w3_checkin import _new_patient, _seed_booked
from test_w3_checkin_races import _checkin
from test_w3_intake import _policy_revision
from test_w3_reconciliation import historical_claim
from test_w3_visits import effects

_SELECT = "SELECT * FROM public.f01_b20_cutoff_candidates(:tenant,:day,:version)"
_APPLY = """SELECT public.f01_b20_cutoff_appointment(
    :tenant,:day,:version,:target,:revision,:token)"""


@pytest.fixture
def worker(role_engine):
    engine = role_engine("carepulse_worker")
    try:
        with engine.connect() as connection:
            assert connection.execute(
                text("SELECT session_user,current_user")
            ).one() == ("carepulse_worker", "carepulse_worker")
        yield engine
    finally:
        engine.dispose()


def _today():
    return datetime.now(CAIRO).date()


def _configure(clinic, *, enabled=True, clock="00:00"):
    publish_signed_schedule(
        clinic,
        _config(
            mode="OPEN",
            working_weekdays=list(range(1, 8)),
            weekly_work_periods=[
                {"weekday": day, "starts_at": "00:00", "ends_at": "23:59"}
                for day in range(1, 8)
            ],
            no_show_cutoff={
                "enabled": enabled,
                "local_time": clock if enabled else None,
            },
        ),
        revision=_policy_revision(clinic, "SCHEDULE"),
    )
    with clinic.migration.connect() as connection:
        return connection.scalar(
            text("""SELECT current_version FROM public.clinic_policy_heads
                WHERE organization_id=:tenant AND kind='SCHEDULE'"""),
            {"tenant": clinic.tenant},
        )


def _seed(clinic, day, name):
    return _seed_booked(clinic, UUID(_new_patient(clinic, clinic.owner, name)), day)


def _scope(clinic, day, version):
    return {"tenant": clinic.tenant, "day": day, "version": version}


def _selected(worker, scope):
    with worker.begin() as connection:
        return [
            {**scope, "target": target, "revision": revision, "token": token}
            for target, revision, token in connection.execute(text(_SELECT), scope)
        ]


def _apply(connection, candidate):
    return connection.scalar(text(_APPLY), candidate)


def _reject(engine, statement, parameters, code):
    with pytest.raises(DBAPIError) as denied, engine.begin() as connection:
        connection.execute(text(statement), parameters)
    assert denied.value.orig.sqlstate == code


def _row(state, table, target):
    return next(row for row in state[table] if row["id"] == str(target))


def _added(before, after, table):
    assert all(row in after[table] for row in before[table]), table
    return [row for row in after[table] if row not in before[table]]


def _assert_system_effects(before, after, candidate, projection):
    target = candidate["target"]
    prior = _row(before, "appointments", target)
    updated = _row(after, "appointments", target)
    assert (
        updated["status"] == "No-show" and updated["revision"] == prior["revision"] + 1
    )
    assert {
        k: v for k, v in prior.items() if k not in {"status", "revision", "updated_at"}
    } == {
        k: v
        for k, v in updated.items()
        if k not in {"status", "revision", "updated_at"}
    }
    for table in (
        "appointment_day_counters",
        "appointment_number_assignments",
        "appointment_price_snapshots",
        "queue_days",
        "queue_entries",
        "visits",
        "completion_attestations",
        "clinical_revisions",
        "clinical_audit",
        "followup_source_state",
        "followup_source_decision_events",
        "followup_authorization_events",
    ):
        assert before[table] == after[table], table
    (history,) = _added(before, after, "appointment_history")
    assert (
        history["appointment_id"],
        history["actor_staff_id"],
        history["actor_role"],
        history["old_status"],
        history["new_status"],
        history["new_service_date"],
    ) == (
        str(target),
        None,
        "SYSTEM",
        "Booked",
        "No-show",
        candidate["day"].isoformat(),
    )
    (audit,) = _added(before, after, "sensitive_audit")
    assert (
        audit["actor_staff_id"],
        audit["actor_role"],
        audit["operator_id"],
        audit["action"],
    ) == (None, "SYSTEM", None, "APPOINTMENT_MARKED_NO_SHOW")
    assert audit["correlation_id"] == history["id"]
    assert audit["after_state"]["service_date"] == candidate["day"].isoformat()
    assert audit["after_state"]["origin"] == "WORKER"
    assert audit["after_state"]["cutoff_policy_version"] == candidate["version"]
    (outbox,) = _added(before, after, "outbox_events")
    assert outbox["aggregate_id"] == str(target)
    assert outbox["event_type"] == "APPOINTMENT_MARKED_NO_SHOW"
    assert outbox["payload"]["actor_role"] == "SYSTEM"
    (receipt,) = _added(before, after, "idempotency_receipts")
    assert (
        receipt["actor_kind"],
        receipt["staff_id"],
        receipt["operator_id"],
        receipt["operation"],
        receipt["idempotency_key"],
        receipt["result_revision"],
        receipt["result_projection"],
    ) == (
        "SYSTEM",
        None,
        None,
        "appointments.cutoff_no_show",
        str(target),
        updated["revision"],
        projection,
    )
    assert len(receipt["request_digest"]) == 64
    old_claims = [
        row for row in before["followup_claims"] if row["appointment_id"] == str(target)
    ]
    if old_claims:
        (old_claim,) = old_claims
        claim = _row(after, "followup_claims", old_claim["id"])
        assert (
            claim["state"],
            claim["revision"],
            claim["terminal_history_revision"],
            claim["terminal_at"],
        ) == (
            "RELEASED",
            old_claim["revision"] + 1,
            updated["revision"],
            history["occurred_at"],
        )
        assert {
            k: v
            for k, v in claim.items()
            if k
            not in {"state", "revision", "terminal_history_revision", "terminal_at"}
        } == {
            k: v
            for k, v in old_claim.items()
            if k
            not in {"state", "revision", "terminal_history_revision", "terminal_at"}
        }
    else:
        assert before["followup_claims"] == after["followup_claims"]


@pytest.mark.parametrize(
    ("day", "clock", "expected"),
    [
        (date(2026, 4, 24), time(0, 30), datetime(2026, 4, 23, 22, tzinfo=UTC)),
        (date(2026, 10, 29), time(23, 30), datetime(2026, 10, 29, 21, 30, tzinfo=UTC)),
    ],
)
def test_database_cutoff_resolves_cairo_gap_and_fold(
    booking_clinic, worker, day, clock, expected
):
    clinic = booking_clinic
    engine = clinic.migration
    target = _seed(clinic, day, "DST boundary")
    scope = _scope(clinic, day, _configure(clinic, clock=clock.strftime("%H:%M")))
    signature = "public.b20_cutoff_authorize(uuid,date,bigint)"
    marker = "clock_timestamp()<resolved"
    before = effects(clinic)
    with engine.begin() as connection:
        assert (
            connection.scalar(
                text("SELECT public.b20_cutoff_instant(:day,:clock)"),
                {"day": day, "clock": clock},
            )
            == expected
        )
        original = connection.scalar(
            text("SELECT pg_get_functiondef(CAST(:signature AS regprocedure))"),
            {"signature": signature},
        )
        assert original.count(marker) == 1
        connection.exec_driver_sql(
            original.replace(
                marker,
                "COALESCE(NULLIF(current_setting('cp.b20_cutoff_asof', true), '')::timestamptz, clock_timestamp())<resolved",
            )
        )
    try:
        with worker.begin() as connection:
            connection.execute(
                text("SELECT set_config('cp.b20_cutoff_asof', :asof, true)"),
                {"asof": (expected - timedelta(microseconds=1)).isoformat()},
            )
            with pytest.raises(DBAPIError) as denied, connection.begin_nested():
                connection.execute(text(_SELECT), scope)
            assert denied.value.orig.sqlstate == "23514"
            connection.execute(
                text("SELECT set_config('cp.b20_cutoff_asof', :asof, true)"),
                {"asof": expected.isoformat()},
            )
            (candidate,) = connection.execute(text(_SELECT), scope).all()
            assert candidate[0] == UUID(target["id"])
    finally:
        with engine.begin() as connection:
            connection.exec_driver_sql(original)
    assert effects(clinic) == before


@pytest.mark.parametrize(
    "role", ["carepulse_worker", "carepulse_api", "carepulse_operator"]
)
def test_worker_only_login_and_no_direct_domain_or_token_privilege(role_engine, role):
    engine = role_engine(role)
    try:
        with engine.connect() as connection:
            assert connection.scalar(text("SELECT session_user")) == role
            assert connection.scalar(
                text("""SELECT has_function_privilege(
                    session_user,'public.f01_b20_cutoff_candidates(uuid,date,bigint)','EXECUTE')""")
            ) is (role == "carepulse_worker")
            assert not connection.scalar(
                text("""SELECT has_function_privilege(session_user,
                    'public.f01_b20_cutoff_token(uuid,date,bigint,uuid,bigint)','EXECUTE')""")
            )
            if role == "carepulse_worker":
                for table in (
                    "appointments",
                    "appointment_history",
                    "followup_claims",
                    "sensitive_audit",
                    "outbox_events",
                    "idempotency_receipts",
                ):
                    assert not connection.scalar(
                        text(
                            "SELECT has_table_privilege(session_user,:table,'SELECT,INSERT,UPDATE,DELETE')"
                        ),
                        {"table": f"public.{table}"},
                    )
        _reject(
            engine, _SELECT, {"tenant": None, "day": None, "version": None}, "42501"
        )
    finally:
        engine.dispose()


def test_current_policy_selected_date_token_replay_and_zero_denied_effects(
    booking_clinic, worker
):
    clinic = booking_clinic
    day = _today() - timedelta(days=1)
    target = _seed(clinic, day, "Due")
    other = _seed(clinic, day - timedelta(days=1), "Other day")
    disabled = _configure(clinic, enabled=False)
    before = effects(clinic)
    _reject(worker, _SELECT, _scope(clinic, day, disabled), "23514")
    assert effects(clinic) == before
    version = _configure(clinic)
    scope = _scope(clinic, day, version)
    before = effects(clinic)
    _reject(worker, _SELECT, {**scope, "version": disabled}, "23514")
    _reject(worker, _SELECT, {**scope, "day": _today() + timedelta(days=1)}, "23514")
    (candidate,) = _selected(worker, scope)
    assert candidate["target"] == UUID(target["id"])
    for changes in (
        {"target": UUID(other["id"])},
        {"day": day - timedelta(days=1)},
        {"revision": 1},
        {"token": bytes(32)},
        {"token": None},
        {"tenant": uuid4()},
    ):
        _reject(worker, _APPLY, {**candidate, **changes}, "42501")
    assert effects(clinic) == before
    with signed_test_transaction(
        clinic,
        clinic.owner,
        method="GET",
        path="/v1/reconciliation",
        purpose="RECONCILIATION_READ",
        query=f"date={day}",
    ) as (connection, _):
        with pytest.raises(DBAPIError) as denied, connection.begin_nested():
            _apply(connection, candidate)
        assert denied.value.orig.sqlstate == "42501"
    with worker.begin() as connection:
        result = _apply(connection, candidate)
    after = effects(clinic)
    _assert_system_effects(before, after, candidate, result)
    assert _row(after, "appointments", other["id"]) == _row(
        before, "appointments", other["id"]
    )
    with worker.begin() as connection:
        assert _apply(connection, candidate) == result
    assert _selected(worker, scope) == []
    assert effects(clinic) == after
    (receipt,) = _added(before, after, "idempotency_receipts")
    projection = {**receipt["result_projection"], "appointment_id": other["id"]}
    forged = {
        **receipt,
        "id": str(uuid4()),
        "idempotency_key": other["id"],
        "result_projection": projection,
    }
    # Probe the evolved database CHECK itself using migration-owned fixture writes.
    # Runtime principals cannot insert such rows directly.
    for changes in (
        {"actor_id": str(uuid4())},
        {"staff_id": clinic.owner.staff_id},
        {"operation": "appointments.manual_no_show"},
        {"result_projection": None},
        {"result_projection": {**projection, "status": "Booked"}},
        {"result_projection": {**projection, "unexpected": True}},
    ):
        _reject(
            clinic.migration,
            "INSERT INTO public.idempotency_receipts SELECT * FROM jsonb_populate_record(NULL::public.idempotency_receipts, CAST(:body AS jsonb))",
            {"body": json.dumps({**forged, **changes})},
            "23514",
        )
    assert effects(clinic) == after


def _claimed_case(clinic):
    version = _configure(clinic)
    target, day = historical_claim(clinic)
    return target, _scope(clinic, day, version)


@contextmanager
def _worker_transaction(worker, candidate):
    with worker.begin() as connection:
        yield connection, lambda: _apply(connection, candidate)


def _race(
    clinic,
    waiter_engine,
    waiter_statement,
    holder_transaction,
    waiter_transaction,
    before,
):
    """No sleep-based race: retain the first transaction until PostgreSQL identifies its waiter."""
    waiting = Event()
    waiter_pid = []
    committed = []

    def observe(connection, _cursor, statement, _params, _context, _many):
        if waiter_statement in statement:
            waiter_pid.append(connection.connection.driver_connection.info.backend_pid)
            waiting.set()

    def compete():
        with waiter_transaction() as (_, execute):
            # For staff, activation already owns the Organization; for worker,
            # the blocking protected call is executed below.
            try:
                result = execute()
            except (ContractError, DBAPIError):
                # After the real lock wait, the committed winner is visible.
                committed.append(effects(clinic))
                raise
            return result

    with ThreadPoolExecutor(max_workers=1) as pool:
        with holder_transaction() as (holder, execute):
            holder_pid = holder.scalar(text("SELECT pg_backend_pid()"))
            winner = execute()
            event.listen(waiter_engine, "before_cursor_execute", observe)
            try:
                future = pool.submit(compete)
                assert waiting.wait(timeout=15)
                assert waiter_pid[0] != holder_pid
                with clinic.migration.connect() as observer:
                    wait_for_postgres_lock(observer, waiter_pid[0], holder_pid)
                assert effects(clinic) == before
            finally:
                event.remove(waiter_engine, "before_cursor_execute", observe)
        return winner, future, committed


@pytest.mark.parametrize("checkin_first", [True, False])
def test_cutoff_and_checkin_both_lock_orders_release_only_if_absent(
    followup_clinic, worker, checkin_first
):
    clinic = followup_clinic
    target, scope = _claimed_case(clinic)
    (candidate,) = _selected(worker, scope)
    assert candidate["target"] == target
    key = str(uuid4())
    command = CheckInAppointment(
        physical_presence_confirmed=True, expected_appointment_revision=0
    )

    def checking():
        return _checkin(clinic, clinic.receptionist, target, command, key)

    def cutting():
        return _worker_transaction(worker, candidate)

    before = effects(clinic)
    with (
        pytest.raises(RuntimeError, match="synthetic claim rollback"),
        worker.begin() as connection,
    ):
        assert _apply(connection, candidate)["claim_revision"] == 2
        raise RuntimeError("synthetic claim rollback")
    assert effects(clinic) == before
    winner, future, committed = _race(
        clinic,
        worker if checkin_first else clinic.runtime,
        "SELECT public.f01_b20_cutoff_appointment"
        if checkin_first
        else "SELECT public.f01_activate()",
        checking if checkin_first else cutting,
        cutting if checkin_first else checking,
        before,
    )
    if checkin_first:
        with pytest.raises(DBAPIError) as denied:
            future.result(timeout=15)
        assert denied.value.orig.sqlstate == "23514"
    else:
        with pytest.raises(ContractError) as denied:
            future.result(timeout=15)
        assert denied.value.status_code == 409
    after = effects(clinic)
    assert committed == [after]
    if not checkin_first:
        _assert_system_effects(before, after, candidate, winner)
        return
    assert _row(after, "appointments", target)["status"] == "Waiting"
    assert [
        row["state"]
        for row in after["followup_claims"]
        if row["appointment_id"] == str(target)
    ] == ["ACTIVE"]
    assert len(_added(before, after, "appointment_history")) == 1
    assert len(_added(before, after, "sensitive_audit")) == 1
    assert len(_added(before, after, "outbox_events")) == 1
    (receipt,) = _added(before, after, "idempotency_receipts")
    assert receipt["actor_kind"] == "STAFF" and receipt["idempotency_key"] == key
    assert len(_added(before, after, "queue_entries")) == 1
    for table in (
        "appointment_day_counters",
        "appointment_number_assignments",
        "appointment_price_snapshots",
        "followup_claims",
        "visits",
        "clinical_revisions",
        "clinical_audit",
    ):
        assert before[table] == after[table]


def test_competing_workers_recover_same_committed_projection(booking_clinic, worker):
    clinic = booking_clinic
    day = _today() - timedelta(days=1)
    _seed(clinic, day, "Concurrent")
    (candidate,) = _selected(worker, _scope(clinic, day, _configure(clinic)))
    before = effects(clinic)
    first, second, committed = _race(
        clinic,
        worker,
        "SELECT public.f01_b20_cutoff_appointment",
        lambda: _worker_transaction(worker, candidate),
        lambda: _worker_transaction(worker, candidate),
        before,
    )
    assert second.result(timeout=15) == first
    assert committed == []
    _assert_system_effects(before, effects(clinic), candidate, first)


def test_batch_failure_rolls_back_only_failed_record_and_retry_finishes(
    booking_clinic, worker
):
    clinic = booking_clinic
    day = _today() - timedelta(days=1)
    for name in ("First", "Second", "Third"):
        _seed(clinic, day, name)
    version = _configure(clinic)
    scope = _scope(clinic, day, version)
    candidates = _selected(worker, scope)
    assert len(candidates) == 3
    before = effects(clinic)
    calls = []

    def fail_after_second_write(
        _connection, _cursor, statement, _parameters, _context, _many
    ):
        if "SELECT public.f01_b20_cutoff_appointment" in statement:
            calls.append(statement)
            if len(calls) == 2:
                raise RuntimeError("synthetic post-write cutoff failure")

    event.listen(worker, "after_cursor_execute", fail_after_second_write)
    try:
        with pytest.raises(RuntimeError, match="synthetic post-write cutoff failure"):
            apply_selected_cutoff(worker, clinic.tenant, day, version)
    finally:
        event.remove(worker, "after_cursor_execute", fail_after_second_write)
    partial = effects(clinic)
    (receipt,) = _added(before, partial, "idempotency_receipts")
    _assert_system_effects(before, partial, candidates[0], receipt["result_projection"])
    assert [row["target"] for row in _selected(worker, scope)] == [
        row["target"] for row in candidates[1:]
    ]
    assert apply_selected_cutoff(worker, clinic.tenant, day, version) == 2
    final = effects(clinic)
    for table in (
        "appointment_history",
        "sensitive_audit",
        "outbox_events",
        "idempotency_receipts",
    ):
        assert len(_added(before, final, table)) == 3
    assert apply_selected_cutoff(worker, clinic.tenant, day, version) == 0
    assert effects(clinic) == final

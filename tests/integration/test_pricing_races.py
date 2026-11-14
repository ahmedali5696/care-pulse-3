"""B-06 conflicting pricing transactions and immutable frozen-price evidence."""

from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from uuid import UUID, uuid4

import pytest
from carepulse.appointments.persistence import persist_appointment
from carepulse.db import transaction
from carepulse.main import app
from carepulse.policies.pricing_contracts import PricingPatch
from carepulse.policies.pricing_service import current_price_state, update_pricing
from carepulse.policies.versions import append_version
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from test_appointment_persistence import (
    _draft,
    _patient,
    _query,
)
from test_patients import patient_backend as patient_backend  # noqa: PLC0414
from test_pricing import (
    _blocked_by,
    _fresh_receptionist_session,
    _owner_changes_manager,
    _patch,
    _pricing_evidence,
    _pricing_transaction,
    pricing_request,
)
from test_request_boundary import boundary_data as boundary_data  # noqa: PLC0414


def _change(revision, **fields):
    return PricingPatch(
        expected_price_revision=revision,
        reason="Synthetic authorized pricing publication",
        **fields,
    )


def test_concurrent_owner_writes_same_revision_have_one_serial_winner(
    patient_backend,
):
    clinic = patient_backend
    ready = Barrier(2)

    def patch(value):
        ready.wait(timeout=15)
        return pricing_request(
            clinic,
            clinic.owner,
            method="PATCH",
            body=_patch(standard_new_exam_egp=value),
            key=str(uuid4()),
        )

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(patch, ("100.00", "120.00")))
    successes = [item for item in results if item[0] == 200]
    stale = [item for item in results if item[0] == 409]
    assert len(successes) == len(stale) == 1, results
    assert stale[0][1]["error"]["code"] == "STALE_REVISION"
    with transaction(clinic.migration, clinic.tenant) as conn:
        state = current_price_state(conn, clinic.tenant)
        assert state["PRICE"]["revision"] == 2
        assert state["PRICE"]["payload"]["standard_new_exam_egp"] in (
            "100.00",
            "120.00",
        )
        assert (
            conn.scalar(
                text("""
            SELECT count(*) FROM public.clinic_policy_versions
            WHERE organization_id = current_setting('app.organization_id')::uuid AND kind = 'PRICE'
        """)
            )
            == 2
        )


def test_signed_identical_pricing_retry_after_policy_lock_replays_original(
    patient_backend,
):
    clinic = patient_backend
    app.state.service_config, app.state.db_engine = clinic.config, clinic.runtime
    body = _patch(standard_new_exam_egp="125.00")
    key = str(uuid4())
    with ThreadPoolExecutor(max_workers=1) as pool:
        with _pricing_transaction(clinic, clinic.owner, body, key) as (
            connection,
            live_actor,
        ):
            published = update_pricing(
                connection, live_actor, PricingPatch.model_validate(body), key
            )
            holder = connection.scalar(text("SELECT pg_backend_pid()"))
            future = pool.submit(
                pricing_request,
                clinic,
                clinic.owner,
                method="PATCH",
                body=body,
                key=key,
            )
            _blocked_by(clinic, holder)
        status, result = future.result(timeout=30)
    assert status == 200 and result["data"] == published
    assert (
        pricing_request(
            clinic,
            clinic.owner,
            method="PATCH",
            body={**body, "expected_price_revision": 999},
            key=key,
        )[0]
        == 409
    )
    with transaction(clinic.migration, clinic.tenant) as connection:
        assert (
            connection.scalar(
                text(
                    "SELECT count(*) FROM public.clinic_policy_versions "
                    "WHERE organization_id = current_setting('app.organization_id')::uuid AND kind='PRICE'"
                )
            )
            == 2
        )
        assert (
            connection.scalar(
                text(
                    "SELECT count(*) FROM public.idempotency_receipts "
                    "WHERE organization_id = current_setting('app.organization_id')::uuid "
                    "AND operation='settings.pricing.update' AND idempotency_key=:key"
                ),
                {"key": key},
            )
            == 1
        )


def test_direct_identical_pricing_rechecks_receipt_after_authority_lock(
    patient_backend,
):
    clinic = patient_backend
    key = str(uuid4())
    change = _change(1, standard_new_exam_egp="125.00")
    body = change.model_dump(mode="json")

    def competing_update():
        with _pricing_transaction(clinic, clinic.owner, body, key) as (
            connection,
            actor,
        ):
            return update_pricing(connection, actor, change, key)

    with ThreadPoolExecutor(max_workers=1) as pool:
        with _pricing_transaction(clinic, clinic.owner, body, key) as (
            connection,
            actor,
        ):
            original = update_pricing(
                connection,
                actor,
                change,
                key,
            )
            holder_pid = connection.scalar(text("SELECT pg_backend_pid()"))
            future = pool.submit(competing_update)
            # F01 holds the organization lock before either request reaches policy locks.
            blocked = _blocked_by(clinic, holder_pid)
            with clinic.migration.connect() as observer:
                blocked_sql = observer.scalar(
                    text("SELECT query FROM pg_stat_activity WHERE pid=:pid"),
                    {"pid": blocked},
                )
                assert blocked_sql and "public.f01_activate()" in blocked_sql
        replay = future.result(timeout=30)
    assert replay == original
    with transaction(clinic.migration, clinic.tenant) as connection:
        assert (
            connection.scalar(
                text(
                    "SELECT count(*) FROM public.clinic_policy_versions "
                    "WHERE organization_id = current_setting('app.organization_id')::uuid AND kind='PRICE'"
                )
            )
            == 2
        )
        assert (
            connection.scalar(
                text(
                    "SELECT count(*) FROM public.idempotency_receipts "
                    "WHERE organization_id = current_setting('app.organization_id')::uuid "
                    "AND operation='settings.pricing.update' AND idempotency_key=:key"
                ),
                {"key": key},
            )
            == 1
        )


@pytest.mark.parametrize("first", ["owner", "manager"])
def test_live_owner_manager_followup_publication_serializes_stale_revision(
    patient_backend, first
):
    clinic = patient_backend
    _owner_changes_manager(clinic, True, 0)
    manager = _fresh_receptionist_session(clinic, 1)
    before = _pricing_evidence(clinic)
    actors = {"owner": clinic.owner, "manager": manager}
    first_body = {
        **_patch(followup_last_day_inclusive=3),
        "expected_followup_revision": 1,
    }
    second_body = {
        **_patch(followup_last_day_inclusive=10),
        "expected_followup_revision": 1,
    }
    second = "manager" if first == "owner" else "owner"

    with ThreadPoolExecutor(max_workers=2) as pool:
        key = str(uuid4())
        with _pricing_transaction(clinic, actors[first], first_body, key) as (
            connection,
            live_actor,
        ):
            published = update_pricing(
                connection,
                live_actor,
                PricingPatch.model_validate(first_body),
                key,
            )
            assert published["followup_revision"] == 2
            holder = connection.scalar(text("SELECT pg_backend_pid()"))
            future = pool.submit(
                pricing_request,
                clinic,
                actors[second],
                method="PATCH",
                body=second_body,
                key=str(uuid4()),
            )
            _blocked_by(clinic, holder)
        status, response = future.result(timeout=30)
    assert status == 409 and response["error"]["code"] == "STALE_REVISION"
    after = _pricing_evidence(clinic)
    assert after[0] == (("FOLLOWUP", 2, 2), ("PRICE", 1, 1))
    assert after[1] == {
        **before[1],
        "versions": before[1]["versions"] + 1,
        "audit": before[1]["audit"] + 1,
        "receipts": before[1]["receipts"] + 1,
    }
    with clinic.migration.connect() as connection:
        evidence = connection.execute(
            text("""
                SELECT actor_staff_id, actor_role
                FROM public.sensitive_audit
                WHERE organization_id=:tenant AND action='settings.pricing.update'
            """),
            {"tenant": clinic.tenant},
        ).all()
    assert evidence == [
        (
            UUID(actors[first].staff_id),
            "DOCTOR_OWNER" if first == "owner" else "RECEPTIONIST",
        )
    ]


def test_standard_raise_and_urgent_enable_cannot_publish_invalid_mixed_state(
    patient_backend,
):
    clinic = patient_backend
    with transaction(clinic.migration, clinic.tenant) as conn:
        append_version(
            conn,
            str(clinic.tenant),
            "SCHEDULE",
            {
                "booking_mode": "OPEN",
                "duration_minutes": None,
                "holiday_booking_enabled": False,
                "routine_walk_in_enabled": False,
                "allow_overlap": False,
                "no_show_cutoff": {"enabled": False, "local_time": None},
            },
            1,
            intervals=[
                {
                    "interval_kind": "WORK",
                    "weekday": 1,
                    "service_date": None,
                    "starts_at": "09:00",
                    "ends_at": "17:00",
                },
            ],
        )
    status, result = pricing_request(
        clinic,
        clinic.owner,
        method="PATCH",
        body=_patch(standard_new_exam_egp="100.00", urgent_base_egp="120.00"),
        key=str(uuid4()),
    )
    assert status == 200, result

    changes = (
        {"standard_new_exam_egp": "130.00"},
        {"urgent_enabled": True},
    )
    ready = Barrier(2)

    def patch(change):
        ready.wait(timeout=15)
        return pricing_request(
            clinic,
            clinic.owner,
            method="PATCH",
            body=_patch(expected_price_revision=2, **change),
            key=str(uuid4()),
        )

    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(pool.map(patch, changes))
    assert sum(outcome[0] == 200 for outcome in outcomes) == 1, outcomes
    assert sum(outcome[0] == 409 for outcome in outcomes) == 1, outcomes
    with transaction(clinic.migration, clinic.tenant) as conn:
        price = current_price_state(conn, clinic.tenant)["PRICE"]
        assert price["revision"] == 3
        payload = price["payload"]
        assert not (
            payload["urgent_enabled"]
            and payload["urgent_base_egp"] <= payload["standard_new_exam_egp"]
        )


def test_existing_snapshot_remains_byte_identical_after_price_and_followup_edits(
    patient_backend,
):
    clinic = patient_backend
    tenant, doctor = clinic.tenant, UUID(clinic.owner.staff_id)
    change = _change(1, standard_new_exam_egp="100.00")
    key = str(uuid4())
    with _pricing_transaction(
        clinic, clinic.owner, change.model_dump(mode="json"), key
    ) as (conn, actor):
        update_pricing(conn, actor, change, key)
    with transaction(clinic.migration, tenant) as conn:
        saved = persist_appointment(
            conn,
            _draft(
                _patient(conn, tenant), doctor, price_snapshot={"pricing_version": 2}
            ),
            _query(tenant, doctor),
        )

    def snapshot(conn):
        return conn.scalar(
            text("""
            SELECT row_to_json(s)::text
            FROM public.appointment_price_snapshots AS s
            WHERE organization_id = :tenant AND appointment_id = :id
        """),
            {"tenant": tenant, "id": saved["id"]},
        )

    with transaction(clinic.migration, tenant) as conn:
        before = snapshot(conn)
    assert before is not None
    change = _change(
        2,
        standard_new_exam_egp="200.00",
        followup_last_day_inclusive=9,
        expected_followup_revision=1,
    )
    key = str(uuid4())
    with _pricing_transaction(
        clinic, clinic.owner, change.model_dump(mode="json"), key
    ) as (conn, actor):
        update_pricing(conn, actor, change, key)
        assert snapshot(conn) == before
    with transaction(clinic.migration, tenant) as conn:
        assert snapshot(conn) == before
        state = current_price_state(conn, tenant)
        assert state["PRICE"]["revision"] == 3
        assert state["FOLLOWUP"]["revision"] == 2


def test_sql_failure_rolls_back_versions_heads_audit_outbox_and_receipt(
    patient_backend,
):
    clinic = patient_backend
    tenant = clinic.tenant
    sql_counts = {
        "versions": "SELECT count(*) FROM public.clinic_policy_versions WHERE organization_id = current_setting('app.organization_id')::uuid",
        "audit": "SELECT count(*) FROM public.sensitive_audit WHERE organization_id = current_setting('app.organization_id')::uuid",
        "outbox": "SELECT count(*) FROM public.outbox_events WHERE organization_id = current_setting('app.organization_id')::uuid",
        "receipts": "SELECT count(*) FROM public.idempotency_receipts WHERE organization_id = current_setting('app.organization_id')::uuid",
    }
    with transaction(clinic.migration, tenant) as conn:
        before = {name: conn.scalar(text(sql)) for name, sql in sql_counts.items()}
        heads = tuple(
            conn.execute(
                text("""
            SELECT kind, current_version, revision
            FROM public.clinic_policy_heads
            WHERE organization_id = current_setting('app.organization_id')::uuid ORDER BY kind
        """)
            ).all()
        )
    change = _change(1, standard_new_exam_egp="100.00")
    key = str(uuid4())
    with (
        pytest.raises(DBAPIError) as error,
        _pricing_transaction(
            clinic, clinic.owner, change.model_dump(mode="json"), key
        ) as (conn, actor),
    ):
        update_pricing(conn, actor, change, key)
        conn.execute(text("SELECT 1/0"))
    assert error.value.orig.sqlstate == "22012"
    with transaction(clinic.migration, tenant) as conn:
        assert {
            name: conn.scalar(text(sql)) for name, sql in sql_counts.items()
        } == before
        assert (
            tuple(
                conn.execute(
                    text("""
            SELECT kind, current_version, revision
            FROM public.clinic_policy_heads
            WHERE organization_id = current_setting('app.organization_id')::uuid ORDER BY kind
        """)
                ).all()
            )
            == heads
        )

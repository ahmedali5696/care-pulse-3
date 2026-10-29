"""B-06 conflicting pricing transactions and immutable frozen-price evidence."""

from uuid import uuid4

import pytest
from carepulse.appointments.persistence import persist_appointment
from carepulse.db import transaction
from carepulse.identity.authorization import AuthorizedStaff
from carepulse.identity.contracts import ContractError
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
from test_appointment_persistence import (
    appointment_storage as appointment_storage,  # noqa: PLC0414 - fixture export
)


def _owner(tenant, staff):
    return AuthorizedStaff(tenant, staff, "DOCTOR_OWNER", False, 0, 0, 0)


def _change(revision, **fields):
    return PricingPatch(
        expected_price_revision=revision,
        reason="Synthetic authorized pricing publication",
        **fields,
    )


def test_concurrent_owner_writes_same_revision_have_one_serial_winner(
    appointment_storage, two_transactions
):
    _, _, (tenant, _), (doctor, _) = appointment_storage
    actor = _owner(tenant, doctor)
    changes = iter(("100.00", "120.00"))

    def patch(connection):
        value = next(changes)
        return update_pricing(
            connection, actor, _change(1, standard_new_exam_egp=value), str(uuid4())
        )

    results = two_transactions("carepulse_runtime", patch)
    successes = [item for item in results if isinstance(item, dict)]
    stale = [item for item in results if isinstance(item, ContractError)]
    assert len(successes) == len(stale) == 1, results
    assert stale[0].status_code == 409 and stale[0].code == "STALE_REVISION"
    runtime = appointment_storage[0]
    with transaction(runtime, tenant) as conn:
        state = current_price_state(conn, tenant)
        assert state["PRICE"]["revision"] == 2
        assert state["PRICE"]["payload"]["standard_new_exam_egp"] in (
            "100.00",
            "120.00",
        )
        assert (
            conn.scalar(
                text("""
            SELECT count(*) FROM public.clinic_policy_versions WHERE kind = 'PRICE'
        """)
            )
            == 2
        )


def test_standard_raise_and_urgent_enable_cannot_publish_invalid_mixed_state(
    appointment_storage, two_transactions
):
    runtime, _, (tenant, _), (doctor, _) = appointment_storage
    actor = _owner(tenant, doctor)
    with transaction(runtime, tenant) as conn:
        append_version(
            conn,
            str(tenant),
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
        update_pricing(
            conn,
            actor,
            _change(1, standard_new_exam_egp="100.00", urgent_base_egp="120.00"),
            str(uuid4()),
        )

    changes = iter(
        (
            {"standard_new_exam_egp": "130.00"},
            {"urgent_enabled": True},
        )
    )

    def patch(connection):
        return update_pricing(
            connection, actor, _change(2, **next(changes)), str(uuid4())
        )

    outcomes = two_transactions("carepulse_runtime", patch)
    assert sum(isinstance(outcome, dict) for outcome in outcomes) == 1, outcomes
    assert (
        sum(
            isinstance(outcome, ContractError) and outcome.status_code == 409
            for outcome in outcomes
        )
        == 1
    ), outcomes
    with transaction(runtime, tenant) as conn:
        price = current_price_state(conn, tenant)["PRICE"]
        assert price["revision"] == 3
        payload = price["payload"]
        assert not (
            payload["urgent_enabled"]
            and payload["urgent_base_egp"] <= payload["standard_new_exam_egp"]
        )


def test_existing_snapshot_remains_byte_identical_after_price_and_followup_edits(
    appointment_storage,
):
    runtime, _, (tenant, _), (doctor, _) = appointment_storage
    actor = _owner(tenant, doctor)
    with transaction(runtime, tenant) as conn:
        update_pricing(
            conn, actor, _change(1, standard_new_exam_egp="100.00"), str(uuid4())
        )
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
            WHERE appointment_id = :id
        """),
            {"id": saved["id"]},
        )

    with transaction(runtime, tenant) as conn:
        before = snapshot(conn)
        update_pricing(
            conn,
            actor,
            _change(
                2,
                standard_new_exam_egp="200.00",
                followup_last_day_inclusive=9,
                expected_followup_revision=1,
            ),
            str(uuid4()),
        )
        assert snapshot(conn) == before
    with transaction(runtime, tenant) as conn:
        assert snapshot(conn) == before
        state = current_price_state(conn, tenant)
        assert state["PRICE"]["revision"] == 3
        assert state["FOLLOWUP"]["revision"] == 2


def test_sql_failure_rolls_back_versions_heads_audit_outbox_and_receipt(
    appointment_storage,
):
    runtime, _, (tenant, _), (doctor, _) = appointment_storage
    actor = _owner(tenant, doctor)
    sql_counts = {
        "versions": "SELECT count(*) FROM public.clinic_policy_versions",
        "audit": "SELECT count(*) FROM public.sensitive_audit",
        "outbox": "SELECT count(*) FROM public.outbox_events",
        "receipts": "SELECT count(*) FROM public.idempotency_receipts",
    }
    with transaction(runtime, tenant) as conn:
        before = {name: conn.scalar(text(sql)) for name, sql in sql_counts.items()}
        heads = tuple(
            conn.execute(
                text("""
            SELECT kind, current_version, revision
            FROM public.clinic_policy_heads ORDER BY kind
        """)
            ).all()
        )
    with pytest.raises(DBAPIError) as error, transaction(runtime, tenant) as conn:
        update_pricing(
            conn, actor, _change(1, standard_new_exam_egp="100.00"), str(uuid4())
        )
        conn.execute(text("SELECT 1/0"))
    assert error.value.orig.sqlstate == "22012"
    with transaction(runtime, tenant) as conn:
        assert {
            name: conn.scalar(text(sql)) for name, sql in sql_counts.items()
        } == before
        assert (
            tuple(
                conn.execute(
                    text("""
            SELECT kind, current_version, revision
            FROM public.clinic_policy_heads ORDER BY kind
        """)
                ).all()
            )
            == heads
        )

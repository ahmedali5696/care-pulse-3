"""T070: real F-01 predecessor receipt survives the final Wave 3 upgrade."""

import os
from pathlib import Path
from uuid import uuid4

import psycopg
import pytest
from carepulse.config import _load_policy
from sqlalchemy import create_engine, text
from test_booking import booking_clinic as booking_clinic  # noqa: PLC0414
from test_database import admin as admin  # noqa: PLC0414
from test_database import disposable_project as disposable_project  # noqa: PLC0414
from test_database import empty_database as empty_database  # noqa: PLC0414
from test_f01_grants_migration import _bootstrap, _historic_rows, _upgrade
from test_followups import followup_clinic as followup_clinic  # noqa: PLC0414
from test_patients import patient_backend as patient_backend  # noqa: PLC0414
from test_request_boundary import boundary_data as boundary_data  # noqa: PLC0414
from test_w3_reconciliation import _body, _request, historical_claim
from test_w3_visits import effects

PREDECESSOR = "0071_b20_staff"
HEAD = "0072_b20_worker"


@pytest.fixture
def role_engine(role_engine, admin, empty_database, monkeypatch):
    _bootstrap(admin, empty_database)
    monkeypatch.setenv(
        "CP_POLICY_FILE",
        str(Path(__file__).parents[1] / "fixtures/foundation-policy.json"),
    )

    def connect_as(role):
        original = role_engine(role)
        try:
            return create_engine(
                original.url.set(database=empty_database), hide_parameters=True
            )
        finally:
            original.dispose()

    admin.execute(
        psycopg.sql.SQL(
            "GRANT CONNECT ON DATABASE {} TO carepulse_api, carepulse_gate"
        ).format(psycopg.sql.Identifier(empty_database))
    )
    migration = connect_as("carepulse_migration")
    try:
        _upgrade(migration, PREDECESSOR)
        policy = _load_policy()
        with migration.begin() as connection:
            connection.execute(text("SET LOCAL ROLE carepulse_identity_owner"))
            connection.execute(
                text("""INSERT INTO f01_private.policy (signed_request_freshness_seconds,
                    signed_request_max_skew_seconds, session_idle_minutes)
                    VALUES (:freshness,:skew,:idle)"""),
                {
                    "freshness": policy.signed_request_freshness_seconds,
                    "skew": policy.signed_request_max_skew_seconds,
                    "idle": policy.session_idle_minutes,
                },
            )
            connection.execute(
                text(
                    "INSERT INTO f01_private.keys (key_id,secret,status) VALUES (:id,:secret,'ACTIVE')"
                ),
                {
                    "id": os.environ["CP_DB_IDENTITY_KEY_ID"],
                    "secret": bytes.fromhex(
                        Path("/run/secrets/db_identity_key").read_text().strip()
                    ),
                },
            )
        yield connect_as
    finally:
        migration.dispose()


def _assert_writer_grants(connection, *, worker_enabled):
    expected = {
        "public.f01_b13_settings()": "carepulse_api",
        "public.f01_b13_publish()": "carepulse_api",
        "public.f01_b13_place()": "carepulse_api",
        "public.f01_b19_store_preview(jsonb)": "carepulse_api",
        "public.f01_b19_read_preview()": "carepulse_api",
        "public.f01_b19_prepare()": "carepulse_api",
        "public.f01_b19_apply(jsonb)": "carepulse_api",
        "public.f01_b20_staff_mutation()": "carepulse_api",
    }
    if worker_enabled:
        expected.update(
            {
                "public.f01_b20_cutoff_candidates(uuid,date,bigint)": "carepulse_worker",
                "public.f01_b20_cutoff_appointment(uuid,date,bigint,uuid,bigint,bytea)": "carepulse_worker",
            }
        )
    for signature, allowed in expected.items():
        assert connection.scalars(
            text("""SELECT role_name FROM unnest(ARRAY['public','carepulse_runtime',
                'carepulse_api','carepulse_gate','carepulse_web','carepulse_worker',
                'carepulse_operator','carepulse_auth','carepulse_sender']) AS role_name
                WHERE has_function_privilege(role_name,:signature,'EXECUTE')"""),
            {"signature": signature},
        ).all() == [allowed], signature


def test_signed_predecessor_claim_receipt_survives_worker_upgrade(followup_clinic):
    clinic = followup_clinic
    target, _ = historical_claim(clinic)
    body = {**_body(), "expected_claim_revision": 1}
    key = str(uuid4())
    with clinic.migration.connect() as connection:
        assert (
            connection.scalar(text("SELECT version_num FROM public.alembic_version"))
            == PREDECESSOR
        )
        _assert_writer_grants(connection, worker_enabled=False)
    with clinic.runtime.connect() as connection:
        assert connection.scalar(text("SELECT session_user")) == "carepulse_api"
    with clinic.gate.connect() as connection:
        assert connection.scalar(text("SELECT session_user")) == "carepulse_gate"
    status, reply = _request(clinic, clinic.owner, target, body, key)
    assert status == 200, reply
    assert reply["data"]["claim_revision"] == 2
    before = effects(clinic)
    (receipt,) = [
        row for row in before["idempotency_receipts"] if row["idempotency_key"] == key
    ]
    assert receipt["staff_id"] == clinic.owner.staff_id
    assert receipt["result_projection"] == reply["data"]
    with clinic.migration.connect() as connection:
        retained = _historic_rows(connection)
    _upgrade(clinic.migration, HEAD)
    with clinic.migration.connect() as connection:
        assert (
            connection.scalar(text("SELECT version_num FROM public.alembic_version"))
            == HEAD
        )
        assert _historic_rows(connection) == retained
        _assert_writer_grants(connection, worker_enabled=True)
    assert _request(clinic, clinic.owner, target, body, key) == (status, reply)
    assert effects(clinic) == before

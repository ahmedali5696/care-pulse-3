"""F-01 adversarial evidence using real service logins in disposable CI."""

from dataclasses import replace
from pathlib import Path
from uuid import uuid4

import pytest
from alembic.config import Config
from alembic.script import ScriptDirectory
from carepulse.identity.contracts import ContractError
from carepulse.identity.runtime_identity import bound_transaction
from carepulse.identity.signing import canonical_payload
from runtime_identity_fixture import db_proof_headers, registered_test_request
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError, OperationalError
from test_request_boundary import _signed
from test_request_boundary import boundary_data as boundary_data  # noqa: PLC0414


def _register(connection, envelope, body, headers):
    proof = dict(headers)
    return connection.scalar(
        text("SELECT public.f01_register_proof(:payload, :key, :mac, :body)"),
        {
            "payload": canonical_payload(envelope),
            "key": proof[b"x-carepulse-db-key-id"].decode("ascii"),
            "mac": bytes.fromhex(proof[b"x-carepulse-db-proof"].decode("ascii")),
            "body": body,
        },
    )


def _spoof_gucs(connection, clinic):
    connection.execute(
        text("""
        SELECT set_config('app.organization_id', :tenant, true),
               set_config('app.staff_id', :staff, true),
               set_config('app.staff_role', 'DOCTOR_OWNER', true)
        """),
        {"tenant": str(clinic.tenant), "staff": str(clinic.staff)},
    )


def test_exact_head_restricts_legacy_login_and_service_role_elevation(role_engine):
    migration = role_engine("carepulse_migration")
    legacy = role_engine("carepulse_runtime")
    try:
        root = Path(__file__).resolve().parents[2]
        config = Config(str(root / "alembic.ini"))
        config.set_main_option("script_location", str(root / "db/migrations"))
        expected_head = ScriptDirectory.from_config(config).get_current_head()
        with migration.connect() as connection:
            assert (
                connection.scalar(
                    text("SELECT version_num FROM public.alembic_version")
                )
                == expected_head
            )
            assert connection.scalar(
                text(
                    "SELECT NOT rolcanlogin FROM pg_roles WHERE rolname='carepulse_runtime'"
                )
            )
        with pytest.raises(OperationalError):
            legacy.connect()
    finally:
        migration.dispose()
        legacy.dispose()


@pytest.mark.parametrize(
    "role", ["carepulse_api", "carepulse_web", "carepulse_worker", "carepulse_gate"]
)
def test_real_service_logins_cannot_forge_authority_with_gucs(
    boundary_data, role_engine, role
):
    clinic = boundary_data
    engine = role_engine(role)
    try:
        with engine.begin() as connection:
            assert connection.scalar(text("SELECT session_user")) == role
            assert connection.scalar(
                text(
                    "SELECT NOT rolsuper AND NOT rolbypassrls AND NOT rolcreaterole FROM pg_roles WHERE rolname=session_user"
                )
            )
            _spoof_gucs(connection, clinic)
            if role == "carepulse_api":
                assert connection.scalar(text("SELECT public.f01_org()")) is None
                assert (
                    connection.scalar(text("SELECT count(*) FROM public.organizations"))
                    == 0
                )
            else:
                with pytest.raises(DBAPIError) as denied, connection.begin_nested():
                    connection.execute(text("SELECT id FROM public.organizations"))
                assert denied.value.orig.sqlstate == "42501"
            with pytest.raises(DBAPIError) as denied, connection.begin_nested():
                connection.execute(text("SELECT public.f01_activate()"))
            assert denied.value.orig.sqlstate == "42501"
            for target in ("carepulse_identity_owner", "carepulse_migration"):
                assert not connection.scalar(
                    text("SELECT pg_has_role(session_user, :role, 'MEMBER')"),
                    {"role": target},
                )
    finally:
        engine.dispose()


@pytest.mark.parametrize(
    "change", ["tenant", "staff", "session", "role", "purpose", "path", "body", "key"]
)
def test_gate_rejects_mutated_proof_envelope(boundary_data, change):
    clinic = boundary_data
    identity, body, _ = _signed(clinic)
    headers = db_proof_headers(identity)
    changes = {
        "tenant": {"organization_id": str(clinic.other)},
        "staff": {"staff_id": str(uuid4())},
        "session": {"session_id": "forged-session"},
        "role": {"role": "RECEPTIONIST"},
        "purpose": {"purpose": "APPOINTMENT_BOOKING"},
        "path": {"path": "/v1/appointments"},
    }
    if change in changes:
        identity = replace(identity, **changes[change])
    elif change == "body":
        body += b" "
    else:
        headers = [
            (name, b"missing-key" if name == b"x-carepulse-db-key-id" else value)
            for name, value in headers
        ]
    with pytest.raises(DBAPIError) as denied, clinic.gate.begin() as connection:
        _register(connection, identity, body, headers)
    assert denied.value.orig.sqlstate in ("42501", "22023")


def test_gate_rejects_valid_mac_for_unapproved_route_purpose(boundary_data):
    clinic = boundary_data
    identity, body, _ = _signed(clinic)
    identity = replace(identity, purpose="APPOINTMENT_BOOKING")
    with pytest.raises(DBAPIError) as denied, clinic.gate.begin() as connection:
        _register(connection, identity, body, db_proof_headers(identity))
    assert denied.value.orig.sqlstate in ("42501", "22023")


@pytest.mark.parametrize("change", ["tenant", "staff", "session", "role"])
def test_valid_signature_cannot_override_live_actor_authority(boundary_data, change):
    clinic = boundary_data
    identity, body, _ = _signed(clinic)
    identity = replace(
        identity,
        **{
            "tenant": {"organization_id": str(clinic.other)},
            "staff": {"staff_id": str(uuid4())},
            "session": {"session_id": "forged-session"},
            "role": {"role": "RECEPTIONIST"},
        }[change],
    )
    with (
        registered_test_request(clinic, identity, body),
        pytest.raises(ContractError) as denied,
        bound_transaction(clinic.runtime, identity),
    ):
        pytest.fail("Forged live authority activated")
    assert denied.value.status_code in (401, 403)


@pytest.mark.parametrize("outcome", ["commit", "rollback"])
def test_bound_identity_is_transaction_local_and_replay_is_denied(
    boundary_data, outcome
):
    clinic = boundary_data
    identity, body, _ = _signed(clinic)
    pid = None
    with registered_test_request(clinic, identity, body):
        try:
            with bound_transaction(clinic.runtime, identity) as connection:
                pid = connection.scalar(text("SELECT pg_backend_pid()"))
                assert connection.scalar(text("SELECT session_user")) == "carepulse_api"
                _spoof_gucs(connection, clinic)
                connection.execute(
                    text("SELECT set_config('app.organization_id', :tenant, true)"),
                    {"tenant": str(clinic.other)},
                )
                assert connection.execute(
                    text(
                        "SELECT organization_id, staff_id, purpose, stage FROM public.f01_context()"
                    )
                ).one() == (
                    str(clinic.tenant),
                    str(clinic.staff),
                    identity.purpose,
                    "PRIMARY",
                )
                assert connection.execute(
                    text("SELECT id FROM public.organizations")
                ).scalars().all() == [clinic.tenant]
                assert (
                    connection.scalar(
                        text("SELECT count(*) FROM public.clinic_policy_versions")
                    )
                    == 0
                )
                if outcome == "rollback":
                    raise RuntimeError("synthetic rollback")
        except RuntimeError as error:
            assert outcome == "rollback" and str(error) == "synthetic rollback"
        with clinic.runtime.begin() as connection:
            assert connection.scalar(text("SELECT pg_backend_pid()")) == pid
            assert connection.scalar(text("SELECT public.f01_org()")) is None
            assert (
                connection.scalar(text("SELECT count(*) FROM public.organizations"))
                == 0
            )
        with pytest.raises(ContractError), bound_transaction(clinic.runtime, identity):
            pytest.fail("Consumed primary stage activated again")
    with pytest.raises(DBAPIError) as denied, clinic.gate.begin() as connection:
        _register(connection, identity, body, db_proof_headers(identity))
    assert denied.value.orig.sqlstate in ("23505", "42501", "22023")

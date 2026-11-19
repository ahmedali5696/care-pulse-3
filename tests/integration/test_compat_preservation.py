"""T053: preserve original tenant/actor/receipt evidence across real 0040→0046."""

import json
import os
import time
from contextlib import contextmanager
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from hmac import new
from pathlib import Path
from types import SimpleNamespace
from urllib.error import HTTPError
from urllib.request import Request, urlopen
from uuid import uuid4
from zoneinfo import ZoneInfo

import pytest
from carepulse.appointments.booking_contracts import BookAppointment
from carepulse.config import ServiceConfig, _load_policy
from carepulse.identity.authorization import protected_transaction
from carepulse.identity.contracts import ContractError, normalized_request_digest
from carepulse.identity.signing import (
    PROTOCOL_VERSION,
    CanonicalEnvelope,
    canonical_payload,
)
from carepulse.main import app
from runtime_identity_fixture import registered_test_request
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from test_booking import booking_request

from ops.compat.control import read_control_record
from ops.compat.manifest import CANDIDATE_HEAD
from ops.compat.verify import (
    VerificationError,
    verify_fenced_security,
    verify_retained_evidence,
)
from tests.integration.compat_ci_helpers import connect_role, make_sa_engine
from tests.integration.compat_fixture import compare_retained_inventory
from tests.integration.compat_recovery_lab import (
    committed_0046_cutover,
    populated_0040_lab,
)


def test_historical_receipts_actor_and_consumed_nonce_survive_each_real_successor() -> (
    None
):
    with (
        populated_0040_lab("compat_preserve") as lab,
        committed_0046_cutover(lab) as cutover,
    ):
        source_data = lab.inventory.expected_rows
        assert len(source_data["idempotency_receipts"]) == 2
        assert all(
            row["actor_kind"] == "STAFF"
            and row["request_digest"]
            and row["result_projection"]["net_egp"] == "100.00"
            and row["scope_kind"] == "TENANT"
            for row in source_data["idempotency_receipts"]
        )
        assert len(source_data["visits"]) == 1
        assert len(source_data["clinical_audit"]) == 1
        assert (
            source_data["clinical_audit"][0]["doctor_staff_id"]
            == (source_data["visits"][0]["doctor_staff_id"])
        )
        compare_retained_inventory(cutover.observer, lab.inventory)
        # A real integrity failure must never echo an original clinical row.
        altered = {
            **source_data,
            "visits": (
                {**source_data["visits"][0], "_private_marker": "sensitive-sentinel"},
            ),
        }
        with pytest.raises(AssertionError) as mismatch:
            compare_retained_inventory(
                cutover.observer,
                replace(lab.inventory, expected_rows=altered),
            )
        assert "sensitive-sentinel" not in str(mismatch.value)
        security = verify_fenced_security(
            cutover.observer,
            source_manifest=lab.source_manifest,
            target_manifest=lab.target_manifest,
            expected_epoch=cutover.owner_epoch,
            expected_head=CANDIDATE_HEAD,
        )
        preservation = verify_retained_evidence(
            cutover.observer,
            baseline=lab.retained_baseline(),
            security=security,
        )
        assert preservation.checked_rows >= 20
        assert preservation.unresolved_work == 0
        tombstone = cutover.observer.execute(
            """
            SELECT count(*) AS n FROM f01_private.nonces
            WHERE http_key_id=%s AND nonce=%s
            """,
            lab.inventory.nonce_reference,
        ).fetchone()["n"]
        assert tombstone == 1
        assert (
            cutover.observer.execute(
                "SELECT count(*) AS n FROM f01_private.activations"
            ).fetchone()["n"]
            == 0
        )

        # A real API database login without a signed F-01 operation binding
        # cannot read either clinic's patients, even though table SELECT exists.
        api = make_sa_engine("carepulse_api", dbname=lab.dbname)
        try:
            with api.begin() as connection:
                count = connection.exec_driver_sql(
                    "SELECT count(*) FROM public.patients"
                ).scalar_one()
                assert count == 0
        finally:
            api.dispose()
        compare_retained_inventory(cutover.observer, lab.inventory)
        assert read_control_record(cutover.observer).phase == "MIGRATING"


def test_uncertain_sender_provenance_prevents_releasing_retained_evidence() -> None:
    with (
        populated_0040_lab("compat_preserve_unknown") as lab,
        committed_0046_cutover(lab) as cutover,
    ):
        # Persist an authentic unresolved category, without claiming an SMTP
        # transport acceptance or synthesizing a successful delivery.
        attempt_id = f"synthetic-unknown-{uuid4().hex}"
        cutover.owner.execute(
            """
            INSERT INTO compat_control.work_evidence (
                attempt_id, target_id, owner_epoch, generation, service,
                invocation_class, correlation_id, disposition, created_by_role
            ) VALUES (%s,%s,%s,'source','sender','sender_claim',%s,
                      'UNKNOWN','carepulse_sender')
            """,
            (
                attempt_id,
                lab.target_id,
                cutover.owner_epoch,
                lab.inventory.delivery_job_a_id,
            ),
        )
        cutover.owner.commit()
        security = verify_fenced_security(
            cutover.observer,
            source_manifest=lab.source_manifest,
            target_manifest=lab.target_manifest,
            expected_epoch=cutover.owner_epoch,
            expected_head=CANDIDATE_HEAD,
        )
        with pytest.raises(VerificationError, match="Unresolved"):
            verify_retained_evidence(
                cutover.observer,
                baseline=lab.retained_baseline(),
                security=security,
            )
        assert (
            cutover.observer.execute(
                "SELECT disposition FROM compat_control.work_evidence WHERE attempt_id=%s",
                (attempt_id,),
            ).fetchone()["disposition"]
            == "UNKNOWN"
        )
        compare_retained_inventory(cutover.observer, lab.inventory)
        assert read_control_record(cutover.observer).active_generation == "none"


def test_disposable_0046_signed_proof_nonce_and_live_tenant_denials() -> None:
    # Independent validation copy: the cutover target remains MIGRATING/none.
    with (
        populated_0040_lab("compat_f01_proof") as lab,
        committed_0046_cutover(lab) as cutover,
    ):
        assert lab.dbname != "carepulse"
        assert read_control_record(cutover.observer).active_generation == "none"
        session = f"synthetic-compat-{uuid4()}"
        with cutover.admin.transaction():
            staff = cutover.admin.execute(
                """
                SELECT id, auth_user_id, security_revision FROM public.staff_accounts
                WHERE organization_id=%s AND auth_user_id=%s AND role='DOCTOR_OWNER'
                  AND enabled AND verified
                """,
                (lab.inventory.org_a_id, lab.inventory.owner_user_ids[0]),
            ).fetchone()
            assert staff is not None
            cutover.admin.execute(
                """
                INSERT INTO public."session"
                    (id, "userId", token, "expiresAt", "createdAt", "updatedAt",
                     "securityRevision", "absoluteDeadline", "lastActivity")
                VALUES (%s, %s, %s,
                        clock_timestamp() + interval '1 hour',
                        clock_timestamp(), clock_timestamp(), %s,
                        clock_timestamp() + interval '1 hour', clock_timestamp())
                """,
                (
                    session,
                    staff["auth_user_id"],
                    f"synthetic-{uuid4()}",
                    staff["security_revision"],
                ),
            )
            cutover.admin.execute("SET LOCAL ROLE carepulse_identity_owner")
            idle_minutes = cutover.admin.execute(
                "SELECT session_idle_minutes FROM f01_private.policy WHERE singleton"
            ).fetchone()["session_idle_minutes"]

        effects_sql = """
            SELECT (SELECT count(*) FROM public.patients) AS patients,
                   (SELECT count(*) FROM public.appointments) AS appointments,
                   (SELECT count(*) FROM public.visits) AS visits,
                   (SELECT count(*) FROM public.idempotency_receipts) AS receipts,
                   (SELECT count(*) FROM public.sensitive_audit) AS audit,
                   (SELECT count(*) FROM public.clinical_audit) AS clinical_audit,
                   (SELECT count(*) FROM public.outbox_events) AS outbox
        """
        before = cutover.observer.execute(effects_sql).fetchone()
        identity = CanonicalEnvelope(
            protocol_version=PROTOCOL_VERSION,
            key_id=os.environ["CP_SIGNING_KEY_ID"],
            service="web",
            method="GET",
            path="/v1/patients",
            query="",
            body_sha256=sha256(b"").hexdigest(),
            organization_id=lab.inventory.org_a_id,
            staff_id=str(staff["id"]),
            session_id=session,
            role="DOCTOR_OWNER",
            security_revision=str(staff["security_revision"]),
            timestamp=str(int(datetime.now(UTC).timestamp() * 1000)),
            nonce=f"synthetic_{uuid4().hex}",
            purpose="PATIENT_REGISTRY",
            idempotency_key="",
        )
        api = make_sa_engine("carepulse_api", dbname=lab.dbname)
        gate = make_sa_engine("carepulse_gate", dbname=lab.dbname)
        clinic = SimpleNamespace(gate=gate, runtime=api)
        try:
            with pytest.raises(DBAPIError) as unbound, api.begin() as connection:
                connection.execute(text("SELECT public.f01_activate()"))
            assert unbound.value.orig.sqlstate == "42501"

            with api.begin() as connection:
                pid, xid = connection.execute(
                    text("SELECT pg_backend_pid(), pg_current_xact_id()::text")
                ).one()
                with pytest.raises(DBAPIError) as unknown, gate.begin() as binding:
                    binding.execute(
                        text(
                            "SELECT public.f01_bind(-1, 'PRIMARY', :pid, CAST(:xid AS xid8))"
                        ),
                        {"pid": pid, "xid": xid},
                    )
            assert unknown.value.orig.sqlstate == "42501"

            with registered_test_request(clinic, identity, b""):
                with protected_transaction(api, identity, idle_minutes) as (
                    connection,
                    actor,
                ):
                    assert str(actor.organization_id) == lab.inventory.org_a_id
                    assert str(actor.staff_id) == str(staff["id"])
                    assert (
                        connection.scalar(text("SELECT public.f01_org()"))
                        == lab.inventory.org_a_id
                    )
                    assert [
                        str(patient)
                        for patient in connection.scalars(
                            text("SELECT id FROM public.patients")
                        )
                    ] == [lab.inventory.patient_a_id]
                with (
                    pytest.raises(ContractError) as reused_stage,
                    protected_transaction(api, identity, idle_minutes),
                ):
                    pytest.fail("Consumed F-01 primary binding activated again")
                assert reused_stage.value.status_code == 403

            with (
                pytest.raises(DBAPIError) as replay,
                registered_test_request(clinic, identity, b""),
            ):
                pytest.fail("Consumed signed nonce was re-registered")
            assert replay.value.orig.sqlstate == "23505"

            foreign = replace(
                identity,
                organization_id=lab.inventory.org_b_id,
                timestamp=str(int(datetime.now(UTC).timestamp() * 1000)),
                nonce=f"synthetic_{uuid4().hex}",
            )
            with registered_test_request(clinic, foreign, b""):
                with (
                    pytest.raises(ContractError) as cross_tenant,
                    protected_transaction(api, foreign, idle_minutes),
                ):
                    pytest.fail("Cross-tenant signed proof activated")
                assert cross_tenant.value.status_code in (401, 403)

            revoked = replace(
                identity,
                timestamp=str(int(datetime.now(UTC).timestamp() * 1000)),
                nonce=f"synthetic_{uuid4().hex}",
            )
            with registered_test_request(clinic, revoked, b""):
                with cutover.admin.transaction():
                    cutover.admin.execute("SET LOCAL ROLE carepulse_identity_owner")
                    retired = cutover.admin.execute(
                        "UPDATE f01_private.keys SET status='REVOKED' WHERE key_id=%s "
                        "AND status='ACTIVE' RETURNING key_id",
                        (os.environ["CP_DB_IDENTITY_KEY_ID"],),
                    ).fetchone()
                    assert retired is not None
                with (
                    pytest.raises(ContractError) as revoked_request,
                    protected_transaction(api, revoked, idle_minutes),
                ):
                    pytest.fail("Revoked database proof activated")
                assert revoked_request.value.status_code == 403

            after_revocation = replace(
                identity,
                timestamp=str(int(datetime.now(UTC).timestamp() * 1000)),
                nonce=f"synthetic_{uuid4().hex}",
            )
            with (
                pytest.raises(DBAPIError) as revoked_key,
                registered_test_request(clinic, after_revocation, b""),
            ):
                pytest.fail("Revoked database proof key registered new request")
            assert revoked_key.value.orig.sqlstate == "42501"
        finally:
            gate.dispose()
            api.dispose()

        assert cutover.observer.execute(effects_sql).fetchone() == before
        evidence = cutover.observer.execute(
            """
            SELECT r.nonce,
                   bool_or(b.stage='PRIMARY'
                       AND b.login_oid='carepulse_api'::regrole::oid) AS bound,
                   bool_or(a.request_id IS NOT NULL) AS activated
            FROM f01_private.requests r
            LEFT JOIN f01_private.bindings b ON b.request_id=r.id
            LEFT JOIN f01_private.activations a
              ON a.request_id=b.request_id AND a.stage=b.stage
            WHERE r.http_key_id=%s
            GROUP BY r.nonce
            """,
            (identity.key_id,),
        ).fetchall()
        assert {row["nonce"]: (row["bound"], row["activated"]) for row in evidence} == {
            identity.nonce: (True, True),
            foreign.nonce: (True, False),
            revoked.nonce: (True, False),
        }
        assert (
            cutover.observer.execute(
                """
                SELECT count(*) AS n FROM f01_private.nonces
                WHERE http_key_id=%s AND nonce = ANY(%s::text[])
                """,
                (
                    identity.key_id,
                    [
                        identity.nonce,
                        foreign.nonce,
                        revoked.nonce,
                        after_revocation.nonce,
                    ],
                ),
            ).fetchone()["n"]
            == 3
        )
        compare_retained_inventory(cutover.observer, lab.inventory)
        assert read_control_record(cutover.observer).phase == "MIGRATING"


def _host_reply(path: Path, expected: dict[str, str], *, status: str) -> dict:
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        if path.is_file():
            response = json.loads(path.read_text(encoding="utf-8"))
            if response.get("status") != status or any(
                response.get(k) != v for k, v in expected.items()
            ):
                raise AssertionError("Source host handshake rejected reviewed binding")
            return response
        time.sleep(0.05)
    raise AssertionError("Source host handshake did not acknowledge reviewed binding")


def _host_signal(path: Path, payload: dict[str, str]) -> None:
    pending = path.with_suffix(".pending")
    pending.write_text(json.dumps(payload, separators=(",", ":")), encoding="utf-8")
    os.replace(pending, path)


@contextmanager
def _immutable_source_api(lab):
    directory = Path("/tmp/compat-host")
    if os.environ.get("CI") != "true" or not directory.is_dir():
        raise AssertionError("Authentic source writer requires scoped host IPC")
    nonce = uuid4().hex
    binding = {
        "version": 1,
        "id": nonce,
        "kind": "booking_source",
        "db_name": lab.dbname,
        "target_id": lab.target_id,
        "source_manifest_id": lab.source_manifest.manifest_id,
    }
    request = directory / f"{nonce}.request.json"
    _host_signal(request, binding)
    launched = False
    try:
        ready = _host_reply(
            directory / f"{nonce}.response.json", binding, status="ready"
        )
        launched = True
        endpoint = ready.get("endpoint")
        if endpoint != "http://source-api:8000":
            raise AssertionError("Host returned an untrusted source endpoint")
        yield endpoint
    finally:
        if launched:
            _host_signal(directory / f"{nonce}.stop.json", binding)
            _host_reply(
                directory / f"{nonce}.stopped.json",
                {"db_name": lab.dbname, "target_id": lab.target_id},
                status="stopped",
            )


def _source_signed_booking(endpoint, *, tenant, staff, session, revision, body, key):
    raw = json.dumps(body, separators=(",", ":")).encode("utf-8")
    signature = CanonicalEnvelope(
        protocol_version=PROTOCOL_VERSION,
        key_id=os.environ.get("CP_SOURCE_SIGNING_KEY_ID", "synthetic-source-v1"),
        service="web",
        method="POST",
        path="/v1/appointments",
        query="",
        body_sha256=sha256(raw).hexdigest(),
        organization_id=tenant,
        staff_id=staff,
        session_id=session,
        role="DOCTOR_OWNER",
        security_revision=str(revision),
        timestamp=str(int(datetime.now(UTC).timestamp() * 1000)),
        nonce=f"synthetic_{uuid4().hex}",
        purpose="APPOINTMENT_BOOKING",
        idempotency_key=key,
    )
    key_bytes = Path("/run/secrets/source_service_key").read_text().strip().encode()
    headers = {
        f"x-carepulse-{field.replace('_', '-')}": str(getattr(signature, field))
        for field in (
            "protocol_version",
            "key_id",
            "service",
            "organization_id",
            "staff_id",
            "session_id",
            "role",
            "security_revision",
            "timestamp",
            "nonce",
            "purpose",
            "idempotency_key",
        )
    }
    headers.update(
        {
            "Content-Type": "application/json",
            "x-carepulse-service-key": key_bytes.decode(),
            "x-carepulse-signature": new(
                key_bytes, canonical_payload(signature), sha256
            ).hexdigest(),
        }
    )
    call = Request(endpoint + "/v1/appointments", data=raw, headers=headers)
    try:
        with urlopen(call, timeout=12) as result:
            return result.status, json.load(result)
    except HTTPError as denied:
        return denied.code, json.load(denied)


def _booking_effects(observer, tenant):
    return observer.execute(
        """
        SELECT (SELECT count(*) FROM public.appointments WHERE organization_id=%s)
                    AS appointments,
               (SELECT count(*) FROM public.idempotency_receipts WHERE organization_id=%s)
                    AS receipts,
               (SELECT count(*) FROM public.sensitive_audit WHERE organization_id=%s)
                    AS audit,
               (SELECT count(*) FROM public.outbox_events WHERE organization_id=%s)
                    AS outbox,
               (SELECT count(*) FROM public.delivery_jobs WHERE organization_id=%s)
                    AS delivery
        """,
        (tenant,) * 5,
    ).fetchone()


def _mail_count() -> int:
    with urlopen("http://mail:8025/api/v1/messages", timeout=5) as mail:
        total = json.load(mail)["total"]
    if type(total) is not int:
        raise AssertionError("SMTP sink did not return a concrete message count")
    return total


def test_authentic_0040_signed_receipt_replays_with_f01_without_any_new_effect() -> (
    None
):
    with populated_0040_lab("compat_real_booking") as lab:
        tenant = lab.inventory.org_a_id
        session = f"compat-replay-{uuid4().hex}"
        key = f"historic-{uuid4().hex}"
        service_date = datetime.now(ZoneInfo("Africa/Cairo")).date() + timedelta(
            days=28
        )
        admin = make_sa_engine("carepulse_bootstrap", dbname=lab.dbname)
        try:
            with admin.begin() as connection:
                staff = (
                    connection.exec_driver_sql(
                        "SELECT id, security_revision FROM public.staff_accounts "
                        "WHERE organization_id=%s AND auth_user_id=%s AND role='DOCTOR_OWNER' "
                        "AND enabled AND verified",
                        (tenant, lab.inventory.owner_user_ids[0]),
                    )
                    .mappings()
                    .one()
                )
                connection.exec_driver_sql(
                    """
                    INSERT INTO public."session"
                    (id, "userId", token, "expiresAt", "createdAt", "updatedAt",
                     "securityRevision", "absoluteDeadline", "lastActivity")
                    VALUES (%s,%s,%s,clock_timestamp()+interval '1 hour',
                            clock_timestamp(),clock_timestamp(),%s,
                            clock_timestamp()+interval '1 hour',clock_timestamp())
                    """,
                    (
                        session,
                        lab.inventory.owner_user_ids[0],
                        f"synthetic-{uuid4().hex}",
                        staff["security_revision"],
                    ),
                )
                # Fixture prerequisite only. The receipt and business effects
                # must be generated by the immutable 0040 HTTP writer.
                schedule = {
                    "booking_mode": "OPEN",
                    "duration_minutes": None,
                    "holiday_booking_enabled": False,
                    "allow_overlap": False,
                    "no_show_cutoff": {"enabled": False, "local_time": None},
                    "working_weekdays": [1, 2, 3, 4, 5, 6, 7],
                    "weekly_work_periods": [],
                    "weekly_breaks": [],
                    "weekly_manual_slots": [],
                    "open_override_dates": [],
                    "override_work_periods": [],
                    "manual_slots_by_date": {},
                    "holidays": [],
                }
                connection.execute(
                    text(
                        "INSERT INTO public.clinic_policy_versions "
                        "(organization_id,kind,version,payload) "
                        "VALUES (:tenant,'SCHEDULE',2,CAST(:payload AS jsonb))"
                    ),
                    {"tenant": tenant, "payload": json.dumps(schedule)},
                )
                connection.exec_driver_sql(
                    "UPDATE public.clinic_policy_heads SET current_version=2,revision=2 "
                    "WHERE organization_id=%s AND kind='SCHEDULE'",
                    (tenant,),
                )
        finally:
            admin.dispose()

        body = {
            "patient_id": lab.inventory.patient_a_id,
            "service_date": service_date.isoformat(),
            "requested_service_type": "NEW_EXAMINATION",
            "discount": {
                "discount_type": "NONE",
                "discount_value": "0",
                "discount_reason": None,
            },
            "expected_base_egp": "100.00",
            "expected_net_egp": "100.00",
            "expected_price_revision": 2,
            "expected_followup_revision": 1,
            "expected_schedule_revision": 2,
        }
        original_mail = _mail_count()
        with _immutable_source_api(lab) as endpoint:
            status, original = _source_signed_booking(
                endpoint,
                tenant=tenant,
                staff=str(staff["id"]),
                session=session,
                revision=staff["security_revision"],
                body=body,
                key=key,
            )
            assert status == 201, (status, original)
            assert original["revision"] == 0
        assert _mail_count() == original_mail

        observer = connect_role(
            "carepulse_bootstrap", dbname=lab.dbname, autocommit=True
        )
        try:
            receipt = observer.execute(
                "SELECT scope_kind, scope_id, actor_kind, actor_id, staff_id, "
                "request_digest, result_projection, result_revision "
                "FROM public.idempotency_receipts "
                "WHERE organization_id=%s AND operation='appointments.book' "
                "AND idempotency_key=%s",
                (tenant, key),
            ).fetchone()
            assert receipt is not None
            assert receipt["scope_kind"] == "TENANT"
            assert str(receipt["scope_id"]) == tenant
            assert receipt["actor_kind"] == "STAFF"
            assert str(receipt["actor_id"]) == str(staff["id"])
            assert str(receipt["staff_id"]) == str(staff["id"])
            assert receipt["request_digest"] == normalized_request_digest(
                BookAppointment.model_validate(body)
            )
            assert receipt["result_projection"] == original["data"]
            assert receipt["result_revision"] == original["revision"]
            before = _booking_effects(observer, tenant)
            original_appointment = observer.execute(
                "SELECT to_jsonb(a) AS row FROM public.appointments a WHERE id=%s",
                (original["data"]["id"],),
            ).fetchone()["row"]
        finally:
            observer.close()

        with committed_0046_cutover(lab) as cutover:
            assert _booking_effects(cutover.observer, tenant) == before
            retained_appointment = cutover.observer.execute(
                "SELECT to_jsonb(a) AS row FROM public.appointments a WHERE id=%s",
                (original["data"]["id"],),
            ).fetchone()["row"]
            assert all(
                retained_appointment.get(field) == value
                for field, value in original_appointment.items()
            )
            config = ServiceConfig(
                environment="test",
                policy=_load_policy(),
                db_host="db",
                db_port=5432,
                db_name=lab.dbname,
                db_user="carepulse_api",
                db_password="synthetic-never-used",
                file_encryption_key=None,
                service_key=Path("/run/secrets/service_key").read_text().strip(),
                signing_key_id=os.environ["CP_SIGNING_KEY_ID"],
                mail_enabled=False,
                scan_enabled=False,
                mail=None,
                scan=None,
            )
            api = make_sa_engine("carepulse_api", dbname=lab.dbname)
            gate = make_sa_engine("carepulse_gate", dbname=lab.dbname)
            previous_state = {
                name: getattr(app.state, name, None)
                for name in ("db_engine", "gate_engine", "service_config")
            }
            app.state.db_engine = api
            app.state.gate_engine = gate
            app.state.service_config = config
            clinic = SimpleNamespace(config=config)
            actor = CanonicalEnvelope(
                protocol_version=PROTOCOL_VERSION,
                key_id=config.signing_key_id,
                service="web",
                method="POST",
                path="/v1/appointments",
                query="",
                body_sha256=sha256(b"").hexdigest(),
                organization_id=tenant,
                staff_id=str(staff["id"]),
                session_id=session,
                role="DOCTOR_OWNER",
                security_revision=str(staff["security_revision"]),
                timestamp=str(int(datetime.now(UTC).timestamp() * 1000)),
                nonce=f"synthetic_{uuid4().hex}",
                purpose="APPOINTMENT_BOOKING",
                idempotency_key=key,
            )
            try:
                assert booking_request(clinic, actor, body=body, key=key) == (
                    201,
                    original,
                )
                changed_status, changed = booking_request(
                    clinic,
                    actor,
                    body={**body, "expected_net_egp": "99.00"},
                    key=key,
                )
                assert changed_status == 409
                assert changed["error"]["code"] == "IDEMPOTENCY_CONFLICT"
                assert booking_request(
                    clinic,
                    replace(actor, organization_id=lab.inventory.org_b_id),
                    body=body,
                    key=key,
                )[0] in (401, 403)
                with cutover.admin.transaction():
                    cutover.admin.execute(
                        'UPDATE public."session" SET "expiresAt"=clock_timestamp() '
                        "WHERE id=%s",
                        (session,),
                    )
                assert booking_request(clinic, actor, body=body, key=key)[0] in (
                    401,
                    403,
                )
                assert _booking_effects(cutover.observer, tenant) == before
                persisted = cutover.observer.execute(
                    "SELECT request_digest,result_projection,result_revision "
                    "FROM public.idempotency_receipts "
                    "WHERE organization_id=%s AND idempotency_key=%s",
                    (tenant, key),
                ).fetchone()
                assert persisted["request_digest"] == receipt["request_digest"]
                assert persisted["result_projection"] == receipt["result_projection"]
                assert persisted["result_revision"] == receipt["result_revision"]
                assert _mail_count() == original_mail
            finally:
                for name, previous in previous_state.items():
                    setattr(app.state, name, previous)
                gate.dispose()
                api.dispose()

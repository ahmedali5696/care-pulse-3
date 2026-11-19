"""T053: preserve original tenant/actor/receipt evidence across real 0040→0046."""

import os
from dataclasses import replace
from datetime import UTC, datetime
from hashlib import sha256
from types import SimpleNamespace
from uuid import uuid4

import pytest
from carepulse.identity.authorization import protected_transaction
from carepulse.identity.contracts import ContractError
from carepulse.identity.signing import PROTOCOL_VERSION, CanonicalEnvelope
from runtime_identity_fixture import registered_test_request
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from ops.compat.control import read_control_record
from ops.compat.manifest import CANDIDATE_HEAD
from ops.compat.verify import (
    VerificationError,
    verify_fenced_security,
    verify_retained_evidence,
)
from tests.integration.compat_ci_helpers import make_sa_engine
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

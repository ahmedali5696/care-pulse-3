"""D-09 real PostgreSQL audit, nonce, receipts and leased email job evidence."""

import base64
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from hashlib import sha256
from threading import Barrier
from uuid import uuid4

import pytest
from carepulse.identity.receipts import (
    Receipt,
    ReceiptConflict,
    assert_same_request,
    insert_operator_create_receipt,
    insert_tenant_receipt,
    lookup_operator_create_receipt,
    lookup_tenant_receipt,
)
from carepulse.notifications.evidence import (
    ClaimPolicy,
    JobOutcome,
    accept_verified_nonce,
    append_clinical_audit,
    append_outbox,
    append_sensitive_audit,
    claim_delivery_job,
    create_delivery_job,
    finish_delivery_job,
    get_claimed_delivery_material,
)
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError, IntegrityError


@pytest.fixture
def evidence(role_engine, tenant_factory):
    operator_id, create_tenant = tenant_factory
    tenant_id = create_tenant("Synthetic D-09 evidence")
    migration = role_engine("carepulse_migration")
    try:
        with migration.begin() as connection:
            user_id = f"synthetic-{uuid4()}"
            email = f"{uuid4()}@example.invalid"
            staff_id = uuid4()
            connection.execute(
                text("""
                    INSERT INTO public."user"
                        (id, name, email, "emailVerified", "createdAt", "updatedAt")
                    VALUES (:id, 'Synthetic doctor', :email, true,
                            clock_timestamp(), clock_timestamp())
                """),
                {"id": user_id, "email": email},
            )
            connection.execute(
                text("""
                    INSERT INTO public.staff_accounts
                        (id, organization_id, auth_user_id, verified_email,
                         role, enabled, verified)
                    VALUES (:staff, :tenant, :user, :email, 'DOCTOR_OWNER', true, true)
                """),
                {
                    "staff": staff_id,
                    "tenant": tenant_id,
                    "user": user_id,
                    "email": email,
                },
            )
            invitation_id = uuid4()
            connection.execute(
                text("""
                    INSERT INTO public.invitations
                        (id, organization_id, purpose, target_email, proof_digest,
                         issued_by_operator_id, expires_at)
                    VALUES (:id, :tenant, 'FIRST_OWNER', :email, :digest,
                            :operator, clock_timestamp() + interval '20 minutes')
                """),
                {
                    "id": invitation_id,
                    "tenant": tenant_id,
                    "email": email,
                    "digest": sha256(str(invitation_id).encode()).hexdigest(),
                    "operator": operator_id,
                },
            )
            connection.execute(
                text("""
                    UPDATE public.invitations
                    SET state = 'ACCEPTED', accepted_by_user_id = :user
                    WHERE id = :invitation
                """),
                {"user": user_id, "invitation": invitation_id},
            )
            connection.execute(
                text(
                    "UPDATE public.organizations SET state = 'ACTIVE' WHERE id = :tenant"
                ),
                {"tenant": tenant_id},
            )
        yield migration, operator_id, tenant_id, staff_id
    finally:
        migration.dispose()


def _outbox_fields(tenant_id):
    return {
        "organization_id": tenant_id,
        "event_type": "SYNTHETIC_COMMITTED",
        "aggregate_type": "test-fixture",
        "aggregate_id": str(uuid4()),
        "payload": {"status": "synthetic"},
    }


def _audit_fields(tenant_id, staff_id):
    return {
        "organization_id": tenant_id,
        "actor_staff_id": staff_id,
        "operator_id": None,
        "actor_role": "DOCTOR_OWNER",
        "action": "SYNTHETIC_SETTINGS_CHANGE",
        "reason": "synthetic",
        "before_state": {"enabled": False},
        "after_state": {"enabled": True},
        "correlation_id": str(uuid4()),
        "recovery_operation_id": None,
    }


def _scope_job(connection, job_id):
    tenant_id = connection.scalar(
        text("SELECT organization_id FROM public.delivery_jobs WHERE id = :id"),
        {"id": job_id},
    )
    connection.execute(
        text("SELECT set_config('app.organization_id', :tenant, true)"),
        {"tenant": str(tenant_id)},
    )


def _new_delivery(connection, operator_id):
    tenant_id = uuid4()
    connection.execute(
        text("""
            INSERT INTO public.organizations (id, branch, created_by_operator_id)
            VALUES (:id, 'Synthetic pending invitation', :operator)
        """),
        {"id": tenant_id, "operator": operator_id},
    )
    proof_id = uuid4()
    recipient = f"{uuid4()}@example.invalid"
    expires = connection.scalar(
        text("SELECT clock_timestamp() + interval '20 minutes'")
    )
    connection.execute(
        text("""
            INSERT INTO public.invitations
                (id, organization_id, purpose, target_email, proof_digest,
                 issued_by_operator_id, expires_at)
            VALUES (:id, :tenant, 'FIRST_OWNER', :recipient, :digest, :operator, :expires)
        """),
        {
            "id": proof_id,
            "tenant": tenant_id,
            "recipient": recipient,
            "digest": sha256(str(proof_id).encode()).hexdigest(),
            "operator": operator_id,
            "expires": expires,
        },
    )
    job_id = create_delivery_job(
        connection,
        {
            "organization_id": tenant_id,
            "staff_id": None,
            "invitation_id": proof_id,
            "verification_id": None,
            "recovery_operation_id": None,
            "proof_reference": str(proof_id),
            "purpose": "FIRST_OWNER",
            "recipient": recipient,
            "template": "first-owner",
            "logical_key": f"synthetic:{proof_id}",
            "proof_expires_at": expires,
        },
        {
            "delivery_key_id": "synthetic-test-key",
            "ciphertext_base64url": base64.urlsafe_b64encode(b"x" * 384)
            .decode()
            .rstrip("="),
        },
    )
    return job_id, proof_id


def test_domain_evidence_commit_and_rollback_are_atomic(evidence):
    migration, _, tenant_id, staff_id = evidence
    with (
        pytest.raises(RuntimeError, match="synthetic rollback"),
        migration.begin() as connection,
    ):
        event_id = append_outbox(connection, _outbox_fields(tenant_id))
        audit_id = append_sensitive_audit(
            connection, _audit_fields(tenant_id, staff_id)
        )
        raise RuntimeError("synthetic rollback")
    with migration.begin() as connection:
        assert (
            connection.scalar(
                text("SELECT count(*) FROM public.outbox_events WHERE id = :id"),
                {"id": event_id},
            )
            == 0
        )
        assert (
            connection.scalar(
                text("SELECT count(*) FROM public.sensitive_audit WHERE id = :id"),
                {"id": audit_id},
            )
            == 0
        )
        kept = append_outbox(connection, _outbox_fields(tenant_id))
    with migration.connect() as connection:
        assert (
            connection.scalar(
                text("SELECT count(*) FROM public.outbox_events WHERE id = :id"),
                {"id": kept},
            )
            == 1
        )


@pytest.mark.parametrize(
    "table", ["sensitive_audit", "clinical_audit", "outbox_events"]
)
def test_immutable_evidence_rejects_update_and_delete(evidence, table):
    migration, _, tenant_id, staff_id = evidence
    with migration.begin() as connection:
        if table == "sensitive_audit":
            evidence_id = append_sensitive_audit(
                connection, _audit_fields(tenant_id, staff_id)
            )
        elif table == "clinical_audit":
            evidence_id = append_clinical_audit(
                connection,
                {
                    "organization_id": tenant_id,
                    "doctor_staff_id": staff_id,
                    "clinical_revision": 1,
                    "before_ref": "synthetic-before",
                    "after_ref": "synthetic-after",
                    "correlation_id": str(uuid4()),
                },
            )
        else:
            evidence_id = append_outbox(connection, _outbox_fields(tenant_id))
    for command in (
        f"UPDATE public.{table} SET id = :new_id WHERE id = :id",
        f"DELETE FROM public.{table} WHERE id = :id",
    ):
        with pytest.raises(DBAPIError), migration.begin() as connection:
            connection.execute(text(command), {"id": evidence_id, "new_id": uuid4()})
    with migration.connect() as connection:
        assert (
            connection.scalar(
                text(f"SELECT count(*) FROM public.{table} WHERE id = :id"),
                {"id": evidence_id},
            )
            == 1
        )


def test_audit_access_fails_for_operator_and_without_doctor_context(
    evidence, role_engine
):
    migration, _, tenant_id, staff_id = evidence
    with migration.begin() as connection:
        append_sensitive_audit(connection, _audit_fields(tenant_id, staff_id))
        append_clinical_audit(
            connection,
            {
                "organization_id": tenant_id,
                "doctor_staff_id": staff_id,
                "clinical_revision": 2,
                "before_ref": "before",
                "after_ref": "after",
                "correlation_id": str(uuid4()),
            },
        )
    operator = role_engine("carepulse_operator")
    runtime = role_engine("carepulse_runtime")
    try:
        with operator.connect() as connection, pytest.raises(DBAPIError):
            connection.execute(text("SELECT * FROM public.clinical_audit")).all()
        with runtime.begin() as connection:
            assert (
                connection.scalar(text("SELECT count(*) FROM public.sensitive_audit"))
                == 0
            )
            assert (
                connection.scalar(text("SELECT count(*) FROM public.clinical_audit"))
                == 0
            )
            connection.execute(
                text("SELECT set_config('app.organization_id', :org, true)"),
                {"org": str(tenant_id)},
            )
            assert (
                connection.scalar(text("SELECT count(*) FROM public.clinical_audit"))
                == 0
            )
            connection.execute(
                text("SELECT set_config('app.staff_id', :id, true)"),
                {"id": str(staff_id)},
            )
            assert (
                connection.scalar(text("SELECT count(*) FROM public.clinical_audit"))
                == 1
            )
    finally:
        operator.dispose()
        runtime.dispose()


def test_receptionist_cannot_forge_doctor_clinical_audit(evidence, role_engine):
    migration, _, tenant_id, doctor_id = evidence
    receptionist = uuid4()
    native_user = f"synthetic-{uuid4()}"
    email = f"{uuid4()}@example.invalid"
    with migration.begin() as connection:
        connection.execute(
            text("""
                INSERT INTO public."user"
                    (id, name, email, "emailVerified", "createdAt", "updatedAt")
                VALUES (:id, 'Synthetic receptionist', :email, true,
                        clock_timestamp(), clock_timestamp())
            """),
            {"id": native_user, "email": email},
        )
        connection.execute(
            text("""
                INSERT INTO public.staff_accounts
                    (id, organization_id, auth_user_id, verified_email,
                     role, enabled, verified)
                VALUES (:id, :tenant, :user, :email, 'RECEPTIONIST', true, true)
            """),
            {
                "id": receptionist,
                "tenant": tenant_id,
                "user": native_user,
                "email": email,
            },
        )
    runtime = role_engine("carepulse_runtime")
    try:
        with pytest.raises(DBAPIError), runtime.begin() as connection:
            connection.execute(
                text("SELECT set_config('app.organization_id', :tenant, true)"),
                {"tenant": str(tenant_id)},
            )
            connection.execute(
                text("SELECT set_config('app.staff_id', :staff, true)"),
                {"staff": str(receptionist)},
            )
            append_clinical_audit(
                connection,
                {
                    "organization_id": tenant_id,
                    "doctor_staff_id": doctor_id,
                    "clinical_revision": 2,
                    "before_ref": "before",
                    "after_ref": "forged",
                    "correlation_id": str(uuid4()),
                },
            )
    finally:
        runtime.dispose()
    with migration.connect() as connection:
        assert (
            connection.scalar(
                text(
                    "SELECT count(*) FROM public.clinical_audit WHERE after_ref = 'forged'"
                )
            )
            == 0
        )


def test_reset_success_evidence_is_unique_per_operation(evidence):
    migration, _, tenant_id, doctor_id = evidence
    operation_id = uuid4()
    with migration.begin() as connection:
        native_user = connection.scalar(
            text("SELECT auth_user_id FROM public.staff_accounts WHERE id = :id"),
            {"id": doctor_id},
        )
        connection.execute(
            text("""
                INSERT INTO public.recovery_operations
                    (id, organization_id, staff_id, auth_user_id, purpose,
                     proof_digest, proof_reference, proof_expires_at,
                     request_fingerprint, fingerprint_key_version,
                     captured_staff_revision, captured_lifecycle_revision,
                     reserved_security_revision)
                VALUES (:id, :tenant, :staff, :user, 'PASSWORD_RESET', :digest,
                        'synthetic', clock_timestamp() + interval '30 minutes',
                        :fingerprint, 'synthetic-v1', 0, 1, 1)
            """),
            {
                "id": operation_id,
                "tenant": tenant_id,
                "staff": doctor_id,
                "user": native_user,
                "digest": sha256(str(operation_id).encode()).hexdigest(),
                "fingerprint": sha256(
                    ("operation:" + str(operation_id)).encode()
                ).hexdigest(),
            },
        )
        connection.execute(
            text("""
                UPDATE public.staff_accounts
                SET reset_block_operation_id = :operation,
                    security_revision = security_revision + 1
                WHERE id = :staff
            """),
            {"operation": operation_id, "staff": doctor_id},
        )
        fields = {
            **_audit_fields(tenant_id, doctor_id),
            "action": "PASSWORD_RESET_SUCCEEDED",
            "recovery_operation_id": operation_id,
        }
        append_sensitive_audit(connection, fields)
    with pytest.raises(IntegrityError), migration.begin() as connection:
        append_sensitive_audit(connection, {**fields, "correlation_id": str(uuid4())})
    with migration.connect() as connection:
        assert (
            connection.scalar(
                text("""
                SELECT count(*) FROM public.sensitive_audit
                WHERE recovery_operation_id = :id AND action = 'PASSWORD_RESET_SUCCEEDED'
            """),
                {"id": operation_id},
            )
            == 1
        )


def test_nonclinical_job_issuance_rolls_back_without_deliverable_material(evidence):
    migration, operator_id, _, _ = evidence
    with pytest.raises(RuntimeError), migration.begin() as connection:
        job_id, _ = _new_delivery(connection, operator_id)
        raise RuntimeError("rolled back issuance")
    with migration.connect() as connection:
        assert (
            connection.scalar(
                text("SELECT count(*) FROM public.delivery_jobs WHERE id = :id"),
                {"id": job_id},
            )
            == 0
        )
        assert (
            connection.scalar(
                text(
                    "SELECT count(*) FROM public.protected_delivery_material WHERE job_id = :id"
                ),
                {"id": job_id},
            )
            == 0
        )


def test_job_claim_is_owned_and_material_disposed_on_success(evidence):
    migration, operator_id, _, _ = evidence
    with migration.begin() as connection:
        job_id, _ = _new_delivery(connection, operator_id)
    with migration.begin() as connection:
        claim = claim_delivery_job(
            connection, ClaimPolicy(timedelta(minutes=4), 3), job_id
        )
        assert claim and claim.generation == 1
    with migration.begin() as connection:
        assert (
            claim_delivery_job(connection, ClaimPolicy(timedelta(minutes=4), 3), job_id)
            is None
        )
        _scope_job(connection, job_id)
        material = get_claimed_delivery_material(connection, claim)
        assert material["format_version"] == 1
        assert finish_delivery_job(connection, claim, JobOutcome("SENT"))
    with migration.connect() as connection:
        assert (
            connection.scalar(
                text("SELECT state FROM public.delivery_jobs WHERE id = :id"),
                {"id": job_id},
            )
            == "SENT"
        )
        assert (
            connection.scalar(
                text(
                    "SELECT count(*) FROM public.protected_delivery_material WHERE job_id = :id"
                ),
                {"id": job_id},
            )
            == 0
        )


def test_two_claimers_get_only_one_delivery_lease(evidence):
    migration, operator_id, _, _ = evidence
    with migration.begin() as connection:
        job_id, _ = _new_delivery(connection, operator_id)
    barrier = Barrier(2)

    def attempt():
        with migration.begin() as connection:
            barrier.wait(timeout=10)
            claim = claim_delivery_job(
                connection, ClaimPolicy(timedelta(minutes=4), 3), job_id
            )
            return claim

    with ThreadPoolExecutor(max_workers=2) as pool:
        claims = [
            future.result() for future in [pool.submit(attempt) for _ in range(2)]
        ]
    assert sum(claim is not None for claim in claims) == 1
    with migration.connect() as connection:
        assert connection.execute(
            text("""
            SELECT attempts, claim_generation FROM public.delivery_jobs WHERE id = :id
        """),
            {"id": job_id},
        ).one() == (1, 1)


def test_expired_owner_is_fenced_and_competing_reclaimers_get_one_new_generation(
    evidence,
):
    migration, operator_id, _, _ = evidence
    with migration.begin() as connection:
        job_id, _ = _new_delivery(connection, operator_id)
        old = claim_delivery_job(
            connection, ClaimPolicy(timedelta(minutes=4), 4), job_id
        )

    # Alter only this disposable CI fixture's stored lease while keeping its
    # original generation, then restore the production guard in the same commit.
    with migration.begin() as connection:
        connection.execute(
            text("ALTER TABLE public.delivery_jobs DISABLE TRIGGER delivery_jobs_guard")
        )
        connection.execute(
            text("""
                UPDATE public.delivery_jobs
                SET claimed_at = clock_timestamp() - interval '1 hour',
                    lease_expires_at = clock_timestamp() - interval '1 minute'
                WHERE id = :id
            """),
            {"id": job_id},
        )
        connection.execute(
            text("ALTER TABLE public.delivery_jobs ENABLE TRIGGER delivery_jobs_guard")
        )

    with migration.begin() as connection:
        _scope_job(connection, job_id)
        assert not finish_delivery_job(connection, old, JobOutcome("SENT"))
        assert get_claimed_delivery_material(connection, old) is None

    barrier = Barrier(2)

    def attempt():
        with migration.begin() as connection:
            barrier.wait(timeout=10)
            return claim_delivery_job(
                connection, ClaimPolicy(timedelta(minutes=4), 4), job_id
            )

    with ThreadPoolExecutor(max_workers=2) as executor:
        outcomes = [f.result() for f in [executor.submit(attempt) for _ in range(2)]]
    claims = [outcome for outcome in outcomes if outcome]
    assert len(claims) == 1
    new = claims[0]
    assert new.generation == old.generation + 1
    assert new.owner != old.owner
    with migration.begin() as connection:
        _scope_job(connection, job_id)
        retry_at = connection.scalar(text("SELECT clock_timestamp()"))
        assert not finish_delivery_job(connection, old, JobOutcome("READY", retry_at))
        assert not finish_delivery_job(connection, old, JobOutcome("FAILED"))
        assert get_claimed_delivery_material(connection, old) is None
        assert finish_delivery_job(connection, new, JobOutcome("SENT"))
    with migration.connect() as connection:
        assert connection.execute(
            text("""
                SELECT state, attempts, claim_generation FROM public.delivery_jobs
                WHERE id = :id
            """),
            {"id": job_id},
        ).one() == ("SENT", 2, 2)


def test_stale_claim_cannot_finish_or_read_material(evidence):
    migration, operator_id, _, _ = evidence
    with migration.begin() as connection:
        job_id, _ = _new_delivery(connection, operator_id)
        claim = claim_delivery_job(
            connection, ClaimPolicy(timedelta(minutes=4), 3), job_id
        )
    stale = type(claim)(claim.id, uuid4(), claim.generation, claim.lease_expires_at)
    with migration.begin() as connection:
        _scope_job(connection, job_id)
        assert get_claimed_delivery_material(connection, stale) is None
        assert not finish_delivery_job(connection, stale, JobOutcome("SENT"))
    with migration.connect() as connection:
        assert (
            connection.scalar(
                text("""
            SELECT state FROM public.delivery_jobs WHERE id = :id
        """),
                {"id": job_id},
            )
            == "CLAIMED"
        )


def test_retry_bound_exhaustion_is_terminal_and_disposes_ciphertext(evidence):
    migration, operator_id, _, _ = evidence
    with migration.begin() as connection:
        job_id, _ = _new_delivery(connection, operator_id)
        claim = claim_delivery_job(
            connection, ClaimPolicy(timedelta(minutes=4), 1), job_id
        )
    with migration.begin() as connection:
        _scope_job(connection, job_id)
        now = connection.scalar(text("SELECT clock_timestamp()"))
        assert finish_delivery_job(connection, claim, JobOutcome("READY", now))
    with migration.begin() as connection:
        assert (
            claim_delivery_job(connection, ClaimPolicy(timedelta(minutes=4), 1), job_id)
            is None
        )
    with migration.connect() as connection:
        assert (
            connection.scalar(
                text("""
            SELECT state FROM public.delivery_jobs WHERE id = :id
        """),
                {"id": job_id},
            )
            == "FAILED"
        )
        assert (
            connection.scalar(
                text("""
            SELECT count(*) FROM public.protected_delivery_material WHERE job_id = :id
        """),
                {"id": job_id},
            )
            == 0
        )


def test_issuance_context_cannot_change_when_retrying(evidence):
    migration, operator_id, _, _ = evidence
    with migration.begin() as connection:
        job_id, _ = _new_delivery(connection, operator_id)
    with pytest.raises(DBAPIError), migration.begin() as connection:
        connection.execute(
            text("""
            UPDATE public.delivery_jobs SET recipient = 'swapped@example.invalid'
            WHERE id = :id
        """),
            {"id": job_id},
        )


def test_one_delivery_job_per_logical_issuance(evidence):
    migration, operator_id, _, _ = evidence
    with migration.begin() as connection:
        job_id, _ = _new_delivery(connection, operator_id)
    with pytest.raises(IntegrityError), migration.begin() as connection:
        connection.execute(
            text("""
                INSERT INTO public.delivery_jobs
                    (id, organization_id, invitation_id, proof_reference,
                     purpose, recipient, template, logical_key, proof_expires_at)
                SELECT :copy, organization_id, invitation_id, proof_reference,
                       purpose, recipient, template, logical_key, proof_expires_at
                FROM public.delivery_jobs WHERE id = :original
            """),
            {"copy": uuid4(), "original": job_id},
        )
    with migration.connect() as connection:
        assert (
            connection.scalar(
                text("SELECT count(*) FROM public.delivery_jobs WHERE id = :id"),
                {"id": job_id},
            )
            == 1
        )


def test_claim_cancels_unusable_proof_without_delivery(evidence):
    migration, operator_id, _, _ = evidence
    with migration.begin() as connection:
        job_id, proof_id = _new_delivery(connection, operator_id)
        connection.execute(
            text("""
                UPDATE public.invitations SET state = 'REVOKED',
                    revoked_at = clock_timestamp(), revocation_reference = 'synthetic'
                WHERE id = :id
            """),
            {"id": proof_id},
        )
    with migration.begin() as connection:
        assert (
            claim_delivery_job(connection, ClaimPolicy(timedelta(minutes=4), 3), job_id)
            is None
        )
    with migration.connect() as connection:
        assert (
            connection.scalar(
                text("SELECT state FROM public.delivery_jobs WHERE id = :id"),
                {"id": job_id},
            )
            == "CANCELLED"
        )


def test_runtime_cannot_read_delivery_ownership_or_ciphertext(evidence, role_engine):
    migration, operator_id, _, _ = evidence
    with migration.begin() as connection:
        job_id, _ = _new_delivery(connection, operator_id)
        tenant_id = connection.scalar(
            text("SELECT organization_id FROM public.delivery_jobs WHERE id = :id"),
            {"id": job_id},
        )
    runtime = role_engine("carepulse_runtime")
    try:
        for forbidden in (
            "SELECT claim_owner FROM public.delivery_jobs WHERE id = :id",
            (
                "SELECT ciphertext_base64url FROM public.protected_delivery_material "
                "WHERE job_id = :id"
            ),
        ):
            with pytest.raises(DBAPIError), runtime.begin() as connection:
                connection.execute(
                    text("SELECT set_config('app.organization_id', :tenant, true)"),
                    {"tenant": str(tenant_id)},
                )
                connection.execute(text(forbidden), {"id": job_id})
    finally:
        runtime.dispose()


def test_runtime_cannot_shortcut_nonce_retention(evidence, role_engine):
    runtime = role_engine("carepulse_runtime")
    try:
        with pytest.raises(DBAPIError), runtime.begin() as connection:
            connection.execute(
                text("SELECT public.purge_request_nonces(interval '1 microsecond')")
            )
    finally:
        runtime.dispose()


def test_nonce_survives_failed_business_and_replay_is_denied(evidence, role_engine):
    migration, _, tenant_id, _ = evidence
    runtime = role_engine("carepulse_runtime")
    try:
        with migration.connect() as connection:
            signed = connection.scalar(text("SELECT clock_timestamp()"))
        nonce = f"synthetic-{uuid4()}"
        accept_verified_nonce(runtime, "synthetic-key", nonce, signed)
        with pytest.raises(RuntimeError), migration.begin() as connection:
            append_outbox(connection, _outbox_fields(tenant_id))
            raise RuntimeError("failed domain")
        with pytest.raises(IntegrityError):
            accept_verified_nonce(runtime, "synthetic-key", nonce, signed)
        with migration.connect() as connection:
            assert (
                connection.scalar(
                    text(
                        "SELECT count(*) FROM public.request_nonces WHERE nonce = :nonce"
                    ),
                    {"nonce": nonce},
                )
                == 1
            )
    finally:
        runtime.dispose()


def test_duplicate_nonce_competition_has_one_committed_winner(role_engine, evidence):
    migration, _, _, _ = evidence
    with migration.connect() as connection:
        signed = connection.scalar(text("SELECT clock_timestamp()"))
    nonce = f"parallel-{uuid4()}"
    barrier = Barrier(2)
    runtime = role_engine("carepulse_runtime")

    def attempt():
        barrier.wait(timeout=10)
        try:
            accept_verified_nonce(runtime, "synthetic-key", nonce, signed)
            return "inserted"
        except IntegrityError:
            return "duplicate"

    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            outcomes = [
                future.result() for future in [pool.submit(attempt) for _ in range(2)]
            ]
        assert sorted(outcomes) == ["duplicate", "inserted"]
    finally:
        runtime.dispose()


def test_operator_receipt_deduplicates_clinic_creation_and_conflicts_on_body(evidence):
    migration, operator_id, _, _ = evidence
    created_id = uuid4()
    digest = sha256(b"synthetic clinic body").hexdigest()
    receipt = Receipt(
        operator_id,
        "clinics.create",
        f"synthetic-{uuid4()}",
        digest,
        {},
        0,
        result_organization_id=created_id,
    )
    with migration.begin() as connection:
        connection.execute(
            text("""
            INSERT INTO public.organizations (id, branch, created_by_operator_id)
            VALUES (:id, 'Synthetic receipt clinic', :operator)
        """),
            {"id": created_id, "operator": operator_id},
        )
        insert_operator_create_receipt(connection, receipt, f"synthetic:{operator_id}")
    with migration.begin() as connection:
        stored = lookup_operator_create_receipt(
            connection, operator_id, receipt.idempotency_key, f"synthetic:{operator_id}"
        )
        assert stored["result_organization_id"] == created_id
        assert_same_request(stored["request_digest"], digest)
        with pytest.raises(ReceiptConflict):
            assert_same_request(
                stored["request_digest"], sha256(b"changed body").hexdigest()
            )
    with pytest.raises(ReceiptConflict), migration.begin() as connection:
        insert_operator_create_receipt(connection, receipt, f"synthetic:{operator_id}")
    with migration.connect() as connection:
        assert (
            connection.scalar(
                text("""
            SELECT count(*) FROM public.idempotency_receipts
            WHERE idempotency_key = :key
        """),
                {"key": receipt.idempotency_key},
            )
            == 1
        )


def test_parallel_operator_create_receipt_rolls_back_loser(evidence):
    migration, operator_id, _, _ = evidence
    key = f"create-{uuid4()}"
    digest = sha256(b"same normalized operator request").hexdigest()
    barrier = Barrier(2)

    def create():
        created = uuid4()
        receipt = Receipt(
            operator_id,
            "clinics.create",
            key,
            digest,
            {},
            0,
            result_organization_id=created,
        )
        try:
            with migration.begin() as connection:
                barrier.wait(timeout=10)
                connection.execute(
                    text("""
                    INSERT INTO public.organizations (id, branch, created_by_operator_id)
                    VALUES (:id, 'Synthetic concurrent clinic', :operator)
                """),
                    {"id": created, "operator": operator_id},
                )
                insert_operator_create_receipt(
                    connection, receipt, f"synthetic:{operator_id}"
                )
            return "winner"
        except ReceiptConflict:
            return "loser"

    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = [
            future.result() for future in [pool.submit(create) for _ in range(2)]
        ]
    assert sorted(outcomes) == ["loser", "winner"]
    with migration.connect() as connection:
        stored = lookup_operator_create_receipt(
            connection, operator_id, key, f"synthetic:{operator_id}"
        )
        assert stored["request_digest"] == digest
        assert (
            connection.scalar(
                text("""
            SELECT count(*) FROM public.organizations
            WHERE branch = 'Synthetic concurrent clinic'
              AND created_by_operator_id = :operator
        """),
                {"operator": operator_id},
            )
            == 1
        )


def test_tenant_receipt_is_not_accessible_to_operator(evidence, role_engine):
    migration, _, tenant_id, staff_id = evidence
    receipt = Receipt(
        staff_id,
        "patients.create",
        f"synthetic-{uuid4()}",
        sha256(b"synthetic patient body").hexdigest(),
        {"id": "synthetic"},
        0,
        organization_id=tenant_id,
        staff_id=staff_id,
    )
    with migration.begin() as connection:
        insert_tenant_receipt(connection, receipt)
    runtime = role_engine("carepulse_runtime")
    operator = role_engine("carepulse_operator")
    try:
        with runtime.begin() as connection:
            connection.execute(
                text("SELECT set_config('app.organization_id', :id, true)"),
                {"id": str(tenant_id)},
            )
            assert lookup_tenant_receipt(
                connection,
                tenant_id,
                staff_id,
                receipt.operation,
                receipt.idempotency_key,
            )["result_projection"] == {"id": "synthetic"}
        with operator.begin() as connection, pytest.raises(DBAPIError):
            connection.execute(
                text("""
                    SELECT * FROM public.idempotency_receipts WHERE scope_kind = 'TENANT'
                """)
            ).all()
    finally:
        runtime.dispose()
        operator.dispose()

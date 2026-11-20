"""US2 sender uncertainty, ambiguous SMTP settlement, and lease-expiry non-reclaim (T041)."""

from __future__ import annotations

import uuid
from datetime import timedelta

import pytest
from carepulse.compat.admission import (
    CANDIDATE_HEAD,
    CompatRuntimeConfig,
    acquire_invocation_permit,
    record_attempt_disposition,
    release_invocation_permit,
)
from carepulse.notifications import email_sender
from carepulse.notifications.evidence import ClaimPolicy, claim_delivery_job
from sqlalchemy import text

from ops.compat.control import claim_owner_epoch, release_owner_lock
from ops.compat.drain import DrainError, drain_and_settle
from ops.compat.work import (
    WorkEvidenceError,
    assert_all_work_settled,
    resolve_work_with_proof,
)
from tests.integration.compat_ci_helpers import (
    build_manifest,
    connect_role,
    disposable_database,
    ensure_roles_provisioned,
    get_cluster_id,
    grant_db_access,
    make_sa_engine,
    require_compat_ci,
    upgrade_to_revision,
)
from tests.integration.compat_matrix_fixture import (
    initial_activate_matched_target,
    provision_target_control,
)


@pytest.fixture(scope="module", autouse=True)
def _ci_guard() -> None:
    require_compat_ci()


def _seed_claimable_job(conn, job_id: uuid.UUID) -> uuid.UUID:
    """A real due, proof-backed job; UNKNOWN must block a valid reclaim."""
    organization_id, operator_id, invitation_id = (uuid.uuid4() for _ in range(3))
    recipient = f"compat-{job_id.hex}@example.invalid"
    conn.execute(
        """
        INSERT INTO public.operator_identities (id, credential_digest, issued_by_reference)
        VALUES (%s, %s, 'compat-delivery-synthetic')
        """,
        (operator_id, f"synthetic:{operator_id}"),
    )
    conn.execute(
        "INSERT INTO public.organizations (id, branch, created_by_operator_id) "
        "VALUES (%s, 'Synthetic compat delivery', %s)",
        (organization_id, operator_id),
    )
    conn.execute(
        """
        INSERT INTO public.invitations
            (id, organization_id, purpose, target_email, proof_digest,
             issued_by_operator_id, expires_at)
        VALUES (%s, %s, 'FIRST_OWNER', %s, %s, %s,
                clock_timestamp() + interval '1 hour')
        """,
        (invitation_id, organization_id, recipient, "a" * 64, operator_id),
    )
    conn.execute(
        """
        INSERT INTO public.delivery_jobs
            (id, organization_id, invitation_id, proof_reference,
             purpose, recipient, template, logical_key, proof_expires_at)
        VALUES (%s, %s, %s, %s,
                'FIRST_OWNER', %s, 'FIRST_OWNER_INVITE', %s,
                clock_timestamp() + interval '1 hour')
        """,
        (
            job_id,
            organization_id,
            invitation_id,
            str(invitation_id),
            recipient,
            str(invitation_id),
        ),
    )
    conn.execute(
        """
        INSERT INTO public.protected_delivery_material
            (job_id, delivery_key_id, ciphertext_base64url)
        VALUES (%s, 'compat-synthetic-key', %s)
        """,
        (job_id, "A" * 512),
    )
    conn.commit()
    return organization_id


def test_ambiguous_sender_attempt_blocks_reclaim_and_drain() -> None:
    with disposable_database("compat_deliv") as dbname:
        admin = connect_role("carepulse_bootstrap", dbname=dbname, autocommit=True)
        try:
            ensure_roles_provisioned(admin, source_only=False)
            grant_db_access(admin, dbname, source_only=False)
            cluster_id = get_cluster_id(admin)
        finally:
            admin.close()

        upgrade_to_revision(dbname, CANDIDATE_HEAD)

        target_id = f"t-{dbname}"
        src_m = build_manifest(
            generation="source",
            target_id=target_id,
            cluster_id=cluster_id,
            db_name=dbname,
        )
        cand_m = build_manifest(
            generation="candidate",
            target_id=target_id,
            cluster_id=cluster_id,
            db_name=dbname,
        )
        mig = connect_role("carepulse_migration", dbname=dbname)
        try:
            provision_target_control(
                mig,
                target_id=target_id,
                cluster_id=cluster_id,
                db_name=dbname,
                source_manifest_id=src_m.manifest_id,
                target_manifest_id=cand_m.manifest_id,
            )
            initial_activate_matched_target(
                mig, manifest=cand_m, expected_target_id=target_id
            )
        finally:
            mig.close()

        # Sender records a claimed attempt that ends in ambiguous UNKNOWN outcome.
        sender_cfg = CompatRuntimeConfig(
            target_id=target_id,
            manifest_id=cand_m.manifest_id,
            generation="candidate",
            expected_head=CANDIDATE_HEAD,
            service="sender",
            db_name=dbname,
        )
        job_id = uuid.uuid4()
        fixture_conn = connect_role("carepulse_migration", dbname=dbname)
        try:
            organization_id = _seed_claimable_job(fixture_conn, job_id)
        finally:
            fixture_conn.close()
        sender_eng = make_sa_engine("carepulse_sender", dbname=dbname)
        try:
            with sender_eng.begin() as conn:
                conn.exec_driver_sql(
                    "SELECT set_config('app.organization_id', %s, true)",
                    (str(organization_id),),
                )
                # The same job was genuinely eligible and claimable before
                # transport became ambiguous; never test an absent job.
                original = claim_delivery_job(
                    conn, ClaimPolicy(timedelta(seconds=30), 3), job_id=job_id
                )
                assert original is not None and original.id == job_id

            # Simulate a crashed sender's lease expiry in this disposable test
            # database without relaxing the production claim/lease guards.
            migration = connect_role("carepulse_migration", dbname=dbname)
            try:
                migration.execute(
                    "ALTER TABLE public.delivery_jobs DISABLE TRIGGER delivery_jobs_guard"
                )
                migration.execute(
                    "UPDATE public.delivery_jobs SET "
                    "claimed_at = clock_timestamp() - interval '2 hours', "
                    "lease_expires_at = clock_timestamp() - interval '1 hour' "
                    "WHERE id = %s",
                    (job_id,),
                )
                migration.execute(
                    "ALTER TABLE public.delivery_jobs ENABLE TRIGGER delivery_jobs_guard"
                )
                migration.commit()
            finally:
                migration.close()

            permit, pconn = acquire_invocation_permit(
                sender_eng,
                config=sender_cfg,
                expected_login="carepulse_sender",
                invocation_class="sender_claim",
                correlation_id=str(job_id),
                record_attempt=True,
            )
            # The exact sender's own PENDING reservation is excluded only while
            # it presents that same live attempt; a crashed sender leaves PENDING.
            with sender_eng.begin() as conn:
                conn.exec_driver_sql(
                    "SELECT set_config('app.organization_id', %s, true)",
                    (str(organization_id),),
                )
                conn.exec_driver_sql(
                    "SELECT set_config('app.compat_attempt_id', %s, true)",
                    (permit.attempt_id,),
                )
                assert not conn.scalar(
                    text(
                        "SELECT compat_control.has_unknown_sender_attempt(:job, :tenant)"
                    ),
                    {"job": str(job_id), "tenant": str(organization_id)},
                )
            with sender_eng.begin() as conn:
                conn.exec_driver_sql(
                    "SELECT set_config('app.organization_id', %s, true)",
                    (str(organization_id),),
                )
                assert (
                    claim_delivery_job(
                        conn, ClaimPolicy(timedelta(seconds=30), 3), job_id=job_id
                    )
                    is None
                )
                assert (
                    claim_delivery_job(conn, ClaimPolicy(timedelta(seconds=30), 3))
                    is None
                )
            verify_pending = connect_role("carepulse_migration", dbname=dbname)
            try:
                row = verify_pending.execute(
                    "SELECT disposition, correlation_id, lease_generation "
                    "FROM compat_control.work_evidence WHERE attempt_id=%s",
                    (permit.attempt_id,),
                ).fetchone()
                assert (
                    row["disposition"],
                    row["correlation_id"],
                    row["lease_generation"],
                ) == (
                    "PENDING",
                    str(job_id),
                    None,
                )
            finally:
                verify_pending.close()
            record_attempt_disposition(
                pconn,
                permit,
                invocation_class="sender_claim",
                disposition="UNKNOWN",
                correlation_id=str(job_id),
                lease_generation=original.generation,
            )
            release_invocation_permit(pconn, permit)

            # Even a remembered attempt ID cannot exempt an UNKNOWN outcome.
            with sender_eng.begin() as conn:
                conn.exec_driver_sql(
                    "SELECT set_config('app.organization_id', %s, true)",
                    (str(organization_id),),
                )
                conn.exec_driver_sql(
                    "SELECT set_config('app.compat_attempt_id', %s, true)",
                    (permit.attempt_id,),
                )
                assert conn.scalar(
                    text(
                        "SELECT compat_control.has_unknown_sender_attempt(:job, :tenant)"
                    ),
                    {"job": str(job_id), "tenant": str(organization_id)},
                )

            # Even after lease expiry or restart, claim_delivery_job refuses to reclaim UNKNOWN job_id!
            with sender_eng.begin() as conn:
                conn.exec_driver_sql(
                    "SELECT set_config('app.organization_id', %s, true)",
                    (str(organization_id),),
                )
                claimed = claim_delivery_job(
                    conn, ClaimPolicy(timedelta(seconds=30), 3), job_id=job_id
                )
                assert claimed is None
            with sender_eng.begin() as conn:
                conn.exec_driver_sql(
                    "SELECT set_config('app.organization_id', %s, true)",
                    (str(organization_id),),
                )
                assert (
                    claim_delivery_job(conn, ClaimPolicy(timedelta(seconds=30), 3))
                    is None
                )
            verify = connect_role("carepulse_migration", dbname=dbname)
            try:
                job = verify.execute(
                    "SELECT state, claim_generation, attempts FROM public.delivery_jobs WHERE id=%s",
                    (job_id,),
                ).fetchone()
                assert job["state"] == "CLAIMED"
                assert job["claim_generation"] == original.generation
                assert job["attempts"] == 1
            finally:
                verify.close()
        finally:
            sender_eng.dispose()

        # Controller drain MUST fail closed while UNKNOWN work exists.
        owner_conn = connect_role("carepulse_migration", dbname=dbname)
        try:
            ctrl = claim_owner_epoch(
                owner_conn,
                target_id=target_id,
                expected_cluster_id=cluster_id,
                expected_db_name=dbname,
            )
            with pytest.raises(WorkEvidenceError):
                drain_and_settle(
                    owner_conn, target_id=target_id, owner_epoch=ctrl.owner_epoch
                )

            # No SMTP transport was invoked by this synthetic negative case.
            resolve_work_with_proof(
                owner_conn,
                attempt_id=permit.attempt_id,
                target_id=target_id,
                proven_disposition="NOT_SENT",
                correlation_id=str(job_id),
            )
            assert_all_work_settled(owner_conn, target_id=target_id)
            # Work disposition alone does not release a durable CLAIMED lease;
            # a source sender claim must be settled independently first.
            with pytest.raises(DrainError, match="Claimed sender jobs"):
                drain_and_settle(
                    owner_conn, target_id=target_id, owner_epoch=ctrl.owner_epoch
                )
        finally:
            release_owner_lock(owner_conn, target_id)
            owner_conn.close()


@pytest.mark.parametrize(
    (
        "transport_result",
        "expected_result",
        "expected_job_state",
        "expected_disposition",
    ),
    [
        ("NOT_SENT", "READY", "READY", "NOT_SENT"),
        ("AMBIGUOUS", "AMBIGUOUS", "CLAIMED", "UNKNOWN"),
        ("NOT_SENT", "INTERRUPTED", "READY", "UNKNOWN"),
    ],
)
def test_real_sender_claim_transport_and_interrupted_settlement(
    monkeypatch,
    transport_result,
    expected_result,
    expected_job_state,
    expected_disposition,
) -> None:
    with disposable_database("compat_send") as dbname:
        admin = connect_role("carepulse_bootstrap", dbname=dbname, autocommit=True)
        try:
            ensure_roles_provisioned(admin, source_only=False)
            grant_db_access(admin, dbname, source_only=False)
            cluster_id = get_cluster_id(admin)
        finally:
            admin.close()
        upgrade_to_revision(dbname, CANDIDATE_HEAD)
        target_id = f"t-{dbname}"
        source_manifest = build_manifest(
            generation="source",
            target_id=target_id,
            cluster_id=cluster_id,
            db_name=dbname,
        )
        candidate_manifest = build_manifest(
            generation="candidate",
            target_id=target_id,
            cluster_id=cluster_id,
            db_name=dbname,
        )
        migration = connect_role("carepulse_migration", dbname=dbname)
        try:
            provision_target_control(
                migration,
                target_id=target_id,
                cluster_id=cluster_id,
                db_name=dbname,
                source_manifest_id=source_manifest.manifest_id,
                target_manifest_id=candidate_manifest.manifest_id,
            )
            initial_activate_matched_target(
                migration,
                manifest=candidate_manifest,
                expected_target_id=target_id,
            )
            job_id = uuid.uuid4()
            tenant_id = _seed_claimable_job(migration, job_id)
        finally:
            migration.close()

        monkeypatch.setenv("CP_COMPAT_TARGET_ID", target_id)
        monkeypatch.setenv("CP_COMPAT_GENERATION", "candidate")
        monkeypatch.setenv("CP_COMPAT_EXPECTED_HEAD", CANDIDATE_HEAD)
        monkeypatch.setenv("CP_COMPAT_MANIFEST_ID", candidate_manifest.manifest_id)
        monkeypatch.setenv("CP_DB_NAME", dbname)
        transport_calls = []

        def transport(payload, _timeout):
            if payload is None:
                return "SENT"  # Local config check; no SMTP connection.
            transport_calls.append(job_id)
            return transport_result

        monkeypatch.setattr(email_sender, "_node_call", transport)
        original_tenant = email_sender._tenant
        if expected_result == "INTERRUPTED":
            count = 0

            def interrupted_readback(connection, organization_id, attempt_id=None):
                nonlocal count
                count += 1
                if count == 4:  # Readback after the actual claim and finish commits.
                    raise RuntimeError("Independent sender settlement read interrupted")
                return original_tenant(connection, organization_id, attempt_id)

            monkeypatch.setattr(email_sender, "_tenant", interrupted_readback)
        sender = make_sa_engine("carepulse_sender", dbname=dbname)
        settings = email_sender.SenderSettings(
            ClaimPolicy(timedelta(seconds=30), 3),
            5,
            30,
            sender.url,
        )
        try:
            if expected_result == "INTERRUPTED":
                with pytest.raises(RuntimeError, match="settlement read interrupted"):
                    email_sender.send_one_job(tenant_id, job_id, settings)
                monkeypatch.setattr(email_sender, "_tenant", original_tenant)
            else:
                assert (
                    email_sender.send_one_job(tenant_id, job_id, settings)
                    == expected_result
                )
            assert transport_calls == [job_id]
            observer = connect_role("carepulse_migration", dbname=dbname)
            try:
                job = observer.execute(
                    "SELECT state, attempts, claim_generation, last_outcome "
                    "FROM public.delivery_jobs WHERE id=%s",
                    (job_id,),
                ).fetchone()
                evidence = observer.execute(
                    "SELECT disposition, correlation_id, lease_generation "
                    "FROM compat_control.work_evidence "
                    "WHERE target_id=%s AND service='sender'",
                    (target_id,),
                ).fetchone()
                assert (job["state"], job["attempts"], job["claim_generation"]) == (
                    expected_job_state,
                    1,
                    1,
                )
                assert (
                    evidence["disposition"],
                    evidence["correlation_id"],
                    evidence["lease_generation"],
                ) == (
                    expected_disposition,
                    str(job_id),
                    1,
                )
                if transport_result == "NOT_SENT":
                    assert job["last_outcome"] == "RETRY_SCHEDULED"
                    if expected_result != "INTERRUPTED":
                        assert (
                            email_sender.send_one_job(tenant_id, job_id, settings)
                            == "NOT_DUE"
                        )
                        assert transport_calls == [job_id]
                    # Scheduled retry becomes due only after its persisted deadline.
                    observer.execute(
                        "ALTER TABLE public.delivery_jobs DISABLE TRIGGER delivery_jobs_guard"
                    )
                    observer.execute(
                        "UPDATE public.delivery_jobs SET not_before=clock_timestamp()-interval '1 second' "
                        "WHERE id=%s",
                        (job_id,),
                    )
                    observer.execute(
                        "ALTER TABLE public.delivery_jobs ENABLE TRIGGER delivery_jobs_guard"
                    )
                    observer.commit()
                    assert email_sender.send_one_job(tenant_id, job_id, settings) == (
                        "NOT_DUE" if expected_result == "INTERRUPTED" else "READY"
                    )
                    assert transport_calls == (
                        [job_id]
                        if expected_result == "INTERRUPTED"
                        else [job_id, job_id]
                    )
                    retried = observer.execute(
                        "SELECT state, attempts, claim_generation FROM public.delivery_jobs "
                        "WHERE id=%s",
                        (job_id,),
                    ).fetchone()
                    assert (
                        retried["state"],
                        retried["attempts"],
                        retried["claim_generation"],
                    ) == (
                        ("READY", 1, 1)
                        if expected_result == "INTERRUPTED"
                        else ("READY", 2, 2)
                    )
                else:
                    assert job["last_outcome"] is None
                    observer.execute(
                        "ALTER TABLE public.delivery_jobs DISABLE TRIGGER delivery_jobs_guard"
                    )
                    observer.execute(
                        "UPDATE public.delivery_jobs SET "
                        "claimed_at=clock_timestamp()-interval '2 hours', "
                        "lease_expires_at=clock_timestamp()-interval '1 hour' "
                        "WHERE id=%s",
                        (job_id,),
                    )
                    observer.execute(
                        "ALTER TABLE public.delivery_jobs ENABLE TRIGGER delivery_jobs_guard"
                    )
                    observer.commit()
                    assert (
                        email_sender.send_one_job(tenant_id, None, settings)
                        == "NOT_DUE"
                    )
                    assert transport_calls == [job_id]
                    unchanged = observer.execute(
                        "SELECT state, attempts, claim_generation FROM public.delivery_jobs "
                        "WHERE id=%s",
                        (job_id,),
                    ).fetchone()
                    assert (
                        unchanged["state"],
                        unchanged["attempts"],
                        unchanged["claim_generation"],
                    ) == ("CLAIMED", 1, 1)
            finally:
                observer.close()

            # An admitted HTTP invocation can finish its socket lifetime with
            # no independently proved domain receipt. Releasing the permit
            # preserves PENDING through a separate observer transaction.
            api_engine = make_sa_engine("carepulse_api", dbname=dbname)
            try:
                api_cfg = CompatRuntimeConfig(
                    target_id=target_id,
                    manifest_id=candidate_manifest.manifest_id,
                    generation="candidate",
                    expected_head=CANDIDATE_HEAD,
                    service="api",
                    db_name=dbname,
                )
                http_permit, http_connection = acquire_invocation_permit(
                    api_engine,
                    config=api_cfg,
                    expected_login="carepulse_api",
                    invocation_class="http",
                    record_attempt=True,
                )
                release_invocation_permit(http_connection, http_permit)
                verification = connect_role("carepulse_migration", dbname=dbname)
                try:
                    http = verification.execute(
                        "SELECT disposition, correlation_id "
                        "FROM compat_control.work_evidence WHERE attempt_id=%s",
                        (http_permit.attempt_id,),
                    ).fetchone()
                    assert (http["disposition"], http["correlation_id"]) == (
                        "PENDING",
                        None,
                    )
                    with pytest.raises(WorkEvidenceError):
                        assert_all_work_settled(verification, target_id=target_id)
                finally:
                    verification.close()
            finally:
                api_engine.dispose()
        finally:
            sender.dispose()

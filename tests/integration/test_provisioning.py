"""B-03: restricted operator privileges and atomic first-Owner activation."""

import hashlib
import secrets
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from pathlib import Path
from threading import Barrier
from uuid import uuid4

import pytest
from carepulse.config import ServiceConfig, _load_policy
from carepulse.db import transaction
from carepulse.identity.receipts import ReceiptConflict
from carepulse.provisioning.invitations import (
    create_clinic,
    reissue_invitation,
    revoke_invitation,
)
from carepulse.provisioning.operators import OperatorDenied, authenticate_operator
from carepulse.provisioning.service import change_clinic_state
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError


@pytest.fixture
def operator_identity(role_engine, monkeypatch):
    monkeypatch.setenv(
        "CP_POLICY_FILE",
        str(Path(__file__).parents[1] / "fixtures/foundation-policy.json"),
    )
    secret = secrets.token_hex(32)
    actor = uuid4()
    migration = role_engine("carepulse_migration")
    with migration.begin() as connection:
        connection.execute(
            text("""
                INSERT INTO public.operator_identities
                    (id, credential_digest, issued_by_reference, issued_at)
                VALUES (:actor, :digest, 'synthetic-b03', clock_timestamp())
            """),
            {"actor": actor, "digest": hashlib.sha256(secret.encode()).hexdigest()},
        )
    operator = role_engine("carepulse_operator")
    try:
        yield migration, operator, actor, secret
    finally:
        operator.dispose()
        migration.dispose()


@pytest.fixture
def operator_config():
    public_key = rsa.generate_private_key(public_exponent=65537, key_size=3072)
    return ServiceConfig(
        environment="test",
        policy=_load_policy(),
        db_host="db",
        db_port=5432,
        db_name="carepulse",
        db_user="carepulse_operator",
        db_password="fixture-unused",
        file_encryption_key=None,
        service_key=None,
        signing_key_id=None,
        mail_enabled=False,
        scan_enabled=False,
        mail=None,
        scan=None,
        delivery_key_id="synthetic-b03-fixture",
        delivery_public_key_pem=public_key.public_key().public_bytes(
            serialization.Encoding.PEM,
            serialization.PublicFormat.SubjectPublicKeyInfo,
        ),
    )


def test_same_key_parallel_create_is_atomic_with_lost_response_retry(
    operator_identity, operator_config
):
    migration, operator, actor, secret = operator_identity
    verified = authenticate_operator(operator, actor, secret, _load_policy())
    key = str(uuid4())
    branch = f"Synthetic parallel {uuid4()}"
    arguments = (
        operator,
        verified,
        operator_config,
        branch,
        f"{uuid4()}@example.invalid",
        key,
        "Synthetic provisioning",
    )

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(create_clinic, *arguments) for _ in range(2)]
        created = [future.result() for future in futures]
    assert created[0] == created[1]
    assert created[0]["state"] == "PROVISIONING"
    tenant = created[0]["organization_id"]

    with migration.connect() as connection:
        assert (
            connection.scalar(
                text("""
            SELECT count(*) FROM public.organizations
            WHERE created_by_operator_id = :actor AND branch = :branch
        """),
                {"actor": actor, "branch": branch},
            )
            == 1
        )
        assert (
            connection.scalar(
                text("""
            SELECT count(*) FROM public.invitations
            WHERE organization_id = :tenant AND state = 'ISSUED'
        """),
                {"tenant": tenant},
            )
            == 1
        )
        assert (
            connection.scalar(
                text("""
            SELECT count(*) FROM public.delivery_jobs
            WHERE organization_id = :tenant AND purpose = 'FIRST_OWNER'
        """),
                {"tenant": tenant},
            )
            == 1
        )
        assert (
            connection.scalar(
                text("""
            SELECT count(*) FROM public.idempotency_receipts
            WHERE result_organization_id = :tenant AND scope_kind = 'OPERATOR_CREATE'
        """),
                {"tenant": tenant},
            )
            == 1
        )
        assert (
            connection.scalar(
                text("""
            SELECT count(*) FROM public.sensitive_audit
            WHERE organization_id = :tenant AND actor_role = 'OPERATOR'
        """),
                {"tenant": tenant},
            )
            == 2
        )
        assert (
            connection.scalar(
                text("""
            SELECT count(*) FROM public.protected_delivery_material m
            JOIN public.delivery_jobs j ON j.id = m.job_id
            WHERE j.organization_id = :tenant
        """),
                {"tenant": tenant},
            )
            == 1
        )

    assert create_clinic(*arguments) == created[0]
    with pytest.raises(ReceiptConflict):
        create_clinic(
            operator,
            verified,
            operator_config,
            branch + " changed",
            arguments[4],
            key,
            arguments[6],
        )
    with migration.begin() as connection:
        connection.execute(
            text("UPDATE public.operator_identities SET enabled=false WHERE id=:actor"),
            {"actor": actor},
        )
    with pytest.raises(DBAPIError):
        create_clinic(*arguments)


def test_operator_credential_is_required_and_clinical_grants_are_denied(
    operator_identity,
):
    migration, operator, actor, secret = operator_identity
    policy = _load_policy()
    with pytest.raises(OperatorDenied):
        authenticate_operator(operator, actor, "invalid" * 8, policy)
    identified = authenticate_operator(operator, actor, secret, policy)
    assert identified.id == actor
    with pytest.raises(DBAPIError), operator.begin() as connection:
        connection.execute(text("SELECT * FROM public.clinical_audit LIMIT 1"))
    with pytest.raises(DBAPIError), operator.begin() as connection:
        connection.execute(
            text("SELECT credential_digest FROM public.operator_identities")
        )
    with migration.begin() as connection:
        connection.execute(
            text("UPDATE public.operator_identities SET enabled=false WHERE id=:actor"),
            {"actor": actor},
        )
    with pytest.raises(OperatorDenied):
        authenticate_operator(operator, actor, secret, policy)


def test_operator_must_commit_complete_provisioning_evidence(operator_identity):
    migration, operator, actor, secret = operator_identity
    digest = hashlib.sha256(secret.encode()).hexdigest()
    clinic = uuid4()
    with pytest.raises(DBAPIError) as missing_evidence, operator.begin() as connection:
        connection.scalar(
            text(
                "SELECT public.operator_create_clinic(:actor, :digest, :clinic, 'Synthetic B-03')"
            ),
            {"actor": actor, "digest": digest, "clinic": clinic},
        )
    assert missing_evidence.value.orig.sqlstate == "23514"
    with migration.begin() as connection:
        connection.execute(
            text("""
                INSERT INTO public.organizations (id, branch, created_by_operator_id)
                VALUES (:clinic, 'Synthetic B-03 fixture', :actor)
            """),
            {"clinic": clinic, "actor": actor},
        )
    with operator.begin() as connection:
        listing = (
            connection.execute(
                text(
                    "SELECT * FROM public.operator_list_clinics(:actor, :digest, :reason)"
                ),
                {"actor": actor, "digest": digest, "reason": "Synthetic list test"},
            )
            .mappings()
            .all()
        )
        assert any(
            row["id"] == clinic and row["state"] == "PROVISIONING" for row in listing
        )
    with pytest.raises(DBAPIError), operator.begin() as connection:
        connection.execute(
            text("UPDATE public.organizations SET state='ACTIVE' WHERE id=:id"),
            {"id": clinic},
        )


def test_reissue_and_revoke_preserve_one_issued_owner_proof(
    operator_identity, operator_config, monkeypatch
):
    migration, operator, actor, secret = operator_identity
    verified = authenticate_operator(operator, actor, secret, _load_policy())
    intended = f"{uuid4()}@example.invalid"
    created = create_clinic(
        operator,
        verified,
        operator_config,
        "Synthetic reissue",
        intended,
        str(uuid4()),
        "Create first owner",
    )
    clinic = uuid4_from_text(created["organization_id"])
    with migration.connect() as connection:
        original = connection.scalar(
            text("""
            SELECT id FROM public.invitations
            WHERE organization_id = :clinic AND purpose = 'FIRST_OWNER'
        """),
            {"clinic": clinic},
        )

    with pytest.raises(ValueError):
        reissue_invitation(
            operator,
            verified,
            operator_config,
            clinic,
            original,
            0,
            0,
            "Wrong recipient",
            owner_email="different@example.invalid",
        )
    from carepulse.notifications import delivery_material

    def deny_encryption(*_args, **_kwargs):
        raise ValueError("Synthetic encryption failure")

    with monkeypatch.context() as patch:
        patch.setattr(delivery_material, "encrypt_delivery_token", deny_encryption)
        with pytest.raises(ValueError):
            reissue_invitation(
                operator,
                verified,
                operator_config,
                clinic,
                original,
                0,
                0,
                "Crypto unavailable",
            )
    with migration.connect() as connection:
        assert (
            connection.scalar(
                text("""
            SELECT count(*) FROM public.invitations
            WHERE organization_id = :clinic AND state = 'ISSUED'
        """),
                {"clinic": clinic},
            )
            == 1
        )

    replacement = reissue_invitation(
        operator,
        verified,
        operator_config,
        clinic,
        original,
        0,
        0,
        "Supersede first proof",
    )
    new_invite = uuid4_from_text(replacement["invitation_id"])
    with migration.connect() as connection:
        assert connection.execute(
            text("""
            SELECT state, superseded_by_id FROM public.invitations WHERE id = :old
        """),
            {"old": original},
        ).one() == ("REVOKED", new_invite)
        assert (
            connection.scalar(
                text("""
            SELECT count(*) FROM public.invitations
            WHERE organization_id = :clinic AND state = 'ISSUED'
        """),
                {"clinic": clinic},
            )
            == 1
        )

    revoked = revoke_invitation(
        operator, verified, clinic, new_invite, 0, 0, "Revoke first owner"
    )
    assert revoked["state"] == "REVOKED"
    with migration.connect() as connection:
        assert (
            connection.scalar(
                text("""
            SELECT count(*) FROM public.invitations
            WHERE organization_id = :clinic AND state = 'ISSUED'
        """),
                {"clinic": clinic},
            )
            == 0
        )
    new_proof = reissue_invitation(
        operator,
        verified,
        operator_config,
        clinic,
        None,
        0,
        0,
        "New proof after revoke",
        owner_email=intended,
    )
    assert new_proof["invitation_id"] not in {str(original), str(new_invite)}


def uuid4_from_text(value: str):
    from uuid import UUID

    return UUID(value)


def test_pending_suspended_clinic_cannot_reactivate(operator_identity, operator_config):
    migration, operator, actor, secret = operator_identity
    verified = authenticate_operator(operator, actor, secret, _load_policy())
    clinic = uuid4_from_text(
        create_clinic(
            operator,
            verified,
            operator_config,
            "Synthetic pending suspend",
            f"{uuid4()}@example.invalid",
            str(uuid4()),
            "Create pending",
        )["organization_id"]
    )
    changed = change_clinic_state(
        operator, verified, clinic, 0, "SUSPENDED", "Hold pending clinic"
    )
    assert changed["state"] == "SUSPENDED" and changed["revision"] == 1
    with pytest.raises(DBAPIError):
        change_clinic_state(
            operator, verified, clinic, 1, "ACTIVE", "Invalid pending reactivation"
        )
    with migration.connect() as connection:
        assert connection.execute(
            text("""
            SELECT state, first_activated_at FROM public.organizations WHERE id = :id
        """),
            {"id": clinic},
        ).one() == ("SUSPENDED", None)


def test_first_owner_proof_is_single_use_and_tenant_bound(
    operator_identity, role_engine
):
    migration, operator, actor, secret = operator_identity
    clinic, other, invite = uuid4(), uuid4(), uuid4()
    proof = secrets.token_urlsafe(32)
    email = f"{uuid4()}@example.invalid"
    prepared_user = f"synthetic-b03-{uuid4()}"
    with migration.begin() as connection:
        for tenant in (clinic, other):
            connection.execute(
                text(
                    "INSERT INTO public.organizations (id, branch, created_by_operator_id) VALUES (:clinic, 'Synthetic', :actor)"
                ),
                {"actor": actor, "clinic": tenant},
            )
        connection.execute(
            text("""
                INSERT INTO public.invitations
                  (id, organization_id, purpose, target_email,
                   proof_digest, issued_by_operator_id, expires_at)
                VALUES (:invite, :clinic, 'FIRST_OWNER', :email, :proof,
                        :actor, :expiry)
            """),
            {
                "actor": actor,
                "clinic": clinic,
                "invite": invite,
                "email": email,
                "proof": hashlib.sha256(proof.encode()).hexdigest(),
                "expiry": datetime.now(UTC) + timedelta(minutes=10),
            },
        )
    with migration.begin() as connection:
        connection.execute(
            text("""
          INSERT INTO public."user" (id, name, email, "emailVerified", "createdAt", "updatedAt")
          VALUES (:id, 'Synthetic Owner', :email, true, clock_timestamp(), clock_timestamp())
        """),
            {"id": prepared_user, "email": email},
        )
    runtime = role_engine("carepulse_runtime")
    try:
        with transaction(runtime, other) as connection:
            assert (
                connection.execute(
                    text(
                        "SELECT * FROM public.verify_first_owner_preparation(:clinic,:invite,:digest)"
                    ),
                    {
                        "clinic": clinic,
                        "invite": invite,
                        "digest": hashlib.sha256(proof.encode()).hexdigest(),
                    },
                ).all()
                == []
            )
        with transaction(runtime, clinic) as connection:
            assert (
                connection.execute(
                    text(
                        "SELECT * FROM public.verify_first_owner_preparation(:clinic,:invite,:digest)"
                    ),
                    {
                        "clinic": clinic,
                        "invite": invite,
                        "digest": hashlib.sha256(proof.encode()).hexdigest(),
                    },
                ).one()[0]
                == email
            )
            assert (
                connection.scalar(
                    text("""
                SELECT owner_id FROM public.activate_first_owner(
                  :clinic, :invite, :proof, :user, 0, 0, :staff)
            """),
                    {
                        "clinic": clinic,
                        "invite": invite,
                        "proof": proof,
                        "user": prepared_user,
                        "staff": uuid4(),
                    },
                )
                is not None
            )
        with transaction(runtime, clinic) as connection:
            assert (
                connection.execute(
                    text(
                        "SELECT * FROM public.verify_first_owner_preparation(:clinic,:invite,:digest)"
                    ),
                    {
                        "clinic": clinic,
                        "invite": invite,
                        "digest": hashlib.sha256(proof.encode()).hexdigest(),
                    },
                ).all()
                == []
            )
            assert (
                connection.scalar(
                    text("""
                SELECT count(*) FROM public.staff_accounts
                WHERE organization_id=:clinic AND role='DOCTOR_OWNER'
            """),
                    {"clinic": clinic},
                )
                == 1
            )
        with pytest.raises(DBAPIError), transaction(runtime, other) as connection:
            connection.execute(
                text("""
                SELECT * FROM public.activate_first_owner(
                    :clinic, :invite, :proof, :user, 0, 0, :staff)
            """),
                {
                    "clinic": clinic,
                    "invite": invite,
                    "proof": proof,
                    "user": prepared_user,
                    "staff": uuid4(),
                },
            )
    finally:
        runtime.dispose()

    verified = authenticate_operator(operator, actor, secret, _load_policy())
    suspended = change_clinic_state(
        operator, verified, clinic, 1, "SUSPENDED", "Synthetic Owner suspension"
    )
    assert suspended["revision"] == 2
    with migration.connect() as connection:
        assert (
            connection.scalar(
                text("SELECT state FROM public.organizations WHERE id=:clinic"),
                {"clinic": clinic},
            )
            == "SUSPENDED"
        )
    with pytest.raises(DBAPIError):
        change_clinic_state(
            operator, verified, clinic, 1, "ACTIVE", "Stale reactivation"
        )
    reactivated = change_clinic_state(
        operator, verified, clinic, 2, "ACTIVE", "Verified Owner reactivation"
    )
    assert reactivated["revision"] == 3
    with migration.connect() as connection:
        assert connection.execute(
            text(
                "SELECT state, lifecycle_revision FROM public.organizations WHERE id=:clinic"
            ),
            {"clinic": clinic},
        ).one() == ("ACTIVE", 3)


def test_concurrent_acceptance_cannot_create_second_owner(
    operator_identity, role_engine
):
    migration, _, actor, _ = operator_identity
    clinic, invite = uuid4(), uuid4()
    proof, prepared_user = secrets.token_urlsafe(32), f"synthetic-{uuid4()}"
    email = f"{uuid4()}@example.invalid"
    with migration.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO public.organizations (id, branch, created_by_operator_id) VALUES (:clinic,'Race clinic',:actor)"
            ),
            {"actor": actor, "clinic": clinic},
        )
        connection.execute(
            text("""
          INSERT INTO public.invitations
            (id, organization_id, purpose, target_email, proof_digest,
             issued_by_operator_id, expires_at)
          VALUES (:invite,:clinic,'FIRST_OWNER',:email,:proof,:actor,:expires)
        """),
            {
                "actor": actor,
                "clinic": clinic,
                "invite": invite,
                "email": email,
                "proof": hashlib.sha256(proof.encode()).hexdigest(),
                "expires": datetime.now(UTC) + timedelta(minutes=10),
            },
        )
    with migration.begin() as connection:
        connection.execute(
            text("""
          INSERT INTO public."user" (id, name, email, "emailVerified", "createdAt", "updatedAt")
          VALUES (:id,'Synthetic',:email,true,clock_timestamp(),clock_timestamp())
        """),
            {"id": prepared_user, "email": email},
        )
    barrier = Barrier(2)

    def attempt():
        engine = role_engine("carepulse_runtime")
        try:
            with transaction(engine, clinic) as connection:
                barrier.wait(timeout=10)
                return connection.scalar(
                    text("""
                  SELECT changed FROM public.activate_first_owner(
                    :clinic,:invite,:proof,:user,0,0,:staff)
                """),
                    {
                        "clinic": clinic,
                        "invite": invite,
                        "proof": proof,
                        "user": prepared_user,
                        "staff": uuid4(),
                    },
                )
        finally:
            engine.dispose()

    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = [
            future.result() for future in [pool.submit(attempt) for _ in range(2)]
        ]
    assert outcomes.count(True) == 1
    assert outcomes.count(False) == 1
    with migration.connect() as connection:
        assert (
            connection.scalar(
                text("""
          SELECT count(*) FROM public.staff_accounts
          WHERE organization_id=:clinic AND role='DOCTOR_OWNER'
        """),
                {"clinic": clinic},
            )
            == 1
        )

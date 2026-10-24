"""B-03 one-shot sender against real disposable PostgreSQL, Node and Mailpit.

CI runs this module in the sender image with a test-only migration credential.
No sender image or ordinary service receives that credential by default.
"""

import hashlib
import json
import os
import re
import secrets
from datetime import timedelta
from pathlib import Path
from urllib.error import URLError
from urllib.request import urlopen
from uuid import uuid4

import pytest
from carepulse.notifications.delivery_material import encrypt_delivery_token
from carepulse.notifications.email_sender import _settings, send_one_job
from carepulse.notifications.evidence import (
    JobOutcome,
    claim_delivery_job,
    create_delivery_job,
    finish_delivery_job,
)
from cryptography.hazmat.primitives import serialization
from sqlalchemy import URL, create_engine, text


@pytest.fixture(scope="module")
def sender_lab():
    if os.getenv("CI") != "true":
        pytest.skip("Sender requires disposable Linux CI and live Mailpit")
    project = os.environ.get("CP_CI_PROJECT", "")
    assert re.fullmatch(r"carepulse-ci-[0-9]+-[0-9]+", project)
    assert os.environ.get("COMPOSE_PROJECT_NAME") == project
    settings = _settings()
    migration = create_engine(
        URL.create(
            "postgresql+psycopg",
            username="carepulse_migration",
            password=Path("/run/secrets/migration_password").read_text().strip(),
            host="db",
            database="carepulse",
        ),
        hide_parameters=True,
    )
    try:
        yield migration, settings
    finally:
        migration.dispose()


def _mailbox(recipient):
    with urlopen("http://mail:8025/api/v1/messages", timeout=6) as response:
        mailbox = json.load(response)
    return [
        message["ID"]
        for message in mailbox["messages"]
        if any(address["Address"] == recipient for address in message["To"])
    ]


def _seed_job(migration, expires_in=timedelta(minutes=20)):
    tenant, operator, invite, job = (uuid4() for _ in range(4))
    token = secrets.token_urlsafe(32)
    recipient = f"mail{uuid4().hex}@example.invalid"
    key_id, private_path = (
        os.environ["CP_DELIVERY_PRIVATE_KEY_FILES"].split(",")[0].split("=", 1)
    )
    private_key = serialization.load_pem_private_key(
        Path(private_path).read_bytes(), password=None
    )
    public_pem = private_key.public_key().public_bytes(
        serialization.Encoding.PEM,
        serialization.PublicFormat.SubjectPublicKeyInfo,
    )
    with migration.begin() as connection:
        expires = connection.scalar(
            text("SELECT clock_timestamp() + :expires_in"),
            {"expires_in": expires_in},
        )
        connection.execute(
            text("""
                INSERT INTO public.operator_identities
                    (id, credential_digest, issued_by_reference, issued_at)
                VALUES (:operator, :digest, 'synthetic-sender-ci', clock_timestamp())
            """),
            {
                "operator": operator,
                "digest": hashlib.sha256(operator.bytes).hexdigest(),
            },
        )
        connection.execute(
            text("""
                INSERT INTO public.organizations (id, branch, created_by_operator_id)
                VALUES (:tenant, 'Synthetic sender CI', :operator)
            """),
            {"tenant": tenant, "operator": operator},
        )
        connection.execute(
            text("""
                INSERT INTO public.invitations
                    (id, organization_id, purpose, target_email, proof_digest,
                     issued_by_operator_id, expires_at)
                VALUES (:invite, :tenant, 'FIRST_OWNER', :recipient,
                        :digest, :operator, :expires)
            """),
            {
                "invite": invite,
                "tenant": tenant,
                "recipient": recipient,
                "digest": hashlib.sha256(token.encode()).hexdigest(),
                "operator": operator,
                "expires": expires,
            },
        )
        context = {
            "job_id": str(job),
            "proof_reference": str(invite),
            "purpose": "FIRST_OWNER",
            "tenant_id": str(tenant),
            "committed_recipient": recipient,
            "expiry_epoch_ms": str(int(expires.timestamp() * 1000)),
        }
        material = encrypt_delivery_token(token, context, key_id, public_pem)
        create_delivery_job(
            connection,
            {
                "id": job,
                "organization_id": tenant,
                "staff_id": None,
                "invitation_id": invite,
                "verification_id": None,
                "recovery_operation_id": None,
                "proof_reference": str(invite),
                "purpose": "FIRST_OWNER",
                "recipient": recipient,
                "template": "FIRST_OWNER_INVITE",
                "logical_key": str(invite),
                "proof_expires_at": expires,
            },
            {
                "delivery_key_id": material["key_id"],
                "ciphertext_base64url": material["ciphertext_base64url"],
            },
        )
    return tenant, invite, job, recipient


def _job_state(migration, job):
    with migration.connect() as connection:
        return connection.execute(
            text("""
                SELECT j.state, j.attempts, j.claim_generation,
                       EXISTS(SELECT 1 FROM public.protected_delivery_material m
                              WHERE m.job_id=j.id) AS has_material
                FROM public.delivery_jobs j WHERE j.id=:job
            """),
            {"job": job},
        ).one()


def _expire_claim_for_test(migration, job):
    # Disposable fixture moves only persisted DB lease time, no sleeps/clock mocks.
    with migration.begin() as connection:
        connection.execute(
            text("ALTER TABLE public.delivery_jobs DISABLE TRIGGER delivery_jobs_guard")
        )
        connection.execute(
            text("""
                UPDATE public.delivery_jobs
                SET claimed_at=clock_timestamp()-interval '2 hours',
                    lease_expires_at=clock_timestamp()-interval '1 hour'
                WHERE id=:job AND state='CLAIMED'
            """),
            {"job": job},
        )
        connection.execute(
            text("ALTER TABLE public.delivery_jobs ENABLE TRIGGER delivery_jobs_guard")
        )


def test_committed_owner_job_delivers_only_intended_mail_and_disposes_material(
    sender_lab,
):
    migration, settings = sender_lab
    tenant, invite, job, recipient = _seed_job(migration)
    assert _mailbox(recipient) == []
    assert send_one_job(tenant, job, settings) == "SENT"
    assert _job_state(migration, job) == ("SENT", 1, 1, False)
    ids = _mailbox(recipient)
    assert len(ids) == 1
    with urlopen(f"http://mail:8025/api/v1/message/{ids[0]}", timeout=6) as response:
        message = json.load(response)
    assert message["Subject"] == "[SYNTHETIC ONLY] CarePulse invitation"
    assert [address["Address"] for address in message["To"]] == [recipient]
    assert "/auth/invite-accept#proof=" in message["Text"]
    assert f"organization_id={tenant}" in message["Text"]
    assert f"proof_reference={invite}" in message["Text"]
    assert send_one_job(tenant, job, settings) == "NOT_DUE"
    assert len(_mailbox(recipient)) == 1


def test_committed_crash_before_smtp_requires_explicit_expired_lease_reclaim(
    sender_lab,
):
    migration, settings = sender_lab
    tenant, _, job, recipient = _seed_job(migration)
    sender = create_engine(settings.connection_url, hide_parameters=True)
    try:
        with sender.begin() as connection:
            connection.execute(
                text("SELECT set_config('app.organization_id', :tenant, true)"),
                {"tenant": str(tenant)},
            )
            original = claim_delivery_job(connection, settings.policy, job)
            assert original and original.generation == 1
        assert send_one_job(tenant, job, settings) == "NOT_DUE"
        assert _job_state(migration, job) == ("CLAIMED", 1, 1, True)
        assert _mailbox(recipient) == []

        _expire_claim_for_test(migration, job)
        assert send_one_job(tenant, job, settings) == "SENT"
        assert _job_state(migration, job) == ("SENT", 2, 2, False)
        assert len(_mailbox(recipient)) == 1
        with sender.begin() as connection:
            connection.execute(
                text("SELECT set_config('app.organization_id', :tenant, true)"),
                {"tenant": str(tenant)},
            )
            assert not finish_delivery_job(connection, original, JobOutcome("FAILED"))
    finally:
        sender.dispose()


def test_expired_invitation_is_cancelled_without_smtp(sender_lab):
    migration, settings = sender_lab
    tenant, invite, job, recipient = _seed_job(migration)
    with migration.begin() as connection:
        connection.execute(
            text(
                "ALTER TABLE public.invitations DISABLE TRIGGER invitations_mutation_guard"
            )
        )
        connection.execute(
            text("""
                UPDATE public.invitations
                SET issued_at=clock_timestamp()-interval '2 hours',
                    expires_at=clock_timestamp()-interval '1 hour'
                WHERE id=:invite
            """),
            {"invite": invite},
        )
        connection.execute(
            text(
                "ALTER TABLE public.invitations ENABLE TRIGGER invitations_mutation_guard"
            )
        )
    assert send_one_job(tenant, job, settings) == "NOT_DUE"
    assert _job_state(migration, job) == ("CANCELLED", 0, 0, False)
    assert _mailbox(recipient) == []


@pytest.mark.skipif(
    os.environ.get("CP_EXPECT_MAILPIT_DOWN") != "true",
    reason="Requires explicitly stopped Mailpit in disposable CI",
)
def test_missing_mailpit_retries_without_losing_proof(sender_lab):
    migration, settings = sender_lab
    tenant, _, job, recipient = _seed_job(migration)
    with pytest.raises(URLError):
        _mailbox(recipient)
    assert send_one_job(tenant, job, settings) == "READY"
    assert _job_state(migration, job) == ("READY", 1, 1, True)
    with migration.connect() as connection:
        assert connection.scalar(
            text(
                "SELECT not_before > updated_at FROM public.delivery_jobs WHERE id=:job"
            ),
            {"job": job},
        )

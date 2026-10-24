"""Explicit one-shot D-09 claim/reclaim/send invocation; no periodic runner."""

import argparse
import json
import subprocess
from dataclasses import dataclass
from datetime import timedelta
from uuid import UUID

from sqlalchemy import URL, create_engine, text
from sqlalchemy.exc import SQLAlchemyError

from carepulse.config import ConfigError, load_config
from carepulse.notifications.evidence import (
    ClaimedJob,
    ClaimPolicy,
    JobOutcome,
    claim_delivery_job,
    finish_delivery_job,
    get_claimed_delivery_material,
)

_NODE = "/usr/local/bin/node"
_ADAPTER = "/srv/apps/web/mail/send-job.ts"


@dataclass(frozen=True)
class SenderSettings:
    policy: ClaimPolicy
    timeout_seconds: int
    retry_seconds: int
    connection_url: URL


def _settings() -> SenderSettings:
    config = load_config("sender")
    if not config.mail_enabled:
        raise ConfigError("Sender requires configured mail")
    url = URL.create(
        "postgresql+psycopg",
        username=config.db_user,
        password=config.db_password,
        host=config.db_host,
        port=config.db_port,
        database=config.db_name,
    )
    return SenderSettings(
        ClaimPolicy(
            timedelta(seconds=config.delivery_lease_seconds),
            config.delivery_max_attempts,
        ),
        config.delivery_attempt_timeout_seconds,
        config.delivery_retry_seconds,
        url,
    )


def _tenant(connection, organization_id: UUID) -> None:
    connection.execute(
        text("SELECT set_config('app.organization_id', :tenant, true)"),
        {"tenant": str(organization_id)},
    )


def _material_payload(connection, claim: ClaimedJob) -> tuple[bytes, float] | None:
    material = get_claimed_delivery_material(connection, claim)
    if material is None:
        return None
    committed = (
        connection.execute(
            text("""
            SELECT j.id, j.proof_reference, j.purpose, j.organization_id,
                   j.recipient, j.proof_expires_at, j.template,
                   COALESCE(i.proof_digest,
                     CASE WHEN j.verification_id IS NOT NULL AND
                               j.proof_reference ~ '^[a-f0-9]{64}$'
                          THEN j.proof_reference END) AS verifier_digest,
                   EXTRACT(EPOCH FROM (j.lease_expires_at - clock_timestamp())) AS remaining
            FROM public.delivery_jobs j
            LEFT JOIN public.invitations i ON i.id = j.invitation_id
            WHERE j.id = :job AND j.claim_owner = :owner
              AND j.claim_generation = :generation
        """),
            {"job": claim.id, "owner": claim.owner, "generation": claim.generation},
        )
        .mappings()
        .one_or_none()
    )
    if committed is None or committed["verifier_digest"] is None:
        raise ConfigError("Missing committed delivery verifier")
    payload = {
        "context": {
            "job_id": str(committed["id"]),
            "proof_reference": committed["proof_reference"],
            "purpose": committed["purpose"],
            "tenant_id": str(committed["organization_id"]),
            "committed_recipient": committed["recipient"],
            "expiry_epoch_ms": str(
                int(committed["proof_expires_at"].timestamp() * 1000)
            ),
        },
        "material": {
            "version": material["format_version"],
            "key_id": material["delivery_key_id"],
            "ciphertext_base64url": material["ciphertext_base64url"],
        },
        "verifier_digest": committed["verifier_digest"],
        "template": committed["template"],
    }
    encoded = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode(
        "utf-8"
    )
    if len(encoded) > 4096:
        raise ConfigError("Oversized committed delivery request")
    return encoded, float(committed["remaining"])


def _node_call(input_bytes: bytes | None, timeout: float) -> bool:
    command = [_NODE, _ADAPTER]
    if input_bytes is None:
        command.append("--check")
    try:
        completed = subprocess.run(
            command,
            input=input_bytes,
            capture_output=True,
            timeout=timeout,
            check=False,
        )
        return completed.returncode == 0 and completed.stdout == b"SENT\n"
    except (OSError, subprocess.TimeoutExpired):
        return False


def send_one_job(
    organization_id: UUID, job_id: UUID | None, settings: SenderSettings
) -> str:
    """Commit claim before Node; finish only if D-09 still owns the generation."""
    if not _node_call(None, settings.timeout_seconds):
        raise ConfigError("Sender key or transport configuration unavailable")
    engine = create_engine(settings.connection_url, hide_parameters=True)
    try:
        with engine.begin() as connection:
            _tenant(connection, organization_id)
            claim = claim_delivery_job(connection, settings.policy, job_id)
        if claim is None:
            return "NOT_DUE"
        with engine.begin() as connection:
            _tenant(connection, organization_id)
            material = _material_payload(connection, claim)
        if material is None:
            return "STALE"
        payload, seconds_remaining = material
        deadline = min(float(settings.timeout_seconds), seconds_remaining - 0.5)
        sent = deadline > 0 and _node_call(payload, deadline)
        with engine.begin() as connection:
            _tenant(connection, organization_id)
            attempts = connection.scalar(
                text("SELECT attempts FROM public.delivery_jobs WHERE id = :job"),
                {"job": claim.id},
            )
            if sent:
                outcome = JobOutcome("SENT", detail="SMTP_ACCEPTED")
            elif attempts is not None and attempts >= settings.policy.max_attempts:
                outcome = JobOutcome("FAILED", detail="ATTEMPTS_EXHAUSTED")
            else:
                retry_at = connection.scalar(
                    text("SELECT clock_timestamp() + (:seconds * interval '1 second')"),
                    {"seconds": settings.retry_seconds},
                )
                outcome = JobOutcome(
                    "READY", not_before=retry_at, detail="RETRY_SCHEDULED"
                )
            return (
                outcome.state
                if finish_delivery_job(connection, claim, outcome)
                else "STALE"
            )
    finally:
        engine.dispose()


def main() -> int:
    parser = argparse.ArgumentParser(description="One-shot nonclinical email sender")
    parser.add_argument("--organization-id", required=True, type=UUID)
    parser.add_argument(
        "--job-id", type=UUID, help="Optional exact committed delivery job"
    )
    args = parser.parse_args()
    try:
        print(send_one_job(args.organization_id, args.job_id, _settings()))
        return 0
    except (ConfigError, SQLAlchemyError, OSError, ValueError, TypeError, KeyError):
        print("Delivery unavailable")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

"""Explicit one-shot D-09 claim/reclaim/send invocation; no periodic runner."""

import argparse
import json
import subprocess
from dataclasses import dataclass
from datetime import timedelta
from uuid import UUID

from sqlalchemy import URL, create_engine, text
from sqlalchemy.exc import SQLAlchemyError

from carepulse.compat.admission import (
    CompatAdmissionError,
    CompatRuntimeConfig,
    acquire_invocation_permit,
    is_compat_enabled,
    record_attempt_disposition,
    release_invocation_permit,
)
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


def _tenant(connection, organization_id: UUID, attempt_id: str | None = None) -> None:
    connection.execute(
        text("SELECT set_config('app.organization_id', :tenant, true)"),
        {"tenant": str(organization_id)},
    )
    if attempt_id is not None:
        connection.execute(
            text("SELECT set_config('app.compat_attempt_id', :attempt, true)"),
            {"attempt": attempt_id},
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


def _node_call(input_bytes: bytes | None, timeout: float) -> str:
    """Invoke Node transport and distinguish SENT, NOT_SENT, and AMBIGUOUS (T035)."""
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
        if completed.returncode == 0 and completed.stdout == b"SENT\n":
            return "SENT"
        if b"NOT_SENT" in completed.stderr or (
            input_bytes is None and completed.returncode != 0
        ):
            return "NOT_SENT"
        return "AMBIGUOUS"
    except subprocess.TimeoutExpired:
        return "AMBIGUOUS"
    except OSError:
        return "NOT_SENT"


def send_one_job(
    organization_id: UUID, job_id: UUID | None, settings: SenderSettings
) -> str:
    """Commit claim before Node; finish only if D-09 still owns the generation."""
    if _node_call(None, settings.timeout_seconds) != "SENT":
        raise ConfigError("Sender key or transport configuration unavailable")
    engine = create_engine(settings.connection_url, hide_parameters=True)
    permit = None
    permit_conn = None
    final_disposition = "UNKNOWN"
    claimed_job_id: str | None = str(job_id) if job_id else None
    claimed_lease_gen: int | None = None
    try:
        if is_compat_enabled():
            compat_cfg = CompatRuntimeConfig.from_env("sender")
            assert compat_cfg is not None
            permit, permit_conn = acquire_invocation_permit(
                engine,
                config=compat_cfg,
                expected_login="carepulse_sender",
                invocation_class="sender_claim",
                correlation_id=str(job_id) if job_id else f"org:{organization_id}",
                record_attempt=True,
            )
        with engine.begin() as connection:
            _tenant(connection, organization_id, permit.attempt_id if permit else None)
            claim = claim_delivery_job(connection, settings.policy, job_id)
            if claim is not None and permit is not None:
                connection.execute(
                    text("""
                        SELECT compat_control.record_own_attempt(
                            :aid, :tid, :epoch, :generation, 'sender',
                            'sender_claim', :job, :lease, 'PENDING'
                        )
                    """),
                    {
                        "aid": permit.attempt_id,
                        "tid": permit.target_id,
                        "epoch": permit.owner_epoch,
                        "generation": permit.generation,
                        "job": str(claim.id),
                        "lease": claim.generation,
                    },
                )
        if claim is None:
            final_disposition = "NOT_SENT"
            return "NOT_DUE"
        claimed_job_id = str(claim.id)
        claimed_lease_gen = claim.generation
        with engine.begin() as connection:
            _tenant(connection, organization_id)
            material = _material_payload(connection, claim)
        if material is None:
            final_disposition = "NOT_SENT"
            return "STALE"
        payload, seconds_remaining = material
        deadline = min(float(settings.timeout_seconds), seconds_remaining - 0.5)
        transport_status = (
            _node_call(payload, deadline) if deadline > 0 else "AMBIGUOUS"
        )
        if transport_status == "AMBIGUOUS":
            # Never auto-retry, discard material, or count ambiguous ack as settled (T035).
            final_disposition = "UNKNOWN"
            return "AMBIGUOUS"
        sent = transport_status == "SENT"
        outcome = None
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
            updated = finish_delivery_job(connection, claim, outcome)
        if not updated:
            if not sent:
                final_disposition = "NOT_SENT"  # Transport was definitely absent.
            return "STALE"
        # Independent readback using only the sender's column-scoped SELECT grants.
        # An acknowledged finish does not prove the transaction committed.
        with engine.begin() as observer:
            _tenant(observer, organization_id)
            row = observer.execute(
                text("""
                    SELECT state, claim_generation, claim_owner, not_before
                    FROM public.delivery_jobs
                    WHERE id = :job AND organization_id = :tenant
                """),
                {"job": claim.id, "tenant": organization_id},
            ).one_or_none()
        if (
            row
            and row.claim_generation == claim.generation
            and row.claim_owner is None
            and row.state == outcome.state
            and (outcome.state != "READY" or row.not_before == outcome.not_before)
        ):
            final_disposition = "SENT" if sent else "NOT_SENT"
        return outcome.state
    except Exception:
        if claimed_lease_gen is not None:
            final_disposition = "UNKNOWN"
        raise
    finally:
        if permit is not None and permit_conn is not None:
            try:
                record_attempt_disposition(
                    permit_conn,
                    permit,
                    invocation_class="sender_claim",
                    disposition=final_disposition,  # type: ignore[arg-type]
                    correlation_id=claimed_job_id,
                    lease_generation=claimed_lease_gen,
                )
            except CompatAdmissionError:
                pass
            release_invocation_permit(permit_conn, permit)
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
    except (
        CompatAdmissionError,
        ConfigError,
        SQLAlchemyError,
        OSError,
        ValueError,
        TypeError,
        KeyError,
    ):
        print("Delivery unavailable")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

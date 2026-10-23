"""D-09 transactional evidence and fenced one-shot delivery storage operations."""

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta
from json import dumps
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import Connection, Engine, text


def _insert(
    connection: Connection,
    relation: str,
    columns: tuple[str, ...],
    fields: Mapping[str, Any],
) -> UUID:
    """Insert an explicitly allowlisted evidence record in the caller's transaction."""
    record_id = fields.get("id") or uuid4()
    names = ("id", *columns)
    json_fields = {"before_state", "after_state", "payload"}
    placeholders = ", ".join(
        f"CAST(:{name} AS jsonb)" if name in json_fields else f":{name}"
        for name in names
    )
    row = {
        "id": record_id,
        **{
            name: dumps(fields[name], allow_nan=False)
            if name in json_fields and fields.get(name) is not None
            else fields.get(name)
            for name in columns
        },
    }
    connection.execute(
        text(
            f"INSERT INTO public.{relation} ({', '.join(names)}) VALUES ({placeholders})"
        ),
        row,
    )
    return record_id


def append_sensitive_audit(connection: Connection, fields: Mapping[str, Any]) -> UUID:
    return _insert(
        connection,
        "sensitive_audit",
        (
            "organization_id",
            "actor_staff_id",
            "operator_id",
            "actor_role",
            "action",
            "reason",
            "before_state",
            "after_state",
            "correlation_id",
            "recovery_operation_id",
        ),
        fields,
    )


def append_clinical_audit(connection: Connection, fields: Mapping[str, Any]) -> UUID:
    return _insert(
        connection,
        "clinical_audit",
        (
            "organization_id",
            "doctor_staff_id",
            "clinical_revision",
            "before_ref",
            "after_ref",
            "correlation_id",
        ),
        fields,
    )


def append_outbox(connection: Connection, fields: Mapping[str, Any]) -> UUID:
    return _insert(
        connection,
        "outbox_events",
        ("organization_id", "event_type", "aggregate_type", "aggregate_id", "payload"),
        fields,
    )


def accept_verified_nonce(
    engine: Engine, key_id: str, nonce: str, signed_at: datetime
) -> None:
    """Call only AFTER B-01 verifies the signature/freshness; commit before domain work."""
    with engine.begin() as connection:
        connection.execute(
            text("""
                INSERT INTO public.request_nonces (key_id, nonce, signed_at)
                VALUES (:key_id, :nonce, :signed_at)
            """),
            {"key_id": key_id, "nonce": nonce, "signed_at": signed_at},
        )


def purge_expired_nonces(connection: Connection, acceptance_window: timedelta) -> int:
    """Only timestamps strictly older than the entire accepted freshness/skew window."""
    if acceptance_window <= timedelta(0):
        raise ValueError("A positive configured accepted window is required")
    return connection.scalar(
        text("SELECT public.purge_request_nonces(:window)"),
        {"window": acceptance_window},
    )


@dataclass(frozen=True)
class ClaimPolicy:
    lease: timedelta
    max_attempts: int

    def __post_init__(self) -> None:
        if self.lease <= timedelta(0) or self.max_attempts < 1:
            raise ValueError(
                "Configured positive delivery lease and attempt bound required"
            )


@dataclass(frozen=True)
class ClaimedJob:
    id: UUID
    owner: UUID
    generation: int
    lease_expires_at: datetime


@dataclass(frozen=True)
class JobOutcome:
    state: str
    not_before: datetime | None = None
    detail: str | None = None

    def __post_init__(self) -> None:
        if self.state not in ("SENT", "READY", "FAILED"):
            raise ValueError("Invalid delivery finish state")
        if (self.state == "READY") != (self.not_before is not None):
            raise ValueError("Retry requires an explicit configured next attempt time")


def create_delivery_job(
    connection: Connection, fields: Mapping[str, Any], material: Mapping[str, Any]
) -> UUID:
    """The issuer commits proof, immutable job context and token ciphertext together."""
    job_id = _insert(
        connection,
        "delivery_jobs",
        (
            "organization_id",
            "invitation_id",
            "verification_id",
            "staff_id",
            "recovery_operation_id",
            "proof_reference",
            "purpose",
            "recipient",
            "template",
            "logical_key",
            "proof_expires_at",
        ),
        fields,
    )
    connection.execute(
        text("""
            INSERT INTO public.protected_delivery_material
                (job_id, format_version, delivery_key_id, ciphertext_base64url)
            VALUES (:job, 1, :key, :ciphertext)
        """),
        {
            "job": job_id,
            "key": material["delivery_key_id"],
            "ciphertext": material["ciphertext_base64url"],
        },
    )
    return job_id


def _database_time(connection: Connection) -> datetime:
    return connection.scalar(text("SELECT clock_timestamp()"))


def _proof_usable(
    connection: Connection, job: Mapping[str, Any], now: datetime
) -> bool:
    if job["proof_expires_at"] <= now:
        return False
    if job["invitation_id"] is not None:
        return bool(
            connection.scalar(
                text("""
            SELECT 1 FROM public.invitations i
            JOIN public.organizations o ON o.id = i.organization_id
            WHERE i.id = :proof AND i.organization_id = :tenant
              AND i.state = 'ISSUED' AND i.expires_at > :now
              AND i.target_email = :recipient
              AND ((i.purpose = 'FIRST_OWNER' AND o.state = 'PROVISIONING') OR
                   (i.purpose = 'RECEPTIONIST' AND o.state = 'ACTIVE'))
        """),
                {
                    "proof": job["invitation_id"],
                    "tenant": job["organization_id"],
                    "now": now,
                    "recipient": job["recipient"],
                },
            )
        )
    return bool(
        connection.scalar(
            text("""
        SELECT 1 FROM public."verification" v
        JOIN public.staff_accounts s ON s.id = :staff AND s.organization_id = :tenant
        JOIN public.organizations o ON o.id = s.organization_id
        WHERE v.id = :proof AND v."expiresAt" > :now
          AND s.enabled AND s.verified AND s.verified_email = :recipient
          AND o.state = 'ACTIVE'
    """),
            {
                "proof": job["verification_id"],
                "staff": job["staff_id"],
                "tenant": job["organization_id"],
                "now": now,
                "recipient": job["recipient"],
            },
        )
    )


def _end_unusable_job(connection: Connection, job_id: UUID, state: str) -> None:
    connection.execute(
        text("""
            UPDATE public.delivery_jobs SET state = :state, claim_owner = NULL,
                claimed_at = NULL, lease_expires_at = NULL, updated_at = clock_timestamp()
            WHERE id = :job
        """),
        {"job": job_id, "state": state},
    )
    connection.execute(
        text("DELETE FROM public.protected_delivery_material WHERE job_id = :job"),
        {"job": job_id},
    )


def claim_delivery_job(
    connection: Connection, policy: ClaimPolicy, job_id: UUID | None = None
) -> ClaimedJob | None:
    """Lock one due job; DB time and the persisted generation decide ownership."""
    job = (
        connection.execute(
            text("""
            SELECT id, organization_id, staff_id, invitation_id, verification_id,
                   recipient, proof_expires_at, state, attempts, claim_generation,
                   lease_expires_at, not_before
            FROM public.delivery_jobs
            WHERE (:job IS NULL OR id = :job)
              AND ((state = 'READY' AND not_before <= clock_timestamp())
                   OR (state = 'CLAIMED' AND lease_expires_at <= clock_timestamp()))
            ORDER BY not_before, id
            FOR UPDATE SKIP LOCKED LIMIT 1
        """),
            {"job": job_id},
        )
        .mappings()
        .one_or_none()
    )
    if job is None:
        return None
    now = _database_time(connection)
    if not _proof_usable(connection, job, now):
        _end_unusable_job(connection, job["id"], "CANCELLED")
        return None
    if job["attempts"] >= policy.max_attempts:
        _end_unusable_job(connection, job["id"], "FAILED")
        return None
    owner = uuid4()
    generation = job["claim_generation"] + 1
    deadline = now + policy.lease
    connection.execute(
        text("""
            UPDATE public.delivery_jobs SET state = 'CLAIMED', attempts = attempts + 1,
                claim_owner = :owner, claim_generation = :generation,
                claimed_at = :now, lease_expires_at = :deadline,
                updated_at = clock_timestamp()
            WHERE id = :job
        """),
        {
            "job": job["id"],
            "owner": owner,
            "generation": generation,
            "now": now,
            "deadline": deadline,
        },
    )
    return ClaimedJob(job["id"], owner, generation, deadline)


def _set_claim_context(connection: Connection, claim: ClaimedJob) -> None:
    connection.execute(
        text("""
            SELECT set_config('app.delivery_claim_owner', :owner, true),
                   set_config('app.delivery_claim_generation', :generation, true)
        """),
        {"owner": str(claim.owner), "generation": str(claim.generation)},
    )


def _locked_valid_claim(connection: Connection, claim: ClaimedJob):
    job = (
        connection.execute(
            text("""
            SELECT id, organization_id, staff_id, invitation_id, verification_id,
                   recipient, proof_expires_at, state, attempts, claim_generation,
                   lease_expires_at
            FROM public.delivery_jobs
            WHERE id = :id AND claim_generation = :generation
            FOR UPDATE SKIP LOCKED
        """),
            {"id": claim.id, "owner": claim.owner, "generation": claim.generation},
        )
        .mappings()
        .one_or_none()
    )
    if job is None:
        return None
    now = _database_time(connection)
    if job["state"] != "CLAIMED" or job["lease_expires_at"] <= now:
        return None
    if not connection.scalar(
        text("SELECT public.is_delivery_claim_owner(:id, :owner, :generation)"),
        {"id": claim.id, "owner": claim.owner, "generation": claim.generation},
    ):
        return None
    return job


def get_claimed_delivery_material(connection: Connection, claim: ClaimedJob):
    """Sender must check persisted ownership AND proof immediately before SMTP."""
    job = _locked_valid_claim(connection, claim)
    if job is None or not _proof_usable(connection, job, _database_time(connection)):
        return None
    _set_claim_context(connection, claim)
    return (
        connection.execute(
            text(
                "SELECT * FROM public.claimed_delivery_material(:id, :owner, :generation)"
            ),
            {"id": claim.id, "owner": claim.owner, "generation": claim.generation},
        )
        .mappings()
        .one_or_none()
    )


def finish_delivery_job(
    connection: Connection, claim: ClaimedJob, outcome: JobOutcome
) -> bool:
    """A stale or expired sender changes zero rows and cannot dispose material."""
    if _locked_valid_claim(connection, claim) is None:
        return False
    _set_claim_context(connection, claim)
    updated = connection.execute(
        text("""
            UPDATE public.delivery_jobs SET state = :state, claim_owner = NULL,
                claimed_at = NULL, lease_expires_at = NULL,
                not_before = COALESCE(:not_before, not_before),
                last_outcome = :detail, updated_at = clock_timestamp()
            WHERE id = :job AND state = 'CLAIMED'
              AND claim_generation = :generation
              AND lease_expires_at > clock_timestamp()
        """),
        {
            "job": claim.id,
            "owner": claim.owner,
            "generation": claim.generation,
            "state": outcome.state,
            "not_before": outcome.not_before,
            "detail": outcome.detail,
        },
    )
    if updated.rowcount != 1:
        return False
    if outcome.state != "READY":
        connection.execute(
            text("DELETE FROM public.protected_delivery_material WHERE job_id = :job"),
            {"job": claim.id},
        )
    return True

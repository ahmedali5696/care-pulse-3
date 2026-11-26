"""Atomic commit of a server-bound, live-revalidated bulk reschedule preview."""

import json

from sqlalchemy import Connection

from carepulse.appointments.bulk_preview import (
    BulkCommitCommand,
    evaluate_cohort,
)
from carepulse.identity.authorization import AuthorizedStaff
from carepulse.identity.contracts import ContractError
from carepulse.queue.admission_control import _call, _signed_identity

_PURPOSE = "APPOINTMENT_BULK_COMMIT"
_SNAPSHOT_FIELDS = frozenset(
    {
        "original_service_date",
        "destination_date",
        "appointment_ids",
        "reason",
        "eligible_count",
        "excluded_count",
        "included",
        "excluded",
        "security_revision",
        "schedule_revision",
        "capacity_revision",
        "row_evidence",
    }
)


def _preview_snapshot(preview: dict) -> dict:
    if not isinstance(preview, dict) or not _SNAPSHOT_FIELDS <= preview.keys():
        raise ContractError(409)
    return {field: preview[field] for field in _SNAPSHOT_FIELDS}


def _committed_projection(envelope: dict) -> tuple[dict, int]:
    if not isinstance(envelope, dict):
        raise ContractError(503)
    projection = envelope.get("projection", envelope)
    revision = envelope.get("revision")
    if (
        not isinstance(projection, dict)
        or type(revision) is not int
        or revision < 0
        or not isinstance(projection.get("moved"), list)
        or not projection["moved"]
    ):
        raise ContractError(503)
    return projection, revision


def commit_bulk_reschedule(
    connection: Connection,
    actor: AuthorizedStaff,
    command: BulkCommitCommand,
    key: str,
) -> tuple[dict, int]:
    _signed_identity(_PURPOSE, key)
    existing = _call(connection, "SELECT public.f01_b19_commit_replay()")
    if existing is not None:
        return _committed_projection(existing)
    preview = _call(connection, "SELECT public.f01_b19_read_preview()")
    bound = _preview_snapshot(preview)
    if bound["eligible_count"] < 1:
        raise ContractError(409)
    _call(connection, "SELECT public.f01_b19_prepare()")
    refreshed = evaluate_cohort(connection, actor, bound)
    if refreshed != bound:
        raise ContractError(409, "STALE_REVISION")
    applied = _call(
        connection,
        "SELECT public.f01_b19_apply(CAST(:revalidated AS jsonb))",
        {"revalidated": json.dumps(refreshed, allow_nan=False)},
    )
    return _committed_projection(applied)


def recover_bulk_reschedule(
    connection: Connection,
    actor: AuthorizedStaff,
    command: BulkCommitCommand,
    key: str,
) -> tuple[dict, int]:
    _signed_identity(_PURPOSE, key)
    envelope = _call(connection, "SELECT public.f01_b19_commit_replay()")
    if envelope is None:
        raise ContractError(409, "IDEMPOTENCY_CONFLICT")
    return _committed_projection(envelope)

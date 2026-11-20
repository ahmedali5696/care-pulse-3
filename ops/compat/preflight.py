"""Read-only source and intended-target preflight for PRE-W3-COMPAT (T028)."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import psycopg
from psycopg import sql

from ops.compat.control import read_control_record
from ops.compat.manifest import (
    SOURCE_HEAD,
    GenerationManifest,
    verify_manifest_pair,
)
from ops.compat.probes import (
    probe_alembic_head,
    probe_role_attributes,
    probe_target_identity,
)
from ops.compat.verify import RetainedBaseline

PUBLISHED_SUCCESSORS: tuple[str, ...] = (
    "0041_f01_runtime_identity",
    "0042_f01_effective_grants",
    "0043_f01_writer_boundary",
    "0044_f01_delivery_acl",
    "0045_f01_revocation_evidence",
    "0046_f01_audit_integrity",
)

# Executed, separately reviewed US1 checkpoint (quickstart T027). A caller's
# arbitrary boolean or a successful local test is not cutover authorization.
US1_EXECUTED_CHECKPOINT = (
    44394714913,
    "70e4226d1c88d04fc0de8f5d50ee425c6e2e5685",
    15,
    5,
    0,
)
_SOURCE_INVENTORY_TABLES = (
    "organizations",
    "patients",
    "appointments",
    "appointment_price_snapshots",
    "appointment_number_assignments",
    "appointment_history",
    "visits",
    "idempotency_receipts",
    "sensitive_audit",
    "outbox_events",
    "delivery_jobs",
    "request_nonces",
)


class PreflightError(RuntimeError):
    """Raised when read-only preflight fails closed (leaves revision and data untouched)."""


@dataclass(frozen=True)
class PreflightReport:
    """Verified read-only preflight snapshot."""

    target_id: str
    cluster_id: str
    db_name: str
    head: str
    phase: str
    owner_epoch: int
    published_successors: tuple[str, ...]
    missing_candidate_roles: tuple[str, ...]
    checked_source_rows: int
    source_baseline: RetainedBaseline
    source_manifest_id: str
    target_manifest_id: str


def verify_populated_source_basis(
    conn: psycopg.Connection,
    baseline: RetainedBaseline,
    target_id: str,
    cluster_id: str,
    db_name: str,
) -> int:
    """Compare the original populated 0040 capture before any cutover writes."""
    if (
        baseline.target_id != target_id
        or baseline.cluster_id != cluster_id
        or baseline.db_name != db_name
        or len(set(baseline.tenant_ids)) != 2
        or not all(baseline.tenant_ids)
        or not set(_SOURCE_INVENTORY_TABLES).issubset(baseline.expected_rows)
    ):
        raise PreflightError("Missing or mismatched populated 0040 recovery basis")
    checked = 0
    for table in _SOURCE_INVENTORY_TABLES:
        originals = baseline.expected_rows[table]
        if not originals:
            raise PreflightError("Missing populated 0040 source evidence")
        if table == "organizations":
            condition, params = "id = ANY(%s::uuid[])", (list(baseline.tenant_ids),)
        elif table == "request_nonces":
            condition, params = "key_id = %s AND nonce = %s", baseline.nonce_reference
        else:
            condition, params = (
                "organization_id = ANY(%s::uuid[])",
                (list(baseline.tenant_ids),),
            )
        rows = [
            row["row"] if isinstance(row, dict) else row[0]
            for row in conn.execute(
                sql.SQL("SELECT to_jsonb(t) AS row FROM public.{} t WHERE ").format(
                    sql.Identifier(table)
                )
                + sql.SQL(condition),
                params,
            )
        ]
        for original in originals:
            match = next(
                (
                    i
                    for i, actual in enumerate(rows)
                    if all(actual.get(k) == v for k, v in original.items())
                ),
                None,
            )
            if match is None:
                raise PreflightError("Inconsistent populated 0040 recovery inventory")
            rows.pop(match)
            checked += 1
    return checked


def verify_published_successor_files(
    versions_dir: Path | None = None,
) -> tuple[str, ...]:
    """Verify the exact six published migration files exist on disk."""
    base = versions_dir or (
        Path(__file__).resolve().parents[2] / "db" / "migrations" / "versions"
    )
    for rev in PUBLISHED_SUCCESSORS:
        if not (base / f"{rev}.py").is_file():
            raise PreflightError(f"Missing published successor migration: {rev}.py")
    return PUBLISHED_SUCCESSORS


def run_source_preflight(
    conn: psycopg.Connection,
    *,
    source_manifest: GenerationManifest,
    target_manifest: GenerationManifest,
    source_baseline: RetainedBaseline,
    versions_dir: Path | None = None,
    us1_checkpoint: tuple[int, str, int, int, int] | None = None,
) -> PreflightReport:
    """Perform read-only source and intended-target preflight without any mutations."""
    if us1_checkpoint != US1_EXECUTED_CHECKPOINT:
        raise PreflightError("US1 executed checkpoint is required before US2 preflight")

    verify_manifest_pair(source_manifest, target_manifest)
    successors = verify_published_successor_files(versions_dir)

    ident = probe_target_identity(
        conn,
        expected_cluster_id=source_manifest.target_binding.cluster_id,
        expected_db_name=source_manifest.target_binding.db_name,
    )
    head = probe_alembic_head(conn, expected_head=SOURCE_HEAD)

    ctrl = read_control_record(conn)
    if ctrl.target_id != source_manifest.target_binding.target_id:
        raise PreflightError("Control target_id mismatch")
    if (
        ctrl.source_manifest_id != source_manifest.manifest_id
        or ctrl.target_manifest_id != target_manifest.manifest_id
    ):
        raise PreflightError("Control manifest binding mismatch")

    required_0040 = (
        "carepulse_migration",
        "carepulse_runtime",
        "carepulse_auth",
        "carepulse_sender",
        "carepulse_operator",
        "carepulse_policy_seed",
    )
    snaps = probe_role_attributes(conn, required_0040)
    for role_name in required_0040:
        snap = snaps.get(role_name)
        if snap is None or snap.rolsuper or snap.rolbypassrls:
            raise PreflightError(f"Missing or overprivileged 0040 role: {role_name}")
        if role_name != "carepulse_policy_seed" and not snap.rolcanlogin:
            raise PreflightError(f"0040 login role {role_name} cannot log in")

    candidate_roles = (
        "carepulse_api",
        "carepulse_web",
        "carepulse_worker",
        "carepulse_gate",
        "carepulse_identity_owner",
    )
    cand_snaps = probe_role_attributes(conn, candidate_roles)
    missing_cand = tuple(r for r in candidate_roles if r not in cand_snaps)

    checked = verify_populated_source_basis(
        conn, source_baseline, ctrl.target_id, ident.cluster_id, ident.db_name
    )

    return PreflightReport(
        target_id=ctrl.target_id,
        cluster_id=ident.cluster_id,
        db_name=ident.db_name,
        head=head,
        phase=ctrl.phase,
        owner_epoch=ctrl.owner_epoch,
        published_successors=successors,
        missing_candidate_roles=missing_cand,
        checked_source_rows=checked,
        source_baseline=source_baseline,
        source_manifest_id=source_manifest.manifest_id,
        target_manifest_id=target_manifest.manifest_id,
    )

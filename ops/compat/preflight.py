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

_SOURCE_CLINICAL_TABLES = (
    "patients",
    "appointments",
    "appointment_price_snapshots",
    "appointment_history",
    "visits",
    "clinical_revisions",
    "clinical_audit",
    "followup_claims",
    "followup_source_state",
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


def _verify_source_effective_grants(conn: psycopg.Connection) -> None:
    """Check the existing 0040 login ACL, including grants inherited via PUBLIC."""
    required = {
        "carepulse_runtime": (
            ("public.patients", "SELECT"),
            ("public.appointments", "INSERT"),
            ("public.delivery_jobs", "UPDATE"),
        ),
        "carepulse_auth": (
            ('public."user"', "SELECT"),
            ('public."session"', "INSERT"),
        ),
        "carepulse_sender": (("public.delivery_jobs", "UPDATE"),),
    }
    forbidden = {
        "carepulse_runtime": (
            ("public.patients", "DELETE"),
            ("public.appointments", "DELETE"),
            ("public.visits", "DELETE"),
            ('public."account"', "SELECT"),
            ('public."session"', "SELECT"),
        ),
        "carepulse_auth": (),
        "carepulse_sender": (("public.delivery_jobs", "INSERT"),),
        "carepulse_operator": (),
    }
    for role, forbidden_grants in forbidden.items():
        row = conn.execute(
            """
            SELECT has_database_privilege(%s, current_database(), 'CONNECT')
                   AND NOT has_database_privilege(%s, current_database(), 'CREATE')
                   AND NOT has_database_privilege(%s, current_database(), 'TEMPORARY')
                   AND NOT has_schema_privilege(%s, 'public', 'CREATE')
                   AND has_schema_privilege(%s, 'compat_control', 'USAGE')
                   AND NOT has_schema_privilege(%s, 'compat_control', 'CREATE')
                   AND has_table_privilege(%s, 'compat_control.target_control', 'SELECT')
                   AND has_function_privilege(%s, 'compat_control.read_target_control()', 'EXECUTE')
                   AND has_function_privilege(%s, 'compat_control.read_target_status()', 'EXECUTE')
                   AND NOT EXISTS (
                       SELECT 1 FROM pg_class c
                       JOIN pg_namespace n ON n.oid = c.relnamespace
                       WHERE n.nspname = 'compat_control'
                         AND c.relkind IN ('r','p','v','m')
                         AND (
                             has_table_privilege(%s, c.oid, 'INSERT')
                             OR has_table_privilege(%s, c.oid, 'UPDATE')
                             OR has_table_privilege(%s, c.oid, 'DELETE')
                             OR has_table_privilege(%s, c.oid, 'TRUNCATE')
                             OR has_any_column_privilege(%s, c.oid, 'INSERT')
                             OR has_any_column_privilege(%s, c.oid, 'UPDATE')
                             OR has_any_column_privilege(%s, c.oid, 'REFERENCES')
                             OR (c.relname <> 'target_control'
                                 AND has_any_column_privilege(%s, c.oid, 'SELECT'))
                         )
                   )
                   AND NOT EXISTS (
                       SELECT 1 FROM pg_namespace n
                       WHERE n.nspname NOT IN (
                           'public','compat_control','pg_catalog','information_schema'
                        ) AND n.nspname NOT LIKE 'pg_%%'
                       AND (
                           has_schema_privilege(%s, n.oid, 'USAGE')
                           OR has_schema_privilege(%s, n.oid, 'CREATE')
                       )
                   ) AS safe
            """,
            (role,) * 19,
        ).fetchone()
        if row is None or row["safe"] is not True:
            raise PreflightError("Source login database, schema or control ACL drift")
        has_public_usage = conn.execute(
            "SELECT has_schema_privilege(%s, 'public', 'USAGE') AS allowed",
            (role,),
        ).fetchone()["allowed"]
        if has_public_usage is not (role != "carepulse_operator"):
            raise PreflightError("Source login public schema usage drift")
        if conn.execute(
            """
            SELECT EXISTS (
                SELECT 1 FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
                WHERE n.nspname = 'public' AND c.relkind IN ('r','p')
                  AND has_table_privilege(%s, c.oid, 'TRUNCATE,REFERENCES,TRIGGER')
            ) AS unsafe
            """,
            (role,),
        ).fetchone()["unsafe"]:
            raise PreflightError("Source login destructive public-table grant drift")
        can_record = conn.execute(
            """
            SELECT has_function_privilege(
                %s, 'compat_control.record_own_attempt(text,text,bigint,text,text,text,text,bigint,text)',
                'EXECUTE'
            ) AS permitted
            """,
            (role,),
        ).fetchone()["permitted"]
        if can_record is not (role != "carepulse_operator"):
            raise PreflightError("Source control attempt-function grant drift")
        sender_guard = conn.execute(
            """
            SELECT has_function_privilege(
                %s, 'compat_control.has_unknown_sender_attempt(text,text)', 'EXECUTE'
            ) AS permitted
            """,
            (role,),
        ).fetchone()["permitted"]
        if sender_guard is not (role == "carepulse_sender"):
            raise PreflightError("Source sender function grant drift")
        for obj, privilege in required.get(role, ()):
            if not conn.execute(
                "SELECT has_table_privilege(%s, %s, %s) AS permitted",
                (role, obj, privilege),
            ).fetchone()["permitted"]:
                raise PreflightError("Source login required grant missing")
        for obj, privilege in forbidden_grants:
            if conn.execute(
                "SELECT has_table_privilege(%s, %s, %s) AS permitted",
                (role, obj, privilege),
            ).fetchone()["permitted"]:
                raise PreflightError("Source login forbidden grant present")
        if role == "carepulse_runtime":
            for relation in ('public."account"', 'public."session"'):
                for permission in ("SELECT", "INSERT", "UPDATE"):
                    if conn.execute(
                        "SELECT has_any_column_privilege(%s, %s, %s) AS permitted",
                        (role, relation, permission),
                    ).fetchone()["permitted"]:
                        raise PreflightError("Source runtime native-auth ACL drift")
                if conn.execute(
                    "SELECT has_table_privilege(%s, %s, 'DELETE') AS permitted",
                    (role, relation),
                ).fetchone()["permitted"]:
                    raise PreflightError("Source runtime native-auth ACL drift")
        if role in ("carepulse_auth", "carepulse_sender", "carepulse_operator"):
            extra_clinical = conn.execute(
                """
                SELECT EXISTS (
                    SELECT 1 FROM pg_class c
                    JOIN pg_namespace n ON n.oid = c.relnamespace
                    WHERE n.nspname = 'public' AND c.relname = ANY(%s::text[])
                      AND (
                          has_any_column_privilege(%s, c.oid, 'SELECT')
                          OR has_any_column_privilege(%s, c.oid, 'INSERT')
                          OR has_any_column_privilege(%s, c.oid, 'UPDATE')
                          OR has_any_column_privilege(%s, c.oid, 'REFERENCES')
                          OR has_table_privilege(%s, c.oid, 'INSERT')
                          OR has_table_privilege(%s, c.oid, 'UPDATE')
                          OR has_table_privilege(%s, c.oid, 'DELETE')
                          OR has_table_privilege(%s, c.oid, 'TRUNCATE')
                      )
                ) AS unexpected
                """,
                (list(_SOURCE_CLINICAL_TABLES), *((role,) * 8)),
            ).fetchone()["unexpected"]
            if extra_clinical:
                raise PreflightError("Source login clinical ACL drift")
        if role == "carepulse_auth":
            for relation in ('"user"', '"session"', '"account"', '"verification"'):
                for permission in ("SELECT", "INSERT", "UPDATE", "DELETE"):
                    if not conn.execute(
                        "SELECT has_table_privilege(%s, %s, %s) AS permitted",
                        (role, f"public.{relation}", permission),
                    ).fetchone()["permitted"]:
                        raise PreflightError(
                            "Source native-auth required grant missing"
                        )
        if role == "carepulse_sender":
            for relation in ('"user"', '"session"', '"account"'):
                for permission in ("SELECT", "INSERT", "UPDATE", "DELETE"):
                    function = (
                        "has_table_privilege"
                        if permission == "DELETE"
                        else "has_any_column_privilege"
                    )
                    granted = conn.execute(
                        sql.SQL("SELECT {}(%s, %s, %s) AS permitted").format(
                            sql.Identifier(function)
                        ),
                        (role, f"public.{relation}", permission),
                    ).fetchone()["permitted"]
                    if granted:
                        raise PreflightError("Source sender native-auth ACL drift")
            if conn.execute(
                """
                SELECT has_any_column_privilege(%s, 'public."verification"', 'INSERT')
                       OR has_any_column_privilege(%s, 'public."verification"', 'UPDATE')
                       OR has_table_privilege(%s, 'public."verification"', 'DELETE')
                       AS permitted
                """,
                (role, role, role),
            ).fetchone()["permitted"]:
                raise PreflightError("Source sender native-auth ACL drift")
    for role, col, permission in (
        ("carepulse_sender", "claim_generation", True),
        ("carepulse_sender", "id", True),
        ("carepulse_sender", "job_id", True),
        ("carepulse_sender", "expiresAt", True),
    ):
        relation = (
            "public.protected_delivery_material"
            if col == "job_id"
            else (
                'public."verification"'
                if col == "expiresAt"
                else "public.delivery_jobs"
            )
        )
        actual = conn.execute(
            "SELECT has_column_privilege(%s, %s, %s, 'SELECT') AS permitted",
            (role, relation, col),
        ).fetchone()["permitted"]
        if actual is not permission:
            raise PreflightError("Source sender column privilege drift")
    for relation, allowed in (
        ('public."verification"', ("id", "expiresAt")),
        ("public.protected_delivery_material", ("job_id",)),
    ):
        row = conn.execute(
            """
            SELECT EXISTS (
                SELECT 1 FROM pg_attribute a
                WHERE a.attrelid = %s::regclass AND a.attnum > 0 AND NOT a.attisdropped
                  AND a.attname <> ALL(%s::text[])
                  AND has_column_privilege('carepulse_sender', a.attrelid, a.attnum, 'SELECT')
            ) AS excess
            """,
            (relation, list(allowed)),
        ).fetchone()
        if row is None or row["excess"]:
            raise PreflightError("Source sender unexpected column grant")


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
        if (
            snap is None
            or snap.rolsuper
            or snap.rolbypassrls
            or snap.rolcreaterole
            or snap.rolcreatedb
            or snap.rolinherit
            or snap.rolreplication
        ):
            raise PreflightError(f"Missing or overprivileged 0040 role: {role_name}")
        if role_name != "carepulse_policy_seed" and not snap.rolcanlogin:
            raise PreflightError(f"0040 login role {role_name} cannot log in")
        if role_name == "carepulse_policy_seed" and snap.rolcanlogin:
            raise PreflightError("0040 private executor gained LOGIN")
        if role_name != "carepulse_migration" and (
            snap.member_of or snap.set_memberships
        ):
            raise PreflightError("0040 product role membership escalation")
        if role_name == "carepulse_migration" and (
            not {"carepulse_policy_seed"}.issubset(snap.set_memberships)
            or snap.member_of - {"carepulse_policy_seed", "carepulse_identity_owner"}
        ):
            raise PreflightError("0040 migration-role membership drift")

    _verify_source_effective_grants(conn)

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

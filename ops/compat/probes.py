"""Read-only PostgreSQL target, revision, and effective-privilege probes."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass

import psycopg

KNOWN_REVISIONS: tuple[str, ...] = (
    "0040_t173_numeric_money",
    "0041_f01_runtime_identity",
    "0042_f01_effective_grants",
    "0043_f01_writer_boundary",
    "0044_f01_delivery_acl",
    "0045_f01_revocation_evidence",
    "0046_f01_audit_integrity",
)


class ProbeError(RuntimeError):
    """Raised when a read-only PostgreSQL target, revision, or grant probe fails closed."""


@dataclass(frozen=True)
class TargetIdentityProbe:
    """Observed cluster and database identity."""

    cluster_id: str
    db_name: str
    session_user: str
    current_user: str


@dataclass(frozen=True)
class RoleAttributeSnapshot:
    """Effective role attributes and memberships."""

    rolname: str
    rolcanlogin: bool
    rolsuper: bool
    rolinherit: bool
    rolcreaterole: bool
    rolcreatedb: bool
    rolbypassrls: bool
    member_of: frozenset[str]
    set_memberships: frozenset[str]


def probe_target_identity(
    conn: psycopg.Connection,
    *,
    expected_cluster_id: str | None = None,
    expected_db_name: str | None = None,
) -> TargetIdentityProbe:
    """Probe actual cluster/database identity and session/effective role."""
    try:
        row = conn.execute(
            """
            SELECT (SELECT system_identifier::text FROM pg_control_system()) AS cluster_id,
                   current_database() AS db_name,
                   session_user::text AS session_user,
                   current_user::text AS current_user
            """
        ).fetchone()
    except psycopg.Error as exc:
        raise ProbeError("Database target identity probe failed") from exc
    if not row:
        raise ProbeError("Empty target identity probe result")
    cluster_id = row["cluster_id"] if isinstance(row, dict) else row[0]
    db_name = row["db_name"] if isinstance(row, dict) else row[1]
    session_user = row["session_user"] if isinstance(row, dict) else row[2]
    current_user = row["current_user"] if isinstance(row, dict) else row[3]

    if expected_cluster_id is not None and cluster_id != expected_cluster_id:
        raise ProbeError(
            f"Cluster identity mismatch: expected {expected_cluster_id}, got {cluster_id}"
        )
    if expected_db_name is not None and db_name != expected_db_name:
        raise ProbeError(
            f"Database identity mismatch: expected {expected_db_name}, got {db_name}"
        )
    return TargetIdentityProbe(
        cluster_id=cluster_id,
        db_name=db_name,
        session_user=session_user,
        current_user=current_user,
    )


def probe_alembic_head(
    conn: psycopg.Connection, *, expected_head: str | None = None
) -> str:
    """Require exactly one known public.alembic_version row."""
    try:
        rows = conn.execute("SELECT version_num FROM public.alembic_version").fetchall()
    except psycopg.Error as exc:
        raise ProbeError("Unreadable public.alembic_version") from exc

    if len(rows) != 1:
        raise ProbeError(
            f"Expected singleton public.alembic_version row, found {len(rows)}"
        )
    head = rows[0]["version_num"] if isinstance(rows[0], dict) else rows[0][0]
    if head not in KNOWN_REVISIONS:
        raise ProbeError(f"Unknown or unapproved Alembic revision: {head!r}")
    if expected_head is not None and head != expected_head:
        raise ProbeError(
            f"Alembic head mismatch: expected {expected_head!r}, got {head!r}"
        )
    return head


def probe_role_attributes(
    conn: psycopg.Connection, roles: Iterable[str]
) -> dict[str, RoleAttributeSnapshot]:
    """Inspect pg_roles and pg_auth_members for each named role."""
    role_list = sorted(set(roles))
    if not role_list:
        return {}
    rows = conn.execute(
        """
        SELECT r.rolname, r.rolcanlogin, r.rolsuper, r.rolinherit,
               r.rolcreaterole, r.rolcreatedb, r.rolbypassrls
        FROM pg_roles r
        WHERE r.rolname = ANY(%s)
        """,
        (role_list,),
    ).fetchall()

    snapshots: dict[str, RoleAttributeSnapshot] = {}
    member_rows = conn.execute(
        """
        SELECT m_role.rolname AS member_name,
               g_role.rolname AS group_name,
               am.set_option
        FROM pg_auth_members am
        JOIN pg_roles m_role ON m_role.oid = am.member
        JOIN pg_roles g_role ON g_role.oid = am.roleid
        WHERE m_role.rolname = ANY(%s)
        """,
        (role_list,),
    ).fetchall()

    memberships: dict[str, set[str]] = {r: set() for r in role_list}
    set_opts: dict[str, set[str]] = {r: set() for r in role_list}
    for m_row in member_rows:
        member_name = m_row["member_name"] if isinstance(m_row, dict) else m_row[0]
        group_name = m_row["group_name"] if isinstance(m_row, dict) else m_row[1]
        set_opt = m_row["set_option"] if isinstance(m_row, dict) else m_row[2]
        memberships.setdefault(member_name, set()).add(group_name)
        if set_opt:
            set_opts.setdefault(member_name, set()).add(group_name)

    for row in rows:
        rolname = row["rolname"] if isinstance(row, dict) else row[0]
        snapshots[rolname] = RoleAttributeSnapshot(
            rolname=rolname,
            rolcanlogin=bool(row["rolcanlogin"] if isinstance(row, dict) else row[1]),
            rolsuper=bool(row["rolsuper"] if isinstance(row, dict) else row[2]),
            rolinherit=bool(row["rolinherit"] if isinstance(row, dict) else row[3]),
            rolcreaterole=bool(
                row["rolcreaterole"] if isinstance(row, dict) else row[4]
            ),
            rolcreatedb=bool(row["rolcreatedb"] if isinstance(row, dict) else row[5]),
            rolbypassrls=bool(row["rolbypassrls"] if isinstance(row, dict) else row[6]),
            member_of=frozenset(memberships.get(rolname, set())),
            set_memberships=frozenset(set_opts.get(rolname, set())),
        )
    return snapshots


def probe_effective_table_privileges(
    conn: psycopg.Connection,
    role_name: str,
    table_name: str,
    privileges: Iterable[str],
) -> dict[str, bool]:
    """Check PostgreSQL effective privileges via has_table_privilege."""
    result: dict[str, bool] = {}
    for priv in privileges:
        row = conn.execute(
            "SELECT has_table_privilege(%s, %s, %s) AS allowed",
            (role_name, table_name, priv),
        ).fetchone()
        allowed = row["allowed"] if isinstance(row, dict) else row[0]
        result[priv.upper()] = bool(allowed)
    return result


def verify_service_effective_role(
    conn: psycopg.Connection,
    *,
    expected_login: str,
    expected_target_id: str,
    expected_cluster_id: str,
    expected_db_name: str,
    forbidden_privileges: Mapping[str, Iterable[str]] | None = None,
    required_privileges: Mapping[str, Iterable[str]] | None = None,
) -> TargetIdentityProbe:
    """Verify actual login, effective role, target binding, and privilege boundaries."""
    ident = probe_target_identity(
        conn,
        expected_cluster_id=expected_cluster_id,
        expected_db_name=expected_db_name,
    )
    if ident.session_user != expected_login or ident.current_user != expected_login:
        raise ProbeError(
            f"Effective role mismatch: expected {expected_login}, got session={ident.session_user}, current={ident.current_user}"
        )

    ctrl = conn.execute(
        "SELECT target_id FROM compat_control.target_control WHERE singleton_id = TRUE"
    ).fetchone()
    if not ctrl:
        raise ProbeError("Missing compat_control.target_control record")
    actual_target = ctrl["target_id"] if isinstance(ctrl, dict) else ctrl[0]
    if actual_target != expected_target_id:
        raise ProbeError(
            f"Target ID mismatch: expected {expected_target_id}, got {actual_target}"
        )

    role_snaps = probe_role_attributes(conn, [expected_login])
    snap = role_snaps.get(expected_login)
    if snap is None or not snap.rolcanlogin or snap.rolsuper or snap.rolbypassrls:
        raise ProbeError(f"Role {expected_login} failed non-privileged login check")

    for table_name, privs in (required_privileges or {}).items():
        observed = probe_effective_table_privileges(
            conn, expected_login, table_name, privs
        )
        missing = [p for p, ok in observed.items() if not ok]
        if missing:
            raise ProbeError(
                f"Role {expected_login} missing required {missing} on {table_name}"
            )

    for table_name, privs in (forbidden_privileges or {}).items():
        observed = probe_effective_table_privileges(
            conn, expected_login, table_name, privs
        )
        unexpected = [p for p, ok in observed.items() if ok]
        if unexpected:
            raise ProbeError(
                f"Role {expected_login} has forbidden effective {unexpected} on {table_name}"
            )

    return ident


def probe_rls_and_triggers(
    conn: psycopg.Connection,
    *,
    required_force_rls_tables: Iterable[str],
    required_triggers: Mapping[str, Iterable[str]] | None = None,
) -> None:
    """Verify FORCE RLS and expected triggers on public/f01_private tables."""
    tables = sorted(set(required_force_rls_tables))
    if tables:
        rows = conn.execute(
            """
            SELECT n.nspname || '.' || c.relname AS fqname,
                   c.relrowsecurity,
                   c.relforcerowsecurity
            FROM pg_class c
            JOIN pg_namespace n ON n.oid = c.relnamespace
            WHERE (n.nspname || '.' || c.relname) = ANY(%s)
            """,
            (tables,),
        ).fetchall()
        found = {}
        for r in rows:
            fq = r["fqname"] if isinstance(r, dict) else r[0]
            rls = r["relrowsecurity"] if isinstance(r, dict) else r[1]
            force_rls = r["relforcerowsecurity"] if isinstance(r, dict) else r[2]
            found[fq] = (bool(rls), bool(force_rls))
        for tbl in tables:
            if tbl not in found or found[tbl] != (True, True):
                raise ProbeError(f"FORCE RLS missing or disabled on {tbl}")

    for tbl, trig_names in (required_triggers or {}).items():
        rows = conn.execute(
            """
            SELECT t.tgname
            FROM pg_trigger t
            JOIN pg_class c ON c.oid = t.tgrelid
            JOIN pg_namespace n ON n.oid = c.relnamespace
            WHERE (n.nspname || '.' || c.relname) = %s AND NOT t.tgisinternal
            """,
            (tbl,),
        ).fetchall()
        existing = {r["tgname"] if isinstance(r, dict) else r[0] for r in rows}
        missing = set(trig_names) - existing
        if missing:
            raise ProbeError(f"Missing triggers on {tbl}: {sorted(missing)}")

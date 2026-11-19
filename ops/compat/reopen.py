"""Designated-owner compare-and-set reopening of a verified generation (T048)."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from uuid import uuid4

import psycopg

from ops.compat.control import (
    assert_current_owner,
    open_nonpooled_connection,
    read_control_record,
)
from ops.compat.launch import ObservedRuntimeUnit, verify_generation_launch
from ops.compat.manifest import (
    CANDIDATE_HEAD,
    SOURCE_HEAD,
    GenerationManifest,
)
from ops.compat.probes import (
    probe_alembic_head,
    probe_role_attributes,
)
from ops.compat.verify import (
    RetainedBaseline,
    verify_fenced_security,
    verify_retained_evidence,
)


class ReopenError(RuntimeError):
    """The reviewed generation cannot safely receive traffic."""


@dataclass(frozen=True)
class ReopenObservation:
    target_id: str
    owner_epoch: int
    committed_head: str
    phase: str
    active_generation: str


def reopen_fenced_generation(
    owner_conn: psycopg.Connection,
    observer_conn: psycopg.Connection,
    *,
    source_manifest: GenerationManifest,
    target_manifest: GenerationManifest,
    baseline: RetainedBaseline,
    owner_epoch: int,
    generation: str,
    observed_units: Iterable[ObservedRuntimeUnit],
    login_probes: Mapping[str, Mapping[str, str | int]],
    predecessor_login_probes: Mapping[str, Mapping[str, str | int]] | None = None,
) -> ReopenObservation:
    """Recheck independent evidence and commit one fenced-to-open CAS.

    Runtime units must be inspected by the trusted launcher; this function
    never issues credentials, restarts jobs, or creates business effects.
    """
    if generation not in ("source", "candidate"):
        raise ReopenError("Unknown generation")
    manifest = source_manifest if generation == "source" else target_manifest
    expected_head = SOURCE_HEAD if generation == "source" else CANDIDATE_HEAD
    phase = "SOURCE_OPEN" if generation == "source" else "TARGET_OPEN"
    binding = source_manifest.target_binding

    # Actual credentials must work against the same physical target, and the
    # probes must terminate before the final stale-pool check.
    required_logins = (
        {
            "carepulse_runtime",
            "carepulse_auth",
            "carepulse_sender",
            "carepulse_operator",
        }
        if generation == "source"
        else {
            "carepulse_api",
            "carepulse_web",
            "carepulse_worker",
            "carepulse_gate",
            "carepulse_auth",
            "carepulse_sender",
            "carepulse_operator",
        }
    )
    if set(login_probes) != required_logins:
        raise ReopenError("Incomplete actual service login probes")
    for role, options in login_probes.items():
        if options.get("user") != role or options.get("dbname") != binding.db_name:
            raise ReopenError("Untrusted service login probe binding")
        with open_nonpooled_connection(**options) as probe:
            identity = probe.execute(
                "SELECT current_database() AS db, session_user::text AS login, "
                "current_user::text AS effective, pg_backend_pid() AS pid"
            ).fetchone()
            if (
                identity["db"] != binding.db_name
                or identity["login"] != role
                or identity["effective"] != role
            ):
                raise ReopenError("Service credential or effective role mismatch")
            marker = f"compat-reopen-{uuid4().hex}"
            probe.execute("SELECT set_config('application_name', %s, false)", (marker,))
            probe.commit()
            if not observer_conn.execute(
                "SELECT 1 FROM pg_stat_activity WHERE pid=%s AND datname=%s "
                "AND usename=%s AND application_name=%s",
                (identity["pid"], binding.db_name, role, marker),
            ).fetchone():
                raise ReopenError(
                    "Independent observer could not bind real login backend"
                )
    if generation == "candidate":
        if not predecessor_login_probes or set(predecessor_login_probes) != {
            "carepulse_runtime",
            "carepulse_auth",
            "carepulse_sender",
        }:
            raise ReopenError("Missing negative predecessor credential probes")
        for role, options in predecessor_login_probes.items():
            reference = login_probes["carepulse_auth"]
            if (
                options.get("user") != role
                or options.get("dbname") != binding.db_name
                or options.get("host") != reference.get("host")
                or options.get("port") != reference.get("port")
                or (
                    role in login_probes
                    and options.get("password") == login_probes[role].get("password")
                )
            ):
                raise ReopenError("Untrusted or reused predecessor credential")
            try:
                old = open_nonpooled_connection(**options)
            except psycopg.OperationalError as exc:
                if exc.sqlstate not in ("28P01", "28000"):
                    raise ReopenError(
                        "Predecessor denial was not an auth rejection"
                    ) from exc
            else:
                old.close()
                raise ReopenError(
                    "A retired predecessor credential still authenticates"
                )

    verify_generation_launch(manifest, observed_units)
    security = verify_fenced_security(
        observer_conn,
        source_manifest=source_manifest,
        target_manifest=target_manifest,
        expected_epoch=owner_epoch,
        expected_head=expected_head,
    )
    verify_retained_evidence(observer_conn, baseline=baseline, security=security)

    control = assert_current_owner(
        owner_conn,
        target_id=binding.target_id,
        expected_epoch=owner_epoch,
        lock_mode="UPDATE",
    )
    if (
        control.phase not in ("CLOSED", "DRAINING", "MIGRATING", "VERIFYING")
        or control.active_generation != "none"
        or control.drain_verified_epoch != owner_epoch
        or control.source_manifest_id != source_manifest.manifest_id
        or control.target_manifest_id != target_manifest.manifest_id
        or control.cluster_id != binding.cluster_id
        or control.db_name != binding.db_name
        or control.phase != security.phase
        or control != read_control_record(observer_conn)
        or probe_alembic_head(owner_conn) != expected_head
    ):
        owner_conn.rollback()
        raise ReopenError("Current owner, drain, head or generation binding mismatch")

    role_names = (
        ("carepulse_runtime", "carepulse_auth", "carepulse_sender")
        if generation == "source"
        else (
            "carepulse_runtime",
            "carepulse_api",
            "carepulse_web",
            "carepulse_worker",
            "carepulse_gate",
            "carepulse_auth",
            "carepulse_sender",
        )
    )
    roles = probe_role_attributes(owner_conn, role_names)
    if (
        set(roles) != set(role_names)
        or any(s.rolsuper or s.rolbypassrls or s.rolcreaterole for s in roles.values())
        or (generation == "source" and not all(r.rolcanlogin for r in roles.values()))
        or (
            generation == "candidate"
            and (
                roles["carepulse_runtime"].rolcanlogin
                or any(
                    not roles[r].rolcanlogin
                    for r in role_names
                    if r != "carepulse_runtime"
                )
                or not control.source_sealed
            )
        )
    ):
        owner_conn.rollback()
        raise ReopenError("Old or new generation login authority is incompatible")

    # A held old pool is still usable after NOLOGIN: deny release until every
    # protected physical backend has ended, even an idle one.
    outstanding = owner_conn.execute(
        """
        SELECT count(*) AS n FROM pg_stat_activity
        WHERE datname=current_database()
          AND usename IN ('carepulse_runtime','carepulse_api','carepulse_web',
                          'carepulse_worker','carepulse_gate','carepulse_auth',
                          'carepulse_sender')
          AND pid<>pg_backend_pid()
        """
    ).fetchone()["n"]
    unknown = owner_conn.execute(
        "SELECT count(*) AS n FROM compat_control.work_evidence WHERE target_id=%s AND disposition IN ('PENDING','UNKNOWN')",
        (binding.target_id,),
    ).fetchone()["n"]
    if outstanding or unknown:
        owner_conn.rollback()
        raise ReopenError("Stale service sessions or uncertain prior effects remain")

    # Row lock orders this CAS against all new admission FOR SHARE readers.
    row = owner_conn.execute(
        """
        UPDATE compat_control.target_control
        SET phase=%s, active_generation=%s,
            source_sealed=%s, updated_at=now()
        WHERE singleton_id=TRUE AND target_id=%s AND owner_epoch=%s
          AND phase=%s AND active_generation='none'
          AND source_manifest_id=%s AND target_manifest_id=%s
          AND drain_verified_epoch=%s
          AND EXISTS (SELECT 1 FROM public.alembic_version WHERE version_num=%s)
        RETURNING owner_epoch
        """,
        (
            phase,
            generation,
            generation != "source",
            binding.target_id,
            owner_epoch,
            control.phase,
            source_manifest.manifest_id,
            target_manifest.manifest_id,
            owner_epoch,
            expected_head,
        ),
    ).fetchone()
    if row is None:
        owner_conn.rollback()
        raise ReopenError("Reopen CAS failed; target remains fenced")
    owner_conn.commit()
    observed = read_control_record(observer_conn)
    if (
        observed.target_id != binding.target_id
        or observed.owner_epoch != owner_epoch
        or observed.phase != phase
        or observed.active_generation != generation
        or probe_alembic_head(observer_conn) != expected_head
    ):
        raise ReopenError(
            "Lost reopen acknowledgement; refence and inspect before retry"
        )
    return ReopenObservation(
        binding.target_id, owner_epoch, expected_head, phase, generation
    )

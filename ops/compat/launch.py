"""Prelaunch binding, digest verification, and fail-closed rejection for PRE-W3-COMPAT."""

from __future__ import annotations

import hashlib
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

from ops.compat.control import assert_current_owner
from ops.compat.manifest import (
    GenerationManifest,
    ServiceKind,
    ServiceManifest,
)


class LaunchBindingError(RuntimeError):
    """Raised when a participant fails prelaunch binding, digest, peer, or credential checks."""


_RAW_UNGUARDED_ENTRIES: tuple[tuple[str, ...], ...] = (
    ("python", "-m", "uvicorn", "carepulse.main:app"),
    ("python", "apps/worker/main.py"),
    ("python", "-m", "carepulse.notifications.email_sender"),
    ("node", "ingress.mjs"),
)


@dataclass(frozen=True)
class ObservedRuntimeUnit:
    """Actual inspected container/runtime unit prior to provisioning secrets or network."""

    service: ServiceKind
    generation: str
    image_digest: str
    entry_command: tuple[str, ...]
    adapter_file: Path | None
    adapter_mount_readonly: bool
    import_reachable: bool
    configured_peers: tuple[str, ...]
    mounted_credentials: frozenset[str]
    target_id: str
    smtp_egress_allowed: bool


def sha256_file(path: Path) -> str:
    """Compute canonical sha256:<hex> digest of an on-disk artifact."""
    if not path.is_file():
        raise LaunchBindingError(f"Missing artifact for digest check: {path}")
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    return f"sha256:{digest}"


def is_raw_unguarded_entry(entry_command: Sequence[str], generation: str) -> bool:
    """Return True if an entry command bypasses the generation's compatibility guard."""
    cmd = tuple(entry_command)
    if generation == "source":
        # Immutable 0040 source images MUST use mounted read-only adapters, never raw CMD/ENTRYPOINT.
        for raw_prefix in _RAW_UNGUARDED_ENTRIES:
            if len(cmd) >= len(raw_prefix) and cmd[: len(raw_prefix)] == raw_prefix:
                return True
        if len(cmd) >= 3 and cmd[0] == "sh" and cmd[1] == "-c":
            script = cmd[2]
            if "exec node ingress.mjs" in script and "source-web.cjs" not in script:
                return True
    return False


def verify_prelaunch_unit(
    manifest: GenerationManifest,
    unit: ObservedRuntimeUnit,
) -> ServiceManifest:
    """Verify image/adapter digest, entry command, mount/import reachability, peers, target, and credentials."""
    if unit.generation != manifest.generation:
        raise LaunchBindingError(
            f"Generation mismatch for {unit.service}: expected {manifest.generation}, got {unit.generation}"
        )
    if unit.target_id != manifest.target_binding.target_id:
        raise LaunchBindingError(
            f"Target mismatch for {unit.service}: expected {manifest.target_binding.target_id}, got {unit.target_id}"
        )

    expected = manifest.participants[unit.service]

    if unit.image_digest != expected.image_digest:
        raise LaunchBindingError(
            f"Image digest mismatch for {unit.service}: expected {expected.image_digest}, got {unit.image_digest}"
        )

    if is_raw_unguarded_entry(unit.entry_command, manifest.generation):
        raise LaunchBindingError(
            f"Rejected raw unguarded entry command for {manifest.generation} {unit.service}"
        )

    if tuple(unit.entry_command) != expected.entry_command:
        raise LaunchBindingError(
            f"Entry command mismatch for {unit.service}: expected {expected.entry_command}, got {unit.entry_command}"
        )

    if not unit.import_reachable:
        raise LaunchBindingError(
            f"Adapter/guard import or mount not reachable for {unit.service}"
        )

    if manifest.generation == "source":
        if unit.adapter_file is None or not unit.adapter_mount_readonly:
            raise LaunchBindingError(
                f"Source {unit.service} requires a read-only mounted adapter artifact"
            )
        actual_adapter_digest = sha256_file(unit.adapter_file)
        if actual_adapter_digest != expected.adapter_digest:
            raise LaunchBindingError(
                f"Source adapter digest mismatch for {unit.service}: expected {expected.adapter_digest}, got {actual_adapter_digest}"
            )
    elif unit.adapter_file is not None:
        actual_adapter_digest = sha256_file(unit.adapter_file)
        if actual_adapter_digest != expected.adapter_digest:
            raise LaunchBindingError(
                f"Candidate guard digest mismatch for {unit.service}: expected {expected.adapter_digest}, got {actual_adapter_digest}"
            )

    if set(unit.configured_peers) != set(expected.trusted_peers):
        raise LaunchBindingError(
            f"Peer mismatch for {unit.service}: expected {expected.trusted_peers}, got {unit.configured_peers}"
        )

    if not unit.mounted_credentials:
        raise LaunchBindingError(f"No credentials mounted for {unit.service}")
    if not unit.mounted_credentials.issubset(expected.allowed_credentials):
        disallowed = sorted(unit.mounted_credentials - expected.allowed_credentials)
        raise LaunchBindingError(
            f"Disallowed credentials mounted for {unit.service}: {disallowed}"
        )

    if unit.service != "sender" and unit.smtp_egress_allowed:
        raise LaunchBindingError(
            f"Only sender may hold SMTP egress capability (got {unit.service})"
        )

    return expected


def verify_generation_launch(
    manifest: GenerationManifest,
    units: Iterable[ObservedRuntimeUnit],
    *,
    sealed: bool = False,
) -> dict[ServiceKind, ServiceManifest]:
    """Verify a complete set of participants for a generation before provisioning access."""
    if sealed:
        raise LaunchBindingError(
            f"Launch sealed for generation {manifest.generation} on target {manifest.target_binding.target_id}"
        )
    verified: dict[ServiceKind, ServiceManifest] = {}
    for unit in units:
        if unit.service in verified:
            raise LaunchBindingError(f"Duplicate launch unit for {unit.service}")
        verified[unit.service] = verify_prelaunch_unit(manifest, unit)
    if set(verified.keys()) != set(manifest.participants.keys()):
        missing = sorted(set(manifest.participants.keys()) - set(verified.keys()))
        raise LaunchBindingError(f"Incomplete generation launch; missing {missing}")
    return verified


def seal_generation_launch(
    seal_state_path: Path, *, target_id: str, generation: str, epoch: int
) -> None:
    """Persist a fail-closed launch seal so auto-restarts cannot re-acquire credentials."""
    seal_state_path.parent.mkdir(parents=True, exist_ok=True)
    seal_state_path.write_text(
        f"{target_id}:{generation}:{epoch}:SEALED\n", encoding="utf-8"
    )


def mark_source_sealed_in_control(conn, *, target_id: str, owner_epoch: int) -> None:
    """Record source_sealed = TRUE in compat_control.target_control under current owner epoch (T037)."""
    ctrl = assert_current_owner(
        conn, target_id=target_id, expected_epoch=owner_epoch, lock_mode="UPDATE"
    )
    if ctrl.phase != "DRAINING" or ctrl.active_generation != "none":
        raise LaunchBindingError("Source sealing requires closed DRAINING admission")
    cur = conn.execute(
        """
        UPDATE compat_control.target_control
        SET source_sealed = TRUE,
            updated_at = now()
        WHERE singleton_id = TRUE
          AND target_id = %s
          AND owner_epoch = %s
          AND phase = 'DRAINING'
        """,
        (target_id, owner_epoch),
    )
    if cur.rowcount != 1:
        conn.rollback()
        raise LaunchBindingError(
            "Cannot seal source launch outside DRAINING under current owner epoch"
        )
    conn.commit()


def is_generation_sealed(
    seal_state_path: Path, *, target_id: str, generation: str
) -> bool:
    """Return True if a launch seal exists for the given target and generation."""
    if not seal_state_path.is_file():
        return False
    prefix = f"{target_id}:{generation}:"
    return any(
        line.startswith(prefix) and line.endswith(":SEALED")
        for line in seal_state_path.read_text(encoding="utf-8").splitlines()
    )


def verify_compose_isolation(compose_doc: Mapping[str, object]) -> None:
    """Verify dual-generation Compose topology has no raw listener, peer, NODE_OPTIONS, or SMTP bypass."""
    networks = compose_doc.get("networks")
    if not isinstance(networks, Mapping):
        raise LaunchBindingError("Compose topology missing networks")
    for net_name, net_cfg in networks.items():
        if not isinstance(net_cfg, Mapping) or net_cfg.get("internal") is not True:
            raise LaunchBindingError(f"Network {net_name} must be internal: true")

    services = compose_doc.get("services")
    if not isinstance(services, Mapping):
        raise LaunchBindingError("Compose topology missing services")

    for svc_name, svc_cfg in services.items():
        if not isinstance(svc_cfg, Mapping):
            continue
        env = svc_cfg.get("environment") or {}
        if isinstance(env, Mapping) and "NODE_OPTIONS" in env:
            raise LaunchBindingError(f"Untrusted NODE_OPTIONS in {svc_name}")
        if svc_name in ("source-api", "source-web", "source-worker", "source-sender"):
            vols = svc_cfg.get("volumes") or []
            has_ro_adapters = any(
                (isinstance(v, str) and "/run/compat-adapters:ro" in v)
                or (
                    isinstance(v, Mapping)
                    and v.get("target") == "/run/compat-adapters"
                    and v.get("read_only") is True
                )
                for v in vols
            )
            if not has_ro_adapters:
                raise LaunchBindingError(
                    f"Source service {svc_name} missing read-only compat-adapters mount"
                )
        if (
            svc_name not in ("source-sender", "candidate-sender", "mail")
            and isinstance(env, Mapping)
            and env.get("CP_SMTP_HOST")
        ):
            raise LaunchBindingError(
                f"Non-sender service {svc_name} must not configure SMTP egress"
            )

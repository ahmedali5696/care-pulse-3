"""Reviewed generation and service manifest parsing for PRE-W3-COMPAT."""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

GenerationKind = Literal["source", "candidate"]
ServiceKind = Literal["web", "api", "worker", "sender"]

SOURCE_HEAD = "0040_t173_numeric_money"
CANDIDATE_HEAD = "0046_f01_audit_integrity"
IMMUTABLE_SOURCE_SHA = "6cf2973c07f6e0ef2d8040d1eb3c235fd1598df7"

_GIT_SHA_RE = re.compile(r"^[0-9a-f]{40}$")
_SHA256_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
_IMAGE_DIGEST_RE = re.compile(r"^(?:[a-z0-9._/-]+@)?sha256:[0-9a-f]{64}$")
_IDENTIFIER_RE = re.compile(r"^[A-Za-z0-9_.-]+$")
_REQUIRED_SERVICES: tuple[ServiceKind, ...] = ("web", "api", "worker", "sender")


class ManifestError(ValueError):
    """Raised when a generation or service manifest is unknown, unmeasured, or mutable."""


@dataclass(frozen=True)
class TargetBinding:
    """Trusted target cluster and database identity."""

    target_id: str
    cluster_id: str
    db_name: str

    def __post_init__(self) -> None:
        if not self.target_id or not _IDENTIFIER_RE.fullmatch(self.target_id):
            raise ManifestError("Invalid target_id in target binding")
        if not self.cluster_id or not _IDENTIFIER_RE.fullmatch(self.cluster_id):
            raise ManifestError("Invalid cluster_id in target binding")
        if not self.db_name or not _IDENTIFIER_RE.fullmatch(self.db_name):
            raise ManifestError("Invalid db_name in target binding")


@dataclass(frozen=True)
class ServiceManifest:
    """Measured participant build, entry command, adapter, peers, and credentials."""

    service: ServiceKind
    image_digest: str
    runnable_target: str
    entry_command: tuple[str, ...]
    adapter_digest: str
    adapter_mount_path: str
    adapter_import_path: str
    packaging_verified: bool
    trusted_peers: tuple[str, ...]
    allowed_credentials: frozenset[str]

    def __post_init__(self) -> None:
        if self.service not in _REQUIRED_SERVICES:
            raise ManifestError(f"Unsupported service kind: {self.service!r}")
        if not _IMAGE_DIGEST_RE.fullmatch(self.image_digest):
            raise ManifestError(
                f"Service {self.service} image_digest must be an immutable sha256 digest"
            )
        if not self.runnable_target or not _IDENTIFIER_RE.fullmatch(
            self.runnable_target
        ):
            raise ManifestError(
                f"Service {self.service} requires a valid runnable_target"
            )
        if not self.entry_command or any(not arg for arg in self.entry_command):
            raise ManifestError(
                f"Service {self.service} requires a non-empty entry_command"
            )
        if not _SHA256_RE.fullmatch(self.adapter_digest):
            raise ManifestError(
                f"Service {self.service} adapter_digest must be sha256:<64-hex>"
            )
        if not self.adapter_mount_path:
            raise ManifestError(
                f"Service {self.service} requires an adapter_mount_path"
            )
        if not self.adapter_import_path:
            raise ManifestError(
                f"Service {self.service} requires an adapter_import_path"
            )
        if self.packaging_verified is not True:
            raise ManifestError(
                f"Service {self.service} packaging must be separately verified"
            )
        if any(not peer for peer in self.trusted_peers):
            raise ManifestError(
                f"Service {self.service} has an empty trusted peer entry"
            )
        if not self.allowed_credentials or any(
            not _IDENTIFIER_RE.fullmatch(cred) for cred in self.allowed_credentials
        ):
            raise ManifestError(
                f"Service {self.service} requires valid allowed_credentials"
            )


@dataclass(frozen=True)
class GenerationManifest:
    """Complete reviewed manifest for one application generation."""

    manifest_id: str
    generation: GenerationKind
    source_sha: str
    expected_head: str
    target_binding: TargetBinding
    participants: Mapping[ServiceKind, ServiceManifest]

    def __post_init__(self) -> None:
        if not self.manifest_id or not _IDENTIFIER_RE.fullmatch(self.manifest_id):
            raise ManifestError("Invalid manifest_id")
        if self.generation not in ("source", "candidate"):
            raise ManifestError(f"Unknown generation: {self.generation!r}")
        if not _GIT_SHA_RE.fullmatch(self.source_sha):
            raise ManifestError("source_sha must be a 40-character lowercase git SHA")
        expected = SOURCE_HEAD if self.generation == "source" else CANDIDATE_HEAD
        if self.expected_head != expected:
            raise ManifestError(
                f"Generation {self.generation} requires head {expected}, got {self.expected_head!r}"
            )
        if set(self.participants.keys()) != set(_REQUIRED_SERVICES):
            raise ManifestError(
                f"Manifest must define all required services: {_REQUIRED_SERVICES}"
            )
        for service_name, participant in self.participants.items():
            if participant.service != service_name:
                raise ManifestError(
                    f"Participant key {service_name} mismatches service {participant.service}"
                )

    def digest(self) -> str:
        """Compute canonical SHA-256 digest of this manifest."""
        payload = {
            "manifest_id": self.manifest_id,
            "generation": self.generation,
            "source_sha": self.source_sha,
            "expected_head": self.expected_head,
            "target_binding": {
                "target_id": self.target_binding.target_id,
                "cluster_id": self.target_binding.cluster_id,
                "db_name": self.target_binding.db_name,
            },
            "participants": {
                svc: {
                    "image_digest": p.image_digest,
                    "runnable_target": p.runnable_target,
                    "entry_command": list(p.entry_command),
                    "adapter_digest": p.adapter_digest,
                    "adapter_mount_path": p.adapter_mount_path,
                    "adapter_import_path": p.adapter_import_path,
                    "packaging_verified": p.packaging_verified,
                    "trusted_peers": list(p.trusted_peers),
                    "allowed_credentials": sorted(p.allowed_credentials),
                }
                for svc, p in sorted(self.participants.items())
            },
        }
        canonical = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode(
            "utf-8"
        )
        return f"sha256:{hashlib.sha256(canonical).hexdigest()}"


def _require_mapping(value: Any, label: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ManifestError(f"{label} must be a mapping")
    return value


def _require_str(mapping: Mapping[str, Any], key: str, label: str) -> str:
    value = mapping.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ManifestError(f"{label}.{key} must be a non-empty string")
    return value


def _require_str_list(
    mapping: Mapping[str, Any], key: str, label: str
) -> tuple[str, ...]:
    value = mapping.get(key)
    if not isinstance(value, (list, tuple)) or any(
        not isinstance(item, str) or not item.strip() for item in value
    ):
        raise ManifestError(f"{label}.{key} must be a list of non-empty strings")
    return tuple(value)


def parse_manifest(raw: Mapping[str, Any] | str | Path) -> GenerationManifest:
    """Parse and strictly validate a generation manifest from dict, JSON text, or Path."""
    if isinstance(raw, Path):
        data = json.loads(raw.read_text(encoding="utf-8"))
    elif isinstance(raw, str):
        data = json.loads(raw)
    else:
        data = raw

    root = _require_mapping(data, "manifest")
    tb_raw = _require_mapping(root.get("target_binding"), "manifest.target_binding")
    target_binding = TargetBinding(
        target_id=_require_str(tb_raw, "target_id", "target_binding"),
        cluster_id=_require_str(tb_raw, "cluster_id", "target_binding"),
        db_name=_require_str(tb_raw, "db_name", "target_binding"),
    )

    participants_raw = _require_mapping(
        root.get("participants"), "manifest.participants"
    )
    participants: dict[ServiceKind, ServiceManifest] = {}
    for svc in _REQUIRED_SERVICES:
        p_raw = _require_mapping(participants_raw.get(svc), f"participants.{svc}")
        packaging_verified = p_raw.get("packaging_verified")
        if not isinstance(packaging_verified, bool):
            raise ManifestError(
                f"participants.{svc}.packaging_verified must be boolean"
            )
        trusted_peers = p_raw.get("trusted_peers", [])
        if not isinstance(trusted_peers, (list, tuple)) or any(
            not isinstance(peer, str) or not peer.strip() for peer in trusted_peers
        ):
            raise ManifestError(
                f"participants.{svc}.trusted_peers must be a list of non-empty strings"
            )
        participants[svc] = ServiceManifest(
            service=svc,
            image_digest=_require_str(p_raw, "image_digest", f"participants.{svc}"),
            runnable_target=_require_str(
                p_raw, "runnable_target", f"participants.{svc}"
            ),
            entry_command=_require_str_list(
                p_raw, "entry_command", f"participants.{svc}"
            ),
            adapter_digest=_require_str(p_raw, "adapter_digest", f"participants.{svc}"),
            adapter_mount_path=_require_str(
                p_raw, "adapter_mount_path", f"participants.{svc}"
            ),
            adapter_import_path=_require_str(
                p_raw, "adapter_import_path", f"participants.{svc}"
            ),
            packaging_verified=packaging_verified,
            trusted_peers=tuple(trusted_peers),
            allowed_credentials=frozenset(
                _require_str_list(p_raw, "allowed_credentials", f"participants.{svc}")
            ),
        )

    generation = _require_str(root, "generation", "manifest")
    if generation not in ("source", "candidate"):
        raise ManifestError(f"Unknown generation: {generation!r}")

    return GenerationManifest(
        manifest_id=_require_str(root, "manifest_id", "manifest"),
        generation=generation,  # type: ignore[arg-type]
        source_sha=_require_str(root, "source_sha", "manifest"),
        expected_head=_require_str(root, "expected_head", "manifest"),
        target_binding=target_binding,
        participants=participants,
    )


def verify_manifest_pair(
    source: GenerationManifest, candidate: GenerationManifest
) -> None:
    """Verify source and candidate manifests are distinct, target-aligned, and non-overlapping."""
    if source.generation != "source" or candidate.generation != "candidate":
        raise ManifestError("Expected one source and one candidate manifest")
    if source.source_sha == candidate.source_sha:
        raise ManifestError("Source and candidate must have distinct source_sha")
    if source.target_binding != candidate.target_binding:
        raise ManifestError("Source and candidate target_binding mismatch")
    if source.manifest_id == candidate.manifest_id:
        raise ManifestError("Source and candidate must have distinct manifest_id")
    source_adapters = {p.adapter_digest for p in source.participants.values()}
    candidate_adapters = {p.adapter_digest for p in candidate.participants.values()}
    if source_adapters & candidate_adapters:
        raise ManifestError("Source and candidate must have distinct adapter digests")

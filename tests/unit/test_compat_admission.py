"""Unit tests for PRE-W3-COMPAT manifest, launch binding, and admission gate (T024)."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest
from carepulse.compat.admission import (
    CANDIDATE_HEAD,
    CompatAdmissionError,
    CompatRuntimeConfig,
    acquire_invocation_permit,
    verify_control_and_head,
)

from ops.compat.launch import (
    LaunchBindingError,
    ObservedRuntimeUnit,
    is_raw_unguarded_entry,
    sha256_file,
    verify_prelaunch_unit,
)
from ops.compat.manifest import (
    IMMUTABLE_SOURCE_SHA,
    ManifestError,
    parse_manifest,
)


def _sample_manifest_dict(generation: str = "candidate") -> dict:
    head = (
        "0040_t173_numeric_money"
        if generation == "source"
        else "0046_f01_audit_integrity"
    )
    sha = IMMUTABLE_SOURCE_SHA if generation == "source" else "a" * 40
    digest = "sha256:" + ("1" * 64 if generation == "source" else "2" * 64)
    adapter_digest = "sha256:" + ("3" * 64 if generation == "source" else "4" * 64)
    return {
        "manifest_id": f"{generation}-v1",
        "generation": generation,
        "source_sha": sha,
        "expected_head": head,
        "target_binding": {
            "target_id": "target-1",
            "cluster_id": "cluster-1",
            "db_name": "carepulse",
        },
        "participants": {
            svc: {
                "image_digest": digest,
                "runnable_target": svc,
                "entry_command": ["python", f"{svc}.py"],
                "adapter_digest": adapter_digest,
                "adapter_mount_path": "/run/compat-adapters",
                "adapter_import_path": "carepulse.compat.admission",
                "packaging_verified": True,
                "trusted_peers": ["api"] if svc == "web" else [],
                "allowed_credentials": [f"carepulse_{svc}"],
            }
            for svc in ("web", "api", "worker", "sender")
        },
    }


def test_manifest_rejects_mutable_image_and_wrong_head() -> None:
    bad_img = _sample_manifest_dict("candidate")
    bad_img["participants"]["api"]["image_digest"] = "carepulse-api:latest"
    with pytest.raises(ManifestError):
        parse_manifest(bad_img)

    bad_head = _sample_manifest_dict("source")
    bad_head["expected_head"] = CANDIDATE_HEAD
    with pytest.raises(ManifestError):
        parse_manifest(bad_head)


def test_prelaunch_rejects_raw_source_entry_and_smtp_on_worker(tmp_path: Path) -> None:
    adapter = tmp_path / "source-api.py"
    adapter.write_text("# adapter\n", encoding="utf-8")
    raw = _sample_manifest_dict("source")
    raw["participants"]["api"]["adapter_digest"] = sha256_file(adapter)
    raw["participants"]["api"]["entry_command"] = [
        "python",
        "/run/compat-adapters/source-api.py",
    ]
    manifest = parse_manifest(raw)

    assert is_raw_unguarded_entry(
        ["python", "-m", "uvicorn", "carepulse.main:app"], "source"
    )
    raw_unit = ObservedRuntimeUnit(
        service="api",
        generation="source",
        image_digest=raw["participants"]["api"]["image_digest"],
        entry_command=("python", "-m", "uvicorn", "carepulse.main:app"),
        adapter_file=adapter,
        adapter_mount_readonly=True,
        import_reachable=True,
        configured_peers=(),
        mounted_credentials=frozenset({"carepulse_api"}),
        target_id="target-1",
        smtp_egress_allowed=False,
    )
    with pytest.raises(LaunchBindingError, match="raw unguarded"):
        verify_prelaunch_unit(manifest, raw_unit)

    worker_adapter = tmp_path / "source-jobs.py"
    worker_adapter.write_text("# jobs\n", encoding="utf-8")
    raw["participants"]["worker"]["adapter_digest"] = sha256_file(worker_adapter)
    manifest = parse_manifest(raw)
    smtp_worker = ObservedRuntimeUnit(
        service="worker",
        generation="source",
        image_digest=raw["participants"]["worker"]["image_digest"],
        entry_command=("python", "worker.py"),
        adapter_file=worker_adapter,
        adapter_mount_readonly=True,
        import_reachable=True,
        configured_peers=(),
        mounted_credentials=frozenset({"carepulse_worker"}),
        target_id="target-1",
        smtp_egress_allowed=True,
    )
    with pytest.raises(LaunchBindingError, match="SMTP egress"):
        verify_prelaunch_unit(manifest, smtp_worker)


def test_verify_control_and_head_denies_missing_or_wrong_phase() -> None:
    cfg = CompatRuntimeConfig(
        target_id="target-1",
        manifest_id="candidate-v1",
        generation="candidate",
        expected_head=CANDIDATE_HEAD,
        service="api",
        db_name="carepulse",
    )
    session = MagicMock()
    # Missing/wrong head
    session.execute.return_value.scalars.return_value.all.return_value = [
        "0045_f01_revocation_evidence"
    ]
    with pytest.raises(CompatAdmissionError, match="Incompatible schema revision"):
        verify_control_and_head(session, config=cfg)

    # Matching head but CLOSED phase
    session.execute.return_value.scalars.return_value.all.return_value = [
        CANDIDATE_HEAD
    ]
    session.execute.return_value.mappings.return_value.first.return_value = {
        "target_id": "target-1",
        "db_name": "carepulse",
        "phase": "CLOSED",
        "owner_epoch": 1,
        "source_manifest_id": "source-v1",
        "target_manifest_id": "candidate-v1",
        "active_generation": "none",
        "source_sealed": False,
    }
    with pytest.raises(CompatAdmissionError, match="fenced"):
        verify_control_and_head(session, config=cfg)


def test_acquire_permit_releases_lock_on_denial() -> None:
    cfg = CompatRuntimeConfig(
        target_id="target-1",
        manifest_id="candidate-v1",
        generation="candidate",
        expected_head=CANDIDATE_HEAD,
        service="api",
        db_name="carepulse",
    )
    conn = MagicMock()
    # First call: pg_try_advisory_lock_shared -> True; second call: role identity -> None
    conn.execute.return_value.scalar.return_value = True
    conn.execute.return_value.mappings.return_value.first.return_value = None
    engine = MagicMock()
    engine.connect.return_value = conn

    with pytest.raises(CompatAdmissionError):
        acquire_invocation_permit(engine, config=cfg, expected_login="carepulse_api")
    conn.rollback.assert_called()
    conn.close.assert_called_once()

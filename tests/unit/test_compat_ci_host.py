"""Host-owned compatibility handshake must reject unsafe observer requests."""

from types import SimpleNamespace

import pytest

from ops.compat.ci_host import Supervisor


def test_disposable_host_rejects_default_database_before_docker(monkeypatch):
    def no_docker(*_args, **_kwargs):
        pytest.fail("Unsafe request reached Docker/PostgreSQL host")

    monkeypatch.setattr("ops.compat.ci_host.compose", no_docker)
    host = Supervisor.__new__(Supervisor)
    with pytest.raises(RuntimeError, match="Invalid disposable database"):
        host.validate(
            {
                "version": 1,
                "kind": "booking_source",
                "db_name": "carepulse",
                "target_id": "t-carepulse",
                "source_manifest_id": "source-0040",
            }
        )


def test_host_refuses_forged_owner_and_head(monkeypatch):
    monkeypatch.setattr(
        "ops.compat.ci_host.compose",
        lambda *_args, **_kwargs: (
            "t-compat_mid_abcdefghijkl|123456|compat_mid_abcdefghijkl|MIGRATING|5|"
            "source-0040|candidate-0046|none|true|0041_f01_runtime_identity"
        ),
    )
    host = Supervisor.__new__(Supervisor)
    request = {
        "version": 1,
        "kind": "intermediate",
        "step": 0,
        "head": "0041_f01_runtime_identity",
        "db_name": "compat_mid_abcdefghijkl",
        "target_id": "t-compat_mid_abcdefghijkl",
        "cluster_id": "123456",
        "source_manifest_id": "source-0040",
        "candidate_manifest_id": "candidate-0046",
        "owner_epoch": 4,
        "tenant_id": "11111111-1111-1111-1111-111111111111",
        "job_id": "22222222-2222-2222-2222-222222222222",
    }
    with pytest.raises(RuntimeError, match="owner epoch"):
        host.validate(request)


def test_host_uses_explicit_boolean_encoding_and_denies_unsealed_source(monkeypatch):
    request = {
        "version": 1,
        "kind": "intermediate",
        "step": 0,
        "head": "0041_f01_runtime_identity",
        "db_name": "compat_mid_abcdefghijkl",
        "target_id": "t-compat_mid_abcdefghijkl",
        "cluster_id": "123456",
        "source_manifest_id": "source-0040",
        "candidate_manifest_id": "candidate-0046",
        "owner_epoch": 4,
        "tenant_id": "11111111-1111-1111-1111-111111111111",
        "job_id": "22222222-2222-2222-2222-222222222222",
    }

    def sealed_query(*args, **_kwargs):
        assert "CASE WHEN t.source_sealed THEN 'true' ELSE 'false' END" in args[-1]
        return (
            "t-compat_mid_abcdefghijkl|123456|compat_mid_abcdefghijkl|MIGRATING|4|"
            "source-0040|candidate-0046|none|false|0041_f01_runtime_identity"
        )

    monkeypatch.setattr("ops.compat.ci_host.compose", sealed_query)
    with pytest.raises(RuntimeError, match="source-seal"):
        Supervisor.__new__(Supervisor).validate(request)


@pytest.mark.parametrize("image_matches", [True, False])
def test_sender_attests_actual_container_image_and_removes_it(
    monkeypatch, image_matches
):
    image = "sha256:" + "a" * 64
    container = "b" * 64
    removed = []
    monkeypatch.setenv("CP_CI_PROJECT", "carepulse-ci-123-1")
    monkeypatch.setattr(
        "ops.compat.ci_host.command",
        lambda *_args, **_kwargs: SimpleNamespace(
            returncode=1, stdout="Delivery unavailable\n", stderr=""
        ),
    )
    monkeypatch.setattr(
        "ops.compat.ci_host.required",
        lambda *_args, **_kwargs: (
            container + "|" + (image if image_matches else "sha256:" + "c" * 64)
        ),
    )
    host = Supervisor.__new__(Supervisor)
    host.observed = {"services": {"source-sender": {"image_id": image}}}
    host.remove = removed.append
    request = {
        "source_manifest_id": "source-0040",
        "db_name": "compat_mid_abcdefghijkl",
        "target_id": "t-compat_mid_abcdefghijkl",
        "tenant_id": "11111111-1111-1111-1111-111111111111",
        "job_id": "22222222-2222-2222-2222-222222222222",
    }
    if image_matches:
        result = host.sender("source-sender", request, "d" * 24)
        assert result["container_id"] == container
        assert result["image_id"] == image
    else:
        with pytest.raises(RuntimeError, match="image mismatches"):
            host.sender("source-sender", request, "d" * 24)
    assert removed == ["carepulse-ci-123-1-" + "d" * 12 + "-source-sender"]


@pytest.mark.parametrize("remove_ok", [True, False])
def test_source_stop_requires_verified_container_removal(monkeypatch, remove_ok):
    image = "sha256:" + "a" * 64
    cid = "b" * 64
    monkeypatch.setenv("CP_CI_PROJECT", "carepulse-ci-123-1")
    monkeypatch.setattr(
        "ops.compat.ci_host.compose",
        lambda *_args, **_kwargs: (
            "t-compat_demo|compat_demo|SOURCE_OPEN|1|source-0040|source|false"
        ),
    )

    def docker(args, **_kwargs):
        if args[:3] == ("docker", "inspect", "--format"):
            return SimpleNamespace(returncode=0, stdout=f"{cid}|{image}|no", stderr="")
        if args[:3] == ("docker", "rm", "-f"):
            return SimpleNamespace(
                returncode=0 if remove_ok else 1,
                stdout=cid if remove_ok else "",
                stderr="",
            )
        if args[:2] == ("docker", "inspect"):
            return SimpleNamespace(returncode=1, stdout="", stderr="")
        if args[:2] == ("docker", "ps"):
            return SimpleNamespace(returncode=0, stdout="", stderr="")
        pytest.fail(f"unexpected Docker request: {args}")

    monkeypatch.setattr("ops.compat.ci_host.command", docker)
    host = Supervisor.__new__(Supervisor)
    request = {
        "version": 1,
        "id": "a" * 24,
        "kind": "booking_source",
        "db_name": "compat_demo",
        "target_id": "t-compat_demo",
        "source_manifest_id": "source-0040",
        "owner_epoch": 1,
    }
    host.active = {request["id"]: (cid, request)}
    host.sealed_source_targets = set()
    host.stopped_source_ids = {}
    host.observed = {"services": {"source-api": {"image_id": image}}}
    if not remove_ok:
        with pytest.raises(RuntimeError, match="command failed"):
            host.stop_source(request)
        assert request["id"] in host.active
        return
    proof = host.stop_source(request)
    assert proof["status"] == "stopped"
    assert proof["owner_epoch"] == 1
    assert proof["source_container_id"] == cid
    assert proof["launch_sealed"] is False
    assert not host.active
    assert host.stopped_source_ids["t-compat_demo"] == cid
    with pytest.raises(RuntimeError, match="sealed"):
        host.launch("source-api", request, name="unsafe-restart")


@pytest.mark.parametrize(
    ("record_phase", "head", "running", "allowed"),
    [
        ("DRAINING", "0040_t173_numeric_money", "", True),
        ("MIGRATING", "0042_f01_effective_grants", "", True),
        ("MIGRATING", "0042_f01_effective_grants", "candidate-api|cafe123", False),
        ("SOURCE_OPEN", "0040_t173_numeric_money", "", False),
        ("DRAINING", "0040_t173_numeric_money", "source-worker|cafe123", False),
    ],
)
def test_host_drain_proof_requires_sealed_epoch_and_absent_consumers(
    monkeypatch,
    record_phase,
    head,
    running,
    allowed,
):
    monkeypatch.setenv("CP_CI_PROJECT", "carepulse-ci-123-1")
    monkeypatch.setattr(
        "ops.compat.ci_host.compose",
        lambda *_args, **_kwargs: (
            f"t-compat_demo|123|compat_demo|{record_phase}|2|"
            f"source-0040|candidate-0046|none|true|{head}"
        ),
    )
    monkeypatch.setattr(
        "ops.compat.ci_host.required",
        lambda *_args, **_kwargs: running,
    )
    host = Supervisor.__new__(Supervisor)
    host.active = {}
    host.sealed_source_targets = set()
    host.stopped_source_ids = {}
    request = {
        "version": 1,
        "kind": "drain_proof",
        "id": "a" * 24,
        "db_name": "compat_demo",
        "target_id": "t-compat_demo",
        "cluster_id": "123",
        "source_manifest_id": "source-0040",
        "candidate_manifest_id": "candidate-0046",
        "owner_epoch": 2,
        "head": head,
    }
    if not allowed:
        with pytest.raises(RuntimeError):
            host.drain_proof(request)
        return
    response = host.drain_proof(request)
    assert response["ok"] is True
    assert response["running_consumer_count"] == 0
    assert response["owner_epoch"] == 2
    assert response["launch_sealed"] is True
    assert response["stop_phase"] == record_phase
    assert len(response["stopped_services"]) == 8

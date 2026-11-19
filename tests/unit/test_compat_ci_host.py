"""Host-owned compatibility handshake must reject unsafe observer requests."""

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

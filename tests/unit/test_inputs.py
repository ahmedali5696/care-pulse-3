"""Subprocess evidence for actual input rejection and native exit propagation."""

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SHELL = "powershell.exe" if os.name == "nt" else "pwsh"


@pytest.fixture
def checkout(tmp_path):
    for relative in (
        "ops/check.ps1",
        "package.json",
        "apps/web/package.json",
        ".npmrc",
        "pnpm-workspace.yaml",
        "pnpm-lock.yaml",
        "requirements.in",
        "requirements.lock",
        "apps/web/Dockerfile",
        "apps/api/Dockerfile",
        "compose.yaml",
        ".github/workflows/foundation.yml",
    ):
        target = tmp_path / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(ROOT / relative, target)
    return tmp_path


def _check(checkout, mode="inputs", env=None):
    if env is None:
        env = {
            **os.environ,
            "PATH": str(Path(sys.executable).parent) + os.pathsep + os.environ["PATH"],
        }
    return subprocess.run(
        [SHELL, "-NoProfile", "-File", str(checkout / "ops/check.ps1"), mode],
        check=False,
        cwd=checkout,
        env=env,
        capture_output=True,
        text=True,
        timeout=45,
    )


@pytest.mark.parametrize("version", ["^15.5.28", "15.5.29"])
def test_floating_or_unapproved_manifest_is_rejected_before_install(checkout, version):
    manifest = checkout / "apps/web/package.json"
    package = json.loads(manifest.read_text())
    package["dependencies"]["next"] = version
    manifest.write_text(json.dumps(package))
    completed = _check(checkout)
    assert completed.returncode != 0
    assert "approved manifest pins" in completed.stdout + completed.stderr


def test_floating_image_is_rejected_before_docker(checkout):
    compose = checkout / "compose.yaml"
    compose.write_text(
        compose.read_text().replace(
            "postgres:17.11-bookworm@sha256:858bb5e98eed02eb8cafbcdc007f9a65d1b613972028944a8ec2133ccaf80d27",
            "postgres:latest",
        )
    )
    completed = _check(checkout)
    assert completed.returncode != 0
    assert "approved image references" in completed.stdout + completed.stderr


@pytest.mark.parametrize("filename", ["pnpm-lock.yaml", "requirements.lock"])
def test_inconsistent_lock_is_rejected_without_regeneration(checkout, filename):
    lock = checkout / filename
    lock.write_bytes(lock.read_bytes() + b"\n# isolated synthetic drift\n")
    before = lock.read_bytes()
    completed = _check(checkout)
    assert completed.returncode != 0
    assert filename + " drift" in completed.stdout + completed.stderr
    assert lock.read_bytes() == before


def _native_stub(checkout, name, body):
    directory = checkout / "stub-bin"
    directory.mkdir(exist_ok=True)
    script = directory / (name + ".cmd" if os.name == "nt" else name)
    script.write_text(("@echo off\n" if os.name == "nt" else "#!/bin/sh\n") + body)
    script.chmod(0o755)
    return {
        **os.environ,
        "PATH": os.pathsep.join(
            (str(directory), str(Path(sys.executable).parent), os.environ["PATH"])
        ),
    }


def test_runtime_version_mismatch_is_rejected(checkout):
    env = _native_stub(checkout, "node", "echo v0.0.0\nexit 0\n")
    completed = _check(checkout, env=env)
    assert completed.returncode != 0
    assert "Unapproved Node version" in completed.stdout + completed.stderr


def test_native_failure_becomes_runner_failure(checkout):
    env = _native_stub(checkout, "python", "exit 23\n")
    completed = _check(checkout, "unit", env)
    assert completed.returncode != 0
    assert "exit 23" in completed.stdout + completed.stderr


def test_peer_validation_cannot_be_disabled(checkout):
    npmrc = checkout / ".npmrc"
    npmrc.write_text(
        npmrc.read_text().replace(
            "strict-peer-dependencies=true", "strict-peer-dependencies=false"
        )
    )
    completed = _check(checkout)
    assert completed.returncode != 0
    assert "strict engines/peers" in completed.stdout + completed.stderr

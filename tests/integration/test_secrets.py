"""F-03 consuming-process checks: run only inside disposable Linux Compose CI.

The test image is the API image with an isolated read-only test-suite mount.
It receives only API file secrets, never auth/operator/migration credentials.
"""

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

API_PROBE = (
    "import asyncio; from carepulse.main import app; "
    "asyncio.run(app.router.lifespan_context(app).__aenter__())"
)
SHARED_REQUIRED = (
    "CP_ENV",
    "CP_SYNTHETIC_TEST_PROFILE",
    "CP_POLICY_FILE",
    "CP_DB_HOST",
    "CP_DB_PORT",
    "CP_DB_NAME",
    "CP_DB_USER",
    "CP_DB_PASSWORD_FILE",
    "CP_FILE_ENCRYPTION_KEY_FILE",
    "CP_MAIL_ENABLED",
    "CP_SCAN_ENABLED",
)
POLICY_REQUIRED = (
    "session_idle_minutes",
    "session_absolute_minutes",
    "login_max_attempts",
    "login_window_minutes",
    "recovery_max_attempts",
    "recovery_window_minutes",
    "invitation_expiry_minutes",
    "reset_expiry_minutes",
    "doctor_confirmation_expiry_minutes",
    "signed_request_freshness_seconds",
    "signed_request_max_skew_seconds",
    "download_grant_expiry_seconds",
)
FILE_POLICY_REQUIRED = (
    "allowed_extensions",
    "allowed_mime_types",
    "allowed_classes",
    "max_file_bytes",
    "max_files_per_record",
    "max_total_storage_bytes_per_record",
    "scanner_required",
    "quarantine_on_failure",
)


def _ci_env() -> dict[str, str]:
    if os.getenv("CI") != "true":
        pytest.skip("Consuming-process integration is CI-only")
    required = (
        *SHARED_REQUIRED,
        "CP_SERVICE_KEY_FILE",
        "CP_SIGNING_KEY_ID",
        "CP_DELIVERY_KEY_ID",
        "CP_DELIVERY_PUBLIC_KEY_FILE",
    )
    for name in required:
        assert os.environ.get(name), f"Missing CI process input: {name}"
    return dict(os.environ)


def _probe(script: str, env: dict[str, str], *, should_start: bool) -> None:
    result = subprocess.run(
        [sys.executable, "-c", script],
        env=env,
        capture_output=True,
        timeout=8,
        check=False,
    )
    output = result.stdout + result.stderr
    for secret_name in (
        "CP_DB_PASSWORD_FILE",
        "CP_SERVICE_KEY_FILE",
        "CP_FILE_ENCRYPTION_KEY_FILE",
    ):
        secret_file = env.get(secret_name)
        if secret_file and Path(secret_file).is_file():
            secret = Path(secret_file).read_bytes().strip()
            assert secret and secret not in output, "Secret appeared in process output"
    if should_start:
        assert result.returncode == 0, "Valid protected process failed startup"
        assert output == b"", "Unexpected process output on valid configuration"
    else:
        assert result.returncode != 0, (
            "Protected process accepted missing policy or key"
        )
        assert b"synthetic-credential-" not in output


def test_complete_synthetic_profile_starts() -> None:
    env = _ci_env()
    _probe(API_PROBE, env, should_start=True)


@pytest.mark.parametrize(
    "missing",
    (
        *SHARED_REQUIRED,
        "CP_SERVICE_KEY_FILE",
        "CP_SIGNING_KEY_ID",
        "CP_DELIVERY_KEY_ID",
        "CP_DELIVERY_PUBLIC_KEY_FILE",
    ),
)
def test_missing_each_api_input_denies_start(missing: str) -> None:
    env = _ci_env()
    env.pop(missing)
    _probe(API_PROBE, env, should_start=False)


@pytest.mark.parametrize("field", POLICY_REQUIRED)
def test_missing_each_policy_value_denies_actual_api_start(
    tmp_path: Path, field: str
) -> None:
    env = _ci_env()
    policy = json.loads(Path(env["CP_POLICY_FILE"]).read_text(encoding="utf-8"))
    del policy[field]
    source = tmp_path / "incomplete-policy.json"
    source.write_text(json.dumps(policy), encoding="utf-8")
    env["CP_POLICY_FILE"] = str(source)
    _probe(API_PROBE, env, should_start=False)


@pytest.mark.parametrize("field", FILE_POLICY_REQUIRED)
def test_missing_each_file_rule_denies_actual_api_start(
    tmp_path: Path, field: str
) -> None:
    env = _ci_env()
    policy = json.loads(Path(env["CP_POLICY_FILE"]).read_text(encoding="utf-8"))
    del policy["file_policy"][field]
    source = tmp_path / "incomplete-file-policy.json"
    source.write_text(json.dumps(policy), encoding="utf-8")
    env["CP_POLICY_FILE"] = str(source)
    _probe(API_PROBE, env, should_start=False)


def test_no_operator_secret_exposure_and_source_canary_scan() -> None:
    env = _ci_env()
    for name in env:
        assert "OPERATOR" not in name and "MIGRATION_PASSWORD" not in name
    assert not Path("/run/secrets/postgres_password").exists()
    assert not Path("/run/secrets/auth_secret").exists()

    runtime_secrets = (
        Path(env[name]).read_text(encoding="utf-8").strip()
        for name in (
            "CP_DB_PASSWORD_FILE",
            "CP_SERVICE_KEY_FILE",
            "CP_FILE_ENCRYPTION_KEY_FILE",
        )
    )
    source_paths = [
        *Path("/srv/apps/api/carepulse").rglob("*.py"),
        Path("/srv/apps/worker/main.py"),
    ]
    source = "\n".join(path.read_text(encoding="utf-8") for path in source_paths)
    for secret in runtime_secrets:
        assert secret and secret not in source

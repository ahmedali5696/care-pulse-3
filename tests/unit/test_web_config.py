"""Exercise server-only web validation through its public loader on real files."""

import hashlib
import hmac
import json
import os
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
POLICY = (ROOT / "tests/fixtures/foundation-policy.json").read_text(encoding="utf-8")
CONFIG_PROBE = """
import { loadWebConfig } from './apps/web/config.ts';
try {
  const config = loadWebConfig();
  console.log(JSON.stringify({ policy: config.policy, apiUrl: config.apiUrl }));
} catch (error) {
  process.stderr.write(error.message);
  process.exitCode = 1;
}
"""


@pytest.fixture
def web_env(tmp_path):
    policy_file = tmp_path / "policy.json"
    policy_file.write_text(POLICY, encoding="utf-8")
    policy_file.chmod(0o600)
    secrets = {}
    for name in (
        "CP_DB_PASSWORD_FILE",
        "CP_AUTH_DB_PASSWORD_FILE",
        "CP_SERVICE_KEY_FILE",
        "CP_AUTH_SECRET_FILE",
    ):
        secret_file = tmp_path / name
        secret_file.write_bytes(f"synthetic-web-{name}-secret-0123456789\r\n".encode())
        secret_file.chmod(0o600)
        secrets[name] = str(secret_file)
    return {
        **{
            name: setting
            for name, setting in os.environ.items()
            if not name.startswith("CP_")
        },
        "CP_ENV": "test",
        "CP_SYNTHETIC_TEST_PROFILE": "true",
        "CP_POLICY_FILE": str(policy_file),
        "CP_DB_HOST": "db",
        "CP_DB_PORT": "5432",
        "CP_DB_NAME": "carepulse",
        "CP_DB_USER": "carepulse_runtime",
        "CP_AUTH_DB_USER": "carepulse_auth",
        "CP_INTERNAL_API_URL": "http://api:8000",
        "CP_SIGNING_KEY_ID": "synthetic-ci-v1",
        **secrets,
        "CP_MAIL_ENABLED": "false",
        "CP_SCAN_ENABLED": "false",
    }


def _web_config(env):
    return subprocess.run(
        ["node", "--no-warnings", "--experimental-strip-types", "--input-type=module"],
        input=CONFIG_PROBE,
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )


# Cases: ordinary/escaped keys, duplicate top-level/nested keys, malformed JSON,
# wrong shape, unknown fields, invalid numbers and invalid file-policy flags.
@pytest.mark.parametrize(
    ("policy_text", "diagnostic"),
    [
        (POLICY, None),
        (POLICY.replace('"profile"', r'"pr\u006ffile"' + " \t\n"), None),
        (
            POLICY.replace('"profile":', '"profile": "synthetic", "profile":'),
            "CP_POLICY_FILE",
        ),
        (
            POLICY.replace('"profile":', r'"pr\u006ffile": "synthetic", "profile":'),
            "CP_POLICY_FILE",
        ),
        (
            POLICY.replace(
                '"scanner_required":', '"scanner_required": false, "scanner_required":'
            ),
            "CP_POLICY_FILE",
        ),
        (POLICY[:-2], "CP_POLICY_FILE"),
        ("[]", "CP_POLICY_FILE"),
        (POLICY.replace('"profile":', '"unexpected":'), "CP_POLICY_FILE"),
        (
            POLICY.replace('"allowed_classes":', '"unexpected":'),
            "CP_POLICY_FILE.file_policy",
        ),
        (POLICY.replace('"session_idle_minutes": 30,', ""), "CP_POLICY_FILE"),
        (
            POLICY.replace('"session_idle_minutes": 30', '"session_idle_minutes": 0'),
            "CP_POLICY_FILE.session_idle_minutes",
        ),
        (
            POLICY.replace('"scanner_required": false', '"scanner_required": "false"'),
            "CP_POLICY_FILE.file_policy.scanner_required",
        ),
    ],
)
def test_web_policy_preserves_values_or_reports_sanitized_failure(
    web_env, policy_text, diagnostic
):
    Path(web_env["CP_POLICY_FILE"]).write_text(policy_text, encoding="utf-8")
    completed = _web_config(web_env)
    if diagnostic:
        assert completed.returncode != 0
        assert completed.stdout == ""
        assert completed.stderr == f"Invalid {diagnostic}"
    else:
        assert completed.returncode == 0, completed.stderr
        assert json.loads(completed.stdout)["policy"] == json.loads(policy_text)


# Cases: loopback/approved hostnames, private IPv4 edges, public addresses,
# unsupported protocols, credentials, path/query/fragment and malformed URLs.
@pytest.mark.parametrize(
    ("api_url", "accepted"),
    [
        ("http://api:8000", True),
        ("http://LOCALHOST:8000/", True),
        ("http://[::1]:8000", True),
        ("http://10.0.0.1:8000", True),
        ("http://127.0.0.1:8000", True),
        ("http://172.15.255.255:8000", False),
        ("http://172.16.0.0:8000", True),
        ("http://172.31.255.255:8000", True),
        ("http://172.32.0.0:8000", False),
        ("http://192.168.0.1:8000", True),
        ("http://192.169.0.1:8000", False),
        ("http://8.8.8.8:8000", False),
        ("http://example.invalid:8000", False),
        ("https://api:8000", False),
        ("http://user:password@api:8000", False),
        ("http://api:8000/path", False),
        ("http://api:8000/?query=1", False),
        ("http://api:8000/#fragment", False),
        ("not-a-url", False),
    ],
)
def test_web_api_url_accepts_only_private_http_origins(web_env, api_url, accepted):
    web_env["CP_INTERNAL_API_URL"] = api_url
    completed = _web_config(web_env)
    if accepted:
        assert completed.returncode == 0, completed.stderr
        assert json.loads(completed.stdout)["apiUrl"] == api_url.rstrip("/").lower()
    else:
        assert completed.returncode != 0
        assert completed.stderr == "Invalid CP_INTERNAL_API_URL"


@pytest.mark.parametrize("key_id", ["", "bad/key", " space", "á", "a" * 129])
def test_web_rejects_invalid_signing_key_id(web_env, key_id):
    web_env["CP_SIGNING_KEY_ID"] = key_id
    completed = _web_config(web_env)
    assert completed.returncode != 0
    assert completed.stderr == "Invalid CP_SIGNING_KEY_ID"


def test_typescript_config_and_signing_bytes_match_python(web_env):
    from carepulse.identity.signing import CanonicalEnvelope, canonical_payload

    vectors = json.loads(
        (ROOT / "tests/fixtures/signing-vectors.json").read_text(encoding="utf-8")
    )["vectors"]
    probe = """
import { createHmac } from 'node:crypto';
import { readFileSync } from 'node:fs';
import { loadWebConfig } from './apps/web/config.ts';
const config = loadWebConfig();
const vectors = JSON.parse(readFileSync('./tests/fixtures/signing-vectors.json', 'utf8')).vectors;
for (const vector of vectors) {
  const fields = [...vector.canonical_fields];
  fields[1] = config.signingKeyId;
  const bytes = Buffer.from(JSON.stringify(fields), 'utf8');
  const signature = createHmac('sha256', config.serviceKey).update(bytes).digest('hex');
  console.log(JSON.stringify({ fields, bytes: bytes.toString('hex'), signature }));
}
"""
    completed = subprocess.run(
        ["node", "--no-warnings", "--experimental-strip-types", "--input-type=module"],
        input=probe,
        cwd=ROOT,
        env=web_env,
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
    service_key = Path(web_env["CP_SERVICE_KEY_FILE"]).read_text().strip().encode()
    for vector, line in zip(vectors, completed.stdout.splitlines(), strict=True):
        signed = json.loads(line)
        envelope = CanonicalEnvelope(*signed["fields"])
        payload = canonical_payload(envelope)
        assert signed["bytes"] == payload.hex()
        assert (
            signed["signature"]
            == hmac.new(service_key, payload, hashlib.sha256).hexdigest()
        )
        assert envelope.key_id == web_env["CP_SIGNING_KEY_ID"]


@pytest.mark.parametrize(
    "field", ["CP_POLICY_FILE", "CP_AUTH_DB_PASSWORD_FILE", "CP_AUTH_SECRET_FILE"]
)
@pytest.mark.parametrize("file_kind", ["missing", "directory", "oversized"])
def test_web_rejects_unreadable_or_oversized_protected_files(
    web_env, tmp_path, field, file_kind
):
    protected_file = tmp_path / "invalid"
    if file_kind == "directory":
        protected_file.mkdir()
    elif file_kind == "oversized":
        # Otherwise-valid contents isolate the byte limit from content validation.
        oversized = POLICY + " " * 65536 if field == "CP_POLICY_FILE" else "€" * 3000
        protected_file.write_bytes(oversized.encode("utf-8"))
    web_env[field] = str(protected_file)
    completed = _web_config(web_env)
    assert completed.returncode != 0
    assert completed.stderr == f"Invalid {field}"


@pytest.mark.skipif(
    os.name == "nt", reason="POSIX file permissions are enforced on Linux"
)
@pytest.mark.parametrize(
    "field", ["CP_POLICY_FILE", "CP_AUTH_DB_PASSWORD_FILE", "CP_AUTH_SECRET_FILE"]
)
def test_web_rejects_writable_protected_files(web_env, field):
    Path(web_env[field]).chmod(0o622)
    completed = _web_config(web_env)
    assert completed.returncode != 0
    assert completed.stderr == f"Invalid {field}"


def test_web_rejects_reused_auth_database_credentials(web_env):
    web_env["CP_AUTH_DB_USER"] = web_env["CP_DB_USER"]
    assert _web_config(web_env).returncode != 0
    web_env["CP_AUTH_DB_USER"] = "carepulse_auth"
    web_env["CP_AUTH_DB_PASSWORD_FILE"] = web_env["CP_DB_PASSWORD_FILE"]
    denied = _web_config(web_env)
    assert denied.returncode != 0
    assert denied.stderr == "Invalid CP_AUTH_DB_PASSWORD_FILE"

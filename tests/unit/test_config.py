"""Deterministic fail-closed configuration tests for API and worker."""

import json
import os
from pathlib import Path

import pytest
from carepulse.config import ConfigError, load_config

POLICY_FIXTURE = (
    Path(__file__).resolve().parents[1] / "fixtures" / "foundation-policy.json"
)
POLICY_NUMBERS = (
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
FILE_NUMBERS = (
    "max_file_bytes",
    "max_files_per_record",
    "max_total_storage_bytes_per_record",
)
FILE_BOOLEANS = ("scanner_required", "quarantine_on_failure")
FILE_LISTS = ("allowed_extensions", "allowed_mime_types", "allowed_classes")
SHARED_ENV = (
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
SECRET_CANARIES = {
    "CP_DB_PASSWORD_FILE": "synthetic-credential-db-password-123456",
    "CP_FILE_ENCRYPTION_KEY_FILE": "synthetic-credential-file-encryption-123456",
    "CP_SERVICE_KEY_FILE": "synthetic-credential-service-key-123456",
}


@pytest.fixture
def policy_path(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    # Start independently of the developer's actual application environment.
    for name in tuple(os.environ):
        if name.startswith("CP_"):
            monkeypatch.delenv(name)
    policy_file = tmp_path / "policy.json"
    policy_file.write_bytes(POLICY_FIXTURE.read_bytes())
    settings = {
        "CP_ENV": "test",
        "CP_SYNTHETIC_TEST_PROFILE": "true",
        "CP_POLICY_FILE": str(policy_file),
        "CP_DB_HOST": "db",
        "CP_DB_PORT": "5432",
        "CP_DB_NAME": "carepulse_test",
        "CP_DB_USER": "carepulse_runtime",
        "CP_MAIL_ENABLED": "false",
        "CP_SCAN_ENABLED": "false",
    }
    for name, setting in settings.items():
        monkeypatch.setenv(name, setting)
    for name, canary in SECRET_CANARIES.items():
        secret_file = tmp_path / (name.lower() + ".txt")
        secret_file.write_text(canary + "\n", encoding="utf-8")
        monkeypatch.setenv(name, str(secret_file))
    return policy_file


def replace_policy(policy_path: Path, policy: dict) -> None:
    policy_path.write_text(json.dumps(policy), encoding="utf-8")


@pytest.mark.parametrize("environment", ["development", "test"])
@pytest.mark.parametrize("consumer", ["api", "worker"])
def test_complete_synthetic_profile(
    policy_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    consumer: str,
    environment: str,
) -> None:
    monkeypatch.setenv("CP_ENV", environment)
    expected = json.loads(policy_path.read_text(encoding="utf-8"))
    config = load_config(consumer)

    assert config.environment == environment
    assert config.db_host == "db" and config.db_port == 5432
    assert config.db_name == "carepulse_test" and config.db_user == "carepulse_runtime"
    assert config.db_password == SECRET_CANARIES["CP_DB_PASSWORD_FILE"]
    assert config.file_encryption_key == SECRET_CANARIES["CP_FILE_ENCRYPTION_KEY_FILE"]
    assert config.service_key == (
        SECRET_CANARIES["CP_SERVICE_KEY_FILE"] if consumer == "api" else None
    )
    assert config.mail_enabled is False and config.scan_enabled is False
    assert config.mail is None and config.scan is None
    for name in POLICY_NUMBERS:
        assert getattr(config.policy, name) == expected[name]
    for name in (*FILE_NUMBERS, *FILE_BOOLEANS):
        assert getattr(config.policy.file_policy, name) == expected["file_policy"][name]
    for name in FILE_LISTS:
        assert getattr(config.policy.file_policy, name) == tuple(
            expected["file_policy"][name]
        )
    assert capsys.readouterr() == ("", "")
    for instance in (config, config.policy, config.policy.file_policy):
        assert all(canary not in repr(instance) for canary in SECRET_CANARIES.values())


@pytest.mark.parametrize(
    ("consumer", "missing"),
    [(consumer, name) for consumer in ("api", "worker") for name in SHARED_ENV]
    + [("api", "CP_SERVICE_KEY_FILE")],
)
def test_each_required_environment_key_is_mandatory(
    policy_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    consumer: str,
    missing: str,
) -> None:
    monkeypatch.delenv(missing)
    with pytest.raises(ConfigError, match=missing):
        load_config(consumer)


@pytest.mark.parametrize("consumer", ["api", "worker"])
@pytest.mark.parametrize(
    ("name", "invalid"),
    [
        ("CP_ENV", "staging"),
        ("CP_SYNTHETIC_TEST_PROFILE", "TRUE"),
        ("CP_SYNTHETIC_TEST_PROFILE", "false"),
        ("CP_POLICY_FILE", "relative-policy.json"),
        ("CP_DB_HOST", "https://db"),
        ("CP_DB_PORT", "0"),
        ("CP_DB_PORT", "65536"),
        ("CP_DB_PORT", "five"),
        ("CP_DB_NAME", " invalid"),
        ("CP_DB_USER", "user name"),
        ("CP_DB_PASSWORD_FILE", "relative-secret"),
        ("CP_FILE_ENCRYPTION_KEY_FILE", "relative-secret"),
        ("CP_MAIL_ENABLED", "1"),
        ("CP_SCAN_ENABLED", "yes"),
    ],
)
def test_invalid_environment_values_are_rejected(
    policy_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    consumer: str,
    name: str,
    invalid: str,
) -> None:
    monkeypatch.setenv(name, invalid)
    with pytest.raises(ConfigError):
        load_config(consumer)


@pytest.mark.parametrize("consumer", ["api", "worker"])
@pytest.mark.parametrize("field", ("profile", "file_policy", *POLICY_NUMBERS))
def test_each_top_level_policy_field_is_required(
    policy_path: Path, consumer: str, field: str
) -> None:
    policy = json.loads(policy_path.read_text(encoding="utf-8"))
    del policy[field]
    replace_policy(policy_path, policy)
    with pytest.raises(ConfigError):
        load_config(consumer)


@pytest.mark.parametrize("consumer", ["api", "worker"])
@pytest.mark.parametrize("field", (*FILE_NUMBERS, *FILE_BOOLEANS, *FILE_LISTS))
def test_each_nested_file_policy_field_is_required(
    policy_path: Path, consumer: str, field: str
) -> None:
    policy = json.loads(policy_path.read_text(encoding="utf-8"))
    del policy["file_policy"][field]
    replace_policy(policy_path, policy)
    with pytest.raises(ConfigError):
        load_config(consumer)


@pytest.mark.parametrize("consumer", ["api", "worker"])
@pytest.mark.parametrize("field", POLICY_NUMBERS)
@pytest.mark.parametrize("invalid", [None, 0, -1, True, 1.5, "5", 2_147_483_648])
def test_each_policy_number_requires_a_bounded_positive_integer(
    policy_path: Path, consumer: str, field: str, invalid: object
) -> None:
    policy = json.loads(policy_path.read_text(encoding="utf-8"))
    policy[field] = invalid
    replace_policy(policy_path, policy)
    with pytest.raises(ConfigError):
        load_config(consumer)


@pytest.mark.parametrize("consumer", ["api", "worker"])
@pytest.mark.parametrize("field", FILE_NUMBERS)
@pytest.mark.parametrize("invalid", [None, 0, -1, True, 0.5, "12", 2_147_483_648])
def test_each_nested_number_requires_a_bounded_positive_integer(
    policy_path: Path, consumer: str, field: str, invalid: object
) -> None:
    policy = json.loads(policy_path.read_text(encoding="utf-8"))
    policy["file_policy"][field] = invalid
    replace_policy(policy_path, policy)
    with pytest.raises(ConfigError):
        load_config(consumer)


@pytest.mark.parametrize("consumer", ["api", "worker"])
@pytest.mark.parametrize("field", FILE_BOOLEANS)
@pytest.mark.parametrize("invalid", [None, 0, 1, "true", []])
def test_each_nested_flag_requires_a_boolean(
    policy_path: Path, consumer: str, field: str, invalid: object
) -> None:
    policy = json.loads(policy_path.read_text(encoding="utf-8"))
    policy["file_policy"][field] = invalid
    replace_policy(policy_path, policy)
    with pytest.raises(ConfigError):
        load_config(consumer)


@pytest.mark.parametrize("consumer", ["api", "worker"])
@pytest.mark.parametrize(
    ("field", "invalid"),
    [
        ("allowed_extensions", []),
        ("allowed_extensions", [".TXT"]),
        ("allowed_extensions", [".txt", ".txt"]),
        ("allowed_extensions", [1]),
        ("allowed_extensions", ".txt"),
        ("allowed_mime_types", []),
        ("allowed_mime_types", ["TEXT/PLAIN"]),
        ("allowed_mime_types", ["text/plain", "text/plain"]),
        ("allowed_mime_types", [1]),
        ("allowed_mime_types", "text/plain"),
        ("allowed_classes", []),
        ("allowed_classes", ["synthetic-test-document", "synthetic-test-document"]),
        ("allowed_classes", [1]),
        ("allowed_classes", "synthetic-test-document"),
    ],
)
def test_nested_allowlists_validate_shape_format_and_uniqueness(
    policy_path: Path, consumer: str, field: str, invalid: object
) -> None:
    policy = json.loads(policy_path.read_text(encoding="utf-8"))
    policy["file_policy"][field] = invalid
    replace_policy(policy_path, policy)
    with pytest.raises(ConfigError):
        load_config(consumer)


@pytest.mark.parametrize("consumer", ["api", "worker"])
def test_session_absolute_timeout_must_exceed_idle(
    policy_path: Path, consumer: str
) -> None:
    policy = json.loads(policy_path.read_text(encoding="utf-8"))
    policy["session_absolute_minutes"] = policy["session_idle_minutes"]
    replace_policy(policy_path, policy)
    with pytest.raises(ConfigError):
        load_config(consumer)


@pytest.mark.parametrize("consumer", ["api", "worker"])
@pytest.mark.parametrize("environment", ["production", "staging"])
def test_unapproved_environments_are_rejected_even_with_synthetic_fixture(
    policy_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    consumer: str,
    environment: str,
) -> None:
    monkeypatch.setenv("CP_ENV", environment)
    with pytest.raises(ConfigError):
        load_config(consumer)


@pytest.mark.parametrize("consumer", ["api", "worker"])
@pytest.mark.parametrize("profile", [None, "production", "development", 42])
def test_only_explicit_synthetic_policy_is_accepted(
    policy_path: Path, consumer: str, profile: object
) -> None:
    policy = json.loads(policy_path.read_text(encoding="utf-8"))
    policy["profile"] = profile
    replace_policy(policy_path, policy)
    with pytest.raises(ConfigError):
        load_config(consumer)


@pytest.mark.parametrize("consumer", ["api", "worker"])
@pytest.mark.parametrize("nested", [False, True])
def test_unknown_policy_keys_fail_closed(
    policy_path: Path, consumer: str, nested: bool
) -> None:
    policy = json.loads(policy_path.read_text(encoding="utf-8"))
    target = policy["file_policy"] if nested else policy
    target["synthetic-secret-key-name-canary"] = "synthetic-secret-value-canary"
    replace_policy(policy_path, policy)
    with pytest.raises(ConfigError) as error:
        load_config(consumer)
    assert "canary" not in str(error.value)


@pytest.mark.parametrize("consumer", ["api", "worker"])
@pytest.mark.parametrize("nested", [False, True])
def test_duplicate_raw_json_keys_fail_closed(
    policy_path: Path, consumer: str, nested: bool
) -> None:
    raw = policy_path.read_text(encoding="utf-8")
    if nested:
        raw = raw.replace(
            '"max_file_bytes": 4096', '"max_file_bytes": 4096, "max_file_bytes": 2048'
        )
    else:
        raw = raw.replace(
            '"profile": "synthetic"', '"profile": "synthetic", "profile": "synthetic"'
        )
    policy_path.write_text(raw, encoding="utf-8")
    with pytest.raises(ConfigError, match="Duplicate CP_POLICY_FILE key"):
        load_config(consumer)


@pytest.mark.parametrize("consumer", ["api", "worker"])
def test_invalid_json_is_rejected_without_echoing_content(
    policy_path: Path, consumer: str
) -> None:
    policy_path.write_text('{"synthetic-secret-value-canary":', encoding="utf-8")
    with pytest.raises(ConfigError) as error:
        load_config(consumer)
    assert "canary" not in str(error.value)


def test_scanner_is_mandatory_when_api_policy_requires_it(
    policy_path: Path,
) -> None:
    policy = json.loads(policy_path.read_text(encoding="utf-8"))
    policy["file_policy"]["scanner_required"] = True
    replace_policy(policy_path, policy)
    with pytest.raises(ConfigError, match="scanner"):
        load_config("api")
    assert load_config("worker").scan is None


@pytest.mark.parametrize(
    ("consumer", "flag", "prefix"),
    [("api", "CP_SCAN_ENABLED", "CP_SCAN")],
)
def test_enabled_consumer_endpoint_requires_host_and_port(
    policy_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    consumer: str,
    flag: str,
    prefix: str,
) -> None:
    monkeypatch.setenv(flag, "true")
    with pytest.raises(ConfigError, match=prefix + "_HOST"):
        load_config(consumer)
    monkeypatch.setenv(prefix + "_HOST", "service")
    with pytest.raises(ConfigError, match=prefix + "_PORT"):
        load_config(consumer)
    monkeypatch.setenv(prefix + "_HOST", "invalid host")
    monkeypatch.setenv(prefix + "_PORT", "1025")
    with pytest.raises(ConfigError, match=prefix + "_HOST"):
        load_config(consumer)
    monkeypatch.setenv(prefix + "_HOST", "service")
    monkeypatch.setenv(prefix + "_PORT", "65536")
    with pytest.raises(ConfigError, match=prefix + "_PORT"):
        load_config(consumer)
    monkeypatch.setenv(prefix + "_PORT", "1025")
    config = load_config(consumer)
    endpoint = config.scan if consumer == "api" else config.mail
    assert endpoint is not None and (endpoint.host, endpoint.port) == ("service", 1025)


@pytest.mark.parametrize(
    ("consumer", "ignored_prefix", "ignored_flag"),
    [
        ("api", "CP_MAIL", "CP_MAIL_ENABLED"),
        ("worker", "CP_SCAN", "CP_SCAN_ENABLED"),
        ("worker", "CP_MAIL", "CP_MAIL_ENABLED"),
    ],
)
def test_nonconsumer_endpoint_is_not_required(
    policy_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    consumer: str,
    ignored_prefix: str,
    ignored_flag: str,
) -> None:
    monkeypatch.setenv(ignored_flag, "true")
    assert load_config(consumer).environment == "test"
    monkeypatch.setenv(ignored_prefix + "_HOST", "invalid host")
    monkeypatch.setenv(ignored_prefix + "_PORT", "invalid port")
    assert load_config(consumer).environment == "test"


def test_worker_does_not_consume_api_service_key(
    policy_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("CP_SERVICE_KEY_FILE")
    assert load_config("worker").service_key is None
    with pytest.raises(ConfigError, match="CP_SERVICE_KEY_FILE"):
        load_config("api")


@pytest.mark.parametrize("consumer", ["api", "worker"])
@pytest.mark.parametrize("name", ["CP_DB_PASSWORD_FILE", "CP_FILE_ENCRYPTION_KEY_FILE"])
def test_secret_file_contents_are_validated(
    policy_path: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    consumer: str,
    name: str,
) -> None:
    secret_file = tmp_path / "short-secret.txt"
    secret_file.write_text("short", encoding="utf-8")
    monkeypatch.setenv(name, str(secret_file))
    with pytest.raises(ConfigError, match=name):
        load_config(consumer)


def test_api_service_key_length_is_validated(
    policy_path: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    secret_file = tmp_path / "service.txt"
    secret_file.write_text("short", encoding="utf-8")
    monkeypatch.setenv("CP_SERVICE_KEY_FILE", str(secret_file))
    with pytest.raises(ConfigError, match="CP_SERVICE_KEY_FILE"):
        load_config("api")


@pytest.mark.parametrize("consumer", ["api", "worker"])
def test_failed_loading_never_prints_or_exposes_secret_values(
    policy_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    consumer: str,
) -> None:
    canary = "synthetic-sensitive-diagnostic-canary"
    monkeypatch.setenv("CP_DB_HOST", "invalid host " + canary)
    with pytest.raises(ConfigError) as error:
        load_config(consumer)
    message = str(error.value) + repr(error.value)
    assert canary not in message
    assert all(secret not in message for secret in SECRET_CANARIES.values())
    assert capsys.readouterr() == ("", "")


def test_invalid_consumer_rejected_without_reading_secrets(
    policy_path: Path,
) -> None:
    with pytest.raises(ConfigError, match="consumer"):
        load_config("unrecognized")

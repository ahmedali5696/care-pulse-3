"""Fail-closed, server-only configuration shared by the API and worker.

Development and test require an explicitly marked synthetic policy. Production
policy numerics are not approved, so production startup is deliberately denied.
No configuration or secret value is included in exception messages or reprs.
"""

import json
import os
import re
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit

type Consumer = Literal["api", "worker", "operator", "sender"]

_MAX_POLICY_BYTES = 65_536
_MAX_SECRET_BYTES = 8_192
_MAX_POLICY_INTEGER = 2_147_483_647
_POLICY_NUMBERS = (
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
_FILE_POLICY_KEYS = frozenset(
    {
        "allowed_extensions",
        "allowed_mime_types",
        "allowed_classes",
        "max_file_bytes",
        "max_files_per_record",
        "max_total_storage_bytes_per_record",
        "scanner_required",
        "quarantine_on_failure",
    }
)
_EXTENSION = re.compile(r"\.[a-z0-9]+(?:[a-z0-9_-]*[a-z0-9])?\Z")
_MIME = re.compile(r"[a-z0-9][a-z0-9!#$&^_.+-]*/[a-z0-9][a-z0-9!#$&^_.+-]*\Z")
_CLASS = re.compile(r"[a-z][a-z0-9]*(?:-[a-z0-9]+)*\Z")
_HOST_LABEL = re.compile(r"[a-zA-Z0-9](?:[a-zA-Z0-9-]{0,61}[a-zA-Z0-9])?\Z")
_SIGNING_KEY_ID = re.compile(r"[A-Za-z0-9_-]{1,128}\Z")
_DELIVERY_KEY_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}\Z")


class ConfigError(RuntimeError):
    """Configuration was rejected; diagnostic strings never contain values."""


@dataclass(frozen=True, slots=True, repr=False)
class FilePolicy:
    allowed_extensions: tuple[str, ...]
    allowed_mime_types: tuple[str, ...]
    allowed_classes: tuple[str, ...]
    max_file_bytes: int
    max_files_per_record: int
    max_total_storage_bytes_per_record: int
    scanner_required: bool
    quarantine_on_failure: bool


@dataclass(frozen=True, slots=True, repr=False)
class SecurityPolicy:
    session_idle_minutes: int
    session_absolute_minutes: int
    login_max_attempts: int
    login_window_minutes: int
    recovery_max_attempts: int
    recovery_window_minutes: int
    invitation_expiry_minutes: int
    reset_expiry_minutes: int
    doctor_confirmation_expiry_minutes: int
    signed_request_freshness_seconds: int
    signed_request_max_skew_seconds: int
    download_grant_expiry_seconds: int
    file_policy: FilePolicy


@dataclass(frozen=True, slots=True, repr=False)
class Endpoint:
    host: str
    port: int


@dataclass(frozen=True, slots=True, repr=False)
class ServiceConfig:
    environment: Literal["development", "test"]
    policy: SecurityPolicy
    db_host: str
    db_port: int
    db_name: str
    db_user: str
    db_password: str
    file_encryption_key: str | None
    service_key: str | None
    signing_key_id: str | None
    mail_enabled: bool
    scan_enabled: bool
    mail: Endpoint | None
    scan: Endpoint | None
    operator_secret: str | None = None
    delivery_public_key_pem: bytes | None = None
    delivery_key_id: str | None = None
    delivery_private_key_path: str | None = None
    delivery_lease_seconds: int | None = None
    delivery_attempt_timeout_seconds: int | None = None
    delivery_max_attempts: int | None = None
    delivery_retry_seconds: int | None = None
    mail_base_url: str | None = None


def _required(name: str) -> str:
    value = os.environ.get(name)
    if value is None or not value or len(value) > 2_048 or value != value.strip():
        raise ConfigError(f"Invalid or missing {name}")
    if any(ord(char) < 33 or ord(char) == 127 for char in value):
        raise ConfigError(f"Invalid or missing {name}")
    return value


def _required_path(name: str) -> str:
    # Windows user profiles can contain spaces; secret file paths are not host
    # names or credentials, but must still be absolute and free of controls.
    value = os.environ.get(name)
    if (
        value is None
        or not value
        or len(value) > 2_048
        or value != value.strip()
        or any(ord(char) < 32 or ord(char) == 127 for char in value)
    ):
        raise ConfigError(f"Invalid or missing {name}")
    return value


def _flag(name: str) -> bool:
    value = _required(name)
    if value not in ("true", "false"):
        raise ConfigError(f"Invalid {name}: expected true or false")
    return value == "true"


def _host(name: str) -> str:
    value = _required(name)
    # Compose DNS names and ordinary IP literals are permitted; no URLs or
    # credentials are allowed in host fields.
    if len(value) > 253 or not all(
        _HOST_LABEL.fullmatch(label) for label in value.split(".")
    ):
        raise ConfigError(f"Invalid {name}")
    return value


def _signing_key_id() -> str:
    value = _required("CP_SIGNING_KEY_ID")
    if not _SIGNING_KEY_ID.fullmatch(value):
        raise ConfigError("Invalid CP_SIGNING_KEY_ID")
    return value


def _port(name: str) -> int:
    value = _required(name)
    if not value.isascii() or not value.isdecimal() or len(value) > 5:
        raise ConfigError(f"Invalid {name}")
    port = int(value)
    if not 1 <= port <= 65_535:
        raise ConfigError(f"Invalid {name}")
    return port


def _positive_seconds(name: str) -> int:
    value = _required(name)
    if not re.fullmatch(r"[1-9][0-9]{0,8}", value):
        raise ConfigError(f"Invalid {name}")
    return int(value)


def _read_file(name: str, *, max_bytes: int) -> str:
    path_value = _required_path(name)
    path = Path(path_value)
    if not path.is_absolute() or ".." in path.parts:
        raise ConfigError(f"Invalid {name} path")

    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    try:
        if path.is_symlink():
            raise ConfigError(f"Invalid {name} path")
        descriptor = os.open(path, flags)
        try:
            info = os.fstat(descriptor)
            if (
                not stat.S_ISREG(info.st_mode)
                or info.st_size > max_bytes
                or (os.name == "posix" and info.st_mode & 0o022)
            ):
                raise ConfigError(f"Invalid {name} file")
            with os.fdopen(descriptor, "rb", closefd=False) as file:
                content = file.read(max_bytes + 1)
        finally:
            os.close(descriptor)
        if not content or len(content) > max_bytes:
            raise ConfigError(f"Invalid {name} file")
        return content.decode("utf-8")
    except (OSError, UnicodeError):
        raise ConfigError(f"Cannot read {name}") from None


def _secret(name: str, *, minimum: int) -> str:
    # A final line ending is allowed for operator-created text secret files.
    value = _read_file(name, max_bytes=_MAX_SECRET_BYTES).rstrip("\r\n")
    if (
        not minimum <= len(value) <= 4_096
        or value != value.strip()
        or not value.isprintable()
    ):
        raise ConfigError(f"Invalid {name} content")
    return value


def _strict_keys(value: object, keys: frozenset[str], label: str) -> dict:
    if not isinstance(value, dict) or set(value) != keys:
        raise ConfigError(f"Invalid {label} structure")
    return value


def _positive(value: object, label: str) -> int:
    if type(value) is not int or not 1 <= value <= _MAX_POLICY_INTEGER:
        raise ConfigError(f"Invalid {label} policy value")
    return value


def _allowed_list(
    value: object, pattern: re.Pattern[str], label: str
) -> tuple[str, ...]:
    if (
        not isinstance(value, list)
        or not 1 <= len(value) <= 128
        or any(
            not isinstance(item, str)
            or len(item) > 128
            or pattern.fullmatch(item) is None
            for item in value
        )
        or len(set(value)) != len(value)
    ):
        raise ConfigError(f"Invalid {label} policy value")
    return tuple(value)


def _boolean(value: object, label: str) -> bool:
    if type(value) is not bool:
        raise ConfigError(f"Invalid {label} policy value")
    return value


def _unique_json_keys(pairs: list[tuple[str, object]]) -> dict:
    result = {}
    for key, value in pairs:
        if key in result:
            raise ConfigError("Duplicate CP_POLICY_FILE key")
        result[key] = value
    return result


def _load_policy() -> SecurityPolicy:
    content = _read_file("CP_POLICY_FILE", max_bytes=_MAX_POLICY_BYTES)
    try:
        parsed = json.loads(content, object_pairs_hook=_unique_json_keys)
    except (json.JSONDecodeError, ValueError, TypeError):
        raise ConfigError("Invalid CP_POLICY_FILE JSON") from None
    policy = _strict_keys(
        parsed,
        frozenset({"profile", "file_policy", *_POLICY_NUMBERS}),
        "CP_POLICY_FILE",
    )
    if policy["profile"] != "synthetic":
        raise ConfigError("Unapproved CP_POLICY_FILE profile")

    numbers = {name: _positive(policy[name], name) for name in _POLICY_NUMBERS}
    if numbers["session_absolute_minutes"] <= numbers["session_idle_minutes"]:
        raise ConfigError("Invalid session policy relationship")

    files = _strict_keys(policy["file_policy"], _FILE_POLICY_KEYS, "file_policy")
    return SecurityPolicy(
        **numbers,
        file_policy=FilePolicy(
            allowed_extensions=_allowed_list(
                files["allowed_extensions"], _EXTENSION, "allowed_extensions"
            ),
            allowed_mime_types=_allowed_list(
                files["allowed_mime_types"], _MIME, "allowed_mime_types"
            ),
            allowed_classes=_allowed_list(
                files["allowed_classes"], _CLASS, "allowed_classes"
            ),
            max_file_bytes=_positive(files["max_file_bytes"], "max_file_bytes"),
            max_files_per_record=_positive(
                files["max_files_per_record"], "max_files_per_record"
            ),
            max_total_storage_bytes_per_record=_positive(
                files["max_total_storage_bytes_per_record"],
                "max_total_storage_bytes_per_record",
            ),
            scanner_required=_boolean(files["scanner_required"], "scanner_required"),
            quarantine_on_failure=_boolean(
                files["quarantine_on_failure"], "quarantine_on_failure"
            ),
        ),
    )


def _endpoint(prefix: str, *, enabled: bool) -> Endpoint | None:
    if not enabled:
        return None
    return Endpoint(host=_host(f"{prefix}_HOST"), port=_port(f"{prefix}_PORT"))


def load_config(consumer: Consumer) -> ServiceConfig:
    """Load only this process's inputs; deny any incomplete active policy."""
    if consumer not in ("api", "worker", "operator", "sender"):
        raise ConfigError("Invalid config consumer")

    environment = _required("CP_ENV")
    if environment not in ("development", "test", "production"):
        raise ConfigError("Invalid CP_ENV")
    if environment == "production":
        raise ConfigError("Production security policy approvals are pending")
    if not _flag("CP_SYNTHETIC_TEST_PROFILE"):
        raise ConfigError("Synthetic policy marker required for nonproduction")

    policy = _load_policy()
    mail_enabled = _flag("CP_MAIL_ENABLED") if consumer != "operator" else False
    scan_enabled = _flag("CP_SCAN_ENABLED") if consumer != "operator" else False
    if consumer == "api" and policy.file_policy.scanner_required and not scan_enabled:
        raise ConfigError("Required file scanner is disabled")

    is_operator = consumer == "operator"
    is_sender = consumer == "sender"
    if is_sender and (not mail_enabled or scan_enabled):
        raise ConfigError("Invalid sender capabilities")
    public_key = (
        _read_file("CP_DELIVERY_PUBLIC_KEY_FILE", max_bytes=_MAX_SECRET_BYTES).encode()
        if is_operator
        else None
    )
    delivery_key_id = _required("CP_DELIVERY_KEY_ID") if is_operator else None
    if delivery_key_id and not _DELIVERY_KEY_ID.fullmatch(delivery_key_id):
        raise ConfigError("Invalid CP_DELIVERY_KEY_ID")
    db_user = _required("CP_DB_USER")
    if is_operator and db_user != "carepulse_operator":
        raise ConfigError("Invalid operator database role")
    if is_sender and db_user != "carepulse_sender":
        raise ConfigError("Invalid sender database role")
    db_password_source = (
        "CP_OPERATOR_DB_PASSWORD_FILE"
        if is_operator
        else "CP_SENDER_DB_PASSWORD_FILE"
        if is_sender
        else "CP_DB_PASSWORD_FILE"
    )
    lease = timeout = max_attempts = retry = None
    private_key_path = mail_base_url = None
    if is_sender:
        lease = _positive_seconds("CP_DELIVERY_LEASE_SECONDS")
        timeout = _positive_seconds("CP_DELIVERY_ATTEMPT_TIMEOUT_SECONDS")
        max_attempts = _positive_seconds("CP_DELIVERY_MAX_ATTEMPTS")
        retry = _positive_seconds("CP_DELIVERY_RETRY_SECONDS")
        if timeout >= lease:
            raise ConfigError("Delivery timeout exceeds lease")
        private_key_path = _required_path("CP_DELIVERY_PRIVATE_KEY_FILE")
        _read_file("CP_DELIVERY_PRIVATE_KEY_FILE", max_bytes=_MAX_SECRET_BYTES)
        _required("CP_DELIVERY_PRIVATE_KEY_FILES")
        mail_base_url = _required("CP_MAIL_BASE_URL")
        try:
            origin = urlsplit(mail_base_url)
            valid_origin = (
                origin.scheme == "http"
                and origin.hostname in ("localhost", "127.0.0.1")
                and origin.port == 3000
                and not origin.username
                and not origin.password
                and origin.path in ("", "/")
                and not origin.query
                and not origin.fragment
            )
        except ValueError as invalid:
            raise ConfigError("Invalid CP_MAIL_BASE_URL") from invalid
        if not valid_origin:
            raise ConfigError("Invalid CP_MAIL_BASE_URL")
    mail = _endpoint("CP_SMTP", enabled=is_sender)
    if mail is not None and (mail.host != "mail" or mail.port != 1025):
        raise ConfigError("Unapproved synthetic SMTP")
    return ServiceConfig(
        environment=environment,
        policy=policy,
        db_host=_host("CP_DB_HOST"),
        db_port=_port("CP_DB_PORT"),
        db_name=_required("CP_DB_NAME"),
        db_user=db_user,
        db_password=_secret(db_password_source, minimum=16),
        file_encryption_key=_secret("CP_FILE_ENCRYPTION_KEY_FILE", minimum=32)
        if consumer in ("api", "worker")
        else None,
        service_key=_secret("CP_SERVICE_KEY_FILE", minimum=32)
        if consumer == "api"
        else None,
        signing_key_id=_signing_key_id() if consumer == "api" else None,
        mail_enabled=mail_enabled,
        scan_enabled=scan_enabled,
        # Only one-shot sender receives synthetic SMTP configuration.
        mail=mail,
        scan=_endpoint("CP_SCAN", enabled=consumer == "api" and scan_enabled),
        operator_secret=_secret("CP_OPERATOR_CREDENTIAL_FILE", minimum=32)
        if is_operator and os.environ.get("CP_OPERATOR_CREDENTIAL_FILE")
        else None,
        delivery_public_key_pem=public_key,
        delivery_key_id=delivery_key_id,
        delivery_private_key_path=private_key_path,
        delivery_lease_seconds=lease,
        delivery_attempt_timeout_seconds=timeout,
        delivery_max_attempts=max_attempts,
        delivery_retry_seconds=retry,
        mail_base_url=mail_base_url,
    )

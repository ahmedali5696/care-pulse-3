import { lstatSync, readFileSync } from "node:fs";
import { isAbsolute } from "node:path";

// Server-only: never import this module from app pages, layouts or client code.
// T012 deliberately does not supply production policy values or fallback secrets.
type Environment = "development" | "test" | "production";

type FilePolicy = {
  allowed_extensions: string[];
  allowed_mime_types: string[];
  allowed_classes: string[];
  max_file_bytes: number;
  max_files_per_record: number;
  max_total_storage_bytes_per_record: number;
  scanner_required: boolean;
  quarantine_on_failure: boolean;
};

type WebPolicy = {
  profile: "synthetic";
  session_idle_minutes: number;
  session_absolute_minutes: number;
  login_max_attempts: number;
  login_window_minutes: number;
  recovery_max_attempts: number;
  recovery_window_minutes: number;
  invitation_expiry_minutes: number;
  reset_expiry_minutes: number;
  doctor_confirmation_expiry_minutes: number;
  signed_request_freshness_seconds: number;
  signed_request_max_skew_seconds: number;
  download_grant_expiry_seconds: number;
  file_policy: FilePolicy;
};

function invalid(field: string): never {
  throw new Error(`Invalid ${field}`);
}

function required(name: string): string {
  const value = process.env[name];
  if (
    !value || !value.trim() || value !== value.trim() ||
    value.length > 2048 || /[\x00-\x1f\x7f]/.test(value)
  ) invalid(name);
  return value;
}

function choice<T extends string>(name: string, options: readonly T[]): T {
  const value = required(name);
  if (!options.includes(value as T)) invalid(name);
  return value as T;
}

function port(name: string): number {
  const value = required(name);
  if (!/^[0-9]+$/.test(value)) invalid(name);
  const parsed = Number(value);
  if (!Number.isInteger(parsed) || parsed < 1 || parsed > 65535) invalid(name);
  return parsed;
}

function hostname(name: string): string {
  const host = required(name);
  if (host.length > 253 || !host.split(".").every(
    (label) => /^[a-zA-Z0-9](?:[a-zA-Z0-9-]{0,61}[a-zA-Z0-9])?$/.test(label),
  )) invalid(name);
  return host;
}

function secretFile(name: string): string {
  const filename = required(name);
  if (!isAbsolute(filename)) invalid(name);
  let value: string;
  try {
    const info = lstatSync(filename);
    if (!info.isFile() || info.size > 8192 ||
        (process.platform !== "win32" && (info.mode & 0o022) !== 0)) invalid(name);
    value = readFileSync(filename, "utf8").replace(/\r?\n$/, "");
  } catch {
    return invalid(name);
  }
  if (
    value.length < 32 || value.length > 4096 ||
    value !== value.trim() || /[\x00-\x1f\x7f]/.test(value)
  ) invalid(name);
  return value;
}

// JSON.parse silently keeps the last duplicate member; policy files must not.
function rejectDuplicateKeys(json: string): void {
  const scopes: Array<Set<string> | null> = [];
  for (let index = 0; index < json.length; index++) {
    const char = json[index];
    if (char === "{") scopes.push(new Set());
    else if (char === "[") scopes.push(null);
    else if (char === "}" || char === "]") scopes.pop();
    else if (char === '"') {
      const start = index;
      index++;
      while (index < json.length && json[index] !== '"') {
        if (json[index] === "\\") index++;
        index++;
      }
      let next = index + 1;
      while (/\s/.test(json[next] ?? "")) next++;
      const scope = scopes[scopes.length - 1];
      if (json[next] === ":" && scope) {
        const key = JSON.parse(json.slice(start, index + 1)) as string;
        if (scope.has(key)) invalid("CP_POLICY_FILE");
        scope.add(key);
      }
    }
  }
}

function record(value: unknown, name: string): Record<string, unknown> {
  if (!value || typeof value !== "object" || Array.isArray(value)) invalid(name);
  return value as Record<string, unknown>;
}

function integer(value: unknown, name: string): number {
  if (
    typeof value !== "number" ||
    !Number.isSafeInteger(value) || value > 2147483647 || value <= 0
  ) invalid(name);
  return value;
}

function stringList(value: unknown, name: string, item: RegExp): string[] {
  if (
    !Array.isArray(value) ||
    value.length === 0 || value.length > 128 ||
    !value.every((entry) => typeof entry === "string" && entry.length <= 128 && item.test(entry)) ||
    new Set(value).size !== value.length
  ) invalid(name);
  return value as string[];
}

function policyFile(): WebPolicy {
  const filename = required("CP_POLICY_FILE");
  if (!isAbsolute(filename)) invalid("CP_POLICY_FILE");

  let data: unknown;
  try {
    const info = lstatSync(filename);
    if (!info.isFile() || info.size > 65536 ||
        (process.platform !== "win32" && (info.mode & 0o022) !== 0)) invalid("CP_POLICY_FILE");
    const json = readFileSync(filename, "utf8");
    data = JSON.parse(json);
    rejectDuplicateKeys(json);
  } catch {
    return invalid("CP_POLICY_FILE");
  }
  const policy = record(data, "CP_POLICY_FILE");
  const numericFields = [
    "session_idle_minutes", "session_absolute_minutes",
    "login_max_attempts", "login_window_minutes",
    "recovery_max_attempts", "recovery_window_minutes",
    "invitation_expiry_minutes", "reset_expiry_minutes",
    "doctor_confirmation_expiry_minutes",
    "signed_request_freshness_seconds", "signed_request_max_skew_seconds",
    "download_grant_expiry_seconds",
  ];
  if (
    Object.keys(policy).length !== numericFields.length + 2 ||
    !numericFields.every((field) => Object.hasOwn(policy, field)) ||
    !Object.hasOwn(policy, "profile") || !Object.hasOwn(policy, "file_policy")
  ) invalid("CP_POLICY_FILE");
  if (policy.profile !== "synthetic") invalid("CP_POLICY_FILE.profile");

  const session_idle_minutes = integer(policy.session_idle_minutes, "CP_POLICY_FILE.session_idle_minutes");
  const session_absolute_minutes = integer(policy.session_absolute_minutes, "CP_POLICY_FILE.session_absolute_minutes");
  const login_max_attempts = integer(policy.login_max_attempts, "CP_POLICY_FILE.login_max_attempts");
  const login_window_minutes = integer(policy.login_window_minutes, "CP_POLICY_FILE.login_window_minutes");
  const recovery_max_attempts = integer(policy.recovery_max_attempts, "CP_POLICY_FILE.recovery_max_attempts");
  const recovery_window_minutes = integer(policy.recovery_window_minutes, "CP_POLICY_FILE.recovery_window_minutes");
  const invitation_expiry_minutes = integer(policy.invitation_expiry_minutes, "CP_POLICY_FILE.invitation_expiry_minutes");
  const reset_expiry_minutes = integer(policy.reset_expiry_minutes, "CP_POLICY_FILE.reset_expiry_minutes");
  const doctor_confirmation_expiry_minutes = integer(policy.doctor_confirmation_expiry_minutes, "CP_POLICY_FILE.doctor_confirmation_expiry_minutes");
  const signed_request_freshness_seconds = integer(policy.signed_request_freshness_seconds, "CP_POLICY_FILE.signed_request_freshness_seconds");
  const signed_request_max_skew_seconds = integer(policy.signed_request_max_skew_seconds, "CP_POLICY_FILE.signed_request_max_skew_seconds");
  const download_grant_expiry_seconds = integer(policy.download_grant_expiry_seconds, "CP_POLICY_FILE.download_grant_expiry_seconds");
  if (session_absolute_minutes <= session_idle_minutes) invalid("CP_POLICY_FILE.session_absolute_minutes");

  const files = record(policy.file_policy, "CP_POLICY_FILE.file_policy");
  const fileFields = [
    "allowed_extensions", "allowed_mime_types", "allowed_classes",
    "max_file_bytes", "max_files_per_record",
    "max_total_storage_bytes_per_record", "scanner_required", "quarantine_on_failure",
  ];
  if (Object.keys(files).length !== fileFields.length ||
      !fileFields.every((field) => Object.hasOwn(files, field))) invalid("CP_POLICY_FILE.file_policy");
  const file_policy: FilePolicy = {
    allowed_extensions: stringList(files.allowed_extensions, "CP_POLICY_FILE.file_policy.allowed_extensions", /^\.[a-z0-9]+(?:[a-z0-9_-]*[a-z0-9])?$/),
    allowed_mime_types: stringList(files.allowed_mime_types, "CP_POLICY_FILE.file_policy.allowed_mime_types", /^[a-z0-9][a-z0-9!#$&^_.+-]*\/[a-z0-9][a-z0-9!#$&^_.+-]*$/),
    allowed_classes: stringList(files.allowed_classes, "CP_POLICY_FILE.file_policy.allowed_classes", /^[a-z][a-z0-9]*(?:-[a-z0-9]+)*$/),
    max_file_bytes: integer(files.max_file_bytes, "CP_POLICY_FILE.file_policy.max_file_bytes"),
    max_files_per_record: integer(files.max_files_per_record, "CP_POLICY_FILE.file_policy.max_files_per_record"),
    max_total_storage_bytes_per_record: integer(files.max_total_storage_bytes_per_record, "CP_POLICY_FILE.file_policy.max_total_storage_bytes_per_record"),
    scanner_required: files.scanner_required as boolean,
    quarantine_on_failure: files.quarantine_on_failure as boolean,
  };
  if (typeof files.scanner_required !== "boolean") invalid("CP_POLICY_FILE.file_policy.scanner_required");
  if (typeof files.quarantine_on_failure !== "boolean") invalid("CP_POLICY_FILE.file_policy.quarantine_on_failure");

  return {
    profile: "synthetic",
    session_idle_minutes, session_absolute_minutes,
    login_max_attempts, login_window_minutes,
    recovery_max_attempts, recovery_window_minutes,
    invitation_expiry_minutes, reset_expiry_minutes,
    doctor_confirmation_expiry_minutes,
    signed_request_freshness_seconds, signed_request_max_skew_seconds,
    download_grant_expiry_seconds, file_policy,
  };
}

function privateHttpUrl(name: string): string {
  let url: URL;
  try {
    url = new URL(required(name));
  } catch {
    return invalid(name);
  }

  const host = url.hostname.toLowerCase();
  const parts = host.split(".");
  const ipv4 = parts.length === 4 && parts.every((part) => /^\d+$/.test(part) && Number(part) <= 255);
  const privateIp = ipv4 && (
    parts[0] === "10" ||
    parts[0] === "127" ||
    (parts[0] === "172" && Number(parts[1]) >= 16 && Number(parts[1]) <= 31) ||
    (parts[0] === "192" && parts[1] === "168")
  );
  const privateHost = host === "api" || host === "localhost" ||
    host === "[::1]" || privateIp;

  if (
    url.protocol !== "http:" || !privateHost ||
    url.username || url.password || url.pathname !== "/" ||
    url.search || url.hash
  ) invalid(name);

  return url.origin;
}

export function loadWebConfig() {
  const environment = choice<Environment>("CP_ENV", ["development", "test", "production"]);
  const synthetic = process.env.CP_SYNTHETIC_TEST_PROFILE;
  if (environment === "production") {
    if (synthetic === "true") invalid("CP_SYNTHETIC_TEST_PROFILE");
    // SECURITY-POLICY-NUMERICS is not approved: production always fails closed.
    return invalid("CP_ENV");
  }
  if (synthetic !== "true") invalid("CP_SYNTHETIC_TEST_PROFILE");

  const policy = policyFile();
  const database = {
    host: hostname("CP_DB_HOST"),
    port: port("CP_DB_PORT"),
    name: required("CP_DB_NAME"),
    user: required("CP_DB_USER"),
    password: secretFile("CP_DB_PASSWORD_FILE"),
  };
  const apiUrl = privateHttpUrl("CP_INTERNAL_API_URL");
  const serviceKey = secretFile("CP_SERVICE_KEY_FILE");
  const authSecret = secretFile("CP_AUTH_SECRET_FILE");
  const mailEnabled = choice("CP_MAIL_ENABLED", ["true", "false"]) === "true";
  const smtp = mailEnabled
    ? { host: hostname("CP_SMTP_HOST"), port: port("CP_SMTP_PORT") }
    : null;
  choice("CP_SCAN_ENABLED", ["false"]);

  return { environment, policy, database, apiUrl, serviceKey, authSecret, mailEnabled, smtp };
}

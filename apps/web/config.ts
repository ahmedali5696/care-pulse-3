import { lstatSync, readFileSync } from "node:fs";
import { isAbsolute } from "node:path";
import { loadMailSettings } from "./mail/config.ts";

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

const POLICY_NUMBERS = [
  "session_idle_minutes", "session_absolute_minutes",
  "login_max_attempts", "login_window_minutes",
  "recovery_max_attempts", "recovery_window_minutes",
  "invitation_expiry_minutes", "reset_expiry_minutes",
  "doctor_confirmation_expiry_minutes",
  "signed_request_freshness_seconds", "signed_request_max_skew_seconds",
  "download_grant_expiry_seconds",
] as const;

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

function signingKeyId(): string {
  const id = required("CP_SIGNING_KEY_ID");
  if (!/^[A-Za-z0-9_-]{1,128}$/.test(id)) invalid("CP_SIGNING_KEY_ID");
  return id;
}

function readProtectedFile(name: string, maxBytes: number): string {
  const filename = required(name);
  if (!isAbsolute(filename)) invalid(name);
  try {
    const info = lstatSync(filename);
    if (!info.isFile() || info.size > maxBytes ||
        (process.platform !== "win32" && (info.mode & 0o022) !== 0)) invalid(name);
    return readFileSync(filename, "utf8");
  } catch {
    return invalid(name);
  }
}

function secretFile(name: string): string {
  const value = readProtectedFile(name, 8192).replace(/\r?\n$/, "");
  if (
    value.length < 32 || value.length > 4096 ||
    value !== value.trim() || /[\x00-\x1f\x7f]/.test(value)
  ) invalid(name);
  return value;
}

function quotedStringEnd(json: string, start: number): number {
  let index = start + 1;
  while (index < json.length && json[index] !== '"') {
    if (json[index] === "\\") index++;
    index++;
  }
  return index;
}

function nextJsonToken(json: string, index: number): string | undefined {
  while (/\s/.test(json[index] ?? "")) index++;
  return json[index];
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
      index = quotedStringEnd(json, start);
      const scope = scopes[scopes.length - 1];
      if (scope && nextJsonToken(json, index + 1) === ":") {
        const key = JSON.parse(json.slice(start, index + 1)) as string;
        if (scope.has(key)) invalid("CP_POLICY_FILE");
        scope.add(key);
      }
    }
  }
}

function record(value: unknown, name: string, fields: readonly string[]): Record<string, unknown> {
  if (!value || typeof value !== "object" || Array.isArray(value)) invalid(name);
  if (Object.keys(value).length !== fields.length ||
      !fields.every((field) => Object.hasOwn(value, field))) invalid(name);
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

function readPolicy(): unknown {
  try {
    const json = readProtectedFile("CP_POLICY_FILE", 65536);
    const policy: unknown = JSON.parse(json);
    rejectDuplicateKeys(json);
    return policy;
  } catch {
    return invalid("CP_POLICY_FILE");
  }
}

function policyBoolean(policyValue: unknown, name: string): boolean {
  if (typeof policyValue !== "boolean") invalid(name);
  return policyValue;
}

function filePolicy(policyValue: unknown): FilePolicy {
  const files = record(policyValue, "CP_POLICY_FILE.file_policy", [
    "allowed_extensions", "allowed_mime_types", "allowed_classes",
    "max_file_bytes", "max_files_per_record",
    "max_total_storage_bytes_per_record", "scanner_required", "quarantine_on_failure",
  ]);
  return {
    allowed_extensions: stringList(files.allowed_extensions, "CP_POLICY_FILE.file_policy.allowed_extensions", /^\.[a-z0-9]+(?:[a-z0-9_-]*[a-z0-9])?$/),
    allowed_mime_types: stringList(files.allowed_mime_types, "CP_POLICY_FILE.file_policy.allowed_mime_types", /^[a-z0-9][a-z0-9!#$&^_.+-]*\/[a-z0-9][a-z0-9!#$&^_.+-]*$/),
    allowed_classes: stringList(files.allowed_classes, "CP_POLICY_FILE.file_policy.allowed_classes", /^[a-z][a-z0-9]*(?:-[a-z0-9]+)*$/),
    max_file_bytes: integer(files.max_file_bytes, "CP_POLICY_FILE.file_policy.max_file_bytes"),
    max_files_per_record: integer(files.max_files_per_record, "CP_POLICY_FILE.file_policy.max_files_per_record"),
    max_total_storage_bytes_per_record: integer(files.max_total_storage_bytes_per_record, "CP_POLICY_FILE.file_policy.max_total_storage_bytes_per_record"),
    scanner_required: policyBoolean(files.scanner_required, "CP_POLICY_FILE.file_policy.scanner_required"),
    quarantine_on_failure: policyBoolean(files.quarantine_on_failure, "CP_POLICY_FILE.file_policy.quarantine_on_failure"),
  };
}

function policyFile(): WebPolicy {
  const policy = record(readPolicy(), "CP_POLICY_FILE", ["profile", "file_policy", ...POLICY_NUMBERS]);
  if (policy.profile !== "synthetic") invalid("CP_POLICY_FILE.profile");
  const numbers = Object.fromEntries(POLICY_NUMBERS.map(
    (field) => [field, integer(policy[field], `CP_POLICY_FILE.${field}`)],
  )) as Record<(typeof POLICY_NUMBERS)[number], number>;
  if (numbers.session_absolute_minutes <= numbers.session_idle_minutes) invalid("CP_POLICY_FILE.session_absolute_minutes");
  return {
    profile: "synthetic",
    ...numbers,
    file_policy: filePolicy(policy.file_policy),
  };
}

function privateIpv4(host: string): boolean {
  const parts = host.split(".");
  if (parts.length !== 4 || !parts.every((part) => /^\d+$/.test(part) && Number(part) <= 255)) return false;
  return ["10", "127"].includes(parts[0]) ||
    (parts[0] === "172" && Number(parts[1]) >= 16 && Number(parts[1]) <= 31) ||
    (parts[0] === "192" && parts[1] === "168");
}

function privateHttpUrl(name: string): string {
  let url: URL;
  try {
    url = new URL(required(name));
  } catch {
    return invalid(name);
  }

  const host = url.hostname.toLowerCase();
  const privateHost = ["api", "localhost", "[::1]"].includes(host) || privateIpv4(host);

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
  const signingKeyIdValue = signingKeyId();
  const authSecret = secretFile("CP_AUTH_SECRET_FILE");
  const { mailEnabled, smtp } = loadMailSettings();
  choice("CP_SCAN_ENABLED", ["false"]);

  return { environment, policy, database, apiUrl, serviceKey, signingKeyId: signingKeyIdValue, authSecret, mailEnabled, smtp };
}

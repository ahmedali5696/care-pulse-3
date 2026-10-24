import { createPrivateKey } from "node:crypto";
import { lstatSync, readFileSync } from "node:fs";
import { isAbsolute } from "node:path";

function required(name: string): string {
  const input = process.env[name];
  if (!input || input !== input.trim() || input.length > 2048 || /[\x00-\x1f\x7f]/.test(input)) {
    throw new Error(`Invalid ${name}`);
  }
  return input;
}

function smtpPort(): number {
  const input = required("CP_SMTP_PORT");
  const parsed = Number(input);
  if (!/^[0-9]+$/.test(input) || !Number.isInteger(parsed) || parsed < 1 || parsed > 65535) {
    throw new Error("Invalid CP_SMTP_PORT");
  }
  return parsed;
}

export function loadMailSettings() {
  const enabled = required("CP_MAIL_ENABLED");
  if (enabled !== "true" && enabled !== "false") throw new Error("Invalid CP_MAIL_ENABLED");
  const smtp = enabled === "true"
    ? { host: required("CP_SMTP_HOST"), port: smtpPort() }
    : null;
  if (smtp && (smtp.host !== "mail" || smtp.port !== 1025)) {
    throw new Error("Synthetic mail unavailable");
  }
  return { mailEnabled: enabled === "true", smtp };
}

function privateDeliveryKeys(): Record<string, string> {
  const entries = required("CP_DELIVERY_PRIVATE_KEY_FILES").split(",");
  if (entries.length > 8) throw new Error("Invalid CP_DELIVERY_PRIVATE_KEY_FILES");
  const keys: Record<string, string> = Object.create(null) as Record<string, string>;
  for (const entry of entries) {
    const separator = entry.indexOf("=");
    const keyId = entry.slice(0, separator);
    const keyFile = entry.slice(separator + 1);
    if (separator < 1 || !/^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$/.test(keyId) ||
        Object.hasOwn(keys, keyId) || !isAbsolute(keyFile)) {
      throw new Error("Invalid CP_DELIVERY_PRIVATE_KEY_FILES");
    }
    const keyStat = lstatSync(keyFile);
    if (!keyStat.isFile() || keyStat.size > 8192 ||
        (process.platform !== "win32" && (keyStat.mode & 0o022) !== 0)) {
      throw new Error("Invalid CP_DELIVERY_PRIVATE_KEY_FILES");
    }
    const pem = readFileSync(keyFile, "utf8");
    if (!pem.startsWith("-----BEGIN PRIVATE KEY-----\n")) {
      throw new Error("Invalid delivery private key");
    }
    const key = createPrivateKey(pem);
    if (key.asymmetricKeyType !== "rsa" || key.asymmetricKeyDetails?.modulusLength !== 3072 ||
        key.asymmetricKeyDetails?.publicExponent !== BigInt(65537)) {
      throw new Error("Invalid delivery private key");
    }
    keys[keyId] = pem;
  }
  return keys;
}

export function loadSenderMailConfig() {
  if (!["development", "test"].includes(required("CP_ENV")) ||
      required("CP_SYNTHETIC_TEST_PROFILE") !== "true") {
    throw new Error("Unapproved sender environment");
  }
  const { smtp } = loadMailSettings();
  if (!smtp) throw new Error("Synthetic mail unavailable");
  const origin = new URL(required("CP_MAIL_BASE_URL"));
  if (!["http://localhost:3000", "http://127.0.0.1:3000"].includes(origin.origin) ||
      origin.href !== `${origin.origin}/`) {
    throw new Error("Invalid CP_MAIL_BASE_URL");
  }
  return { smtp, origin: origin.origin, privateKeysById: privateDeliveryKeys() };
}

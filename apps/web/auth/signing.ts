import { createHash, createHmac, randomBytes } from "node:crypto";

const PATH_SAFE = new Set("/-._~!$&'()*+,;=:@");
const TOKEN = /^[A-Za-z0-9_-]{1,128}$/;

function decoded(source: string): string {
  if (/%(?![0-9A-Fa-f]{2})/.test(source)) throw new Error("Invalid request URL");
  const plain = decodeURIComponent(source);
  if (/[\x00-\x1f\x7f-\x9f]/.test(plain)) throw new Error("Invalid request URL");
  return plain;
}

export function canonicalPath(rawPath: string): string {
  if (!rawPath.startsWith("/") || /[?#]/.test(rawPath) || /%2f/i.test(rawPath)) {
    throw new Error("Invalid request path");
  }
  const path = decoded(rawPath);
  if (path.includes("\\") || /%[0-9a-f]{2}/i.test(path) ||
      path.split("/").some((part, index, parts) =>
        part === "." || part === ".." || (part === "" && index > 0 && index < parts.length - 1))) {
    throw new Error("Ambiguous request path");
  }
  return encodeURIComponent(path).replace(/%([0-9A-F]{2})/g, (escaped, code: string) => {
    const char = String.fromCharCode(parseInt(code, 16));
    return PATH_SAFE.has(char) ? char : escaped;
  });
}

function queryPart(input: string): string {
  return encodeURIComponent(decoded(input.replace(/\+/g, "%20")))
    .replace(/[!'()*]/g, (char) => `%${char.charCodeAt(0).toString(16).toUpperCase()}`);
}

export function canonicalQuery(rawQuery: string): string {
  if (!rawQuery) return "";
  const pairs = rawQuery.split("&").map((pair) => {
    if (!pair) throw new Error("Empty query pair");
    const equal = pair.indexOf("=");
    return [queryPart(equal < 0 ? pair : pair.slice(0, equal)),
      queryPart(equal < 0 ? "" : pair.slice(equal + 1))] as const;
  });
  pairs.sort(([a, b], [c, d]) => a < c ? -1 : a > c ? 1 : b < d ? -1 : b > d ? 1 : 0);
  return pairs.map(([key, val]) => `${key}=${val}`).join("&");
}

export type SignedRequest = {
  method: string;
  rawPath: string;
  rawQuery: string;
  body: Uint8Array;
  organizationId: string;
  staffId: string;
  sessionId: string;
  role: string;
  securityRevision: number;
  purpose: string;
  idempotencyKey?: string;
  keyId: string;
  serviceKey: string;
  timestamp?: number;
  nonce?: string;
};

export function signRequest(request: SignedRequest) {
  if (!TOKEN.test(request.keyId) || !/^[A-Z]{1,16}$/.test(request.method)) {
    throw new Error("Invalid request signing input");
  }
  const path = canonicalPath(request.rawPath);
  const query = canonicalQuery(request.rawQuery);
  const timestamp = String(request.timestamp ?? Date.now());
  const nonce = request.nonce ?? randomBytes(24).toString("base64url");
  const fields = [
    "carepulse-request-v1", request.keyId, "web", request.method, path, query,
    createHash("sha256").update(request.body).digest("hex"),
    request.organizationId, request.staffId, request.sessionId, request.role,
    String(request.securityRevision), timestamp, nonce, request.purpose,
    request.idempotencyKey ?? "",
  ];
  if (!TOKEN.test(nonce) || nonce.length < 22 || !/^(0|[1-9][0-9]*)$/.test(timestamp) ||
      fields.some((field) => field.length > 4096 || /[\x00-\x1f\x7f-\x9f\uD800-\uDFFF]/.test(field))) {
    throw new Error("Invalid request signing input");
  }
  const signature = createHmac("sha256", request.serviceKey)
    .update(JSON.stringify(fields), "utf8").digest("hex");
  const headerNames = ["protocol-version", "key-id", "service", "organization-id", "staff-id",
    "session-id", "role", "security-revision", "timestamp", "nonce", "purpose", "idempotency-key"];
  const headers = new Headers();
  const values = [fields[0], fields[1], fields[2], ...fields.slice(7)];
  for (let i = 0; i < headerNames.length; i++) {
    headers.set(`X-CarePulse-${headerNames[i]}`, values[i]);
  }
  headers.set("X-CarePulse-Signature", signature);
  headers.set("X-CarePulse-Service-Key", request.serviceKey);
  return { path, query, headers, signature, fields };
}

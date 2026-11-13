import { loadWebConfig } from "../config.ts";
import { currentActor } from "./session.ts";
import { failure, validMutation } from "./http.ts";
import { canonicalPath, signRequest } from "./signing.ts";

type ApprovedRoute = { path: string; purpose: string; method: string };
const MAX_MUTATION_BODY_BYTES = 1_048_576;

export async function boundedBody(request: Request): Promise<Uint8Array | null> {
  const declared = request.headers.get("content-length");
  if (declared !== null) {
    const digits = declared.replace(/^0+/, "") || "0";
    if (!/^[0-9]+$/.test(declared) || digits.length > 7 ||
        (digits.length === 7 && digits > "1048576")) return null;
  }
  if (!request.body) return new Uint8Array();
  const reader = request.body.getReader();
  const chunks: Uint8Array[] = [];
  let size = 0;
  try {
    while (true) {
      const { done, value } = await reader.read();
      if (done) break;
      size += value.byteLength;
      if (size > MAX_MUTATION_BODY_BYTES) return null;
      chunks.push(value);
    }
    const body = new Uint8Array(size);
    let offset = 0;
    for (const chunk of chunks) { body.set(chunk, offset); offset += chunk.byteLength; }
    return body;
  } finally {
    if (size > MAX_MUTATION_BODY_BYTES) await reader.cancel();
    reader.releaseLock();
  }
}

// Every browser-visible operation has one explicit route/method/purpose owner.
// B-03–05 will register their own approved routes when their handlers exist.
export async function forwardPrivate(request: Request, route: ApprovedRoute): Promise<Response> {
  if (request.method !== route.method || !route.path.startsWith("/v1/")) return failure(403);
  if (request.method !== "GET" && !validMutation(request)) return failure(403);
  let actor;
  try { actor = await currentActor(request); } catch { return failure(503); }
  if (!actor) return failure(401);
  try {
    const config = loadWebConfig();
    const path = canonicalPath(route.path);
    const rawQuery = new URL(request.url).search.slice(1);
    const body = await boundedBody(request);
    if (body === null) return failure(422);
    const idempotencyKey = request.headers.get("idempotency-key") ?? "";
    if (idempotencyKey.length > 128 || /[\x00-\x1f\x7f-\x9f]/.test(idempotencyKey)) return failure(422);
    const signed = signRequest({
      method: request.method, rawPath: path, rawQuery, body,
      organizationId: actor.organizationId, staffId: actor.staffId,
      sessionId: actor.sessionId, role: actor.role,
      securityRevision: actor.securityRevision, purpose: route.purpose,
      idempotencyKey, keyId: config.signingKeyId, serviceKey: config.serviceKey,
    });
    const headers = signed.headers;
    const contentType = request.headers.get("content-type");
    if (contentType) headers.set("Content-Type", contentType);
    const target = `${config.apiUrl}${signed.path}${signed.query ? `?${signed.query}` : ""}`;
    const response = await fetch(target, {
      method: request.method, headers, body: request.method === "GET" ? undefined : Buffer.from(body),
      cache: "no-store", redirect: "manual", signal: AbortSignal.timeout(5000),
    });
    if (response.status >= 300 && response.status < 400) return failure(503);
    const safeHeaders = new Headers({ "Cache-Control": "no-store" });
    const type = response.headers.get("content-type");
    if (type?.startsWith("application/json")) safeHeaders.set("Content-Type", "application/json");
    return new Response(response.body, { status: response.status, headers: safeHeaders });
  } catch {
    return failure(503);
  }
}

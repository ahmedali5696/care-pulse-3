import { getAuth } from "../../../../../auth.ts";
import { currentActor } from "../../../../../auth/session.ts";
import { failure, validMutation } from "../../../../../auth/http.ts";
import { forwardPrivate } from "../../../../../auth/proxy.ts";

export const dynamic = "force-dynamic";
export const runtime = "nodejs";

const UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;
type RouteContext = { params: Promise<{ path?: string[] }> };

async function preparedReceptionist(request: Request): Promise<Request | Response> {
  if (!validMutation(request)) return failure(403);
  if (!/^[A-Za-z0-9._:-]{1,128}$/.test(request.headers.get("idempotency-key") ?? "")) {
    return failure(422);
  }
  const actor = await currentActor(request);
  if (!actor || actor.role !== "DOCTOR_OWNER") return failure(403);
  let body: unknown;
  try {
    const raw = await request.text();
    if (raw.length > 8192) return failure(422);
    body = JSON.parse(raw);
  } catch {
    return failure(422);
  }
  if (!body || typeof body !== "object" || Array.isArray(body)) return failure(422);
  const fields = body as Record<string, unknown>;
  if (Object.keys(fields).sort().join(",") !== "email,name,reason") return failure(422);
  const { email, name, reason } = fields;
  if (typeof email !== "string" || !/^[^@\s]+@[^@\s]+\.[^@\s]+$/.test(email.trim()) ||
      email.length > 254 || typeof name !== "string" || !name.trim() || name.length > 128 ||
      /[\x00-\x1f\x7f-\x9f]/.test(name) ||
      typeof reason !== "string" || !reason.trim() || reason.length > 512 ||
      /[\x00-\x1f\x7f-\x9f]/.test(reason)) return failure(422);

  const canonicalEmail = email.trim().toLowerCase();
  const context = await getAuth().$context;
  const existing = await context.internalAdapter.findUserByEmail(canonicalEmail, { includeAccounts: true });
  let preparedUserId = existing?.user.id;
  if (!preparedUserId) {
    try {
      preparedUserId = (await context.internalAdapter.createUser({
        email: canonicalEmail, name: name.trim(), emailVerified: false,
      }, { method: "email-password" })).id;
    } catch (error) {
      const pg = error as { code?: unknown; constraint?: unknown };
      if (pg?.code !== "23505" || pg.constraint !== "user_email_key") throw error;
      const winner = await context.internalAdapter.findUserByEmail(canonicalEmail);
      if (!winner) throw error;
      preparedUserId = winner.user.id;
    }
  }
  const safe = JSON.stringify({
    email: canonicalEmail, name: name.trim(), reason: reason.trim(),
    prepared_user_id: preparedUserId,
  });
  return new Request(request.url, { method: "POST", headers: request.headers, body: safe });
}

async function delegate(request: Request, context: RouteContext): Promise<Response> {
  const { path = [] } = await context.params;
  let route: string;
  if (path.length === 0 && (request.method === "GET" || request.method === "POST")) {
    route = "/v1/staff/receptionists";
  } else if (path.length === 1 && UUID.test(path[0]) && request.method === "GET") {
    route = `/v1/staff/receptionists/${path[0]}`;
  } else if (path.length === 2 && UUID.test(path[0]) && (
    (request.method === "POST" && ["enable", "disable", "invite", "revoke-invite"].includes(path[1])) ||
    (request.method === "PATCH" && path[1] === "permissions")
  )) {
    route = `/v1/staff/receptionists/${path[0]}/${path[1]}`;
  } else {
    return failure(403);
  }
  try {
    const forwarded = path.length === 0 && request.method === "POST"
      ? await preparedReceptionist(request) : request;
    if (forwarded instanceof Response) return forwarded;
    return forwardPrivate(forwarded, { path: route, purpose: "STAFF_MANAGEMENT", method: request.method });
  } catch {
    return failure(503);
  }
}

export const GET = delegate;
export const POST = delegate;
export const PATCH = delegate;

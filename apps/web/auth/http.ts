import { randomUUID } from "node:crypto";
import { APIError } from "better-auth/api";
import { getAuth } from "../auth.ts";
import { captureLoginAuthority, currentActor, withLoginAuthority } from "./session.ts";
import { allowLoginAttempt } from "./throttle.ts";

export function failure(status: 401 | 403 | 409 | 422 | 429 | 503): Response {
  const errors = {
    401: ["UNAUTHENTICATED", "Authentication required"],
    403: ["FORBIDDEN", "Operation unavailable"],
    409: ["CONFLICT", "Operation conflict"],
    422: ["INVALID_INPUT", "Invalid request"],
    429: ["RATE_LIMITED", "Try again later"],
    503: ["UNAVAILABLE", "Service unavailable"],
  } as const;
  const [code, message] = errors[status];
  return Response.json({ error: { code, message, request_id: randomUUID() } }, {
    status, headers: { "Cache-Control": "no-store" },
  });
}

export function json(data: unknown): Response {
  return Response.json({ data }, { headers: { "Cache-Control": "no-store" } });
}

export function validMutation(request: Request): boolean {
  const origin = request.headers.get("origin");
  const from = request.headers.get("sec-fetch-site");
  const host = request.headers.get("host");
  if (!origin || !host || request.headers.get("x-requested-with") !== "XMLHttpRequest" ||
      (from !== null && from !== "same-origin")) return false;
  try {
    const source = new URL(origin);
    return source.origin === origin && source.host === host &&
      source.protocol === new URL(request.url).protocol;
  } catch {
    return false;
  }
}

export async function activeActor(request: Request) {
  try {
    const actor = await currentActor(request);
    return actor ?? failure(401);
  } catch {
    return failure(503);
  }
}

export async function sessionResponse(request: Request): Promise<Response> {
  const actor = await activeActor(request);
  if (actor instanceof Response) return actor;
  return json({
    id: actor.staffId, organization_id: actor.organizationId,
    role: actor.role, bulk_reschedule: actor.bulkReschedule,
    revision: actor.staffRevision,
  });
}

export async function loginResponse(request: Request): Promise<Response> {
  if (!validMutation(request)) return failure(403);
  let body: unknown;
  try { body = await request.json(); } catch { return failure(422); }
  if (!body || typeof body !== "object" || Array.isArray(body) ||
      Object.keys(body).sort().join(",") !== "email,password") return failure(422);
  const { email, password } = body as Record<string, unknown>;
  if (typeof email !== "string" || typeof password !== "string" ||
      !email || !password) return failure(422);
  try {
    if (!await allowLoginAttempt(email)) return failure(429);
    const capture = await captureLoginAuthority(email);
    if (!capture) return failure(401);
    const native = await withLoginAuthority(capture, () => getAuth().api.signInEmail({
      body: { email, password, rememberMe: false },
      headers: new Headers({ "user-agent": request.headers.get("user-agent") ?? "" }),
      asResponse: true,
    }));
    if (!native.ok) return failure(401);
    const issued = await native.clone().json() as { token?: string };
    const cookies = native.headers.getSetCookie();
    const cookie = cookies.map((header) => header.split(";", 1)[0]).join("; ");
    const actor = await currentActor(new Request(request.url, { headers: { cookie } }));
    if (!actor || actor.userId !== capture.userId ||
        actor.securityRevision !== capture.securityRevision) {
      if (issued.token) {
        const { runtimeDatabase } = await import("../db.ts");
        const { loadWebConfig } = await import("../config.ts");
        await runtimeDatabase(loadWebConfig()).query('DELETE FROM public."session" WHERE token = $1', [issued.token]);
      }
      return failure(401);
    }
    const reply = json({ id: actor.staffId, organization_id: actor.organizationId,
      role: actor.role, bulk_reschedule: actor.bulkReschedule, revision: actor.staffRevision });
    for (const setCookie of cookies) reply.headers.append("Set-Cookie", setCookie);
    return reply;
  } catch (error) {
    return failure(error instanceof APIError ? 401 : 503);
  }
}

export async function logoutResponse(request: Request): Promise<Response> {
  if (!validMutation(request)) return failure(403);
  try {
    const headers = new Headers({ cookie: request.headers.get("cookie") ?? "" });
    const native = await getAuth().api.signOut({ headers, asResponse: true });
    const stillExists = await getAuth().api.getSession({ headers, query: { disableRefresh: true } });
    if (stillExists) return failure(503);
    const reply = json({ success: true });
    for (const setCookie of native.headers.getSetCookie()) reply.headers.append("Set-Cookie", setCookie);
    return reply;
  } catch {
    return failure(503);
  }
}

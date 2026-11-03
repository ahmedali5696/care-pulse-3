import { failure } from "../../../../auth/http.ts";
import { forwardPrivate } from "../../../../auth/proxy.ts";

export const dynamic = "force-dynamic";
export const runtime = "nodejs";

const UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;
type RouteContext = { params: Promise<{ path?: string[] }> };

async function delegate(request: Request, context: RouteContext): Promise<Response> {
  const segments = (await context.params).path ?? [];
  const allowed = segments.length === 1 && UUID.test(segments[0]) ? ["PATCH"] :
    segments.length === 2 && UUID.test(segments[0]) && segments[1] === "extend-followup" ? ["POST"] : [];
  if (!allowed.includes(request.method)) return failure(403);
  return forwardPrivate(request, {
    path: `/v1/visits/${segments.join("/")}`, purpose: "VISIT_FOLLOWUP", method: request.method,
  });
}

export const PATCH = delegate;
export const POST = delegate;

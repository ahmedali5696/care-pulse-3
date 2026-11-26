import { failure } from "../../../../auth/http.ts";
import { forwardPrivate } from "../../../../auth/proxy.ts";
import { visitPatchPurpose } from "../../../../auth/visit-patch.ts";

export const dynamic = "force-dynamic";
export const runtime = "nodejs";

const UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;
type RouteContext = { params: Promise<{ path?: string[] }> };

async function delegate(request: Request, context: RouteContext): Promise<Response> {
  const segments = (await context.params).path ?? [];
  if (!UUID.test(segments[0] ?? "")) return failure(403);
  const purpose = segments.length === 1 && request.method === "PATCH" ? visitPatchPurpose :
    segments.length === 1 && request.method === "GET" ? "VISIT_READ" :
    segments.length !== 2 || request.method !== "POST" ? null :
    segments[1] === "extend-followup" ? "VISIT_FOLLOWUP" :
    segments[1] === "complete" ? "VISIT_COMPLETE" :
    segments[1] === "cancel-exceptional" ? "VISIT_CANCEL_EXCEPTIONAL" : null;
  if (!purpose) return failure(403);
  return forwardPrivate(request, {
    path: `/v1/visits/${segments.join("/")}`, purpose, method: request.method,
  });
}

export const PATCH = delegate;
export const POST = delegate;
export const GET = delegate;
export const PUT = delegate;
export const DELETE = delegate;
export const HEAD = delegate;
export const OPTIONS = delegate;

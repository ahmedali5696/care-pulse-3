import { failure } from "../../../../auth/http.ts";
import { forwardPrivate } from "../../../../auth/proxy.ts";

export const dynamic = "force-dynamic";
export const runtime = "nodejs";

const UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;
type RouteContext = { params: Promise<{ path?: string[] }> };

async function delegate(request: Request, context: RouteContext): Promise<Response> {
  const segments = (await context.params).path ?? [];
  const allowed = segments.length === 0 ? ["GET", "POST"] :
    segments.length === 1 && UUID.test(segments[0]) ? ["GET", "PATCH"] :
    segments.length === 2 && UUID.test(segments[0]) &&
      ["archive", "unarchive"].includes(segments[1]) ? ["POST"] : [];
  if (!allowed.includes(request.method)) return failure(403);
  const path = `/v1/patients${segments.length ? `/${segments.join("/")}` : ""}`;
  return forwardPrivate(request, { path, purpose: "PATIENT_REGISTRY", method: request.method });
}

export const GET = delegate;
export const POST = delegate;
export const PATCH = delegate;

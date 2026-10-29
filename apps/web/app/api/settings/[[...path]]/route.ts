import { failure } from "../../../../auth/http.ts";
import { forwardPrivate } from "../../../../auth/proxy.ts";

export const dynamic = "force-dynamic";
export const runtime = "nodejs";

type RouteContext = { params: Promise<{ path?: string[] }> };

async function delegate(request: Request, context: RouteContext): Promise<Response> {
  const segments = (await context.params).path ?? [];
  if (segments.length !== 1 || segments[0] !== "pricing" ||
      (request.method !== "GET" && request.method !== "PATCH")) return failure(403);
  return forwardPrivate(request, {
    path: "/v1/settings/pricing", purpose: "SETTINGS_PRICING", method: request.method,
  });
}

export const GET = delegate;
export const PATCH = delegate;
export const POST = delegate;
export const PUT = delegate;
export const DELETE = delegate;
export const HEAD = delegate;
export const OPTIONS = delegate;

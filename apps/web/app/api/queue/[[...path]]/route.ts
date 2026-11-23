import { failure } from "../../../../auth/http.ts";
import { forwardPrivate } from "../../../../auth/proxy.ts";

export const dynamic = "force-dynamic";
export const runtime = "nodejs";

type RouteContext = { params: Promise<{ path?: string[] }> };

async function delegate(request: Request, context: RouteContext): Promise<Response> {
  const segments = (await context.params).path ?? [];
  if (segments.length !== 0 || request.method !== "GET") return failure(403);
  return forwardPrivate(request, { path: "/v1/queue", purpose: "QUEUE_READ", method: request.method });
}

export const GET = delegate;

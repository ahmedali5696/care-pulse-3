import { failure } from "../../../../auth/http.ts";
import { forwardPrivate } from "../../../../auth/proxy.ts";

export const dynamic = "force-dynamic";
export const runtime = "nodejs";

type RouteContext = { params: Promise<{ path?: string[] }> };

const UUID = /^[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}$/;

function queuePurpose(segments: string[], method: string): string | null {
  if (segments.length === 0 && method === "GET") return "QUEUE_READ";
  if (segments.length === 1 && segments[0] === "capacity" && method === "PATCH") return "QUEUE_CAPACITY_UPDATE";
  if (segments.length === 1 && segments[0] === "pause" && method === "POST") return "QUEUE_PAUSE";
  if (segments.length === 1 && segments[0] === "admit" && method === "POST") return "QUEUE_ADMIT";
  if (segments[0] !== "admission-confirmations") return null;
  if (segments.length === 1 && method === "POST") return "QUEUE_CONFIRMATION_ISSUE";
  if (segments.length === 2 && segments[1] === "usable" && method === "GET") return "QUEUE_CONFIRMATION_USABLE";
  if (segments.length === 3 && UUID.test(segments[1]) && segments[2] === "revoke" && method === "POST") return "QUEUE_CONFIRMATION_REVOKE";
  return null;
}

async function delegate(request: Request, context: RouteContext): Promise<Response> {
  const segments = (await context.params).path ?? [];
  const purpose = queuePurpose(segments, request.method);
  if (!purpose) return failure(403);
  return forwardPrivate(request, {
    path: ["/v1/queue", ...segments].join("/"), purpose, method: request.method,
  });
}

export const GET = delegate;
export const PATCH = delegate;
export const POST = delegate;

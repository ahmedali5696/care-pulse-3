import { failure } from "../../../../auth/http.ts";
import { forwardPrivate } from "../../../../auth/proxy.ts";

export const dynamic = "force-dynamic";
export const runtime = "nodejs";

type RouteContext = { params: Promise<{ path?: string[] }> };

async function delegate(request: Request, context: RouteContext): Promise<Response> {
  const segments = (await context.params).path ?? [];
  const isSettingsReadOrPatch = segments.length === 1 &&
    ["pricing", "schedule"].includes(segments[0]) &&
    (request.method === "GET" || request.method === "PATCH");
  const isScheduleAction = segments.length === 2 && segments[0] === "schedule" &&
    ["preview", "activate"].includes(segments[1]) && request.method === "POST";
  if (!isSettingsReadOrPatch && !isScheduleAction) return failure(403);
  return forwardPrivate(request, {
    path: `/v1/settings/${segments.join("/")}`,
    purpose: segments[0] === "pricing" ? "SETTINGS_PRICING" : "SETTINGS_SCHEDULE",
    method: request.method,
  });
}

export const GET = delegate;
export const PATCH = delegate;
export const POST = delegate;
export const PUT = delegate;
export const DELETE = delegate;
export const HEAD = delegate;
export const OPTIONS = delegate;

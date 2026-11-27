import { failure } from "../../../../auth/http.ts";
import { forwardPrivate } from "../../../../auth/proxy.ts";

export const dynamic = "force-dynamic";
export const runtime = "nodejs";

const UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;
type RouteContext = { params: Promise<{ path?: string[] }> };

async function delegate(request: Request, context: RouteContext): Promise<Response> {
  const segments = (await context.params).path ?? [];
  if (segments.length === 2 && segments[0] === "bulk-reschedule" &&
      (segments[1] === "preview" || segments[1] === "commit")) {
    if (request.method !== "POST") return failure(403);
    return forwardPrivate(request, {
      path: `/v1/appointments/bulk-reschedule/${segments[1]}`,
      purpose: segments[1] === "preview" ? "APPOINTMENT_BULK_PREVIEW" : "APPOINTMENT_BULK_COMMIT",
      method: request.method,
    });
  }
  if (segments.length === 2 && UUID.test(segments[0])) {
    const purpose = segments[1] === "check-in" ? "QUEUE_CHECK_IN" :
      segments[1] === "cancel" ? "APPOINTMENT_CANCEL" :
      segments[1] === "replace-urgent" ? "APPOINTMENT_REPLACE_URGENT" :
      segments[1] === "reschedule" ? "APPOINTMENT_RESCHEDULE" :
      segments[1] === "manual-no-show" ? "APPOINTMENT_MANUAL_NO_SHOW" : null;
    if (request.method !== "POST" || !purpose) return failure(403);
    return forwardPrivate(request, {
      path: `/v1/appointments/${segments[0]}/${segments[1]}`,
      purpose,
      method: request.method,
    });
  }
  const allowed = segments.length === 0 ? ["GET", "POST"] :
    segments.length === 1 && UUID.test(segments[0]) ? ["GET"] : [];
  if (!allowed.includes(request.method)) return failure(403);
  const path = `/v1/appointments${segments.length ? `/${segments[0]}` : ""}`;
  return forwardPrivate(request, { path, purpose: "APPOINTMENT_BOOKING", method: request.method });
}

export const GET = delegate;
export const POST = delegate;

import { failure } from "../../../../auth/http.ts";
import { forwardPrivate } from "../../../../auth/proxy.ts";

export const dynamic = "force-dynamic";
export const runtime = "nodejs";

type RouteContext = { params: Promise<{ path?: string[] }> };

export async function POST(request: Request, context: RouteContext): Promise<Response> {
  const segments = (await context.params).path ?? [];
  const [kind] = segments;
  if (segments.length !== 1 || !["routine", "emergency", "urgent"].includes(kind) || request.method !== "POST") {
    return failure(403);
  }
  return forwardPrivate(request, { path: `/v1/intake/${kind}`, purpose: `INTAKE_${kind.toUpperCase()}`, method: "POST" });
}

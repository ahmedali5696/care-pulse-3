import { failure } from "../../../../auth/http.ts";
import { forwardPrivate } from "../../../../auth/proxy.ts";

export const dynamic = "force-dynamic";
export const runtime = "nodejs";

export async function GET(request: Request, context: { params: Promise<{ path?: string[] }> }) {
  if ((await context.params).path?.length) return failure(403);
  return forwardPrivate(request, { path: "/v1/reconciliation", purpose: "RECONCILIATION_READ", method: "GET" });
}

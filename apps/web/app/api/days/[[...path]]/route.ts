import { failure } from "../../../../auth/http.ts";
import { forwardPrivate } from "../../../../auth/proxy.ts";

export const dynamic = "force-dynamic";
export const runtime = "nodejs";

export async function POST(request: Request, context: { params: Promise<{ path?: string[] }> }) {
  const path = (await context.params).path ?? [];
  if (path.length !== 2 || !/^[0-9]{4}-[0-9]{2}-[0-9]{2}$/.test(path[0])) return failure(403);
  const purpose = path[1] === "review" ? "DAY_REVIEW" : path[1] === "close" ? "DAY_CLOSE" : null;
  if (!purpose) return failure(403);
  return forwardPrivate(request, { path: `/v1/days/${path[0]}/${path[1]}`, purpose, method: "POST" });
}

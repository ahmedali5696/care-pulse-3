import { randomUUID } from "node:crypto";
import { getAuth } from "../../../../auth";

export const dynamic = "force-dynamic";
export const runtime = "nodejs";

async function deny(request: Request) {
  let status = 401;
  try {
    // Only the cookie reaches Better Auth; no browser authority headers survive.
    const headers = new Headers();
    headers.set("cookie", request.headers.get("cookie") ?? "");
    const session = await getAuth().api.getSession({ headers });
    if (session) status = 403;
  } catch {
    status = 503;
  }
  // No forwarding exists until D-02/B-01/B-02 provide live identity and replay checks.
  const code = status === 401 ? "UNAUTHENTICATED" : status === 403 ? "FORBIDDEN" : "UNAVAILABLE";
  const message = status === 401 ? "Authentication required" : status === 403 ? "Operation unavailable" : "Service unavailable";
  return Response.json({ error: { code, message, request_id: randomUUID() } }, {
    status, headers: { "Cache-Control": "no-store" },
  });
}

export { deny as GET, deny as POST, deny as PUT, deny as PATCH, deny as DELETE, deny as HEAD, deny as OPTIONS };

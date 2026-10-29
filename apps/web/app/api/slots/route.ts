import { failure } from "../../../auth/http.ts";
import { forwardPrivate } from "../../../auth/proxy.ts";

export const dynamic = "force-dynamic";
export const runtime = "nodejs";

export const GET = (request: Request) => forwardPrivate(request, {
  path: "/v1/slots", purpose: "SLOTS", method: "GET",
});

const denied = () => failure(403);
export const POST = denied;
export const PATCH = denied;
export const PUT = denied;
export const DELETE = denied;
export const HEAD = denied;
export const OPTIONS = denied;

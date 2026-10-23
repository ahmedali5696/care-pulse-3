import { activeActor, failure } from "../../../../auth/http.ts";

export const dynamic = "force-dynamic";
export const runtime = "nodejs";

async function deny(request: Request) {
  const actor = await activeActor(request);
  return actor instanceof Response ? actor : failure(403);
}

export { deny as GET, deny as POST, deny as PUT, deny as PATCH, deny as DELETE, deny as HEAD, deny as OPTIONS };

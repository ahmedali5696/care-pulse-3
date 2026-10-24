import { activeActor, failure } from "../../../../auth/http.ts";

export const dynamic = "force-dynamic";
export const runtime = "nodejs";

export async function GET(request: Request) {
  const actor = await activeActor(request);
  return actor instanceof Response ? actor : failure(403);
}

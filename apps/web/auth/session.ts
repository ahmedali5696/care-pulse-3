import { AsyncLocalStorage } from "node:async_hooks";
import type { PoolClient } from "pg";
import { loadWebConfig } from "../config.ts";
import { runtimeDatabase } from "../db.ts";
import { getAuth } from "../auth.ts";

type Authority = {
  userId: string;
  staffId: string;
  organizationId: string;
  role: "DOCTOR_OWNER" | "RECEPTIONIST";
  securityRevision: number;
  staffRevision: number;
  bulkReschedule: boolean;
};

export type CurrentActor = Authority & { sessionId: string };
const loginAuthority = new AsyncLocalStorage<Authority>();

async function resolveTenant(userId: string): Promise<string | null> {
  const lookup = await runtimeDatabase(loadWebConfig()).query<{ tenant: string }>(
    "SELECT public.resolve_login_tenant($1) AS tenant", [userId]);
  return lookup.rows[0]?.tenant ?? null;
}

async function inTenant<T>(tenant: string, action: (client: PoolClient) => Promise<T>): Promise<T | null> {
  const client = await runtimeDatabase(loadWebConfig()).connect();
  try {
    await client.query("BEGIN");
    await client.query("SELECT set_config('app.organization_id', $1, true)", [tenant]);
    const locked = await client.query<{ active: boolean }>(
      "SELECT public.lock_active_organization($1::uuid) AS active", [tenant]);
    if (!locked.rows[0]?.active) {
      await client.query("COMMIT");
      return null;
    }
    const answer = await action(client);
    await client.query("COMMIT");
    return answer;
  } catch (error) {
    await client.query("ROLLBACK");
    throw error;
  } finally {
    client.release();
  }
}

const liveStaff = `
  SELECT s.id AS "staffId", s.organization_id AS "organizationId",
         s.auth_user_id AS "userId", s.role, s.security_revision::integer AS "securityRevision",
         s.revision::integer AS "staffRevision", s.bulk_reschedule AS "bulkReschedule"
    FROM public.staff_accounts s
    JOIN public.organizations o ON o.id = s.organization_id
    JOIN public."user" u ON u.id = s.auth_user_id
   WHERE s.auth_user_id = $1 AND o.state = 'ACTIVE'
     AND s.enabled AND s.verified AND s.reset_block_operation_id IS NULL
     AND u."emailVerified" AND u.email = s.verified_email`;

export async function captureLoginAuthority(email: string): Promise<Authority | null> {
  const db = runtimeDatabase(loadWebConfig());
  const native = await db.query<{ id: string }>(
    'SELECT id FROM public."user" WHERE lower(email) = lower($1) AND "emailVerified"', [email]);
  if (native.rows.length !== 1) return null;
  const tenant = await resolveTenant(native.rows[0].id);
  if (!tenant) return null;
  return inTenant(tenant, async (client) => {
    const query = await client.query<Authority>(liveStaff, [native.rows[0].id]);
    return query.rows[0] ?? null;
  });
}

export function withLoginAuthority<T>(authority: Authority, action: () => Promise<T>) {
  return loginAuthority.run(authority, action);
}

// Supported Better Auth session.create.before hook; a direct unguarded sign-in
// call has no pre-verification snapshot and therefore cannot create a session.
export async function bindSessionAuthority(session: { userId: string }) {
  const captured = loginAuthority.getStore();
  if (!captured || captured.userId !== session.userId) return false;
  const current = await inTenant(captured.organizationId, async (client) =>
    client.query<Authority>(liveStaff, [session.userId]));
  if (current?.rows[0]?.securityRevision !== captured.securityRevision ||
      current?.rows[0]?.staffRevision !== captured.staffRevision ||
      current?.rows[0]?.organizationId !== captured.organizationId) return false;
  const now = new Date();
  const absoluteDeadline = new Date(now.getTime() + loadWebConfig().policy.session_absolute_minutes * 60_000);
  return { data: {
    securityRevision: captured.securityRevision, absoluteDeadline, lastActivity: now,
    expiresAt: absoluteDeadline,
  } };
}

async function authorizedSession(client: PoolClient, userId: string, sessionId: string) {
  const staff = await client.query<Authority>(`${liveStaff} FOR UPDATE OF s`, [userId]);
  const actor = staff.rows[0];
  if (!actor) return null;
  const policy = loadWebConfig().policy;
  const updated = await client.query<{ id: string }>(`
    WITH now_clock AS (SELECT clock_timestamp() AS moment)
    UPDATE public."session" sess
       SET "lastActivity" = GREATEST(sess."lastActivity", now_clock.moment)
      FROM now_clock
     WHERE sess.id = $1 AND sess."userId" = $2
       AND sess."securityRevision" = $3
       AND now_clock.moment < sess."expiresAt"
       AND now_clock.moment < sess."absoluteDeadline"
       AND now_clock.moment < sess."lastActivity" + ($4 * interval '1 minute')
    RETURNING sess.id`, [sessionId, userId, actor.securityRevision, policy.session_idle_minutes]);
  return updated.rowCount === 1 ? { ...actor, sessionId } : null;
}

export async function currentActor(request: Request): Promise<CurrentActor | null> {
  const cookie = request.headers.get("cookie") ?? "";
  const native = await getAuth().api.getSession({
    headers: new Headers({ cookie }), query: { disableRefresh: true, disableCookieCache: true },
  });
  if (!native) return null;
  const tenant = await resolveTenant(native.user.id);
  if (!tenant) return null;
  return inTenant(tenant, (client) => authorizedSession(client, native.user.id, native.session.id));
}

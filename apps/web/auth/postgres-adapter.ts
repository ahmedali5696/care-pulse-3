// Better Auth's public adapter API, backed by the existing least-privilege pool.
// Only native B-02/B-03 models are available in Stage 1.
import { AsyncLocalStorage } from "node:async_hooks";
import { createAdapterFactory, type CleanedWhere, type CustomAdapter, type DBAdapter } from "better-auth/adapters";
import type { Pool, PoolClient } from "pg";

const connection = new AsyncLocalStorage<{
  client: PoolClient; pool: Pool; tenant: string | null; closed: boolean;
}>();
const uuid = /^[0-9a-f]{8}-[0-9a-f]{4}-[1-8][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/i;

const columns = {
  user: ["id", "name", "email", "emailVerified", "image", "createdAt", "updatedAt"],
  session: ["id", "expiresAt", "token", "createdAt", "updatedAt", "ipAddress", "userAgent",
    "userId", "securityRevision", "absoluteDeadline", "lastActivity"],
  account: ["id", "accountId", "providerId", "userId", "accessToken", "refreshToken",
    "idToken", "accessTokenExpiresAt", "refreshTokenExpiresAt", "scope", "password",
    "createdAt", "updatedAt"],
  verification: ["id", "identifier", "value", "expiresAt", "createdAt", "updatedAt"],
} as const;

type NativeModel = keyof typeof columns;

function identifier(model: string, field?: string): string {
  if (!Object.prototype.hasOwnProperty.call(columns, model)) throw new Error("Unsupported auth model");
  if (field && !(columns[model as NativeModel] as readonly string[]).includes(field)) {
    throw new Error("Unsupported auth field");
  }
  return `"${field ?? model}"`;
}

function whereSql(model: string, where: CleanedWhere[] | undefined, values: unknown[]): string {
  if (!where?.length) return "";
  const and: string[] = [];
  const or: string[] = [];
  for (const item of where) {
    const column = identifier(model, item.field);
    const mode = item.mode === "insensitive";
    if (item.connector !== "AND" && item.connector !== "OR") throw new Error("Unsupported auth connector");
    let clause: string;
    if (item.value === null) {
      if (item.operator !== "eq" && item.operator !== "ne") throw new Error("Invalid null predicate");
      clause = `${column} IS ${item.operator === "ne" ? "NOT " : ""}NULL`;
    } else if (item.operator === "in" || item.operator === "not_in") {
      if (!Array.isArray(item.value)) throw new Error("Invalid auth set predicate");
      const insensitive = mode && item.value.every((value) => typeof value === "string");
      values.push(insensitive ? item.value.map((value) => (value as string).toLowerCase()) : item.value);
      clause = `${insensitive ? `LOWER(${column})` : column} ${item.operator === "in" ? "= ANY" : "<> ALL"}($${values.length})`;
    } else {
      const operators: Record<string, string> = {
        eq: "=", ne: "<>", lt: "<", lte: "<=", gt: ">", gte: ">=",
        contains: "LIKE", starts_with: "LIKE", ends_with: "LIKE",
      };
      const operator = operators[item.operator];
      if (!operator) throw new Error("Unsupported auth predicate");
      const pattern = ["contains", "starts_with", "ends_with"].includes(item.operator);
      if ((pattern || mode) && typeof item.value !== "string") throw new Error("Invalid text predicate");
      let value = item.value;
      if (pattern) {
        const escaped = (value as string).replace(/[\\%_]/g, "\\$&");
        value = (item.operator === "starts_with" ? "" : "%") + escaped +
          (item.operator === "ends_with" ? "" : "%");
      }
      values.push(value);
      clause = `${mode ? `LOWER(${column})` : column} ${operator} ${mode ? `LOWER($${values.length})` : `$${values.length}`}`;
    }
    (item.connector === "OR" ? or : and).push(clause);
  }
  // Better Auth groups AND and OR predicates separately, then intersects both groups.
  const groups = [and.length ? `(${and.join(" AND ")})` : "",
    or.length ? `(${or.join(" OR ")})` : ""].filter(Boolean);
  return ` WHERE ${groups.join(" AND ")}`;
}

function assignments(model: string, data: Record<string, unknown>, values: unknown[]): string {
  const entries = Object.entries(data);
  if (!entries.length) throw new Error("Empty auth mutation");
  return entries.map(([field, value]) => {
    values.push(value);
    return `${identifier(model, field)} = $${values.length}`;
  }).join(", ");
}

export async function withAuthTransaction<T>(pool: Pool, tenant: string | null,
  action: () => Promise<T>): Promise<T> {
  if (tenant !== null && !uuid.test(tenant)) throw new Error("Invalid auth tenant");
  const active = connection.getStore();
  if (active) {
    if (active.closed || active.pool !== pool || active.tenant !== tenant) {
      throw new Error("Auth transaction boundary mismatch");
    }
    return action();
  }
  return (async () => {
    const client = await pool.connect();
    let released = false;
    const transaction = { client, pool, tenant, closed: false };
    try {
      await client.query("BEGIN");
      await client.query("SELECT set_config('app.organization_id', $1, true)", [tenant ?? ""]);
      const result = await connection.run(transaction, action);
      if ((await client.query("COMMIT")).command !== "COMMIT") {
        throw new Error("Auth transaction did not commit");
      }
      return result;
    } catch (error) {
      try { await client.query("ROLLBACK"); } catch { client.release(true); released = true; throw error; }
      throw error;
    } finally {
      transaction.closed = true;
      // SET LOCAL is cleared by COMMIT/ROLLBACK; never reuse an open transaction.
      if (!released) client.release();
    }
  })();
}

export function scopedAuthClient(pool: Pool, tenant: string): PoolClient | null {
  const active = connection.getStore();
  if (!active) return null;
  if (active.closed || active.pool !== pool || active.tenant !== tenant) {
    throw new Error("Auth transaction boundary mismatch");
  }
  return active.client;
}

export function postgresAuthAdapter(pool: Pool) {
  const query = <T extends Record<string, unknown>>(statement: string, values: unknown[] = []) => {
    const active = connection.getStore();
    if (active) {
      if (active.closed || active.pool !== pool) throw new Error("Closed auth transaction");
      return active.client.query<T>(statement, values);
    }
    return withAuthTransaction(pool, null, () => {
      const owned = connection.getStore();
      if (!owned) throw new Error("Missing auth transaction");
      return owned.client.query<T>(statement, values);
    });
  };

  const find = async (model: string, where: CleanedWhere[] | undefined, limit: number,
    offset = 0, sortBy?: { field: string; direction: "asc" | "desc" }) => {
    identifier(model);
    if (!Number.isSafeInteger(limit) || limit < 0 || !Number.isSafeInteger(offset) || offset < 0) {
      throw new Error("Invalid auth pagination");
    }
    const values: unknown[] = [];
    const filter = whereSql(model, where, values);
    const order = sortBy ? ` ORDER BY ${identifier(model, sortBy.field)} ${sortBy.direction === "asc" ? "ASC" : sortBy.direction === "desc" ? "DESC" : (() => { throw new Error("Invalid auth sort"); })()}` : "";
    values.push(limit, offset);
    return (await query(`SELECT * FROM public.${identifier(model)}${filter}${order} LIMIT $${values.length - 1} OFFSET $${values.length}`, values)).rows;
  };

  const update = async (model: string, where: CleanedWhere[], data: Record<string, unknown>,
    single: boolean) => {
    identifier(model);
    if (!where.length) return single ? null : 0;
    const values: unknown[] = [];
    const set = assignments(model, data, values);
    const filter = whereSql(model, where, values);
    const statement = single ?
      `UPDATE public.${identifier(model)} SET ${set} WHERE id = (SELECT id FROM public.${identifier(model)}${filter} ORDER BY id LIMIT 1 FOR UPDATE) RETURNING *` :
      `UPDATE public.${identifier(model)} SET ${set}${filter}`;
    const result = await query(statement, values);
    return single ? result.rows[0] ?? null : result.rowCount ?? 0;
  };

  const remove = async (model: string, where: CleanedWhere[], single: boolean, returning: boolean) => {
    identifier(model);
    if (!where.length) return returning ? null : 0;
    const values: unknown[] = [];
    const filter = whereSql(model, where, values);
    const whereClause = single ?
      ` WHERE id = (SELECT id FROM public.${identifier(model)}${filter} ORDER BY id LIMIT 1 FOR UPDATE)` : filter;
    const result = await query(`DELETE FROM public.${identifier(model)}${whereClause}${returning ? " RETURNING *" : ""}`, values);
    return returning ? result.rows[0] ?? null : result.rowCount ?? 0;
  };

  return (options: Parameters<ReturnType<typeof createAdapterFactory>>[0]) => {
  const adapter: DBAdapter = createAdapterFactory({
    config: {
      adapterId: "carepulse-postgres", supportsDates: true, supportsBooleans: true,
      supportsJSON: true,
      transaction: (work) => withAuthTransaction(pool, connection.getStore()?.tenant ?? null,
        () => work(adapter)),
    },
    adapter: () => ({
      create: async ({ model, data }) => {
        identifier(model);
        const entries = Object.entries(data);
        if (!entries.length) throw new Error("Empty auth create");
        const fields = entries.map(([field]) => identifier(model, field)).join(", ");
        const values = entries.map(([, value]) => value);
        return (await query(`INSERT INTO public.${identifier(model)} (${fields}) VALUES (${values.map((_, i) => `$${i + 1}`).join(", ")}) RETURNING *`, values)).rows[0];
      },
      findOne: async ({ model, where }) => (await find(model, where, 1))[0] ?? null,
      findMany: async ({ model, where, limit, offset, sortBy }) =>
        find(model, where, limit, offset, sortBy),
      count: async ({ model, where }) => {
        const values: unknown[] = [];
        const filter = whereSql(model, where, values);
        return Number((await query<{ total: string }>(`SELECT COUNT(*)::text AS total FROM public.${identifier(model)}${filter}`, values)).rows[0].total);
      },
      update: async ({ model, where, update: data }) => update(model, where, data as Record<string, unknown>, true),
      updateMany: async ({ model, where, update: data }) => update(model, where, data as Record<string, unknown>, false) as Promise<number>,
      delete: async ({ model, where }) => { await remove(model, where, true, false); },
      deleteMany: async ({ model, where }) => remove(model, where, false, false) as Promise<number>,
      consumeOne: async ({ model, where }) => remove(model, where, true, true),
      incrementOne: async ({ model, where, increment, set }) => {
        if (!where.length) return null;
        const values: unknown[] = [];
        const changes = Object.entries(increment).map(([field, delta]) => {
          if (!Number.isFinite(delta)) throw new Error("Invalid auth increment");
          values.push(delta);
          const column = identifier(model, field);
          return `${column} = ${column} + $${values.length}`;
        });
        if (set) changes.push(assignments(model, set, values));
        if (!changes.length) throw new Error("Empty auth increment");
        const filter = whereSql(model, where, values);
        return (await query(`UPDATE public.${identifier(model)} SET ${changes.join(", ")} WHERE id = (SELECT id FROM public.${identifier(model)}${filter} ORDER BY id LIMIT 1 FOR UPDATE) RETURNING *`, values)).rows[0] ?? null;
      },
    }) as CustomAdapter,
  })(options);
  return adapter;
  };
}

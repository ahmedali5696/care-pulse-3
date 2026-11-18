// Server-only pools: runtime operations never borrow Better Auth credentials.
import { AsyncLocalStorage } from "node:async_hooks";
import { Pool, type PoolClient, type QueryConfig, type QueryResult, type QueryResultRow } from "pg";
import type { loadWebConfig } from "./config.ts";
import { checkWebPoolCheckout, isCompatEnabled } from "./compat/admission.ts";

let database: Pool | undefined;
let authPool: Pool | undefined;
const compatCheckActive = new AsyncLocalStorage<boolean>();

function createPool(
  settings: ReturnType<typeof loadWebConfig>["database" | "authDatabase"],
  expectedRole: "carepulse_web" | "carepulse_auth",
): Pool {
  const pool = new Pool({
    host: settings.host, port: settings.port,
    database: settings.name, user: settings.user,
    password: settings.password, max: 2,
    connectionTimeoutMillis: 1000, query_timeout: 1000,
    statement_timeout: 1000, lock_timeout: 1000,
  });
  // Idle socket errors must not log credentials or terminate the process.
  pool.on("error", () => {});

  const rawConnect = pool.connect.bind(pool);
  pool.connect = (async () => {
    const client = await rawConnect();
    if (isCompatEnabled() && !compatCheckActive.getStore()) {
      try {
        await compatCheckActive.run(true, () => checkWebPoolCheckout(client, expectedRole));
      } catch (err) {
        client.release(true);
        throw err;
      }
    }
    return client;
  }) as Pool["connect"];

  const rawQuery = pool.query.bind(pool);
  pool.query = (async <R extends QueryResultRow = QueryResultRow>(
    textOrConfig: string | QueryConfig,
    values?: unknown[],
  ): Promise<QueryResult<R>> => {
    if (!isCompatEnabled() || compatCheckActive.getStore()) {
      return rawQuery(textOrConfig as string, values) as Promise<QueryResult<R>>;
    }
    const client: PoolClient = await pool.connect();
    try {
      return await client.query<R>(textOrConfig as string, values);
    } finally {
      client.release();
    }
  }) as Pool["query"];

  return pool;
}

export function runtimeDatabase(config: ReturnType<typeof loadWebConfig>): Pool {
  return database ??= createPool(config.database, "carepulse_web");
}

export function authDatabase(config: ReturnType<typeof loadWebConfig>): Pool {
  return authPool ??= createPool(config.authDatabase, "carepulse_auth");
}

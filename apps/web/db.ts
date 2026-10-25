// Server-only pools: runtime operations never borrow Better Auth credentials.
import { Pool } from "pg";
import type { loadWebConfig } from "./config.ts";

let database: Pool | undefined;
let authPool: Pool | undefined;

function createPool(settings: ReturnType<typeof loadWebConfig>["database"]): Pool {
  const pool = new Pool({
    host: settings.host, port: settings.port,
    database: settings.name, user: settings.user,
    password: settings.password, max: 2,
    connectionTimeoutMillis: 1000, query_timeout: 1000,
    statement_timeout: 1000, lock_timeout: 1000,
  });
  // Idle socket errors must not log credentials or terminate the process.
  pool.on("error", () => {});
  return pool;
}

export function runtimeDatabase(config: ReturnType<typeof loadWebConfig>): Pool {
  return database ??= createPool(config.database);
}

export function authDatabase(config: ReturnType<typeof loadWebConfig>): Pool {
  return authPool ??= createPool(config.authDatabase);
}

// Server-only pool shared by foundation readiness and Better Auth.
import { Pool } from "pg";
import type { loadWebConfig } from "./config.ts";

let database: Pool | undefined;

export function runtimeDatabase(config: ReturnType<typeof loadWebConfig>): Pool {
  if (!database) {
    database = new Pool({
      host: config.database.host, port: config.database.port,
      database: config.database.name, user: config.database.user,
      password: config.database.password, max: 2,
      connectionTimeoutMillis: 1000, query_timeout: 1000,
      statement_timeout: 1000, lock_timeout: 1000,
    });
    // Idle socket errors must not log credentials or terminate the process.
    database.on("error", () => {});
  }
  return database;
}

// Server-only operational dependencies; no session/schema creation or diagnostics.
import { createConnection } from "node:net";
import { Pool, type PoolClient } from "pg";
import { loadWebConfig } from "./config";

const DEPENDENCY_TIMEOUT_MS = 1000;
export const PROBE_DEADLINE_MS = 4000;
let database: Pool | undefined;
type WebConfig = ReturnType<typeof loadWebConfig>;

function runtimeDatabase(config: WebConfig): Pool {
  if (!database) {
    database = new Pool({
      host: config.database.host, port: config.database.port,
      database: config.database.name, user: config.database.user,
      password: config.database.password, max: 2,
      connectionTimeoutMillis: DEPENDENCY_TIMEOUT_MS,
      query_timeout: DEPENDENCY_TIMEOUT_MS,
      statement_timeout: DEPENDENCY_TIMEOUT_MS,
      lock_timeout: DEPENDENCY_TIMEOUT_MS,
    });
    // pg removes failed idle sockets; the next probe observes dependency state.
    database.on("error", () => {});
  }
  return database;
}

async function databaseReady(config: WebConfig): Promise<boolean> {
  let client: PoolClient | undefined;
  let completed = false;
  try {
    client = await runtimeDatabase(config).connect();
    const revision = await client.query<{ version_num: string }>(
      "SELECT version_num FROM public.alembic_version",
    );
    completed = true;
    return revision.rows.length === 1 && revision.rows[0].version_num === "0001_foundation";
  } catch {
    return false;
  } finally {
    client?.release(!completed);
  }
}

async function apiLive(config: WebConfig): Promise<boolean> {
  try {
    const response = await fetch(`${config.apiUrl}/health/live`, {
      cache: "no-store", signal: AbortSignal.timeout(DEPENDENCY_TIMEOUT_MS),
    });
    return response.status === 200 && (await response.json()).status === "alive";
  } catch {
    return false;
  }
}

function smtpReady(endpoint: NonNullable<WebConfig["smtp"]>): Promise<boolean> {
  return new Promise((resolve) => {
    const socket = createConnection(endpoint);
    let greeting = "";
    const finish = (ready: boolean) => {
      clearTimeout(timer);
      socket.destroy();
      resolve(ready);
    };
    const timer = setTimeout(() => finish(false), DEPENDENCY_TIMEOUT_MS);
    socket.on("data", (chunk: Buffer) => {
      greeting += chunk.toString("ascii");
      if (greeting.length > 512) finish(false);
      else if (greeting.includes("\n")) finish(/^220[ -]/.test(greeting));
    });
    socket.once("end", () => finish(false));
    socket.once("error", () => finish(false));
  });
}

export async function foundationReady(): Promise<boolean> {
  let timer: ReturnType<typeof setTimeout> | undefined;
  try {
    const config = loadWebConfig();
    const dependencies = Promise.all([
      databaseReady(config), apiLive(config),
      config.smtp ? smtpReady(config.smtp) : Promise.resolve(true),
    ]).then((checks) => checks.every(Boolean));
    return await Promise.race([
      dependencies,
      new Promise<boolean>((resolve) => {
        timer = setTimeout(() => resolve(false), PROBE_DEADLINE_MS);
      }),
    ]);
  } catch {
    return false;
  } finally {
    clearTimeout(timer);
  }
}

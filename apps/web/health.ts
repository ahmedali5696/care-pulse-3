// Server-only operational dependencies; no session/schema creation or diagnostics.
import { createConnection } from "node:net";
import type { PoolClient } from "pg";
import { loadWebConfig } from "./config";
import { isCompatEnabled, verifyWebReadinessCompat } from "./compat/admission";
import { runtimeDatabase } from "./db";

const DEPENDENCY_TIMEOUT_MS = 1000;
export const PROBE_DEADLINE_MS = 4000;
type WebConfig = ReturnType<typeof loadWebConfig>;

async function databaseReady(config: WebConfig): Promise<boolean> {
  let client: PoolClient | undefined;
  let completed = false;
  try {
    client = await runtimeDatabase(config).connect();
    const revision = await client.query<{ version_num: string }>(
      "SELECT version_num FROM public.alembic_version",
    );
    completed = true;
    return revision.rows.length === 1 && revision.rows[0].version_num === "0072_b20_worker";
  } catch {
    return false;
  } finally {
    client?.release(!completed);
  }
}

async function apiAvailable(config: WebConfig, compatEnabled: boolean): Promise<boolean> {
  try {
    const response = await fetch(`${config.apiUrl}/health/${compatEnabled ? "ready" : "live"}`, {
      cache: "no-store",
      signal: AbortSignal.timeout(compatEnabled ? PROBE_DEADLINE_MS : DEPENDENCY_TIMEOUT_MS),
    });
    return response.status === 200 &&
      (await response.json()).status === (compatEnabled ? "ready" : "alive");
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
    const compatEnabled = isCompatEnabled();
    const dependencies = Promise.all([
      compatEnabled ? verifyWebReadinessCompat() : databaseReady(config),
      apiAvailable(config, compatEnabled),
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

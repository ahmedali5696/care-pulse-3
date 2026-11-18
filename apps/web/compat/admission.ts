/**
 * Web and Better Auth compatibility admission gate for PRE-W3-COMPAT.
 * Enforces target-bound control, singleton head, effective DB role, and trusted API peer.
 */

import { createHash, randomUUID } from "node:crypto";
import type { Pool, PoolClient } from "pg";
import { loadWebConfig } from "../config.ts";
import { authDatabase, runtimeDatabase } from "../db.ts";

export const CANDIDATE_HEAD = "0046_f01_audit_integrity";

export class CompatAdmissionError extends Error {
  constructor(message = "Compatibility admission denied") {
    super(message);
    this.name = "CompatAdmissionError";
  }
}

export interface CompatWebConfig {
  targetId: string;
  manifestId: string;
  generation: "source" | "candidate";
  expectedHead: string;
  dbName: string;
  expectedApiPeer: string;
}

export interface WebAdmissionPermit {
  attemptId: string;
  targetId: string;
  ownerEpoch: number;
  generation: "source" | "candidate";
  permitKey: string;
  client: PoolClient;
}

export function isCompatEnabled(): boolean {
  return Boolean(process.env.CP_COMPAT_TARGET_ID?.trim());
}

export function loadCompatWebConfig(apiUrlOverride?: string): CompatWebConfig | null {
  const targetId = process.env.CP_COMPAT_TARGET_ID?.trim();
  if (!targetId) return null;
  const generation = process.env.CP_COMPAT_GENERATION?.trim();
  if (generation !== "source" && generation !== "candidate") {
    throw new CompatAdmissionError("Invalid generation configuration");
  }
  const expectedHead = process.env.CP_COMPAT_EXPECTED_HEAD?.trim();
  const manifestId = process.env.CP_COMPAT_MANIFEST_ID?.trim();
  const dbName = (process.env.CP_DB_NAME ?? "carepulse").trim();
  const actualApiUrl = apiUrlOverride ?? loadWebConfig().apiUrl;
  const expectedApiPeer = (
    process.env.CP_COMPAT_EXPECTED_API_PEER ?? actualApiUrl
  ).trim();
  if (!expectedHead || !manifestId || !dbName || !expectedApiPeer) {
    throw new CompatAdmissionError("Incomplete web compatibility configuration");
  }
  if (actualApiUrl !== expectedApiPeer) {
    throw new CompatAdmissionError("Untrusted API peer configuration");
  }
  return {
    targetId,
    manifestId,
    generation,
    expectedHead,
    dbName,
    expectedApiPeer,
  };
}

export function permitLockKey(targetId: string): string {
  const digest = createHash("sha256")
    .update(`compat:permit:${targetId}`, "utf8")
    .digest();
  return digest.readBigInt64BE(0).toString();
}

export async function verifyPoolClientIdentity(
  client: PoolClient,
  config: CompatWebConfig,
  expectedRole: "carepulse_web" | "carepulse_auth" | "carepulse_runtime",
): Promise<void> {
  const res = await client.query<{
    db_name: string;
    session_user: string;
    current_user: string;
    rolsuper: boolean;
    rolbypassrls: boolean;
    rolcanlogin: boolean;
  }>(
    `SELECT current_database() AS db_name,
            session_user::text AS session_user,
            current_user::text AS current_user,
            r.rolsuper,
            r.rolbypassrls,
            r.rolcanlogin
     FROM pg_roles r
     WHERE r.rolname = current_user`,
  );
  if (res.rows.length !== 1) {
    throw new CompatAdmissionError("Role identity unavailable");
  }
  const row = res.rows[0];
  if (row.db_name !== config.dbName) {
    throw new CompatAdmissionError("Target database mismatch");
  }
  if (
    row.session_user !== expectedRole ||
    row.current_user !== expectedRole ||
    row.rolsuper ||
    row.rolbypassrls ||
    !row.rolcanlogin
  ) {
    throw new CompatAdmissionError("Effective role privilege mismatch");
  }
}

export async function verifyControlAndHead(
  client: PoolClient,
  config: CompatWebConfig,
  lockForShare = true,
): Promise<number> {
  const expectedPhase =
    config.generation === "source" ? "SOURCE_OPEN" : "TARGET_OPEN";
  const manifestCol =
    config.generation === "source" ? "source_manifest_id" : "target_manifest_id";
  const ctrl = await client.query<{
    target_id: string;
    db_name: string;
    phase: string;
    owner_epoch: string | number;
    manifest_id: string;
    active_generation: string;
    source_sealed: boolean;
    alembic_head: string | null;
  }>(
    lockForShare
      ? `SELECT target_id, db_name, phase, owner_epoch,
                ${manifestCol} AS manifest_id, active_generation, source_sealed, alembic_head
         FROM compat_control.read_target_control()`
      : `SELECT target_id, db_name, phase, owner_epoch,
                ${manifestCol} AS manifest_id, active_generation, source_sealed, alembic_head
         FROM compat_control.read_target_status()`,
  );
  if (ctrl.rows.length !== 1) {
    throw new CompatAdmissionError("Missing target control");
  }
  const row = ctrl.rows[0];
  if (row.alembic_head !== config.expectedHead) {
    throw new CompatAdmissionError("Incompatible schema revision");
  }
  if (row.target_id !== config.targetId || row.db_name !== config.dbName) {
    throw new CompatAdmissionError("Target binding mismatch");
  }
  if (row.phase !== expectedPhase || row.active_generation !== config.generation) {
    throw new CompatAdmissionError("Web generation fenced");
  }
  if (config.generation === "source" && row.source_sealed) {
    throw new CompatAdmissionError("Source generation sealed");
  }
  if (row.manifest_id !== config.manifestId) {
    throw new CompatAdmissionError("Manifest mismatch");
  }
  return Number(row.owner_epoch);
}

export async function acquireWebAdmissionPermit(
  config: CompatWebConfig,
  pool?: Pool,
  expectedRole: "carepulse_web" | "carepulse_runtime" = "carepulse_web",
): Promise<WebAdmissionPermit> {
  const targetPool = pool ?? runtimeDatabase(loadWebConfig());
  const client = await targetPool.connect();
  const key = permitLockKey(config.targetId);
  let locked = false;
  try {
    const lockRes = await client.query<{ acquired: boolean }>(
      "SELECT pg_try_advisory_lock_shared($1::bigint) AS acquired",
      [key],
    );
    if (!lockRes.rows[0]?.acquired) {
      throw new CompatAdmissionError("Drain in progress");
    }
    locked = true;
    await client.query("BEGIN ISOLATION LEVEL READ COMMITTED");
    await verifyPoolClientIdentity(client, config, expectedRole);
    const ownerEpoch = await verifyControlAndHead(client, config, true);
    const attemptId = randomUUID();
    await client.query(
      `SELECT compat_control.record_own_attempt($1, $2, $3, $4, 'web', 'http', NULL, NULL, 'PENDING')`,
      [attemptId, config.targetId, ownerEpoch, config.generation],
    );
    await client.query("COMMIT");
    return {
      attemptId,
      targetId: config.targetId,
      ownerEpoch,
      generation: config.generation,
      permitKey: key,
      client,
    };
  } catch (err) {
    try {
      await client.query("ROLLBACK");
    } catch {
      // ignore rollback error
    }
    if (locked) {
      try {
        await client.query("SELECT pg_advisory_unlock_shared($1::bigint)", [key]);
      } catch {
        // ignore unlock error
      }
    }
    client.release(true);
    if (err instanceof CompatAdmissionError) throw err;
    throw new CompatAdmissionError("Web admission failed");
  }
}

export async function correlateWebAttempt(
  permit: WebAdmissionPermit,
  correlationId: string,
  disposition: "COMMITTED" | "ROLLED_BACK" = "COMMITTED",
): Promise<void> {
  await permit.client.query(
    `SELECT compat_control.record_own_attempt($1, $2, $3, $4, 'web', 'http', $5, NULL, $6)`,
    [
      permit.attemptId,
      permit.targetId,
      permit.ownerEpoch,
      permit.generation,
      correlationId,
      disposition,
    ],
  );
}

export async function releaseWebAdmissionPermit(
  permit: WebAdmissionPermit,
  disposition: "COMMITTED" | "ROLLED_BACK" = "COMMITTED",
): Promise<void> {
  try {
    await permit.client.query(
      `SELECT compat_control.record_own_attempt($1, $2, $3, $4, 'web', 'http', NULL, NULL, $5)`,
      [
        permit.attemptId,
        permit.targetId,
        permit.ownerEpoch,
        permit.generation,
        disposition,
      ],
    );
  } catch {
    // preserve best-effort disposition without leaking
  } finally {
    try {
      await permit.client.query("SELECT pg_advisory_unlock_shared($1::bigint)", [
        permit.permitKey,
      ]);
      permit.client.release();
    } catch {
      permit.client.release(true);
    }
  }
}

export async function checkWebPoolCheckout(
  client: PoolClient,
  expectedRole: "carepulse_web" | "carepulse_auth" | "carepulse_runtime",
): Promise<void> {
  const config = loadCompatWebConfig();
  if (!config) return;
  await verifyPoolClientIdentity(client, config, expectedRole);
  await verifyControlAndHead(client, config, false);
}

export async function verifyWebReadinessCompat(): Promise<boolean> {
  const config = loadCompatWebConfig();
  if (!config) return true;
  const webCfg = loadWebConfig();
  let webClient: PoolClient | undefined;
  let authClient: PoolClient | undefined;
  try {
    webClient = await runtimeDatabase(webCfg).connect();
    await verifyPoolClientIdentity(
      webClient,
      config,
      config.generation === "source" ? "carepulse_runtime" : "carepulse_web",
    );
    await verifyControlAndHead(webClient, config, false);
    authClient = await authDatabase(webCfg).connect();
    await verifyPoolClientIdentity(authClient, config, "carepulse_auth");
    return true;
  } catch {
    return false;
  } finally {
    webClient?.release();
    authClient?.release();
  }
}

/**
 * Read-only predecessor Web & pg-pool startup adapter for immutable 0040 images (T020).
 * Loaded via: node --require /run/compat-adapters/source-web.cjs ingress.mjs
 */
"use strict";

const http = require("node:http");
const crypto = require("node:crypto");
const fs = require("node:fs");
const { AsyncLocalStorage } = require("node:async_hooks");

const SOURCE_HEAD = "0040_t173_numeric_money";
const compatCheckActive = new AsyncLocalStorage();

function permitLockKey(targetId) {
  const digest = crypto
    .createHash("sha256")
    .update(`compat:permit:${targetId}`, "utf8")
    .digest();
  return digest.readBigInt64BE(0).toString();
}

function readSecret(envVar) {
  const filePath = process.env[envVar];
  if (!filePath) throw new Error(`Missing secret path ${envVar}`);
  return fs.readFileSync(filePath, "utf8").trim();
}

function loadSourceConfig() {
  const targetId = (process.env.CP_COMPAT_TARGET_ID || "").trim();
  const manifestId = (process.env.CP_COMPAT_MANIFEST_ID || "").trim();
  const generation = (process.env.CP_COMPAT_GENERATION || "").trim();
  const expectedHead = (process.env.CP_COMPAT_EXPECTED_HEAD || "").trim();
  const expectedPeer = (process.env.CP_COMPAT_EXPECTED_API_PEER || "").trim();
  const actualPeer = (process.env.CP_INTERNAL_API_URL || "").trim();
  const dbName = (process.env.CP_DB_NAME || "carepulse").trim();

  if (
    !targetId ||
    !manifestId ||
    generation !== "source" ||
    expectedHead !== SOURCE_HEAD ||
    !expectedPeer ||
    actualPeer !== expectedPeer
  ) {
    throw new Error("Invalid source Web compatibility binding");
  }
  return { targetId, manifestId, generation, expectedHead, dbName };
}

function getPg() {
  const resolved = require.resolve("pg", { paths: [process.cwd(), "/workspace/apps/web"] });
  return require(resolved);
}

let _runtimePool;
function getRuntimePool() {
  if (_runtimePool) return _runtimePool;
  const { Pool } = getPg();
  _runtimePool = new Pool({
    host: process.env.CP_DB_HOST || "db",
    port: Number(process.env.CP_DB_PORT || "5432"),
    database: process.env.CP_DB_NAME || "carepulse",
    user: process.env.CP_DB_USER || "carepulse_runtime",
    password: readSecret("CP_DB_PASSWORD_FILE"),
    max: 2,
    connectionTimeoutMillis: 1000,
    query_timeout: 1000,
    statement_timeout: 1000,
    lock_timeout: 1000,
  });
  _runtimePool.on("error", () => {});
  return _runtimePool;
}

async function verifyClient(client, cfg, expectedRole) {
  const ident = await client.query(
    `SELECT current_database() AS db_name,
            session_user::text AS session_user,
            current_user::text AS current_user,
            r.rolsuper, r.rolbypassrls, r.rolcanlogin
     FROM pg_roles r WHERE r.rolname = current_user`,
  );
  if (ident.rows.length !== 1) throw new Error("Role identity unavailable");
  const r = ident.rows[0];
  if (
    r.db_name !== cfg.dbName ||
    r.session_user !== expectedRole ||
    r.current_user !== expectedRole ||
    r.rolsuper ||
    r.rolbypassrls ||
    !r.rolcanlogin
  ) {
    throw new Error("Role/database mismatch");
  }

  const rev = await client.query("SELECT version_num FROM public.alembic_version");
  if (rev.rows.length !== 1 || rev.rows[0].version_num !== SOURCE_HEAD) {
    throw new Error("Incompatible schema revision");
  }

  const ctrl = await client.query(
    `SELECT target_id, db_name, phase, owner_epoch,
            source_manifest_id, active_generation, source_sealed
     FROM compat_control.read_target_control()`,
  );
  if (ctrl.rows.length !== 1) throw new Error("Missing target control");
  const c = ctrl.rows[0];
  if (
    c.target_id !== cfg.targetId ||
    c.db_name !== cfg.dbName ||
    c.phase !== "SOURCE_OPEN" ||
    c.active_generation !== "source" ||
    c.source_sealed ||
    c.source_manifest_id !== cfg.manifestId
  ) {
    throw new Error("Source generation fenced");
  }
  return Number(c.owner_epoch);
}

async function acquireSourcePermit(cfg) {
  const pool = getRuntimePool();
  const client = await pool.connect();
  const key = permitLockKey(cfg.targetId);
  let locked = false;
  try {
    const lockRes = await client.query(
      "SELECT pg_try_advisory_lock_shared($1::bigint) AS acquired",
      [key],
    );
    if (!lockRes.rows[0] || !lockRes.rows[0].acquired) {
      throw new Error("Drain in progress");
    }
    locked = true;
    await client.query("BEGIN ISOLATION LEVEL READ COMMITTED");
    const epoch = await verifyClient(
      client,
      cfg,
      process.env.CP_DB_USER || "carepulse_runtime",
    );
    const attemptId = crypto.randomUUID();
    await client.query(
      `SELECT compat_control.record_own_attempt($1, $2, $3, 'source', 'web', 'http', NULL, NULL, 'PENDING')`,
      [attemptId, cfg.targetId, epoch],
    );
    await client.query("COMMIT");
    return { attemptId, targetId: cfg.targetId, epoch, key, client };
  } catch (err) {
    try {
      await client.query("ROLLBACK");
    } catch {}
    if (locked) {
      try {
        await client.query("SELECT pg_advisory_unlock_shared($1::bigint)", [key]);
      } catch {}
    }
    client.release(true);
    throw err;
  }
}

async function releaseSourcePermit(permit, disposition) {
  try {
    await permit.client.query(
      `SELECT compat_control.record_own_attempt($1, $2, $3, 'source', 'web', 'http', NULL, NULL, $4)`,
      [permit.attemptId, permit.targetId, permit.epoch, disposition],
    );
  } catch {}
  try {
    await permit.client.query("SELECT pg_advisory_unlock_shared($1::bigint)", [
      permit.key,
    ]);
    permit.client.release();
  } catch {
    permit.client.release(true);
  }
}

// Intercept pg.Pool.prototype.connect so every pool checkout/reuse is verified.
try {
  const pg = getPg();
  const origConnect = pg.Pool.prototype.connect;
  pg.Pool.prototype.connect = async function (...args) {
    const client = await origConnect.apply(this, args);
    if (!compatCheckActive.getStore()) {
      try {
        const cfg = loadSourceConfig();
        const role =
          this.options && this.options.user === "carepulse_auth"
            ? "carepulse_auth"
            : process.env.CP_DB_USER || "carepulse_runtime";
        await compatCheckActive.run(true, () => verifyClient(client, cfg, role));
      } catch (err) {
        client.release(true);
        throw err;
      }
    }
    return client;
  };
} catch {
  // pg resolution failure will fail closed at request time
}

// Intercept http.createServer to gate original Web dispatch before Next/Better Auth.
const origCreateServer = http.createServer.bind(http);
http.createServer = function (optionsOrListener, maybeListener) {
  const listener =
    typeof optionsOrListener === "function" ? optionsOrListener : maybeListener;
  const wrappedListener = async (request, response) => {
    const urlPath = (request.url || "/").split("?")[0];
    if (urlPath === "/health/live" || urlPath === "/health/ready") {
      return listener(request, response);
    }
    delete request.headers["x-carepulse-generation"];
    let permit;
    try {
      const cfg = loadSourceConfig();
      permit = await compatCheckActive.run(true, () => acquireSourcePermit(cfg));
    } catch {
      if (!response.headersSent) {
        response.writeHead(503, {
          "Cache-Control": "no-store",
          "Content-Type": "application/json",
        });
      }
      response.end(JSON.stringify({ error: "Service unavailable" }));
      return;
    }

    let released = false;
    const finish = (disp) => {
      if (released) return;
      released = true;
      compatCheckActive
        .run(true, () => releaseSourcePermit(permit, disp))
        .catch(() => {});
    };
    response.once("finish", () =>
      finish(response.statusCode < 500 ? "COMMITTED" : "ROLLED_BACK"),
    );
    response.once("close", () =>
      finish(
        response.writableEnded && response.statusCode < 500
          ? "COMMITTED"
          : "ROLLED_BACK",
      ),
    );
    return listener(request, response);
  };

  if (typeof optionsOrListener === "function") {
    return origCreateServer(wrappedListener);
  }
  return origCreateServer(optionsOrListener, wrappedListener);
};

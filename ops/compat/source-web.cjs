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
    (process.env.CP_DB_USER || "carepulse_runtime") !== "carepulse_runtime" ||
    (process.env.CP_AUTH_DB_USER || "carepulse_auth") !== "carepulse_auth" ||
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

async function sourceReady() {
  const cfg = loadSourceConfig();
  const client = await getRuntimePool().connect();
  try {
    await verifyClient(client, cfg, "carepulse_runtime");
  } finally {
    client.release();
  }
}

async function verifyClient(client, cfg, expectedRole) {
  const ident = await client.query(
    `SELECT current_database() AS db_name,
            session_user::text AS session_user,
            current_user::text AS current_user,
            r.rolsuper, r.rolbypassrls, r.rolcanlogin,
            r.rolcreatedb, r.rolcreaterole, r.rolreplication, r.rolinherit,
            EXISTS (SELECT 1 FROM pg_auth_members m WHERE m.member = r.oid) AS role_member,
            has_database_privilege(current_user, current_database(), 'CONNECT') AS can_connect,
            has_database_privilege(current_user, current_database(), 'CREATE,TEMP') AS db_create_temp,
            has_schema_privilege(current_user, 'public', 'USAGE') AS public_usage,
            has_schema_privilege(current_user, 'public', 'CREATE') AS public_create,
            has_schema_privilege(current_user, 'compat_control', 'USAGE') AS control_usage,
            has_schema_privilege(current_user, 'compat_control', 'CREATE') AS control_create,
            has_function_privilege(current_user,
                'compat_control.read_target_control()', 'EXECUTE') AS control_reader,
            has_function_privilege(current_user,
                'compat_control.read_target_status()', 'EXECUTE') AS control_status_reader,
            has_function_privilege(current_user,
                'compat_control.record_own_attempt(text,text,bigint,text,text,text,text,bigint,text)',
                'EXECUTE') AS attempt_writer,
            has_function_privilege(current_user,
                'compat_control.has_unknown_sender_attempt(text,text)',
                'EXECUTE') AS sender_authority,
            has_table_privilege(current_user, 'compat_control.target_control', 'SELECT') AS control_select,
            has_table_privilege(current_user, 'compat_control.target_control',
                'INSERT,UPDATE,DELETE,TRUNCATE,REFERENCES,TRIGGER') AS control_mutation,
            has_any_column_privilege(current_user, 'compat_control.target_control',
                'INSERT,UPDATE,REFERENCES') AS control_column_mutation,
            has_table_privilege(current_user, 'compat_control.work_evidence',
                'SELECT,INSERT,UPDATE,DELETE,TRUNCATE,REFERENCES,TRIGGER') AS work_access,
            has_any_column_privilege(current_user, 'compat_control.work_evidence',
                'SELECT,INSERT,UPDATE,REFERENCES') AS work_column_access,
            EXISTS (
              SELECT 1 FROM pg_namespace n
              WHERE n.nspname NOT IN ('public', 'compat_control', 'pg_catalog', 'information_schema')
                AND n.nspname NOT LIKE 'pg_%'
                AND has_schema_privilege(current_user, n.oid, 'USAGE,CREATE')
            ) AS other_schema_access,
            has_table_privilege(current_user, 'public.patients', 'SELECT') AS patient_read,
            has_table_privilege(current_user, 'public.appointments', 'INSERT') AS appointment_write,
            (SELECT bool_and(
                has_table_privilege(current_user, format('public.%I', native_table), 'SELECT')
                AND has_table_privilege(current_user, format('public.%I', native_table), 'INSERT')
                AND has_table_privilege(current_user, format('public.%I', native_table), 'UPDATE')
                AND has_table_privilege(current_user, format('public.%I', native_table), 'DELETE')
             ) FROM (VALUES ('user'), ('session'), ('account'), ('verification'))
               AS native_tables(native_table)) AS auth_native_crud,
            EXISTS (
              SELECT 1 FROM pg_class c
              JOIN pg_namespace n ON n.oid = c.relnamespace
              WHERE n.nspname = 'public' AND c.relname IN ('user', 'verification')
                AND (has_table_privilege(current_user, c.oid,
                    'INSERT,UPDATE,DELETE,TRUNCATE,REFERENCES,TRIGGER')
                  OR has_any_column_privilege(current_user, c.oid,
                    'INSERT,UPDATE,REFERENCES'))
            ) OR has_table_privilege(current_user, 'public."account"',
                'SELECT,INSERT,UPDATE,DELETE,TRUNCATE,REFERENCES,TRIGGER')
              OR has_any_column_privilege(current_user, 'public."account"',
                'SELECT,INSERT,UPDATE,REFERENCES')
              OR has_table_privilege(current_user, 'public."session"',
                'SELECT,INSERT,UPDATE,DELETE,TRUNCATE,REFERENCES,TRIGGER')
              OR has_any_column_privilege(current_user, 'public."session"',
                'SELECT,INSERT,UPDATE,REFERENCES') AS runtime_native_access,
            EXISTS (
              SELECT 1 FROM pg_class c
              JOIN pg_namespace n ON n.oid = c.relnamespace
              WHERE n.nspname = 'public'
                AND c.relkind IN ('r', 'p', 'v', 'm', 'f')
                AND c.relname NOT IN (
                  'user', 'session', 'account', 'verification', 'organizations',
                  'staff_accounts', 'invitations', 'recovery_operations',
                  'auth_recovery_completions', 'delivery_jobs',
                  'protected_delivery_material', 'sensitive_audit'
                )
                AND (
                  has_table_privilege(current_user, c.oid,
                    'SELECT,INSERT,UPDATE,DELETE,TRUNCATE,REFERENCES,TRIGGER')
                  OR has_any_column_privilege(current_user, c.oid,
                    'SELECT,INSERT,UPDATE,REFERENCES')
                )
            ) AS auth_unexpected_relation_access,
            EXISTS (
              SELECT 1 FROM pg_class c
              JOIN pg_namespace n ON n.oid = c.relnamespace
              WHERE n.nspname = 'public'
                AND c.relname IN (
                  'organizations', 'staff_accounts', 'invitations',
                  'auth_recovery_completions'
                )
                AND (has_table_privilege(current_user, c.oid,
                    'INSERT,UPDATE,DELETE,TRUNCATE,REFERENCES,TRIGGER')
                  OR has_any_column_privilege(current_user, c.oid,
                    'INSERT,UPDATE,REFERENCES'))
            ) OR has_table_privilege(current_user, 'public.recovery_operations',
                'INSERT,UPDATE,DELETE,TRUNCATE,REFERENCES,TRIGGER')
              OR has_any_column_privilege(current_user, 'public.recovery_operations',
                'INSERT,REFERENCES')
              OR EXISTS (
                SELECT 1 FROM pg_attribute a
                WHERE a.attrelid = 'public.recovery_operations'::regclass
                  AND a.attnum > 0 AND NOT a.attisdropped AND a.attname <> 'auth_fence'
                  AND has_column_privilege(current_user, a.attrelid, a.attnum, 'UPDATE')
              )
              OR has_table_privilege(current_user, 'public.delivery_jobs',
                'UPDATE,DELETE,TRUNCATE,REFERENCES,TRIGGER')
              OR has_any_column_privilege(current_user, 'public.delivery_jobs',
                'UPDATE,REFERENCES')
              OR has_table_privilege(current_user, 'public.protected_delivery_material',
                'SELECT,UPDATE,DELETE,TRUNCATE,REFERENCES,TRIGGER')
              OR has_any_column_privilege(current_user, 'public.protected_delivery_material',
                'SELECT,UPDATE,REFERENCES')
              OR has_table_privilege(current_user, 'public.sensitive_audit',
                'SELECT,UPDATE,DELETE,TRUNCATE,REFERENCES,TRIGGER')
              OR has_any_column_privilege(current_user, 'public.sensitive_audit',
                'SELECT,UPDATE,REFERENCES')
              OR EXISTS (
                SELECT 1 FROM pg_attribute a
                WHERE a.attrelid = 'public.organizations'::regclass
                  AND a.attnum > 0 AND NOT a.attisdropped
                  AND a.attname NOT IN ('id', 'state', 'lifecycle_revision', 'first_activated_at')
                  AND has_column_privilege(current_user, a.attrelid, a.attnum, 'SELECT')
              )
              OR EXISTS (
                SELECT 1 FROM pg_attribute a
                WHERE a.attrelid = 'public.delivery_jobs'::regclass
                  AND a.attnum > 0 AND NOT a.attisdropped
                  AND a.attname NOT IN ('id', 'organization_id', 'state', 'purpose')
                  AND has_column_privilege(current_user, a.attrelid, a.attnum, 'SELECT')
              )
              OR EXISTS (
                SELECT 1 FROM pg_class c
                JOIN pg_namespace n ON n.oid = c.relnamespace
                WHERE n.nspname = 'public'
                  AND c.relname IN ('user', 'session', 'account', 'verification')
                  AND has_table_privilege(current_user, c.oid,
                    'TRUNCATE,REFERENCES,TRIGGER')
              ) AS auth_widened_metadata
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
    !r.rolcanlogin ||
    r.rolcreatedb || r.rolcreaterole || r.rolreplication ||
    r.rolinherit || r.role_member ||
    !r.can_connect || r.db_create_temp ||
    !r.public_usage || r.public_create ||
    !r.control_usage || r.control_create ||
    !r.control_reader || !r.control_status_reader || !r.attempt_writer || r.sender_authority ||
    !r.control_select || r.control_mutation || r.control_column_mutation ||
    r.work_access || r.work_column_access || r.other_schema_access ||
    (expectedRole === 'carepulse_auth'
      ? r.patient_read || r.appointment_write || r.auth_unexpected_relation_access ||
        r.auth_widened_metadata ||
        !r.auth_native_crud
      : !r.patient_read || !r.appointment_write || r.runtime_native_access)
  ) {
    throw new Error("Effective source role privilege mismatch");
  }

  const ctrl = await client.query(
    `SELECT target_id, db_name, phase, owner_epoch,
            source_manifest_id, active_generation, source_sealed, alembic_head
     FROM compat_control.read_target_control()`,
  );
  if (ctrl.rows.length !== 1) throw new Error("Missing target control");
  const c = ctrl.rows[0];
  if (
    c.target_id !== cfg.targetId ||
    c.db_name !== cfg.dbName ||
    c.alembic_head !== SOURCE_HEAD ||
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
    const epoch = await verifyClient(client, cfg, "carepulse_runtime");
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

async function releaseSourcePermit(permit) {
  // HTTP completion is not proof of the domain transaction or its receipt.
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
  pg.Pool.prototype.connect = function (callback) {
    const check = async (client) => {
      if (!compatCheckActive.getStore()) {
        try {
          const cfg = loadSourceConfig();
          const role = this.options && this.options.user;
          if (role !== "carepulse_runtime" && role !== "carepulse_auth") {
            throw new Error("Unexpected source PostgreSQL login");
          }
          await compatCheckActive.run(true, () => verifyClient(client, cfg, role));
        } catch (err) {
          client.release(true);
          throw err;
        }
      }
      return client;
    };
    if (typeof callback === "function") {
      return origConnect.call(this, (err, client, release) => {
        if (err) return callback(err);
        check(client).then(() => callback(null, client, release), callback);
      });
    }
    return origConnect.call(this).then(check);
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
    if (urlPath === "/health/live") {
      return listener(request, response);
    }
    if (urlPath === "/health/ready") {
      try {
        await compatCheckActive.run(true, sourceReady);
      } catch {
        response.writeHead(503, {
          "Cache-Control": "no-store",
          "Content-Type": "application/json",
        });
        response.end(JSON.stringify({ ready: false }));
        return;
      }
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
    const finish = () => {
      if (released) return;
      released = true;
      compatCheckActive
        .run(true, () => releaseSourcePermit(permit))
        .catch(() => {});
    };
    response.once("finish", finish);
    response.once("close", finish);
    return listener(request, response);
  };

  if (typeof optionsOrListener === "function") {
    return origCreateServer(wrappedListener);
  }
  return origCreateServer(optionsOrListener, wrappedListener);
};

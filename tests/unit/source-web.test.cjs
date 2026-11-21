"use strict";

const assert = require("node:assert/strict");
const { AsyncLocalStorage } = require("node:async_hooks");
const crypto = require("node:crypto");
const { EventEmitter } = require("node:events");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");
const vm = require("node:vm");

function loadAdapter() {
  const roles = {};
  const released = [];
  const executed = [];
  const sqlChecks = [];
  const control = {
    target_id: "target-1", db_name: "carepulse", phase: "SOURCE_OPEN",
    owner_epoch: 1, source_manifest_id: "source-0040",
    active_generation: "source", source_sealed: false,
    alembic_head: "0040_t173_numeric_money",
  };

  function role(user) {
    return {
      db_name: "carepulse", session_user: user, current_user: user,
      rolsuper: false, rolbypassrls: false, rolcanlogin: true,
      rolcreatedb: false, rolcreaterole: false, rolreplication: false,
      rolinherit: false, role_member: false,
      can_connect: true, db_create_temp: false,
      public_usage: true, public_create: false,
      control_usage: true, control_create: false,
      control_reader: true, control_status_reader: true, attempt_writer: true,
      sender_authority: false,
      control_select: true, control_mutation: false,
      control_column_mutation: false, work_access: false,
      work_column_access: false, other_schema_access: false,
      patient_read: user === "carepulse_runtime",
      appointment_write: user === "carepulse_runtime",
      auth_native_crud: user === "carepulse_auth",
      runtime_native_access: false,
      auth_unexpected_relation_access: false,
      auth_widened_metadata: false,
    };
  }
  roles.carepulse_runtime = role("carepulse_runtime");
  roles.carepulse_auth = role("carepulse_auth");

  class Pool {
    constructor(options) { this.options = options; }
    on() { return this; }
    connect(callback) {
      const user = this.options.user;
      const client = {
        async query(statement) {
          if (statement.includes("FROM pg_roles r")) {
            sqlChecks.push(statement);
            return { rows: [{ ...roles[user] }] };
          }
          if (statement.includes("read_target_control()")) {
            return { rows: [{ ...control }] };
          }
          if (statement.includes("pg_try_advisory_lock_shared")) {
            return { rows: [{ acquired: true }] };
          }
          executed.push(statement);
          return { rows: [{ ok: true }] };
        },
        release(destroy) { released.push({ user, destroy: destroy === true }); },
      };
      if (callback) {
        queueMicrotask(() => callback(null, client, client.release));
        return;
      }
      return Promise.resolve(client);
    }
    query(statement) {
      return new Promise((resolve, reject) => {
        this.connect((err, client, release) => {
          if (err) return reject(err);
          client.query(statement).then(
            (result) => { release(); resolve(result); },
            (error) => { release(); reject(error); },
          );
        });
      });
    }
  }

  const http = { createServer(listener) { return listener; } };
  const mockRequire = (name) => {
    if (name === "pg") return { Pool };
    if (name === "node:http") return http;
    if (name === "node:fs") return { readFileSync: () => "synthetic-password" };
    if (name === "node:crypto") return crypto;
    if (name === "node:async_hooks") return { AsyncLocalStorage };
    throw new Error(`Unexpected import ${name}`);
  };
  mockRequire.resolve = () => "pg";
  const context = {
      require: mockRequire,
      process: {
        cwd: () => "/workspace/apps/web",
        env: {
          CP_COMPAT_TARGET_ID: "target-1", CP_COMPAT_MANIFEST_ID: "source-0040",
          CP_COMPAT_GENERATION: "source", CP_COMPAT_EXPECTED_HEAD: "0040_t173_numeric_money",
          CP_COMPAT_EXPECTED_API_PEER: "http://source-api:8000",
          CP_INTERNAL_API_URL: "http://source-api:8000",
          CP_DB_NAME: "carepulse", CP_DB_USER: "carepulse_runtime",
          CP_DB_PASSWORD_FILE: "/run/secrets/source_runtime_db_password",
        },
      },
  };
  vm.runInNewContext(
    fs.readFileSync(process.env.CP_SOURCE_WEB_ADAPTER_PATH ||
      path.join(__dirname, "../../ops/compat/source-web.cjs"), "utf8") +
      "\nthis.checkSourceReady = sourceReady;",
    context,
    { filename: "source-web.cjs" },
  );
  return { Pool, http, roles, executed, released, sqlChecks, checkSourceReady: context.checkSourceReady };
}

test("source native-auth pool.query and promise checkouts fail closed on effective privilege drift", async () => {
  const { Pool, roles, executed, released, sqlChecks } = loadAdapter();
  const auth = new Pool({ user: "carepulse_auth" });
  const runtime = new Pool({ user: "carepulse_runtime" });

  assert.equal((await auth.query("SELECT auth_data")).rows[0].ok, true);
  const client = await runtime.connect();
  client.release();
  assert.match(sqlChecks[0], /has_database_privilege/);
  assert.match(sqlChecks[0], /has_any_column_privilege/);
  assert.match(sqlChecks[0], /auth_unexpected_relation_access/);
  assert.match(sqlChecks[0], /format\('public\.%I', native_table\), 'INSERT'/);
  assert.match(sqlChecks[0], /\('user'\), \('session'\), \('account'\), \('verification'\)/);

  // REVOKE INSERT ON public."session" FROM carepulse_auth breaks the 0040 contract.
  roles.carepulse_auth.auth_native_crud = false;
  await assert.rejects(auth.query('INSERT INTO public."session" VALUES (...)'), /privilege mismatch/);
  assert.ok(!executed.includes('INSERT INTO public."session" VALUES (...)'));
  roles.carepulse_auth.auth_native_crud = true;

  roles.carepulse_auth.auth_unexpected_relation_access = true;
  await assert.rejects(auth.query("SELECT leaked_clinical_data"), /privilege mismatch/);
  assert.ok(!executed.includes("SELECT leaked_clinical_data"));
  assert.ok(released.some((r) => r.user === "carepulse_auth" && r.destroy));

  roles.carepulse_auth.auth_unexpected_relation_access = false;
  roles.carepulse_auth.auth_widened_metadata = true;
  await assert.rejects(auth.query("UPDATE organizations"), /privilege mismatch/);
  assert.ok(!executed.includes("UPDATE organizations"));
  roles.carepulse_auth.auth_widened_metadata = false;
  roles.carepulse_runtime.control_column_mutation = true;
  await assert.rejects(runtime.connect(), /privilege mismatch/);
  assert.ok(released.some((r) => r.user === "carepulse_runtime" && r.destroy));
  roles.carepulse_runtime.control_column_mutation = false;
  roles.carepulse_runtime.runtime_native_access = true;
  await assert.rejects(runtime.connect(), /privilege mismatch/);
});

test("already-running source Web fences readiness and requests after privilege drift", async () => {
  const { Pool, http, roles, executed, checkSourceReady } = loadAdapter();
  await checkSourceReady();
  let reached = 0;
  const auth = new Pool({ user: "carepulse_auth" });
  const handler = http.createServer(async (request, response) => {
    if (request.url === "/native") {
      try {
        await auth.query("SELECT authorized_native_data");
      } catch {
        response.writeHead(503);
        response.end("unavailable");
        return;
      }
    }
    reached++;
    response.writeHead(200);
    response.end("ok");
  });
  const call = async (url) => {
    const response = new (class extends EventEmitter {
      headersSent = false;
      writeHead(status) { this.status = status; this.headersSent = true; }
      end(body) { this.body = body; this.emit("finish"); }
    })();
    await handler({ url, headers: {} }, response);
    return response;
  };

  assert.equal((await call("/health/ready")).status, 200);
  assert.equal((await call("/login")).status, 200);
  roles.carepulse_auth.patient_read = true;
  assert.equal((await call("/native")).status, 503);
  assert.ok(!executed.includes("SELECT authorized_native_data"));
  roles.carepulse_runtime.rolinherit = true;
  assert.equal((await call("/health/ready")).status, 503);
  assert.equal((await call("/login")).status, 503);
  assert.equal(reached, 2);
  assert.ok(executed.some((statement) => statement.includes("record_own_attempt")));
});

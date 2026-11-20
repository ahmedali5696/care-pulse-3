/**
 * Web compatibility admission, proxy peer, and readiness unit/regression tests (T025).
 */
import assert from "node:assert/strict";
import test from "node:test";
import { Pool } from "pg";
import { authDatabase } from "../db.ts";
import {
  CompatAdmissionError,
  loadCompatWebConfig,
  permitLockKey,
  releaseWebAdmissionPermit,
  verifyControlAndHead,
  verifyPoolClientIdentity,
  verifyWebReadinessCompat,
} from "../compat/admission.ts";

test("permitLockKey produces deterministic signed 64-bit integer string", () => {
  const k1 = permitLockKey("target-1");
  const k2 = permitLockKey("target-1");
  const k3 = permitLockKey("target-2");
  assert.equal(k1, k2);
  assert.notEqual(k1, k3);
  assert.match(k1, /^-?\d+$/);
});

test("native auth pool.query uses the verified promise connection", { timeout: 5000 }, async () => {
  const originalConnect = Pool.prototype.connect;
  const savedTarget = process.env.CP_COMPAT_TARGET_ID;
  let releases = 0;
  Object.defineProperty(Pool.prototype, "connect", {
    configurable: true,
    writable: true,
    value: async (...args: unknown[]) => {
      assert.equal(args.length, 0, "callback-style Pool.connect would hang");
      return {
        query: async () => ({ rows: [{ ok: 1 }] }),
        release: () => { releases++; },
      };
    },
  });
  delete process.env.CP_COMPAT_TARGET_ID;
  try {
    const pool = authDatabase({
      authDatabase: { host: "127.0.0.1", port: 5432, name: "carepulse", user: "carepulse_auth", password: "synthetic" },
    } as Parameters<typeof authDatabase>[0]);
    const result = await pool.query("SELECT 1");
    assert.equal(result.rows[0].ok, 1);
    assert.equal(releases, 1);
    await pool.end();
  } finally {
    Object.defineProperty(Pool.prototype, "connect", {
      configurable: true,
      writable: true,
      value: originalConnect,
    });
    if (savedTarget === undefined) delete process.env.CP_COMPAT_TARGET_ID;
    else process.env.CP_COMPAT_TARGET_ID = savedTarget;
  }
});

test("loadCompatWebConfig rejects mismatched API peer", () => {
  const saved = { ...process.env };
  try {
    process.env.CP_DB_NAME = "carepulse";
    process.env.CP_COMPAT_TARGET_ID = "target-1";
    process.env.CP_COMPAT_GENERATION = "candidate";
    process.env.CP_COMPAT_EXPECTED_HEAD = "0046_f01_audit_integrity";
    process.env.CP_COMPAT_MANIFEST_ID = "candidate-0046";
    process.env.CP_COMPAT_EXPECTED_API_PEER = "http://source-api:8000";

    assert.throws(
      () => loadCompatWebConfig("http://candidate-api:8000"),
      CompatAdmissionError,
    );
  } finally {
    process.env = saved;
  }
});

test("loadCompatWebConfig rejects source head for candidate Web", async () => {
  const saved = { ...process.env };
  try {
    process.env.CP_COMPAT_TARGET_ID = "target-1";
    process.env.CP_COMPAT_GENERATION = "candidate";
    process.env.CP_COMPAT_EXPECTED_HEAD = "0040_t173_numeric_money";
    process.env.CP_COMPAT_MANIFEST_ID = "candidate-v1";
    assert.throws(() => loadCompatWebConfig("http://candidate-api:8000"), CompatAdmissionError);
    assert.equal(await verifyWebReadinessCompat(), false);
  } finally {
    process.env = saved;
  }
});

test("Web role verifies effective grants and denies privilege drift on a surviving connection", async () => {
  const config = {
    targetId: "target-1", manifestId: "candidate-v1",
    generation: "candidate" as const,
    expectedHead: "0046_f01_audit_integrity", dbName: "carepulse",
    expectedApiPeer: "http://candidate-api:8000",
  };
  const role = {
    db_name: "carepulse", session_user: "carepulse_web", current_user: "carepulse_web",
    rolsuper: false, rolbypassrls: false, rolcanlogin: true,
    rolcreatedb: false, rolcreaterole: false, rolreplication: false,
    rolinherit: false, role_member: false, can_connect: true,
    control_usage: true, control_reader: true, control_status_reader: true,
    control_update: false, control_delete: false, public_usage: true,
    patient_read: false, web_attempt_insert: true,
    private_usage: false, crypto_usage: false,
  };
  let current = { ...role };
  const fakeClient = { query: async () => ({ rows: [{ ...current }] }) };
  await verifyPoolClientIdentity(fakeClient as never, config, "carepulse_web");
  for (const change of [
    { control_reader: false }, { can_connect: false },
    { control_update: true }, { patient_read: true },
    { rolcanlogin: false }, { role_member: true },
    { web_attempt_insert: false },
  ]) {
    current = { ...role, ...change };
    await assert.rejects(
      verifyPoolClientIdentity(fakeClient as never, config, "carepulse_web"),
      CompatAdmissionError,
    );
  }
  current = role;
  await verifyPoolClientIdentity(fakeClient as never, config, "carepulse_web");
});

test("verifyPoolClientIdentity rejects superuser or wrong role", async () => {
  const fakeClient = {
    async query() {
      return {
        rows: [
          {
            db_name: "carepulse",
            session_user: "carepulse_runtime",
            current_user: "carepulse_runtime",
            rolsuper: false,
            rolbypassrls: false,
            rolcanlogin: true,
          },
        ],
      };
    },
  };
  await assert.rejects(
    verifyPoolClientIdentity(
      fakeClient as never,
      {
        targetId: "t1",
        manifestId: "m1",
        generation: "candidate",
        expectedHead: "0046_f01_audit_integrity",
        dbName: "carepulse",
        expectedApiPeer: "http://candidate-api:8000",
      },
      "carepulse_web",
    ),
    CompatAdmissionError,
  );
});

test("verifyControlAndHead rejects closed or intermediate head", async () => {
  const fakeClient = {
    async query() {
      return {
        rows: [
          {
            target_id: "t1",
            db_name: "carepulse",
            phase: "TARGET_OPEN",
            owner_epoch: 1,
            manifest_id: "m1",
            active_generation: "candidate",
            source_sealed: false,
            alembic_head: "0043_f01_writer_boundary",
          },
        ],
      };
    },
  };
  await assert.rejects(
    verifyControlAndHead(fakeClient as never, {
      targetId: "t1",
      manifestId: "m1",
      generation: "candidate",
      expectedHead: "0046_f01_audit_integrity",
      dbName: "carepulse",
      expectedApiPeer: "http://candidate-api:8000",
    }),
    CompatAdmissionError,
  );
});

test("source and candidate Web control readers admit only their active generation", async () => {
  for (const [generation, expectedHead, phase] of [
    ["source", "0040_t173_numeric_money", "SOURCE_OPEN"],
    ["candidate", "0046_f01_audit_integrity", "TARGET_OPEN"],
  ] as const) {
    const config = {
      targetId: "target-1", manifestId: `${generation}-manifest`,
      generation, expectedHead, dbName: "carepulse",
      expectedApiPeer: `http://${generation}-api:8000`,
    };
    const row = {
      target_id: "target-1", db_name: "carepulse", phase,
      owner_epoch: 3, manifest_id: config.manifestId,
      active_generation: generation, source_sealed: false, alembic_head: expectedHead,
    };
    const client = { query: async () => ({ rows: [{ ...row }] }) };
    assert.equal(await verifyControlAndHead(client as never, config, false), 3);
    Object.assign(row, { phase: "DRAINING", active_generation: "none" });
    await assert.rejects(
      verifyControlAndHead(client as never, config, false),
      CompatAdmissionError,
    );
  }
});

test("Web permit release cannot settle an unproven HTTP attempt", async () => {
  const statements: string[] = [];
  let released = false;
  const client = {
    async query(statement: string) { statements.push(statement); return { rows: [] }; },
    release() { released = true; },
  };
  await releaseWebAdmissionPermit({
    attemptId: "attempt-1", targetId: "target-1", ownerEpoch: 1,
    generation: "candidate", permitKey: "42", client: client as never,
  });
  assert.equal(released, true);
  assert.equal(statements.length, 1);
  assert.match(statements[0], /pg_advisory_unlock_shared/);
});

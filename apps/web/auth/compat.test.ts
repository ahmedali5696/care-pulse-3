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
  verifyControlAndHead,
  verifyPoolClientIdentity,
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

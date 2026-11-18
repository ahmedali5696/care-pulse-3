/**
 * Web compatibility admission, proxy peer, and readiness unit/regression tests (T025).
 */
import assert from "node:assert/strict";
import test from "node:test";
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
    async query(sql: string) {
      if (sql.includes("alembic_version")) {
        return { rows: [{ version_num: "0043_f01_writer_boundary" }] };
      }
      return { rows: [] };
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

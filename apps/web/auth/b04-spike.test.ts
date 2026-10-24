// Throwaway Issue #7 experiment. No production module imports this file.
import assert from "node:assert/strict";
import { AsyncLocalStorage } from "node:async_hooks";
import { createHash, generateKeyPairSync, randomUUID } from "node:crypto";
import { readFileSync } from "node:fs";
import { betterAuth, type BetterAuthOptions } from "better-auth";
import { createAdapterFactory, type CleanedWhere, type DBAdapter } from "better-auth/adapters";
import { verifyPassword } from "better-auth/crypto";
import { Pool, type PoolClient } from "pg";
import { encryptDeliveryToken, decryptDeliveryToken } from "../mail/delivery-material.ts";
import { lifecycleSchemaPlugin, sessionFields } from "./schema.ts";

assert.equal(process.env.CI, "true");
assert.equal(process.env.CP_ENV, "test");
assert.match(process.env.CP_CI_PROJECT ?? "", /^carepulse-ci-[0-9]+-[0-9]+$/);
const fixture = JSON.parse(readFileSync(0, "utf8")) as {
  clinic: string; staff: string; user: string; email: string; password: string;
};
assert.ok(fixture.email.endsWith("@example.invalid"));
const runtime = new Pool({ host: "db", database: "carepulse", user: "carepulse_runtime",
  password: readFileSync("/run/secrets/runtime_db_password", "utf8").trim(), max: 1 });
const observer = new Pool({ host: "db", database: "carepulse", user: "carepulse_migration",
  password: readFileSync("/run/secrets/migration_password", "utf8").trim(), max: 1 });
type Trace = { step: string; pid: number; xid: string; tenant: string | null };
type Transaction = { client: PoolClient; tenant: string | null; callbackError?: unknown };
const storage = new AsyncLocalStorage<Transaction>();
let trace: Trace[] = [];
const digest = (value: string) => createHash("sha256").update(value).digest("hex");
const quoted = (value: string) => `"${value.replaceAll('"', '""')}"`;

async function sql(statement: string, values: unknown[] = [], step = "domain") {
  const tx = storage.getStore();
  assert.ok(tx, "Every adapter/domain query requires the owned transaction");
  const identity = (await tx.client.query(
    "SELECT pg_backend_pid() pid, pg_current_xact_id()::text xid, " +
    "NULLIF(current_setting('app.organization_id',true),'') tenant",
  )).rows[0] as Omit<Trace, "step">;
  trace.push({ step, ...identity });
  assert.equal(identity.tenant, tx.tenant);
  return tx.client.query(statement, values);
}

async function owned<T>(tenant: string | null, work: () => Promise<T>): Promise<T> {
  if (storage.getStore()) {
    assert.equal(storage.getStore()!.tenant, tenant);
    return work();
  }
  const client = await runtime.connect();
  try {
    await client.query("BEGIN");
    await client.query("SELECT set_config('app.organization_id',$1,true)", [tenant ?? ""]);
    return await storage.run({ client, tenant }, async () => {
      const result = await work();
      // Better Auth swallows sendResetPassword errors; the owner must surface them.
      if (storage.getStore()!.callbackError) throw storage.getStore()!.callbackError;
      assert.equal((await client.query("COMMIT")).command, "COMMIT");
      return result;
    });
  } catch (error) {
    await client.query("ROLLBACK");
    throw error;
  } finally {
    client.release();
  }
}

function predicate(where: CleanedWhere[] = [], values: unknown[]) {
  return where.length ? " WHERE " + where.map((clause, index) => {
    assert.ok(!clause.connector || clause.connector === "AND");
    const operators: Partial<Record<CleanedWhere["operator"], string>> =
      { eq: "=", lt: "<", gt: ">", lte: "<=", gte: ">=" };
    const operator = operators[clause.operator];
    assert.ok(operator, `Unsupported spike predicate: ${clause.operator}`);
    values.push(clause.value);
    return `${index ? "AND " : ""}${quoted(clause.field)} ${operator} $${values.length}`;
  }).join(" ") : "";
}

function database(options: BetterAuthOptions): DBAdapter {
  const table = (model: string) => {
    assert.ok(["user", "account", "session", "verification"].includes(model));
    return `public.${quoted(model)}`;
  };
  const find = async (model: string, where: CleanedWhere[] = [], limit = 100,
    sortBy?: { field: string; direction: "asc" | "desc" }) => {
    const values: unknown[] = [];
    const filter = predicate(where, values);
    values.push(limit);
    const order = sortBy ? ` ORDER BY ${quoted(sortBy.field)} ${sortBy.direction === "asc" ? "ASC" : "DESC"}` : "";
    return (await sql(`SELECT * FROM ${table(model)}${filter}${order} LIMIT $${values.length}`,
      values, `${model}.read`)).rows;
  };
  const update = async (model: string, where: CleanedWhere[], data: Record<string, unknown>) => {
    assert.equal(model, "account");
    const values = Object.values(data);
    const set = Object.keys(data).map((key, index) => `${quoted(key)}=$${index + 1}`).join(",");
    return sql(`UPDATE ${table(model)} SET ${set}${predicate(where, values)} RETURNING *`,
      values, "credential.update");
  };
  const adapter: DBAdapter = createAdapterFactory({
    config: { adapterId: "b04-spike-only", supportsDates: true, supportsBooleans: true,
      supportsJSON: true, transaction: (work) =>
        owned(storage.getStore()?.tenant ?? null, () => work(adapter)) },
    adapter: () => ({
      create: async ({ model, data }) => {
        assert.equal(model, "verification");
        const fields = Object.keys(data);
        await sql(`INSERT INTO ${table(model)} (${fields.map(quoted).join(",")}) VALUES (` +
          fields.map((_, index) => `$${index + 1}`).join(",") + ")", Object.values(data), "verification.create");
        return data;
      },
      findOne: async <T>({ model, where }: { model: string; where: CleanedWhere[] }) =>
        (await find(model, where, 1))[0] as T ?? null,
      findMany: async <T>({ model, where, limit, sortBy }: {
        model: string; where?: CleanedWhere[]; limit: number;
        sortBy?: { field: string; direction: "asc" | "desc" };
      }) => await find(model, where, limit, sortBy) as T[],
      update: async <T>({ model, where, update: data }: { model: string; where: CleanedWhere[]; update: T }) =>
        (await update(model, where, data as Record<string, unknown>)).rows[0] as T ?? null,
      updateMany: async ({ model, where, update: data }) => (await update(model, where, data)).rowCount ?? 0,
      delete: async () => { throw new Error("Unused spike adapter method: delete"); },
      deleteMany: async ({ model, where }) => {
        assert.ok(["session", "verification"].includes(model));
        const values: unknown[] = [];
        return (await sql(`DELETE FROM ${table(model)}${predicate(where, values)}`, values,
          `${model}.delete`)).rowCount ?? 0;
      },
      consumeOne: async <T>({ model, where }: { model: string; where: CleanedWhere[] }) => {
        assert.equal(model, "verification");
        const values: unknown[] = [];
        const filter = predicate(where, values);
        return (await sql(`DELETE FROM ${table(model)} WHERE id = (SELECT id FROM ${table(model)}` +
          `${filter} LIMIT 1 FOR UPDATE) RETURNING *`, values, "verification.consume")).rows[0] as T ?? null;
      },
      count: async () => { throw new Error("Unused spike adapter method: count"); },
    }),
  })(options);
  return adapter;
}

const { publicKey, privateKey } = generateKeyPairSync("rsa", { modulusLength: 3072,
  publicKeyEncoding: { type: "spki", format: "pem" }, privateKeyEncoding: { type: "pkcs8", format: "pem" } });
const keyId = "synthetic-b04-spike";
let fault = "";
let issued: { token: string; verification: string; job: string; context: Parameters<typeof encryptDeliveryToken>[1] };
const callbackFailure = new Error("synthetic-callback-failure");
const auth = betterAuth({ database, baseURL: "http://localhost:3000",
  secret: "synthetic-b04-spike-only-never-use-in-production-123456789",
  plugins: [lifecycleSchemaPlugin], session: { additionalFields: sessionFields },
  verification: { storeInDatabase: true, storeIdentifier: "hashed", disableCleanup: true },
  emailAndPassword: { enabled: true, disableSignUp: true, revokeSessionsOnPasswordReset: true,
    sendResetPassword: async ({ token }) => {
      try {
        const verification = (await sql('SELECT id,"expiresAt" FROM public.verification WHERE value=$1 ORDER BY "createdAt" DESC LIMIT 1',
          [fixture.user], "issuance.lookup")).rows[0];
        const job = randomUUID();
        const context = { job_id: job, proof_reference: verification.id, purpose: "PASSWORD_RESET",
          tenant_id: fixture.clinic, committed_recipient: fixture.email,
          expiry_epoch_ms: String(verification.expiresAt.getTime()) };
        issued = { token, verification: verification.id, job, context };
        await sql(`INSERT INTO public.delivery_jobs
          (id,organization_id,staff_id,verification_id,proof_reference,purpose,recipient,template,logical_key,proof_expires_at)
          VALUES ($1::uuid,$2,$3,$4,$4,'PASSWORD_RESET',$5,'password-reset',$1::text,$6)`,
        [job, fixture.clinic, fixture.staff, verification.id, fixture.email, verification.expiresAt], "job.insert");
        if (fault === "after-job") throw callbackFailure;
        const material = encryptDeliveryToken(token, context, keyId, publicKey);
        await sql(`INSERT INTO public.protected_delivery_material
          (job_id,format_version,delivery_key_id,ciphertext_base64url) VALUES ($1,1,$2,$3)`,
        [job, material.key_id, material.ciphertext_base64url], "material.insert");
        if (fault === "after-material") throw callbackFailure;
      } catch (error) {
        storage.getStore()!.callbackError = error;
        throw error;
      }
    },
  }, logger: { disabled: true },
});

function sameTransaction(required: string[], tenant = fixture.clinic) {
  for (const step of required) assert.ok(trace.some((row) => row.step === step), `Missing observed effect: ${step}`);
  assert.equal(new Set(trace.map((row) => row.pid)).size, 1);
  assert.equal(new Set(trace.map((row) => row.xid)).size, 1);
  assert.ok(trace.every((row) => row.tenant === tenant));
  console.log(JSON.stringify({ observed: required, connection: trace[0].pid, transaction: trace[0].xid }));
}

async function counts() {
  return (await observer.query(`SELECT
    (SELECT count(*)::int FROM public.verification WHERE value=$1) verification,
    (SELECT count(*)::int FROM public.delivery_jobs WHERE staff_id=$2) jobs,
    (SELECT count(*)::int FROM public.protected_delivery_material m JOIN public.delivery_jobs j ON j.id=m.job_id WHERE j.staff_id=$2) material`,
  [fixture.user, fixture.staff])).rows[0];
}
async function cleanReuse() {
  const client = await runtime.connect();
  try {
    const row = (await client.query("SELECT pg_backend_pid() pid, NULLIF(current_setting('app.organization_id',true),'') tenant")).rows[0];
    assert.equal(row.pid, trace[0].pid);
    assert.equal(row.tenant, null);
    assert.equal((await client.query("SELECT id FROM public.staff_accounts WHERE id=$1", [fixture.staff])).rowCount, 0);
  } finally { client.release(); }
}
async function request(tenant: string | null) {
  return owned(tenant, () => auth.api.requestPasswordReset({ body: { email: fixture.email } }));
}

try {
  for (const mode of ["after-job", "after-material"]) {
    fault = mode;
    trace = [];
    await assert.rejects(() => request(fixture.clinic), (error) => error === callbackFailure);
    assert.deepEqual(await counts(), { verification: 0, jobs: 0, material: 0 });
    sameTransaction(["verification.create", "job.insert", ...(mode === "after-material" ? ["material.insert"] : [])]);
    await cleanReuse();
    console.log(`PASS atomic issuance rollback and original callback error: ${mode}`);
  }
  fault = "";
  for (const tenant of [null, randomUUID()]) {
    trace = [];
    await assert.rejects(() => request(tenant), (error: unknown) => (error as { code: string }).code === "42501");
    assert.deepEqual(await counts(), { verification: 0, jobs: 0, material: 0 });
    await cleanReuse();
    console.log(`PASS denied ${tenant === null ? "missing" : "foreign"} context and clean rollback reuse`);
  }
  trace = [];
  assert.equal((await request(fixture.clinic)).status, true);
  assert.deepEqual(await counts(), { verification: 1, jobs: 1, material: 1 });
  sameTransaction(["verification.create", "job.insert", "material.insert"]);
  await cleanReuse();
  const stored = (await observer.query("SELECT identifier FROM public.verification WHERE id=$1", [issued!.verification])).rows[0];
  assert.ok(stored.identifier !== `reset-password:${issued!.token}`, "Native proof must be hashed");
  const material = (await observer.query("SELECT * FROM public.protected_delivery_material WHERE job_id=$1", [issued!.job])).rows[0];
  assert.ok(decryptDeliveryToken({ version: 1, key_id: material.delivery_key_id,
    ciphertext_base64url: material.ciphertext_base64url }, issued!.context, { [keyId]: privateKey }, digest(issued!.token)) === issued!.token,
  "Encrypted material must preserve the exact native proof");
  console.log("PASS committed native hashed verification, job and original-token encrypted material");

  const operation = randomUUID();
  await owned(fixture.clinic, async () => {
    await sql(`INSERT INTO public.recovery_operations
      (id,organization_id,staff_id,auth_user_id,purpose,proof_digest,proof_reference,proof_expires_at,
       request_fingerprint,fingerprint_key_version,captured_staff_revision,captured_lifecycle_revision,reserved_security_revision)
      VALUES ($1,$2,$3,$4,'PASSWORD_RESET',$5,$6,clock_timestamp()+interval '20 minutes',$7,'synthetic-v1',0,0,1)`,
    [operation, fixture.clinic, fixture.staff, fixture.user, digest(issued!.token), issued!.verification, digest("synthetic-request")]);
    await sql("UPDATE public.staff_accounts SET reset_block_operation_id=$1,security_revision=security_revision+1 WHERE id=$2",
      [operation, fixture.staff]);
  });
  for (let index = 0; index < 2; index++) {
    await observer.query(`INSERT INTO public.session
      (id,token,"userId","createdAt","updatedAt","expiresAt","securityRevision","absoluteDeadline","lastActivity")
      VALUES ($1,$2,$3,clock_timestamp(),clock_timestamp(),clock_timestamp()+interval '1 hour',0,clock_timestamp()+interval '1 hour',clock_timestamp())`,
    [randomUUID(), randomUUID(), fixture.user]);
  }
  const passwordBefore = (await observer.query('SELECT password FROM public.account WHERE "userId"=$1', [fixture.user])).rows[0].password;
  const newPassword = "Synthetic-B04-new-password-for-spike-only!";
  const reset = (fail: boolean) => owned(fixture.clinic, async () => {
    assert.equal((await sql(`SELECT id FROM public.recovery_operations
      WHERE id=$1 AND state='RESERVED' AND NOT invalidated FOR UPDATE`, [operation])).rowCount, 1);
    const result = await auth.api.resetPassword({ body: { token: issued!.token, newPassword } });
    assert.equal(result.status, true);
    assert.equal((await sql("SELECT id FROM public.verification WHERE id=$1", [issued!.verification])).rowCount, 0);
    assert.equal((await sql('SELECT id FROM public.session WHERE "userId"=$1', [fixture.user])).rowCount, 0);
    const changedHash = (await sql('SELECT password FROM public.account WHERE "userId"=$1', [fixture.user])).rows[0].password;
    assert.ok(await verifyPassword({ hash: changedHash, password: newPassword }));
    assert.ok((await observer.query('SELECT password FROM public.account WHERE "userId"=$1', [fixture.user])).rows[0].password === passwordBefore);
    await sql("UPDATE public.recovery_operations SET auth_fence=1,state='AUTH_DONE',auth_done_at=clock_timestamp() WHERE id=$1 AND state='RESERVED' AND NOT invalidated",
      [operation], "recovery.fence");
    // A real CHECK violation proves SQL persistence failure, rather than a mocked callback.
    await sql(`INSERT INTO public.auth_recovery_completions
      (id,proof_digest,auth_user_id,request_fingerprint,reserved_security_revision,auth_fence,completed_at)
      SELECT id,proof_digest,auth_user_id,request_fingerprint,reserved_security_revision,
      ${fail ? "0" : "auth_fence"},auth_done_at FROM public.recovery_operations WHERE id=$1`,
    [operation], "completion.insert");
  });
  trace = [];
  await assert.rejects(() => reset(true), (error: unknown) => (error as { code: string }).code === "23514");
  sameTransaction(["verification.consume", "session.delete", "credential.update", "recovery.fence", "completion.insert"]);
  await cleanReuse();
  assert.equal((await observer.query('SELECT password FROM public.account WHERE "userId"=$1', [fixture.user])).rows[0].password, passwordBefore);
  assert.equal((await observer.query('SELECT id FROM public.session WHERE "userId"=$1', [fixture.user])).rowCount, 2);
  assert.equal((await counts()).verification, 1);
  assert.equal((await observer.query("SELECT id FROM public.auth_recovery_completions WHERE id=$1", [operation])).rowCount, 0);
  assert.deepEqual((await observer.query("SELECT state,auth_fence::int FROM public.recovery_operations WHERE id=$1", [operation])).rows[0],
    { state: "RESERVED", auth_fence: 0 });
  console.log("PASS failed completion persistence rolls back proof, credential, both sessions and recovery fence");
  trace = [];
  await reset(false);
  sameTransaction(["verification.consume", "session.delete", "credential.update", "recovery.fence", "completion.insert"]);
  await cleanReuse();
  const passwordAfter = (await observer.query('SELECT password FROM public.account WHERE "userId"=$1', [fixture.user])).rows[0].password;
  assert.equal(await verifyPassword({ hash: passwordAfter, password: newPassword }), true);
  assert.equal(await verifyPassword({ hash: passwordAfter, password: fixture.password }), false);
  assert.equal((await observer.query('SELECT id FROM public.session WHERE "userId"=$1', [fixture.user])).rowCount, 0);
  assert.equal((await counts()).verification, 0);
  assert.equal((await observer.query("SELECT id FROM public.auth_recovery_completions WHERE id=$1", [operation])).rowCount, 1);
  console.log("PASS native reset commit control");

  for (const statement of ["SELECT ciphertext_base64url FROM public.protected_delivery_material",
    "INSERT INTO public.protected_delivery_material (job_id,format_version,delivery_key_id,ciphertext_base64url) VALUES ($1,1,$2,$3) RETURNING ciphertext_base64url"]) {
    await assert.rejects(() => owned(fixture.clinic, () => sql(statement,
      statement.startsWith("INSERT") ? [issued!.job, keyId, material.ciphertext_base64url] : [])),
    (error: unknown) => (error as { code: string }).code === "42501");
  }
  const protectedTables = ["staff_accounts", "recovery_operations", "auth_recovery_completions", "delivery_jobs", "protected_delivery_material"];
  const rls = await observer.query("SELECT relname,relrowsecurity,relforcerowsecurity FROM pg_class WHERE relnamespace='public'::regnamespace AND relname=ANY($1)", [protectedTables]);
  assert.equal(rls.rowCount, protectedTables.length);
  assert.ok(rls.rows.every((row) => row.relrowsecurity && row.relforcerowsecurity));
  const role = (await runtime.query("SELECT rolsuper,rolbypassrls FROM pg_roles WHERE rolname=current_user")).rows[0];
  assert.deepEqual(role, { rolsuper: false, rolbypassrls: false });
  console.log("PASS unchanged FORCE RLS, non-bypass runtime, denied ciphertext SELECT/RETURNING");
  console.log("FEASIBILITY PROOFS PASSED; Issue #7 remains OPEN/BLOCKED pending architecture review");
} catch (error) {
  // Keep proof/password/ciphertext values out of failing assertion and SQL diagnostics.
  const failure = error as { name?: string; code?: string; constraint?: string; stack?: string };
  console.error(JSON.stringify({ blocked: true, name: failure.name, code: failure.code,
    constraint: failure.constraint, lastStep: trace.at(-1)?.step,
    frames: failure.stack?.split("\n").slice(1, 5) }));
  process.exitCode = 1;
} finally {
  await runtime.end();
  await observer.end();
}

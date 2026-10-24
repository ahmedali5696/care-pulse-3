// Disposable Compose integration: exercise the production adapter against real PostgreSQL.
import assert from "node:assert/strict";
import { randomUUID } from "node:crypto";
import test from "node:test";
import type { BetterAuthOptions } from "better-auth";
import { Pool, type PoolClient } from "pg";
import { loadWebConfig } from "../config.ts";
import { postgresAuthAdapter, scopedAuthClient, withAuthTransaction } from "./postgres-adapter.ts";
import { lifecycleSchemaPlugin, sessionFields } from "./schema.ts";

assert.equal(process.env.CI, "true");
assert.equal(process.env.CP_ENV, "test");
assert.match(process.env.CP_CI_PROJECT ?? "", /^carepulse-ci-[0-9]+-[0-9]+$/);
const clinic = process.env.CP_ADAPTER_TEST_CLINIC ?? "";
assert.match(clinic, /^[0-9a-f]{8}-[0-9a-f]{4}-[1-8][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/i);
const settings = loadWebConfig().database;
assert.equal(settings.user, "carepulse_runtime");
const credentials = {
  host: settings.host, port: settings.port, database: settings.name,
  user: settings.user, password: settings.password,
  connectionTimeoutMillis: 1000, statement_timeout: 3000, lock_timeout: 2000,
};
const pool = new Pool({ ...credentials, max: 2 });
const single = new Pool({ ...credentials, max: 1 });
const runtimeFactory = postgresAuthAdapter(pool);
const options: BetterAuthOptions = {
  database: runtimeFactory, plugins: [lifecycleSchemaPlugin],
  session: { additionalFields: sessionFields },
  emailAndPassword: { enabled: true },
};
const adapter = runtimeFactory(options);
const singleAdapter = postgresAuthAdapter(single)(options);
const run = randomUUID();
const emailA = `adapter-${run}-a@example.invalid`;
const emailB = `adapter-${run}-b@example.invalid`;
const identifier = `adapter-${run}`;
const current = new Date();
const expiry = new Date(current.getTime() + 3600_000);
const eq = (field: string, value: string | number | null) =>
  [{ field, value, operator: "eq" as const, connector: "AND" as const }];
type UserRow = { id: string; email: string; name: string; image: string | null; createdAt: Date };
type VerificationRow = { id: string; identifier: string; expiresAt: Date };

async function createUser(email: string): Promise<UserRow> {
  return await adapter.create({
    model: "user", data: { name: `Adapter ${run}`, email, emailVerified: false,
      image: null, createdAt: current, updatedAt: current },
  }) as UserRow;
}

async function createVerification(suffix: string): Promise<VerificationRow> {
  return await adapter.create({
    model: "verification", data: {
      identifier: `${identifier}-${suffix}`, value: randomUUID(),
      expiresAt: expiry, createdAt: current, updatedAt: current,
    },
  }) as VerificationRow;
}

async function cleanSyntheticRows() {
  await pool.query('DELETE FROM public."verification" WHERE identifier LIKE $1', [`${identifier}%`]);
  // Native FK CASCADE removes any account/session rows created by these cases.
  await pool.query('DELETE FROM public."user" WHERE email = ANY($1::text[])', [[emailA, emailB]]);
}

test("Stage-1 native adapter parity with real PostgreSQL, RLS and leased transactions", async (t) => {
  try {
    await t.test("all four native models persist fields, dates, nulls and fallback joins", async () => {
      const user = await createUser(emailA);
      assert.ok(user.id);
      assert.equal(user.image, null);
      assert.equal(user.createdAt.getTime(), current.getTime());
      const account = await adapter.create({ model: "account", data: {
        accountId: user.id, providerId: "credential", userId: user.id,
        password: "synthetic-adapter-credential", createdAt: current, updatedAt: current,
      } });
      assert.ok(account.id);
      const session = await adapter.create({ model: "session", data: {
        userId: user.id, token: randomUUID(), expiresAt: expiry,
        createdAt: current, updatedAt: current, securityRevision: 0,
        absoluteDeadline: expiry, lastActivity: current,
        ipAddress: null, userAgent: null,
      } });
      assert.ok(session.id);
      const proof = await createVerification("basic");
      assert.ok(proof.id);
      assert.equal(proof.expiresAt.getTime(), expiry.getTime());
      const linked = await adapter.findOne<{ account: Array<{ id: string }> }>({
        model: "user", where: eq("id", user.id), join: { account: true },
      });
      assert.equal(linked?.account.length, 1);
      assert.equal(linked.account[0].id, account.id);
      const sessionWithUser = await adapter.findOne<{ user: UserRow; lastActivity: Date; ipAddress: null }>({
        model: "session", where: eq("id", session.id), join: { user: true },
      });
      assert.equal(sessionWithUser?.user.id, user.id);
      assert.equal(sessionWithUser?.ipAddress, null);
      assert.equal(sessionWithUser?.lastActivity.getTime(), current.getTime());
      const advancedActivity = new Date(current.getTime() + 1000);
      const advanced = await adapter.update<{ lastActivity: Date }>({ model: "session",
        where: eq("id", session.id), update: { lastActivity: advancedActivity } });
      assert.equal(advanced?.lastActivity.getTime(), advancedActivity.getTime());
      await adapter.delete({ model: "session", where: eq("id", session.id) });
      assert.equal(await adapter.count({ model: "session", where: eq("id", session.id) }), 0);
      assert.equal((await adapter.update<{ password: string }>({ model: "account",
        where: eq("id", account.id), update: { password: "synthetic-changed-credential" } }))?.password,
      "synthetic-changed-credential");
      await adapter.delete({ model: "account", where: eq("id", account.id) });
      assert.equal(await adapter.count({ model: "account", where: eq("id", account.id) }), 0);
      assert.equal((await adapter.findOne<VerificationRow>({ model: "verification",
        where: eq("id", proof.id) }))?.id, proof.id);
    });

    await t.test("where, null, pagination, select and count preserve native semantics", async () => {
      const other = await createUser(emailB);
      assert.ok(other.id);
      const found = await adapter.findMany<UserRow>({ model: "user", where: [
        { field: "email", operator: "starts_with", value: `adapter-${run}`, connector: "AND" },
        { field: "image", operator: "eq", value: null, connector: "AND" },
      ], limit: 10, sortBy: { field: "email", direction: "asc" } });
      assert.deepEqual(found.map((user) => user.email), [emailA, emailB]);
      assert.equal(await adapter.count({ model: "user", where: [
        { field: "email", operator: "ends_with", value: "-a@example.invalid" },
      ] }), 1);
      assert.equal(await adapter.count({ model: "user", where: [
        { field: "email", operator: "contains", value: run + "-b@" },
      ] }), 1);
      await adapter.update({ model: "user", where: eq("email", emailB),
        update: { image: "synthetic-avatar" } });
      assert.equal(await adapter.count({ model: "user", where: [
        { field: "email", operator: "eq", value: emailB },
        { field: "image", operator: "ne", value: null, connector: "AND" },
      ] }), 1);
      assert.equal(await adapter.count({ model: "user", where: [
        { field: "email", operator: "in", value: [emailA, emailB] },
      ] }), 2);
      assert.deepEqual((await adapter.findMany<UserRow>({ model: "user", where: [
        { field: "email", operator: "in", value: [emailA, emailB] },
      ], limit: 1, offset: 1, sortBy: { field: "email", direction: "asc" } }))
        .map((user) => user.email), [emailB]);
      const selected = await adapter.findOne<{ email: string; name?: string }>({
        model: "user", where: [{ field: "email", operator: "eq", value: emailA.toUpperCase(),
          mode: "insensitive" }], select: ["email"],
      });
      assert.equal(selected?.email, emailA);
      assert.equal(Object.hasOwn(selected ?? {}, "name"), false);
      assert.equal(await adapter.count({ model: "user", where: [
        { field: "email", operator: "eq", value: emailA, connector: "OR" },
        { field: "email", operator: "eq", value: emailB, connector: "OR" },
      ] }), 2);
      assert.equal(await adapter.count({ model: "user", where: [
        { field: "email", operator: "not_in", value: [emailA, emailB] },
        { field: "email", operator: "starts_with", value: `adapter-${run}`, connector: "AND" },
      ] }), 0);
    });

    await t.test("writes expose exact affected rows and reject unsupported models", async () => {
      assert.equal(await adapter.update<UserRow>({ model: "user", where: eq("email", emailA),
        update: { name: `Updated ${run}` } }).then((row) => row?.name), `Updated ${run}`);
      assert.equal(await adapter.update<UserRow>({ model: "user", where: eq("email", "missing@example.invalid"),
        update: { name: "Absent" } }), null);
      assert.equal(await adapter.updateMany({ model: "user", where: eq("name", `Adapter ${run}`),
        update: { name: `Updated ${run}` } }), 1);
      assert.equal(await adapter.updateMany({ model: "user", where: eq("name", "Absent"),
        update: { name: "Absent" } }), 0);
      assert.equal(await adapter.count({ model: "user", where: eq("name", `Updated ${run}`) }), 2);
      assert.equal(await adapter.updateMany({ model: "user", where: eq("name", `Updated ${run}`),
        update: { name: `Adapter ${run}` } }), 2);
      assert.equal(await adapter.deleteMany({ model: "verification", where: eq("identifier", `${identifier}-absent`) }), 0);
      const basic = await adapter.findOne<VerificationRow>({ model: "verification",
        where: eq("identifier", `${identifier}-basic`) });
      assert.ok(basic);
      const revisedExpiry = new Date(expiry.getTime() + 1000);
      const revised = await adapter.update<VerificationRow>({ model: "verification",
        where: eq("id", basic.id), update: { expiresAt: revisedExpiry } });
      assert.equal(revised?.expiresAt.getTime(), revisedExpiry.getTime());
      assert.equal(await adapter.deleteMany({ model: "verification", where: eq("identifier", `${identifier}-basic`) }), 1);
      await createVerification("remove");
      await createVerification("remove");
      assert.equal(await adapter.deleteMany({ model: "verification", where: eq("identifier", `${identifier}-remove`) }), 2);
      await adapter.delete({ model: "session", where: eq("userId", "missing") });
      await adapter.delete({ model: "user", where: eq("email", emailB) });
      assert.equal(await adapter.count({ model: "user", where: eq("email", emailB) }), 0);
      await assert.rejects(adapter.findOne({ model: "staff_accounts", where: eq("organization_id", clinic) }));
      await assert.rejects(adapter.findOne({ model: "protected_delivery_material", where: eq("job_id", randomUUID()) }));
      await assert.rejects(adapter.findOne({ model: "user", where: eq("unexpected", "x") }));
    });

    await t.test("verification consumeOne has one winner under contention and deletes at most one", async () => {
      const first = await createVerification("consume");
      const second = await createVerification("consume");
      const consumed = await adapter.consumeOne<VerificationRow>({ model: "verification",
        where: eq("identifier", `${identifier}-consume`) });
      assert.ok(consumed?.id === first.id || consumed?.id === second.id);
      assert.equal(await adapter.count({ model: "verification", where: eq("identifier", `${identifier}-consume`) }), 1);
      const competing = await Promise.all(Array.from({ length: 4 }, () =>
        adapter.consumeOne<VerificationRow>({ model: "verification",
          where: eq("identifier", `${identifier}-consume`) })));
      assert.equal(competing.filter(Boolean).length, 1);
      assert.equal(await adapter.count({ model: "verification", where: eq("identifier", `${identifier}-consume`) }), 0);
    });

    await t.test("nested writes roll back together; tenant, lease and GUC do not leak", async () => {
      const originalConnect = single.connect.bind(single);
      const leased: PoolClient[] = [];
      single.connect = (async () => {
        const client = await originalConnect();
        leased.push(client);
        return client;
      }) as typeof single.connect;
      const before = await single.connect();
      const pid = (await before.query<{ pid: number }>("SELECT pg_backend_pid() AS pid")).rows[0].pid;
      before.release();
      leased.length = 0;
      const failed = new Error("synthetic-rollback");
      assert.equal(scopedAuthClient(single, clinic), null);
      await assert.rejects(withAuthTransaction(single, clinic, async () => {
        const client = leased[0];
        assert.ok(client);
        assert.strictEqual(scopedAuthClient(single, clinic), client);
        assert.throws(() => scopedAuthClient(single, randomUUID()), /Auth transaction boundary mismatch/);
        assert.throws(() => scopedAuthClient(pool, clinic), /Auth transaction boundary mismatch/);
        const active = await client.query<{ pid: number; tenant: string }>(
          "SELECT pg_backend_pid() AS pid, current_setting('app.organization_id',true) AS tenant");
        assert.equal(active.rows[0].pid, pid);
        assert.equal(active.rows[0].tenant, clinic);
        const visible = await client.query("SELECT id FROM public.staff_accounts WHERE organization_id=$1", [clinic]);
        assert.ok((visible.rowCount ?? 0) > 0);
        await singleAdapter.transaction(async (transaction) => {
          await transaction.create({ model: "verification", data: {
            identifier: `${identifier}-rollback`, value: randomUUID(),
            expiresAt: expiry, createdAt: current, updatedAt: current,
          } });
          assert.equal((await transaction.count({ model: "verification",
            where: eq("identifier", `${identifier}-rollback`) })), 1);
        });
        assert.equal(leased.length, 1);
        await assert.rejects(async () => withAuthTransaction(single, randomUUID(), async () => true),
          /Auth transaction boundary mismatch/);
        await assert.rejects(async () => withAuthTransaction(pool, clinic, async () => true),
          /Auth transaction boundary mismatch/);
        throw failed;
      }), (error) => error === failed);
      assert.equal(scopedAuthClient(single, clinic), null);
      assert.equal(await adapter.count({ model: "verification", where: eq("identifier", `${identifier}-rollback`) }), 0);
      const after = await single.connect();
      try {
        const clean = await after.query<{ pid: number; tenant: string | null }>(
          "SELECT pg_backend_pid() AS pid, NULLIF(current_setting('app.organization_id',true),'') AS tenant");
        assert.equal(clean.rows[0].pid, pid);
        assert.equal(clean.rows[0].tenant, null);
        assert.equal((await after.query("SELECT id FROM public.staff_accounts WHERE organization_id=$1", [clinic])).rowCount, 0);
      } finally { after.release(); }
      assert.equal(await withAuthTransaction(single, clinic, () => singleAdapter.count({
        model: "verification", where: eq("identifier", `${identifier}-rollback`),
      })), 0);
      for (const unavailable of [null, randomUUID()]) {
        leased.length = 0;
        await withAuthTransaction(single, unavailable, async () => {
          const held = leased[0];
          assert.ok(held);
          assert.equal((await held.query("SELECT id FROM public.staff_accounts WHERE organization_id=$1", [clinic])).rowCount, 0);
        });
      }
      await assert.rejects(withAuthTransaction(single, clinic, async () => {
        const held = leased.at(-1);
        assert.ok(held);
        await held.query("SELECT ciphertext_base64url FROM public.protected_delivery_material");
      }), (error: unknown) => (error as { code?: string }).code === "42501");
      await assert.rejects(withAuthTransaction(single, clinic, async () => {
        const held = leased.at(-1);
        assert.ok(held);
        try { await held.query("SELECT ciphertext_base64url FROM public.protected_delivery_material"); }
        catch (error) {
          assert.equal((error as { code?: string }).code, "42501");
        }
        return "must-not-commit";
      }), /Auth transaction did not commit/);
      const again = await single.connect();
      try {
        assert.equal((await again.query<{ tenant: string | null }>(
          "SELECT NULLIF(current_setting('app.organization_id',true),'') AS tenant")).rows[0].tenant, null);
      } finally { again.release(); }
    });
  } finally {
    try { await cleanSyntheticRows(); }
    finally { await Promise.all([pool.end(), single.end()]); }
  }
});

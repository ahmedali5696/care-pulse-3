// Private CLI only; never mount as an HTTP route or create an auth identity.
import assert from "node:assert/strict";
import { createHmac } from "node:crypto";
import { getAuth } from "../auth.ts";
import { loadWebConfig } from "../config.ts";
import { runtimeDatabase } from "../db.ts";
import { sendSyntheticMail } from "./transport.ts";

async function smoke() {
  const mode = process.argv[2];
  const config = loadWebConfig();
  if (mode === "mail") {
    await sendSyntheticMail("invitation", "invitation@example.invalid");
    await sendSyntheticMail("reset", "reset@example.invalid");
    console.log("PASS two synthetic mail templates accepted by Mailpit");
  } else if (mode === "auth") {
    try {
      const auth = getAuth();
      assert.equal(typeof auth.options.database, "function");
      assert.equal(auth.options.session?.cookieCache?.enabled, false);
      assert.equal("secondaryStorage" in auth.options, false);
      assert.equal(auth.options.emailAndPassword?.enabled, true);
      assert.equal(auth.options.emailAndPassword?.disableSignUp, true);
      assert.equal("socialProviders" in auth.options, false);
      assert.deepEqual(auth.options.plugins?.map((plugin) => plugin.id), ["carepulse-lifecycle"]);
      for (const cookie of ["", "better-auth.session_token=forged; better-auth.session_data=forged"]) {
        assert.equal(await auth.api.getSession({ headers: new Headers({ cookie }) }), null);
      }
      const context = await auth.$context;
      assert.equal(context.adapter.id, "carepulse-postgres");
      const token = "synthetic-nonexistent-session";
      const signature = createHmac("sha256", config.authSecret).update(token).digest("base64");
      const cookie = `${context.authCookies.sessionToken.name}=${encodeURIComponent(`${token}.${signature}`)}`;
      const response = await fetch("http://127.0.0.1:3000/api/backend/__foundation_denied__", {
        headers: { cookie }, signal: AbortSignal.timeout(5000),
      });
      assert.equal(response.status, 401, "Unknown native session must be unauthenticated");
      assert.equal(response.headers.get("cache-control"), "no-store");
      const body = await response.json();
      assert.equal(body.error.code, "UNAUTHENTICATED");
      console.log("PASS PostgreSQL/no-cache/guarded auth and unknown-session HTTP denial");
    } finally {
      await runtimeDatabase(config).end();
    }
  } else {
    throw new Error("Unknown smoke mode");
  }
}

try {
  await smoke();
} catch {
  console.error("FAIL private foundation smoke (diagnostics withheld)");
  process.exitCode = 1;
}

// Private CLI only; never mount as an HTTP route or create an auth identity.
import assert from "node:assert/strict";
import { createHmac } from "node:crypto";
import { Pool } from "pg";
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
      assert(auth.options.database instanceof Pool);
      assert.equal(auth.options.session?.cookieCache?.enabled, false);
      assert.equal("secondaryStorage" in auth.options, false);
      assert.equal(auth.options.emailAndPassword?.enabled, false);
      assert.equal(auth.options.emailAndPassword?.disableSignUp, true);
      assert.equal("socialProviders" in auth.options, false);
      assert.equal("plugins" in auth.options, false);
      const context = await auth.$context;
      const token = "synthetic-nonexistent-session";
      const signature = createHmac("sha256", config.authSecret).update(token).digest("base64");
      const cookie = `${context.authCookies.sessionToken.name}=${encodeURIComponent(`${token}.${signature}`)}`;
      const response = await fetch("http://127.0.0.1:3000/api/backend/__foundation_denied__", {
        headers: { cookie }, signal: AbortSignal.timeout(5000),
      });
      assert.equal(response.status, 503, "Missing D-02 storage must fail closed");
      assert.equal(response.headers.get("cache-control"), "no-store");
      const body = await response.json();
      assert.equal(body.error.code, "UNAVAILABLE");
      console.log("PASS PostgreSQL/no-cache/closed auth and actual unavailable-storage HTTP denial");
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

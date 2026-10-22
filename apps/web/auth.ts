// Server-only; initialize at request time, never during a secret-free Next build.
import { betterAuth } from "better-auth";
import { loadWebConfig } from "./config.ts";
import { runtimeDatabase } from "./db.ts";

function createAuth() {
  const config = loadWebConfig();
  return betterAuth({
    // F-03 admits only synthetic HTTP development; keep cookie names consistent in CLI/Next.
    baseURL: "http://localhost:3000",
    secret: config.authSecret,
    database: runtimeDatabase(config),
    // shortcut: no D-02 tables in Wave 0, restore schema preflight with D-02; real lookups still fail closed.
    advanced: { database: { validateSchema: false } },
    session: { cookieCache: { enabled: false } },
    emailAndPassword: { enabled: false, disableSignUp: true },
    // Adapter failures are translated at the boundary, not logged with SQL.
    logger: { disabled: true },
  });
}

let auth: ReturnType<typeof createAuth> | undefined;

export function getAuth() {
  return auth ??= createAuth();
}

// Server-only; initialize at request time, never during a secret-free Next build.
import { betterAuth } from "better-auth";
import { lifecycleSchemaPlugin, sessionFields } from "./auth/schema.ts";
import { loadWebConfig } from "./config.ts";
import { runtimeDatabase } from "./db.ts";

function createAuth() {
  const config = loadWebConfig();
  return betterAuth({
    // F-03 admits only synthetic HTTP development; keep cookie names consistent in CLI/Next.
    baseURL: "http://localhost:3000",
    secret: config.authSecret,
    database: runtimeDatabase(config),
    plugins: [lifecycleSchemaPlugin],
    advanced: { database: { validateSchema: true } },
    session: { cookieCache: { enabled: false }, additionalFields: sessionFields },
    verification: { storeInDatabase: true, storeIdentifier: "hashed" },
    emailAndPassword: {
      enabled: false, disableSignUp: true, revokeSessionsOnPasswordReset: true,
    },
    // Adapter failures are translated at the boundary, not logged with SQL.
    logger: { disabled: true },
  });
}

let auth: ReturnType<typeof createAuth> | undefined;

export function getAuth() {
  return auth ??= createAuth();
}

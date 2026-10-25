// Server-only; initialize at request time, never during a secret-free Next build.
import { betterAuth } from "better-auth";
import { lifecycleSchemaPlugin, sessionFields } from "./auth/schema.ts";
import { bindSessionAuthority } from "./auth/session.ts";
import { loadWebConfig } from "./config.ts";
import { runtimeDatabase } from "./db.ts";
import { postgresAuthAdapter } from "./auth/postgres-adapter.ts";
import { persistRecoveryIntent } from "./auth/recovery.ts";

function createAuth() {
  const config = loadWebConfig();
  return betterAuth({
    // F-03 admits only synthetic HTTP development; keep cookie names consistent in CLI/Next.
    baseURL: "http://localhost:3000",
    secret: config.authSecret,
    database: postgresAuthAdapter(runtimeDatabase(config)),
    plugins: [lifecycleSchemaPlugin],
    advanced: {
      database: { validateSchema: true },
      useSecureCookies: process.env.CP_ENV === "production",
      defaultCookieAttributes: { httpOnly: true, sameSite: "lax", path: "/" },
    },
    session: {
      cookieCache: { enabled: false }, disableSessionRefresh: true,
      additionalFields: sessionFields,
    },
    databaseHooks: { session: { create: { before: bindSessionAuthority } } },
    verification: { storeInDatabase: true, storeIdentifier: "hashed" },
    emailAndPassword: {
      enabled: true, disableSignUp: true, requireEmailVerification: true,
      revokeSessionsOnPasswordReset: true,
      resetPasswordTokenExpiresIn: config.policy.reset_expiry_minutes * 60,
      sendResetPassword: persistRecoveryIntent,
    },
    // Adapter failures are translated at the boundary, not logged with SQL.
    logger: { disabled: true },
  });
}

let auth: ReturnType<typeof createAuth> | undefined;

export function getAuth() {
  return auth ??= createAuth();
}

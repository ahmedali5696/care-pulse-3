import { createHmac } from "node:crypto";
import { loadWebConfig } from "../config.ts";
import { runtimeDatabase } from "../db.ts";

async function allowAttempt(email: string, source: string, purpose: "login" | "recovery"): Promise<boolean> {
  const configuration = loadWebConfig();
  const policy = configuration.policy;
  const maxAttempts = purpose === "login" ? policy.login_max_attempts : policy.recovery_max_attempts;
  const windowMinutes = purpose === "login" ? policy.login_window_minutes : policy.recovery_window_minutes;
  const scopes: Array<[string, string]> = [
    ["identity", email.toLowerCase()],
    ["source", source],
  ];
  const client = await runtimeDatabase(loadWebConfig()).connect();
  try {
    await client.query("BEGIN");
    let allowed = true;
    for (const [dimension, key] of scopes) {
      const digest = createHmac("sha256", configuration.authSecret)
        .update(`carepulse-${purpose}:${dimension}:${key}`).digest("hex");
      const count = await client.query<{ attempts: number }>(`
        INSERT INTO public.auth_login_attempts (dimension, scope_digest, attempts, window_start)
        VALUES ($1, $2, 1, clock_timestamp())
        ON CONFLICT (dimension, scope_digest) DO UPDATE
          SET attempts = CASE WHEN auth_login_attempts.window_start +
                         ($3 * interval '1 minute') <= EXCLUDED.window_start
                              THEN 1 ELSE auth_login_attempts.attempts + 1 END,
              window_start = CASE WHEN auth_login_attempts.window_start +
                         ($3 * interval '1 minute') <= EXCLUDED.window_start
                                  THEN EXCLUDED.window_start ELSE auth_login_attempts.window_start END
        RETURNING attempts`, [dimension, digest, windowMinutes]);
      if (count.rows[0].attempts > maxAttempts) allowed = false;
    }
    await client.query("COMMIT");
    return allowed;
  } catch (error) {
    await client.query("ROLLBACK");
    throw error;
  } finally {
    client.release();
  }
}

export function allowLoginAttempt(email: string, source: string): Promise<boolean> {
  return allowAttempt(email, source, "login");
}

export function allowRecoveryAttempt(email: string, source: string): Promise<boolean> {
  return allowAttempt(email, source, "recovery");
}

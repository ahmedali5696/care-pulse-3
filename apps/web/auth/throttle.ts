import { createHmac } from "node:crypto";
import { loadWebConfig } from "../config.ts";
import { runtimeDatabase } from "../db.ts";

export async function allowLoginAttempt(email: string, source: string): Promise<boolean> {
  const configuration = loadWebConfig();
  const policy = configuration.policy;
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
        .update(`carepulse-login:${dimension}:${key}`).digest("hex");
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
        RETURNING attempts`, [dimension, digest, policy.login_window_minutes]);
      if (count.rows[0].attempts > policy.login_max_attempts) allowed = false;
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

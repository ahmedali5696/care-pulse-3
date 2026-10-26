import { createHash, timingSafeEqual } from "node:crypto";
import { getAuth } from "../auth.ts";
import { loadWebConfig } from "../config.ts";
import { authDatabase } from "../db.ts";
import { scopedAuthClient, withAuthTransaction } from "./postgres-adapter.ts";

export type ReservedReset = {
  tenant: string;
  operationId: string;
  nativeUserId: string;
  proofReference: string;
  proofDigest: string;
  fingerprint: string;
  fingerprintKeyVersion: string;
  reservedSecurityRevision: number;
};

/** Commit the native Better Auth reset and immutable completion on one connection. */
export async function applyReservedReset(
  reserved: ReservedReset,
  token: string,
  newPassword: string,
): Promise<"AUTH_DONE" | "ALREADY_DONE"> {
  const proofDigest = createHash("sha256").update(token, "utf8").digest();
  if (reserved.proofDigest.length !== 64 ||
      !/^[a-f0-9]{64}$/.test(reserved.proofDigest) ||
      !timingSafeEqual(proofDigest, Buffer.from(reserved.proofDigest, "hex"))) {
    throw new Error("Recovery proof binding unavailable");
  }
  const expectedIdentifier = createHash("sha256")
    .update(`reset-password:${token}`, "utf8").digest("base64url");
  const pool = authDatabase(loadWebConfig());
  let stage = "claim";
  return withAuthTransaction(pool, reserved.tenant, async () => {
    const client = scopedAuthClient(pool, reserved.tenant);
    if (!client) throw new Error("Recovery transaction unavailable");

    // PostgreSQL rechecks the RecoveryOperation's Organization/Staff FKs
    // after the native completion updates its already-written row. Acquire
    // the same lifecycle lock before the operation fence, never afterward,
    // so an operator suspension cannot deadlock with native completion.
    const authority = await client.query<{ active: boolean }>(
      "SELECT public.lock_active_organization($1::uuid) AS active",
      [reserved.tenant],
    );
    if (authority.rows.length !== 1 || authority.rows[0].active !== true) {
      throw new Error("Recovery organization unavailable");
    }

    // The guarded UPDATE takes the operation row's real write lock. A competing
    // handler waits until this transaction commits or rolls back, then rechecks.
    const claimed = await client.query<{ auth_fence: string }>(`
      UPDATE public.recovery_operations SET auth_fence = auth_fence + 1
       WHERE organization_id = $1 AND id = $2 AND auth_user_id = $3
         AND proof_reference = $4 AND proof_digest = $5
         AND request_fingerprint = $6 AND fingerprint_key_version = $7
         AND reserved_security_revision = $8
         AND state = 'RESERVED' AND NOT invalidated AND closed_at IS NULL
      RETURNING auth_fence::text`, [reserved.tenant, reserved.operationId,
        reserved.nativeUserId, reserved.proofReference, reserved.proofDigest,
        reserved.fingerprint, reserved.fingerprintKeyVersion,
        reserved.reservedSecurityRevision]);
    if (claimed.rowCount !== 1) {
      const completed = await client.query(`
        SELECT 1 FROM public.recovery_operations o
        JOIN public.auth_recovery_completions c ON c.id = o.id
        WHERE o.organization_id = $1 AND o.id = $2 AND o.auth_user_id = $3
          AND o.proof_reference = $4 AND o.proof_digest = $5
          AND o.request_fingerprint = $6 AND o.fingerprint_key_version = $7
          AND o.reserved_security_revision = $8
          AND o.state IN ('AUTH_DONE', 'FINALIZED') AND NOT o.invalidated
          AND c.proof_digest = o.proof_digest AND c.auth_user_id = o.auth_user_id
          AND c.request_fingerprint = o.request_fingerprint
          AND c.reserved_security_revision = o.reserved_security_revision
          AND c.auth_fence = o.auth_fence AND c.completed_at = o.auth_done_at
      `, [reserved.tenant, reserved.operationId, reserved.nativeUserId,
        reserved.proofReference, reserved.proofDigest, reserved.fingerprint,
        reserved.fingerprintKeyVersion, reserved.reservedSecurityRevision]);
      if (completed.rowCount === 1) return "ALREADY_DONE";
      throw new Error("Recovery operation unavailable");
    }
    // Better Auth's native expiry comparator accepts equality. Persisted
    // PostgreSQL clock time must be strictly earlier at both boundaries.
    stage = "proof";
    const proof = await client.query(`
      SELECT 1 FROM public.verification v
      JOIN public.recovery_operations o ON o.proof_reference = v.id
      WHERE o.id = $1 AND o.organization_id = $2
        AND v.id = $3 AND v.identifier = $4 AND v.value = $5
        AND v."expiresAt" = o.proof_expires_at
        AND clock_timestamp() < v."expiresAt"
      FOR UPDATE OF v`, [reserved.operationId, reserved.tenant,
        reserved.proofReference, expectedIdentifier, reserved.nativeUserId]);
    if (proof.rowCount !== 1) throw new Error("Recovery proof expired or unavailable");

    stage = "native";
    const native = await getAuth().api.resetPassword({
      body: { token, newPassword },
    });
    if (!native.status) throw new Error("Native reset failed");
    // Only the database's verified native effects may record completion.
    // The account-password and proof-consumption triggers must both fire in
    // this same transaction before the application is allowed to commit.
    stage = "verify";
    const done = await client.query(`
      SELECT 1 FROM public.recovery_operations o
      JOIN public.auth_recovery_completions c ON c.id = o.id
      WHERE o.id = $1 AND o.organization_id = $2 AND o.state = 'AUTH_DONE'
        AND NOT o.invalidated AND o.closed_at IS NULL
        AND o.auth_fence = $3 AND o.auth_done_at IS NOT NULL
        AND o.proof_expires_at > o.auth_done_at
        AND c.proof_digest = o.proof_digest AND c.auth_user_id = o.auth_user_id
        AND c.request_fingerprint = o.request_fingerprint
        AND c.reserved_security_revision = o.reserved_security_revision
        AND c.auth_fence = o.auth_fence AND c.completed_at = o.auth_done_at
    `, [reserved.operationId, reserved.tenant, claimed.rows[0].auth_fence]);
    if (done.rowCount !== 1) throw new Error("Native recovery effects unverified");
    return "AUTH_DONE";
  }).catch((error: unknown) => {
    if (process.env.CP_ENV === "test" && process.env.CP_SYNTHETIC_TEST_PROFILE === "true") {
      const code = error && typeof error === "object" && "code" in error &&
        typeof error.code === "string" && /^[A-Z0-9]{5}$/.test(error.code) ? error.code : "none";
      console.error("B04_SYNTHETIC_STAGE", stage, code);
    }
    throw error;
  });
}

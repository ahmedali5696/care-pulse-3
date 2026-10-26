import { AsyncLocalStorage } from "node:async_hooks";
import { createHash, createHmac, randomUUID } from "node:crypto";
import { getAuth } from "../auth.ts";
import { loadRecoveryFingerprintKeys, loadWebConfig } from "../config.ts";
import { authDatabase } from "../db.ts";
import { encryptDeliveryToken } from "../mail/delivery-material.ts";
import { failAuthIssuance, scopedAuthClient, withAuthTransaction } from "./postgres-adapter.ts";
import { allowRecoveryAttempt } from "./throttle.ts";
import { failure, json, validMutation } from "./http.ts";
import { signRequest } from "./signing.ts";
import { applyReservedReset } from "./reset-effect.ts";
import type { Pool } from "pg";

type Issuance = {
  tenant: string;
  staff: string;
  user: string;
  email: string;
  keyId: string;
  publicKey: string;
  pool: Pool;
  issued: boolean;
};

const issuance = new AsyncLocalStorage<Issuance>();

// The source and tenant are established by the server-only reset-request boundary.
// Its tenant locator must allow the account's *own* recovery block (T054).
export async function requestRecoveryIssuance(
  email: string, source: string, tenant: string | null,
): Promise<"ACKNOWLEDGED" | "RATE_LIMITED"> {
  const config = loadWebConfig();
  const { deliveryKeyId, deliveryPublicKey } = config;
  if (!config.mailEnabled || !deliveryKeyId || !deliveryPublicKey) {
    throw new Error("Recovery delivery unavailable");
  }
  if (!source || !email || !await allowRecoveryAttempt(email, source)) return "RATE_LIMITED";
  if (!tenant) return "ACKNOWLEDGED";
  const pool = authDatabase(config);
  // Initialize Better Auth before leasing the recovery transaction connection.
  await getAuth().$context;
  await withAuthTransaction(pool, tenant, async () => {
    const client = scopedAuthClient(pool, tenant);
    if (!client) throw new Error("Recovery requires owned transaction");
    const eligible = await client.query<{ staff: string; user: string; email: string }>(`
      SELECT s.id::text AS staff, u.id AS "user", u.email
        FROM public.staff_accounts s
        JOIN public.organizations o ON o.id = s.organization_id
        JOIN public."user" u ON u.id = s.auth_user_id
       WHERE lower(u.email) = lower($1) AND u.email = s.verified_email
         AND o.state = 'ACTIVE' AND s.role = 'DOCTOR_OWNER'
         AND s.enabled AND s.verified AND u."emailVerified"
         AND EXISTS (SELECT 1 FROM public."account" a
                      WHERE a."userId" = u.id AND a."providerId" = 'credential'
                        AND a.password IS NOT NULL)
    `, [email]);
    if (eligible.rows.length !== 1) return;
    const owner = eligible.rows[0];
    const context: Issuance = {
      tenant, staff: owner.staff, user: owner.user, email: owner.email,
      keyId: deliveryKeyId, publicKey: deliveryPublicKey, pool, issued: false,
    };
    await issuance.run(context, () => getAuth().api.requestPasswordReset({
      body: { email: owner.email },
    }));
    if (!context.issued) throw new Error("Native recovery proof was not committed");
  });
  return "ACKNOWLEDGED";
}

export async function persistRecoveryIntent({ token, user }: {
  token: string; user: { id: string; email: string };
}): Promise<void> {
  let stage = "binding";
  try {
    const current = issuance.getStore();
    if (!current || current.user !== user.id || current.email !== user.email || current.issued) {
      throw new Error("Unowned recovery callback");
    }
    const client = scopedAuthClient(current.pool, current.tenant);
    if (!client) throw new Error("Recovery callback lost transaction");
    const identifier = createHash("sha256").update(`reset-password:${token}`).digest("base64url");
    stage = "verification";
    const found = await client.query<{ id: string; expires: Date }>(`
      SELECT v.id, v."expiresAt" AS expires
        FROM public.verification v
        JOIN public.staff_accounts s ON s.auth_user_id = v.value
        JOIN public.organizations o ON o.id = s.organization_id
        JOIN public."user" u ON u.id = v.value
       WHERE v.identifier = $1 AND v.value = $2 AND s.id = $3
         AND s.role = 'DOCTOR_OWNER' AND s.enabled AND s.verified
         AND s.verified_email = u.email AND u."emailVerified"
         AND o.state = 'ACTIVE' AND v."expiresAt" > clock_timestamp()
    `, [identifier, user.id, current.staff]);
    if (found.rows.length !== 1) throw new Error("Invalid native recovery proof binding");
    const job = randomUUID();
    const verifier = found.rows[0];
    const proofDigest = createHash("sha256").update(token, "utf8").digest("hex");
    const context = {
      job_id: job, proof_reference: proofDigest, purpose: "PASSWORD_RESET",
      tenant_id: current.tenant, committed_recipient: current.email,
      expiry_epoch_ms: String(verifier.expires.getTime()),
    };
    stage = "encrypt";
    const material = encryptDeliveryToken(token, context, current.keyId, current.publicKey);
    stage = "job";
    await client.query(`
      INSERT INTO public.delivery_jobs
        (id, organization_id, staff_id, verification_id, proof_reference,
         purpose, recipient, template, logical_key, proof_expires_at)
      VALUES ($1, $2, $3, $4, $5, 'PASSWORD_RESET', $6, 'PASSWORD_RESET', $8, $7)
    `, [job, current.tenant, current.staff, verifier.id, proofDigest, current.email, verifier.expires, job]);
    stage = "material";
    await client.query(`
      INSERT INTO public.protected_delivery_material
        (job_id, format_version, delivery_key_id, ciphertext_base64url)
      VALUES ($1, 1, $2, $3)
    `, [job, material.key_id, material.ciphertext_base64url]);
    stage = "audit";
    await client.query(`
      INSERT INTO public.sensitive_audit
        (id, organization_id, actor_role, action, after_state, correlation_id)
      VALUES ($1, $2, 'SYSTEM', 'PASSWORD_RESET_PROOF_ISSUED',
              jsonb_build_object('staff_id', $3::text, 'verification_id', $4::text), $5)
    `, [randomUUID(), current.tenant, current.staff, verifier.id, job]);
    current.issued = true;
  } catch (error) {
    if (process.env.CP_ENV === "test" && process.env.CP_SYNTHETIC_TEST_PROFILE === "true") {
      const code = error && typeof error === "object" && "code" in error &&
        typeof error.code === "string" && /^[A-Z0-9]{5}$/.test(error.code) ? error.code : "none";
      console.error("B04_SYNTHETIC_ISSUE", stage, code);
    }
    failAuthIssuance(error);
    throw error;
  }
}

export async function requestOwnerRecovery(request: Request): Promise<Response> {
  if (!validMutation(request)) return failure(403);
  if (!request.headers.get("content-type")?.startsWith("application/json")) return failure(422);
  const source = request.headers.get("x-carepulse-ingress-source");
  if (!source) return failure(503);
  let body: unknown;
  try {
    const raw = await request.text();
    if (raw.length > 8192) return failure(422);
    body = JSON.parse(raw);
  } catch { return failure(422); }
  if (!body || typeof body !== "object" || Array.isArray(body) ||
      Object.keys(body).join(",") !== "email") return failure(422);
  const email = (body as Record<string, unknown>).email;
  if (typeof email !== "string" || email.length > 254 ||
      !/^[^\s@]+@[^\s@]+\.[^\s@]+$/.test(email)) return failure(422);
  try {
    const pool = authDatabase(loadWebConfig());
    const users = await pool.query<{ id: string }>(
      'SELECT id FROM public."user" WHERE lower(email) = lower($1) AND "emailVerified"', [email]);
    const tenant = users.rows.length === 1
      ? (await pool.query<{ tenant: string | null }>(
        "SELECT public.resolve_recovery_tenant($1) AS tenant", [users.rows[0].id])).rows[0]?.tenant ?? null
      : null;
    const outcome = await requestRecoveryIssuance(email, source, tenant);
    return outcome === "RATE_LIMITED" ? failure(429) : json({ acknowledged: true });
  } catch (error) {
    if (process.env.CP_ENV === "test" && process.env.CP_SYNTHETIC_TEST_PROFILE === "true") {
      const code = error && typeof error === "object" && "code" in error &&
        typeof error.code === "string" && /^[A-Z0-9]{5}$/.test(error.code) ? error.code : "none";
      console.error("B04_SYNTHETIC_ISSUE", "request", code);
    }
    return failure(503);
  }
}

type RecoveryIdentity = {
  tenant: string;
  staff: string;
  user: string;
  proofReference: string;
  keyVersion: string | null;
};

type RecoveryCommand = {
  native_user_id: string;
  proof_reference: string;
  native_identifier_hash: string;
  proof_digest: string;
  request_fingerprint: string;
  fingerprint_key_version: string;
};

class RecoveryConflict extends Error {
  readonly code: "CONFLICT" | "IDEMPOTENCY_CONFLICT" | "RESET_IN_PROGRESS";

  constructor(code: RecoveryConflict["code"]) {
    super("Recovery conflict");
    this.code = code;
  }
}

function conflictResponse(code: RecoveryConflict["code"]): Response {
  return Response.json({ error: { code, message: "Operation conflict", request_id: randomUUID() } }, {
    status: 409, headers: { "Cache-Control": "no-store" },
  });
}

async function recoveryIdentity(
  pool: Pool, proofDigest: string, nativeIdentifier: string,
): Promise<RecoveryIdentity | null> {
  const retained = await pool.query<{
    organization_id: string; staff_id: string; native_user_id: string; proof_reference: string;
    fingerprint_key_version: string;
  }>("SELECT * FROM public.resolve_recovery_operation($1::text)", [proofDigest]);
  if (retained.rows.length === 1) {
    const owner = retained.rows[0];
    return { tenant: owner.organization_id, staff: owner.staff_id,
      user: owner.native_user_id, proofReference: owner.proof_reference,
      keyVersion: owner.fingerprint_key_version };
  }
  const verification = await pool.query<{ id: string; user_id: string }>(`
    SELECT v.id, v.value AS user_id FROM public.verification v
     WHERE v.identifier = $1
     ORDER BY v."createdAt" DESC LIMIT 2`, [nativeIdentifier]);
  if (verification.rows.length !== 1) return null;
  const proof = verification.rows[0];
  const tenant = (await pool.query<{ tenant: string | null }>(
    "SELECT public.resolve_recovery_tenant($1::text) AS tenant", [proof.user_id])).rows[0]?.tenant;
  if (!tenant) return null;
  return withAuthTransaction(pool, tenant, async () => {
    const client = scopedAuthClient(pool, tenant);
    if (!client) throw new Error("Recovery tenant transaction unavailable");
    const owner = await client.query<{ staff: string }>(`
      SELECT s.id::text AS staff FROM public.staff_accounts s
      JOIN public.organizations o ON o.id = s.organization_id
      JOIN public."user" u ON u.id = s.auth_user_id
      WHERE s.auth_user_id = $1 AND o.state = 'ACTIVE' AND s.role = 'DOCTOR_OWNER'
        AND s.enabled AND s.verified AND u."emailVerified"
        AND s.verified_email = u.email`, [proof.user_id]);
    return owner.rows.length === 1
      ? { tenant, staff: owner.rows[0].staff, user: proof.user_id,
        proofReference: proof.id, keyVersion: null }
      : null;
  });
}

function recoveryFingerprint(
  identity: RecoveryIdentity, proofDigest: string, password: string, secret: string,
): string {
  const passwordBytes = Buffer.from(password, "utf8");
  if (!passwordBytes.equals(Buffer.from(passwordBytes.toString("utf8"), "utf8")) ||
      passwordBytes.toString("utf8") !== password) throw new Error("Invalid credential encoding");
  const key = createHmac("sha256", secret)
    .update("carepulse-password-recovery-fingerprint-key-v1", "utf8").digest();
  return createHmac("sha256", key)
    .update(JSON.stringify(["carepulse-password-recovery-v1", "PASSWORD_RESET",
      identity.tenant, identity.staff, identity.user, proofDigest]), "utf8")
    .update(Buffer.from([0]))
    .update(String(passwordBytes.length), "ascii")
    .update(Buffer.from([0]))
    .update(passwordBytes).digest("hex");
}

async function recoveryCommand(
  identity: RecoveryIdentity, command: RecoveryCommand, action: "reserve" | "finalize",
): Promise<{ operation_id?: string; state: string; reserved_security_revision?: number } | null> {
  const config = loadWebConfig();
  const payload = new TextEncoder().encode(JSON.stringify(command));
  const signed = signRequest({
    method: "POST", rawPath: `/v1/staff/recovery/${action}`, rawQuery: "", body: payload,
    organizationId: identity.tenant, staffId: identity.staff,
    sessionId: "", role: "BOOTSTRAP", securityRevision: 0, purpose: "OWNER_RECOVERY",
    keyId: config.signingKeyId, serviceKey: config.serviceKey,
  });
  signed.headers.set("Content-Type", "application/json");
  const response = await fetch(`${config.apiUrl}${signed.path}`, {
    method: "POST", body: payload, headers: signed.headers,
    cache: "no-store", redirect: "manual", signal: AbortSignal.timeout(5000),
  });
  if (!response.ok) {
    if (response.status === 409) {
      const body = await response.json() as { error?: { code?: string } };
      const code = body.error?.code;
      throw new RecoveryConflict(code === "IDEMPOTENCY_CONFLICT" || code === "RESET_IN_PROGRESS"
        ? code : "CONFLICT");
    }
    if (response.status === 403) throw new RecoveryConflict("CONFLICT");
    throw new Error("Recovery coordination unavailable");
  }
  return await response.json() as { operation_id?: string; state: string;
    reserved_security_revision?: number };
}

export async function completeOwnerRecovery(request: Request): Promise<Response> {
  if (!validMutation(request)) return failure(403);
  if (!request.headers.get("content-type")?.startsWith("application/json")) return failure(422);
  if (!request.headers.get("x-carepulse-ingress-source")) return failure(503);
  let body: unknown;
  try {
    const raw = await request.text();
    if (raw.length > 8192) return failure(422);
    body = JSON.parse(raw);
  } catch { return failure(422); }
  if (!body || typeof body !== "object" || Array.isArray(body) ||
      Object.keys(body).sort().join(",") !== "newPassword,token") return failure(422);
  const { token, newPassword } = body as Record<string, unknown>;
  if (typeof token !== "string" || !/^[A-Za-z0-9_-]{24}$/.test(token) ||
      typeof newPassword !== "string") return failure(422);
  let stage = "initialize";
  try {
    const config = loadWebConfig();
    stage = "auth_context";
    const authContext = await getAuth().$context;
    if (newPassword.length < authContext.password.config.minPasswordLength ||
        newPassword.length > authContext.password.config.maxPasswordLength) return failure(422);
    const digest = createHash("sha256").update(token, "utf8").digest("hex");
    const identifier = createHash("sha256").update(`reset-password:${token}`, "utf8").digest("base64url");
    stage = "identity";
    const identity = await recoveryIdentity(authDatabase(config), digest, identifier);
    if (!identity) return failure(409);
    const fingerprintKeys = loadRecoveryFingerprintKeys(config.authSecret);
    const keyVersion = identity.keyVersion ?? fingerprintKeys.activeVersion;
    if (!Object.hasOwn(fingerprintKeys.keys, keyVersion)) return failure(503);
    const command: RecoveryCommand = {
      native_user_id: identity.user, proof_reference: identity.proofReference,
      native_identifier_hash: identifier, proof_digest: digest,
      request_fingerprint: recoveryFingerprint(identity, digest, newPassword, fingerprintKeys.keys[keyVersion]),
      fingerprint_key_version: keyVersion,
    };
    stage = "reserve";
    const reservation = await recoveryCommand(identity, command, "reserve");
    if (!reservation) return failure(503);
    if (reservation.state === "FINALIZED") return json({ success: true });
    if (reservation.state === "RESERVED") {
      if (!reservation.operation_id || !Number.isSafeInteger(reservation.reserved_security_revision)) {
        return failure(503);
      }
      stage = "native_reset";
      await applyReservedReset({
        tenant: identity.tenant, operationId: reservation.operation_id,
        nativeUserId: identity.user, proofReference: identity.proofReference,
        proofDigest: digest, fingerprint: command.request_fingerprint,
        fingerprintKeyVersion: keyVersion, reservedSecurityRevision: reservation.reserved_security_revision!,
      }, token, newPassword);
    } else if (reservation.state !== "AUTH_DONE") return failure(409);
    stage = "finalize";
    const finalized = await recoveryCommand(identity, command, "finalize");
    return finalized?.state === "FINALIZED" ? json({ success: true }) : failure(503);
  } catch (error) {
    if (process.env.CP_ENV === "test" && process.env.CP_SYNTHETIC_TEST_PROFILE === "true" &&
        !(error instanceof RecoveryConflict)) {
      const code = error && typeof error === "object" && "code" in error &&
        typeof error.code === "string" && /^[A-Z0-9]{5}$/.test(error.code) ? error.code : "none";
      console.error("B04_SYNTHETIC_STAGE", stage, code);
    }
    return error instanceof RecoveryConflict ? conflictResponse(error.code) : failure(503);
  }
}

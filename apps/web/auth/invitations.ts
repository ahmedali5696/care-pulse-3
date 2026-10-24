import { createHash } from "node:crypto";
import { getAuth } from "../auth.ts";
import { loadWebConfig } from "../config.ts";
import { runtimeDatabase } from "../db.ts";
import { failure, validMutation } from "./http.ts";
import { signRequest } from "./signing.ts";
import { withAuthTransaction } from "./postgres-adapter.ts";

const UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-[1-8][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/i;
const INVITE_PROOF = /^[A-Za-z0-9_-]{43}$/;

type Acceptance = {
  organization_id: string;
  invitation_id: string;
  proof: string;
  name: string;
  password: string;
};

type Invitation = {
  purpose: "FIRST_OWNER" | "RECEPTIONIST";
  target_email: string;
  lifecycle_revision: number;
  invitation_revision: number;
  staff_revision?: number;
  security_revision?: number;
  prepared_user_id?: string;
};

type PersistedInvitation = {
  target_email: string;
  lifecycle_revision: string;
  invitation_revision: string;
};

type ReceptionistInvitation = PersistedInvitation & {
  staff_revision: string;
  security_revision: string;
  prepared_user_id: string;
};

function revision(value: string): number {
  if (typeof value !== "string" || !/^(0|[1-9][0-9]*)$/.test(value)) {
    throw new Error("Invalid persisted revision");
  }
  const number = Number(value);
  if (!Number.isSafeInteger(number)) throw new Error("Invalid persisted revision");
  return number;
}

function acceptanceBody(raw: unknown): Acceptance | null {
  if (!raw || typeof raw !== "object" || Array.isArray(raw)) return null;
  const fields = raw as Record<string, unknown>;
  if (Object.keys(fields).sort().join(",") !==
      "invitation_id,name,organization_id,password,proof") return null;
  const { organization_id, invitation_id, proof, name, password } = fields;
  if (typeof organization_id !== "string" || !UUID.test(organization_id) ||
      typeof invitation_id !== "string" || !UUID.test(invitation_id) ||
      typeof proof !== "string" || !INVITE_PROOF.test(proof) ||
      typeof name !== "string" || !name.trim() || name.length > 128 ||
      /[\x00-\x1f\x7f-\x9f]/.test(name) || typeof password !== "string") return null;
  return { organization_id, invitation_id, proof, name: name.trim(), password };
}

async function verifiedInvitation(body: Acceptance): Promise<Invitation | null> {
  const digest = createHash("sha256").update(body.proof, "utf8").digest("hex");
  const client = await runtimeDatabase(loadWebConfig()).connect();
  try {
    await client.query("BEGIN");
    await client.query("SELECT set_config('app.organization_id', $1, true)", [body.organization_id]);
    const receptionist = await client.query<ReceptionistInvitation>(`
      SELECT i.target_email, i.revision::text AS invitation_revision,
             o.lifecycle_revision::text AS lifecycle_revision,
             s.revision::text AS staff_revision,
             s.security_revision::text AS security_revision,
             s.auth_user_id AS prepared_user_id
        FROM public.invitations i
        JOIN public.organizations o ON o.id = i.organization_id
        JOIN public.staff_accounts s ON s.organization_id = i.organization_id
                                     AND s.id = i.staff_id
       WHERE i.id = $2 AND i.organization_id = $1 AND i.proof_digest = $3
         AND i.purpose = 'RECEPTIONIST' AND i.state = 'ISSUED'
         AND i.expires_at > clock_timestamp()
         AND o.state = 'ACTIVE' AND s.role = 'RECEPTIONIST'
         AND s.enabled AND NOT s.verified AND s.verified_email = i.target_email
    `, [body.organization_id, body.invitation_id, digest]);
    if (receptionist.rows.length === 1) {
      await client.query("COMMIT");
      const found = receptionist.rows[0];
      return {
        purpose: "RECEPTIONIST",
        target_email: found.target_email,
        lifecycle_revision: revision(found.lifecycle_revision),
        invitation_revision: revision(found.invitation_revision),
        staff_revision: revision(found.staff_revision),
        security_revision: revision(found.security_revision),
        prepared_user_id: found.prepared_user_id,
      };
    }
    const rows = await client.query<PersistedInvitation>(
      "SELECT * FROM public.verify_first_owner_preparation($1::uuid,$2::uuid,$3::text)",
      [body.organization_id, body.invitation_id, digest],
    );
    await client.query("COMMIT");
    if (rows.rows.length !== 1) return null;
    const found = rows.rows[0];
    return {
      purpose: "FIRST_OWNER",
      target_email: found.target_email,
      lifecycle_revision: revision(found.lifecycle_revision),
      invitation_revision: revision(found.invitation_revision),
    };
  } catch (error) {
    await client.query("ROLLBACK");
    throw error;
  } finally {
    client.release();
  }
}

async function prepareVerifiedOwner(body: Acceptance, invitation: Invitation): Promise<string> {
  const context = await getAuth().$context;
  if (body.password.length < context.password.config.minPasswordLength ||
      body.password.length > context.password.config.maxPasswordLength) throw new Error("Invalid credential");
  const existing = await context.internalAdapter.findUserByEmail(invitation.target_email, { includeAccounts: true });
  if (invitation.purpose === "RECEPTIONIST" &&
      (!existing || existing.user.id !== invitation.prepared_user_id)) {
    throw new Error("Receptionist identity mismatch");
  }
  if (existing) {
    const credential = existing.accounts.find((account) => account.providerId === "credential");
    if (credential?.password) {
      if (!await context.password.verify({ password: body.password, hash: credential.password })) {
        throw new Error("Credential differs from prepared identity");
      }
    } else if (!existing.accounts.length) {
      const digest = await context.password.hash(body.password);
      await context.internalAdapter.linkAccount({
        providerId: "credential", accountId: existing.user.id,
        userId: existing.user.id, password: digest,
      });
    } else {
      throw new Error("Identity already associated with another provider");
    }
    if (!existing.user.emailVerified) {
      await context.internalAdapter.updateUser(existing.user.id, { emailVerified: true });
    }
    if (invitation.purpose === "RECEPTIONIST" && existing.user.name !== body.name) {
      await context.internalAdapter.updateUser(existing.user.id, { name: body.name });
    }
    return existing.user.id;
  }
  const digest = await context.password.hash(body.password);
  const created = await context.internalAdapter.createUser({
    email: invitation.target_email, name: body.name, emailVerified: false,
  }, { method: "email-password" });
  await context.internalAdapter.linkAccount({
    providerId: "credential", accountId: created.id, userId: created.id, password: digest,
  });
  await context.internalAdapter.updateUser(created.id, { emailVerified: true });
  return created.id;
}

async function activateOwner(body: Acceptance, invitation: Invitation, userId: string): Promise<Response> {
  const config = loadWebConfig();
  const receptionist = invitation.purpose === "RECEPTIONIST";
  const payload = new TextEncoder().encode(JSON.stringify({
    invitation_id: body.invitation_id, proof: body.proof,
    prepared_user_id: userId,
    expected_lifecycle_revision: invitation.lifecycle_revision,
    expected_invitation_revision: invitation.invitation_revision,
    ...(receptionist ? {
      expected_staff_revision: invitation.staff_revision,
      expected_security_revision: invitation.security_revision,
    } : {}),
  }));
  const path = receptionist ? "/v1/staff/receptionists/invite-accept" : "/v1/provisioning/owner-activate";
  const signed = signRequest({
    method: "POST", rawPath: path, rawQuery: "", body: payload,
    organizationId: body.organization_id, staffId: "", sessionId: "", role: "BOOTSTRAP",
    securityRevision: 0, purpose: receptionist ? "RECEPTIONIST_ACCEPT" : "OWNER_ACTIVATION",
    keyId: config.signingKeyId,
    serviceKey: config.serviceKey,
  });
  signed.headers.set("Content-Type", "application/json");
  const response = await fetch(`${config.apiUrl}${signed.path}`, {
    method: "POST", body: payload, headers: signed.headers,
    cache: "no-store", redirect: "manual", signal: AbortSignal.timeout(5000),
  });
  if (!response.ok) return failure(response.status === 409 ? 409 : 403);
  const answer = await response.json() as { data?: Record<string, unknown>; revision?: number };
  if (!Number.isSafeInteger(answer.revision)) return failure(503);
  if (receptionist) {
    if (typeof answer.data?.id !== "string" || !UUID.test(answer.data.id) ||
        answer.data.verified !== true || answer.data.enabled !== true ||
        typeof answer.data.revision !== "number") return failure(503);
  } else if (answer.data?.organization_id !== body.organization_id || answer.data.state !== "ACTIVE" ||
             typeof answer.data?.staff_id !== "string" || !UUID.test(answer.data.staff_id)) {
    return failure(503);
  }
  return Response.json({ data: answer.data, revision: answer.revision }, {
    headers: { "Cache-Control": "no-store", "Referrer-Policy": "no-referrer" },
  });
}

export async function acceptFirstOwner(request: Request): Promise<Response> {
  if (!validMutation(request)) return failure(403);
  if (!request.headers.get("content-type")?.startsWith("application/json")) return failure(422);
  let body: Acceptance | null;
  try {
    const input = await request.text();
    if (input.length > 8192) return failure(422);
    body = acceptanceBody(JSON.parse(input));
  } catch {
    return failure(422);
  }
  if (!body) return failure(422);
  try {
    const invitation = await verifiedInvitation(body);
    if (!invitation) return failure(403);
    const preparedUserId = await withAuthTransaction(runtimeDatabase(loadWebConfig()),
      body.organization_id, () => prepareVerifiedOwner(body, invitation));
    return await activateOwner(body, invitation, preparedUserId);
  } catch {
    return failure(503);
  }
}

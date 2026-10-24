import { loadSenderMailConfig } from "./config.ts";
import {
  decryptDeliveryToken, type DeliveryContext, type DeliveryMaterial,
} from "./delivery-material.ts";
import { sendSyntheticMail } from "./transport.ts";

type CommittedJob = {
  context: DeliveryContext;
  material: DeliveryMaterial;
  verifier_digest: string;
  template: string;
};

async function boundedStdin(): Promise<CommittedJob> {
  const chunks: Buffer[] = [];
  let length = 0;
  for await (const chunk of process.stdin) {
    const bytes = Buffer.isBuffer(chunk) ? chunk : Buffer.from(chunk);
    length += bytes.length;
    if (length > 4096) throw new Error("Oversized sender request");
    chunks.push(bytes);
  }
  if (length === 0) throw new Error("Empty sender request");
  const request = JSON.parse(Buffer.concat(chunks).toString("utf8")) as CommittedJob;
  if (!request || typeof request !== "object" ||
      Object.keys(request).sort().join(",") !== "context,material,template,verifier_digest") {
    throw new Error("Invalid sender request");
  }
  return request;
}

function deliveryLink(job: CommittedJob, token: string, origin: string): string {
  const { purpose, committed_recipient: recipient } = job.context;
  if (!/^[a-z0-9-]+@example\.invalid$/.test(recipient)) {
    throw new Error("Invalid synthetic recipient");
  }
  const path = purpose === "PASSWORD_RESET" && job.template === "PASSWORD_RESET"
    ? "/auth/reset"
    : purpose === "FIRST_OWNER" && job.template === "FIRST_OWNER_INVITE"
      ? "/auth/invite-accept"
      : purpose === "RECEPTIONIST" && job.template === "RECEPTIONIST_INVITE"
      ? "/auth/invite-accept" : null;
  if (!path) throw new Error("Invalid delivery purpose");
  const link = new URL(path, origin);
  const fragment = new URLSearchParams({
    proof: token,
    organization_id: job.context.tenant_id,
    proof_reference: job.context.proof_reference,
  });
  link.hash = fragment.toString();
  return link.toString();
}

async function sendCommittedJob(): Promise<void> {
  const settings = loadSenderMailConfig();
  if (process.argv.length === 3 && process.argv[2] === "--check") return;
  if (process.argv.length !== 2) throw new Error("Invalid sender entrypoint");
  const job = await boundedStdin();
  const proof = decryptDeliveryToken(
    job.material, job.context, settings.privateKeysById, job.verifier_digest,
  );
  const link = deliveryLink(job, proof, settings.origin);
  const kind = job.context.purpose === "PASSWORD_RESET" ? "reset" : "invitation";
  await sendSyntheticMail(kind, job.context.committed_recipient, link);
}

try {
  await sendCommittedJob();
  process.stdout.write("SENT\n");
} catch {
  process.stderr.write("Delivery unavailable\n");
  process.exitCode = 1;
}

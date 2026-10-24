import { readFileSync } from "node:fs";
import { expect, test } from "@playwright/test";

const syntheticPassword = "CarePulse-Synthetic-B03-Only!384920";

test("first-Owner invitation activates exactly one real authenticated clinic", async ({ page }) => {
  const privateArtifact = process.env.CP_INVITE_LINK_FILE;
  const intendedEmail = process.env.CP_INVITE_OWNER_EMAIL;
  if (!privateArtifact || !intendedEmail) throw new Error("Missing disposable CI invitation");
  const invitationUrl = new URL(readFileSync(privateArtifact, "utf8").trim());
  expect(invitationUrl.pathname).toBe("/auth/invite-accept");
  expect(invitationUrl.search).toBe("");
  const deliveryParameters = new URLSearchParams(invitationUrl.hash.slice(1));
  const proofReference = deliveryParameters.get("proof_reference");
  const organizationId = deliveryParameters.get("organization_id");
  const proof = deliveryParameters.get("proof");
  expect(proofReference).toMatch(/^[0-9a-f-]{36}$/i);
  expect(organizationId).toMatch(/^[0-9a-f-]{36}$/i);
  expect(proof).toMatch(/^[A-Za-z0-9_-]{43}$/);

  // Keep the proof out of Playwright's page navigation/error/log artifacts.
  await page.goto("/");
  const preActivation = await page.evaluate(async () => {
    const response = await fetch("/api/session", { credentials: "same-origin" });
    return response.status;
  });
  expect(preActivation).toBe(401);

  const activation = await page.evaluate(async (intent) => {
    const response = await fetch("/api/auth/invite-accept", {
      method: "POST", credentials: "same-origin",
      headers: { "Content-Type": "application/json", "X-Requested-With": "XMLHttpRequest" },
      body: JSON.stringify(intent),
    });
    return { status: response.status, body: await response.json() };
  }, { organization_id: organizationId, invitation_id: proofReference,
       proof, name: "Synthetic First Owner", password: syntheticPassword });
  expect(activation.status).toBe(200);
  expect(activation.body.data.state).toBe("ACTIVE");
  expect(activation.body.data.organization_id).toBe(organizationId);

  const login = await page.evaluate(async ({ email, password }) => {
    const response = await fetch("/api/auth/login", {
      method: "POST", credentials: "same-origin",
      headers: { "Content-Type": "application/json", "X-Requested-With": "XMLHttpRequest" },
      body: JSON.stringify({ email, password }),
    });
    return response.status;
  }, { email: intendedEmail, password: syntheticPassword });
  expect(login).toBe(200);

  const session = await page.evaluate(async () => {
    const response = await fetch("/api/session", { credentials: "same-origin" });
    return { status: response.status, payload: await response.json() };
  });
  expect(session.status).toBe(200);
  expect(session.payload.data.organization_id).toBe(organizationId);
  expect(session.payload.data.role).toBe("DOCTOR_OWNER");
  expect(session.payload.data.id).toMatch(/^[0-9a-f-]{36}$/i);
  const replay = await page.evaluate(async (intent) => {
    const response = await fetch("/api/auth/invite-accept", {
      method: "POST", credentials: "same-origin",
      headers: { "Content-Type": "application/json", "X-Requested-With": "XMLHttpRequest" },
      body: JSON.stringify(intent),
    });
    return response.status;
  }, { organization_id: organizationId, invitation_id: proofReference,
       proof, name: "Synthetic First Owner", password: syntheticPassword });
  expect(replay).toBe(403);
});

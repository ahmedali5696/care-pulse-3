import assert from "node:assert/strict";
import { randomUUID } from "node:crypto";
import { request } from "node:http";
import { test } from "node:test";
import { loadWebConfig } from "../config.ts";

function loginFrom(source, spoof = {}, email = `${randomUUID()}@example.invalid`) {
  return new Promise((resolve, reject) => {
    const payload = JSON.stringify({ email, password: "wrong-password" });
    const req = request({
      host: "127.0.0.1", port: 3000, localAddress: source,
      path: "/api/auth/login", method: "POST",
      headers: {
        Host: "127.0.0.1:3000", Origin: "http://127.0.0.1:3000",
        "X-Requested-With": "XMLHttpRequest", "Sec-Fetch-Site": "same-origin",
        "Content-Type": "application/json", ...spoof,
      },
    }, (response) => {
      response.resume();
      response.on("end", () => resolve(response.statusCode));
    });
    req.on("error", reject);
    req.end(payload);
  });
}

test("real TCP peers have independent login limits despite spoofed forwarding headers", async () => {
  const limit = loadWebConfig().policy.login_max_attempts;
  const first = "127.0.0.2";
  const second = "127.0.0.3";
  for (let i = 0; i < limit; i++) {
    assert.equal(await loginFrom(first), 401);
  }
  assert.equal(await loginFrom(first, {
    "X-CarePulse-Ingress-Source": second,
    "X-Forwarded-For": second,
    "X-Real-IP": second,
    Forwarded: `for=${second}`,
  }), 429);
  assert.equal(await loginFrom(second), 401);
  assert.equal(await loginFrom(first, { "X-Forwarded-For": randomUUID() }), 429);
});

test("identity limit holds across distinct TCP peers", async () => {
  const limit = loadWebConfig().policy.login_max_attempts;
  const email = `${randomUUID()}@example.invalid`;
  for (let i = 0; i < limit; i++) {
    assert.equal(await loginFrom(i % 2 ? "127.0.0.4" : "127.0.0.5", {}, email), 401);
  }
  assert.equal(await loginFrom("127.0.0.4", {}, email), 429);
});

test("Wave 3 ingress rejects forged staff identity across every protected operation", async () => {
  const target = randomUUID();
  const routes = [
    ["GET", "/queue"], ["POST", `/appointments/${target}/check-in`],
    ...["routine", "emergency", "urgent"].map(kind => ["POST", `/intake/${kind}`]),
    ["GET", "/settings/admission"], ["PATCH", "/settings/admission"],
    ["PATCH", "/queue/capacity"], ["POST", "/queue/pause"],
    ["POST", "/queue/admission-confirmations"],
    ["GET", "/queue/admission-confirmations/usable"],
    ["POST", `/queue/admission-confirmations/${target}/revoke`],
    ["POST", "/queue/admit"], ["POST", "/queue/placement"],
    ["GET", "/settings/queue"], ["PATCH", "/settings/queue"],
    ["GET", `/visits/${target}`], ["PATCH", `/visits/${target}`],
    ["POST", `/visits/${target}/complete`],
    ["POST", `/visits/${target}/cancel-exceptional`],
    ["POST", `/visits/${target}/extend-followup`],
    ...["cancel", "replace-urgent", "reschedule", "manual-no-show"].map(action =>
      ["POST", `/appointments/${target}/${action}`]),
    ["POST", "/appointments/bulk-reschedule/preview"],
    ["POST", "/appointments/bulk-reschedule/commit"],
    ["GET", "/reconciliation"],
    ["POST", "/days/2026-11-01/review"], ["POST", "/days/2026-11-01/close"],
  ];
  const headers = {
    Origin: "http://127.0.0.1:3000", "X-Requested-With": "XMLHttpRequest",
    "Sec-Fetch-Site": "same-origin", "Content-Type": "application/json",
    "X-CarePulse-Organization-Id": target, "X-CarePulse-Staff-Id": target,
    "X-CarePulse-Session-Id": target, "X-CarePulse-Role": "DOCTOR_OWNER",
    "X-CarePulse-Purpose": "QUEUE_ADMIT", "X-CarePulse-Signature": "forged",
  };
  for (const [method, path] of routes) {
    const response = await fetch(`http://127.0.0.1:3000/api${path}`, {
      method, headers, body: method === "GET" ? undefined : "{}", redirect: "manual",
    });
    assert.equal(response.status, 401, `${method} ${path}`);
    const payload = await response.json();
    assert.deepEqual(Object.keys(payload), ["error"]);
    assert.equal(payload.error.code, "UNAUTHENTICATED");
  }
  const crossOrigin = await fetch("http://127.0.0.1:3000/api/queue/admit", {
    method: "POST", headers: { ...headers, Origin: "https://example.invalid" }, body: "{}",
  });
  assert.equal(crossOrigin.status, 403);
  assert.equal((await crossOrigin.json()).error.code, "FORBIDDEN");
});

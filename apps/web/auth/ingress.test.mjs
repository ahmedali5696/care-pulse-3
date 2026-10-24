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

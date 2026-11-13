import assert from "node:assert/strict";
import { createHash, createHmac } from "node:crypto";
import { readFileSync } from "node:fs";
import { test } from "node:test";

import { canonicalPath, canonicalQuery, signRequest } from "./signing.ts";
import { boundedBody } from "./proxy.ts";

test("BFF accepts one MiB and rejects declared or streamed oversize", async () => {
  const limit = 1_048_576;
  assert.equal((await boundedBody(new Request("http://localhost", {
    method: "POST", body: new Uint8Array(limit),
  })))?.byteLength, limit);

  let pulled = 0;
  const declared = new Request("http://localhost", {
    method: "POST", body: "unused", headers: { "content-length": String(limit + 1) },
  });
  assert.equal(await boundedBody(declared), null);
  assert.equal(declared.bodyUsed, false);

  const streamed = new ReadableStream<Uint8Array>({
    pull(controller) {
      pulled++;
      controller.enqueue(new Uint8Array(524_289));
      if (pulled === 2) controller.close();
    },
  });
  const request = new Request("http://localhost", {
    method: "POST", body: streamed, duplex: "half",
  } as RequestInit & { duplex: string });
  assert.equal(await boundedBody(request), null);
  assert.equal(pulled, 2);
});

type GoldenVector = {
  name: string;
  raw_path: string;
  raw_query: string;
  raw_body: string;
  canonical_fields: string[];
  signature: string;
};

const fixture = JSON.parse(readFileSync(
  new URL("../../../tests/fixtures/signing-vectors.json", import.meta.url), "utf8",
)) as { test_only_key: string; vectors: GoldenVector[] };

for (const vector of fixture.vectors) {
  test(`Python signing vector: ${vector.name}`, () => {
    const fields = vector.canonical_fields;
    const body = Buffer.from(vector.raw_body, "utf8");
    const originalBody = Buffer.from(body);
    const signed = signRequest({
      method: fields[3], rawPath: vector.raw_path, rawQuery: vector.raw_query, body,
      organizationId: fields[7], staffId: fields[8], sessionId: fields[9], role: fields[10],
      securityRevision: Number(fields[11]), timestamp: Number(fields[12]), nonce: fields[13],
      purpose: fields[14], idempotencyKey: fields[15], keyId: fields[1],
      serviceKey: fixture.test_only_key,
    });

    assert.equal(canonicalPath(vector.raw_path), fields[4]);
    assert.equal(canonicalQuery(vector.raw_query), fields[5]);
    assert.equal(signed.path, fields[4]);
    assert.equal(signed.query, fields[5]);
    assert.deepEqual(signed.fields, fields);
    assert.deepEqual(body, originalBody);
    assert.equal(signed.fields[6], createHash("sha256").update(originalBody).digest("hex"));

    const canonicalBytes = Buffer.from(JSON.stringify(fields), "utf8");
    assert.equal(createHmac("sha256", fixture.test_only_key)
      .update(canonicalBytes).digest("hex"), vector.signature);
    assert.equal(signed.signature, vector.signature);

    for (const [header, fieldIndex] of Object.entries({
      "Protocol-Version": 0, "Key-Id": 1, Service: 2, "Organization-Id": 7,
      "Staff-Id": 8, "Session-Id": 9, Role: 10, "Security-Revision": 11,
      Timestamp: 12, Nonce: 13, Purpose: 14, "Idempotency-Key": 15,
    })) {
      assert.equal(signed.headers.get(`X-CarePulse-${header}`), fields[fieldIndex]);
    }
    assert.equal(signed.headers.get("X-CarePulse-Signature"), vector.signature);
    assert.equal(signed.headers.get("X-CarePulse-Service-Key"), fixture.test_only_key);
    assert.equal(signed.headers.has("Cookie"), false);
    assert.equal(signed.headers.has("Authorization"), false);
  });
}

test("canonical request targets reject ambiguous encodings", () => {
  for (const path of ["/v1/%2Fadmin", "/v1/%5Cadmin", "/v1/%2E%2E/patients", "/v1//patients", "/v1/%25ab"]) {
    assert.throws(() => canonicalPath(path));
  }
  for (const query of ["tag=%ZZ", "tag=%FF", "tag=a&&tag=b"]) {
    assert.throws(() => canonicalQuery(query));
  }
});

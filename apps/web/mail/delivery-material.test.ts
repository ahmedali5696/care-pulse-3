import assert from "node:assert/strict";
import { createHash, generateKeyPairSync } from "node:crypto";
import { readFileSync } from "node:fs";
import { test } from "node:test";

import {
  decryptDeliveryToken, deliveryLabel, encryptDeliveryToken,
  type DeliveryContext, type DeliveryMaterial,
} from "./delivery-material.ts";

const vector = JSON.parse(readFileSync(
  new URL("../../../tests/fixtures/delivery-material-vectors.json", import.meta.url), "utf8",
)) as { key_id: string; context: DeliveryContext; token: string; label_utf8_hex: string };

function syntheticKeys() {
  const pair = generateKeyPairSync("rsa", { modulusLength: 3072, publicExponent: 65537 });
  return {
    publicPem: pair.publicKey.export({ type: "spki", format: "pem" }).toString(),
    privatePem: pair.privateKey.export({ type: "pkcs8", format: "pem" }).toString(),
  };
}

const keys = syntheticKeys();
const proofDigest = (token: string) => createHash("sha256").update(token, "utf8").digest("hex");

test("Unicode label has the same canonical byte sequence as Python", () => {
  assert.equal(deliveryLabel(vector.context, vector.key_id).toString("hex"), vector.label_utf8_hex);
});

test("Node encrypts exactly bounded UTF-8 proof and decrypts with SHA-256 verifier", () => {
  for (const token of [vector.token, "a".repeat(318)]) {
    const material = encryptDeliveryToken(token, vector.context, vector.key_id, keys.publicPem);
    assert.deepEqual(Object.keys(material), ["version", "key_id", "ciphertext_base64url"]);
    assert.equal(Buffer.from(material.ciphertext_base64url, "base64url").length, 384);
    assert.equal(decryptDeliveryToken(material, vector.context, {
      [vector.key_id]: keys.privatePem,
    }, proofDigest(token)), token);
  }
});

test("all committed context substitutions and proof mismatch reject delivery", () => {
  const material = encryptDeliveryToken(vector.token, vector.context, vector.key_id, keys.publicPem);
  for (const field of Object.keys(vector.context) as (keyof DeliveryContext)[]) {
    const changed = { ...vector.context, [field]: `${vector.context[field]}x` };
    assert.throws(() => decryptDeliveryToken(material, changed, {
      [vector.key_id]: keys.privatePem,
    }, proofDigest(vector.token)), `tampered ${field}`);
  }
  assert.throws(() => decryptDeliveryToken(material, vector.context, {
    [vector.key_id]: keys.privatePem,
  }, proofDigest("a different proof")));
});

test("two versions of rotated keys retain old ciphertext without exposing private keys to issuer", () => {
  const replacement = syntheticKeys();
  const oldMaterial = encryptDeliveryToken(vector.token, vector.context, vector.key_id, keys.publicPem);
  const newMaterial = encryptDeliveryToken(vector.token, vector.context, "delivery-test-v2", replacement.publicPem);
  const privateKeys = { [vector.key_id]: keys.privatePem, "delivery-test-v2": replacement.privatePem };
  for (const material of [oldMaterial, newMaterial]) {
    assert.equal(decryptDeliveryToken(material, vector.context, privateKeys, proofDigest(vector.token)), vector.token);
  }
  assert.throws(() => decryptDeliveryToken(oldMaterial, vector.context,
    { "delivery-test-v2": replacement.privatePem }, proofDigest(vector.token)));
});

test("ciphertext, version, key and canonical base64url tampering reject", () => {
  const material = encryptDeliveryToken(vector.token, vector.context, vector.key_id, keys.publicPem);
  const keyMap = { [vector.key_id]: keys.privatePem };
  const decrypt = (changed: DeliveryMaterial) =>
    decryptDeliveryToken(changed, vector.context, keyMap, proofDigest(vector.token));
  assert.throws(() => decrypt({ ...material, version: 2 as 1 }));
  assert.throws(() => decrypt({ ...material, key_id: "unknown-v1" }));
  assert.throws(() => decrypt({ ...material, ciphertext_base64url: material.ciphertext_base64url.slice(1) }));
  assert.throws(() => decrypt({ ...material, ciphertext_base64url: `${material.ciphertext_base64url.slice(0, -1)}!` }));
  const corrupted = Buffer.from(material.ciphertext_base64url, "base64url");
  corrupted[0] ^= 0x80;
  assert.throws(() => decrypt({ ...material, ciphertext_base64url: corrupted.toString("base64url") }));
  assert.throws(() => decrypt({ ...material, ciphertext_base64url: `${material.ciphertext_base64url}\n` }));
  assert.throws(() => decrypt({ ...material, extra: "wrong" } as DeliveryMaterial));
});

test("bad keys and invalid token or label encoding fail closed", () => {
  for (const token of ["", "x".repeat(319), "\ud800"]) {
    assert.throws(() => encryptDeliveryToken(token, vector.context, vector.key_id, keys.publicPem));
  }
  for (const expiry of ["", "01", "1\n"]) {
    assert.throws(() => deliveryLabel({ ...vector.context, expiry_epoch_ms: expiry }, vector.key_id));
  }
  assert.throws(() => deliveryLabel({ ...vector.context, tenant_id: "\ud800" }, vector.key_id));
  assert.throws(() => deliveryLabel(vector.context, "bad-id\n"));
  const shortKey = generateKeyPairSync("rsa", { modulusLength: 2048 });
  assert.throws(() => encryptDeliveryToken(vector.token, vector.context, vector.key_id,
    shortKey.publicKey.export({ type: "spki", format: "pem" }).toString()));
  const material = encryptDeliveryToken(vector.token, vector.context, vector.key_id, keys.publicPem);
  assert.throws(() => decryptDeliveryToken(material, vector.context,
    { [vector.key_id]: keys.publicPem }, proofDigest(vector.token)));
  assert.throws(() => encryptDeliveryToken(vector.token, vector.context,
    vector.key_id, `${keys.publicPem}UNEXPECTED TRAILING BYTES`));
  assert.throws(() => decryptDeliveryToken(material, vector.context,
    { [vector.key_id]: `${keys.privatePem}UNEXPECTED TRAILING BYTES` }, proofDigest(vector.token)));
  assert.throws(() => decryptDeliveryToken(material, vector.context,
    { [vector.key_id]: keys.privatePem }, `${proofDigest(vector.token)}\n`));
});

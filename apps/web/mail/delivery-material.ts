import {
  constants, createHash, createPrivateKey, createPublicKey,
  privateDecrypt, publicEncrypt, timingSafeEqual,
} from "node:crypto";

export type DeliveryContext = {
  job_id: string;
  proof_reference: string;
  purpose: string;
  tenant_id: string;
  committed_recipient: string;
  expiry_epoch_ms: string;
};

export type DeliveryMaterial = {
  version: 1;
  key_id: string;
  ciphertext_base64url: string;
};

const contextFields = [
  "job_id", "proof_reference", "purpose", "tenant_id",
  "committed_recipient", "expiry_epoch_ms",
] as const;

function utf8Strict(input: string): Buffer {
  const bytes = Buffer.from(input, "utf8");
  if (bytes.toString("utf8") !== input) throw new Error("Invalid delivery text");
  return bytes;
}

export function deliveryLabel(context: DeliveryContext, keyId: string): Buffer {
  if (typeof keyId !== "string" || keyId.length < 1 || keyId.length > 64 ||
      !/^[A-Za-z0-9]/.test(keyId) || /[^A-Za-z0-9._-]/.test(keyId)) {
    throw new Error("Invalid delivery key ID");
  }
  if (!context || typeof context !== "object" ||
      Object.keys(context).length !== contextFields.length ||
      !contextFields.every((field) => Object.hasOwn(context, field) &&
        typeof context[field] === "string" && context[field].length > 0) ||
      !/^[1-9]/.test(context.expiry_epoch_ms) || /[^0-9]/.test(context.expiry_epoch_ms)) {
    throw new Error("Invalid delivery context");
  }
  for (const field of contextFields) utf8Strict(context[field]);
  return utf8Strict(JSON.stringify([
    "carepulse-delivery-v1", keyId, ...contextFields.map((field) => context[field]),
  ]));
}

function checkRsaKey(key: ReturnType<typeof createPublicKey>): void {
  if (key.asymmetricKeyType !== "rsa" || key.asymmetricKeyDetails?.modulusLength !== 3072 ||
      key.asymmetricKeyDetails?.publicExponent !== BigInt(65537)) {
    throw new Error("Invalid delivery RSA key");
  }
}

function tokenBytes(token: string): Buffer {
  if (typeof token !== "string") throw new Error("Invalid delivery token");
  const bytes = utf8Strict(token);
  if (bytes.length < 1 || bytes.length > 318) throw new Error("Invalid delivery token size");
  return bytes;
}

export function encryptDeliveryToken(
  token: string, context: DeliveryContext, keyId: string, publicKeyPem: string,
): DeliveryMaterial {
  const label = deliveryLabel(context, keyId);
  const plaintext = tokenBytes(token);
  if (typeof publicKeyPem !== "string" || !publicKeyPem.startsWith("-----BEGIN PUBLIC KEY-----\n")) {
    throw new Error("Invalid delivery public key");
  }
  const key = createPublicKey(publicKeyPem);
  checkRsaKey(key);
  if (key.export({ type: "spki", format: "pem" }).toString() !== publicKeyPem) {
    throw new Error("Invalid delivery public key");
  }
  const encrypted = publicEncrypt({
    key, padding: constants.RSA_PKCS1_OAEP_PADDING, oaepHash: "sha256", oaepLabel: label,
  }, plaintext);
  return { version: 1, key_id: keyId, ciphertext_base64url: encrypted.toString("base64url") };
}

export function decryptDeliveryToken(
  material: DeliveryMaterial, context: DeliveryContext,
  privateKeysById: Readonly<Record<string, string>>, expectedProofDigest: string,
): string {
  if (!material || typeof material !== "object" ||
      Object.keys(material).length !== 3 || material.version !== 1 ||
      typeof material.key_id !== "string" ||
      typeof material.ciphertext_base64url !== "string" ||
      !/^[A-Za-z0-9_-]{512}$/.test(material.ciphertext_base64url)) {
    throw new Error("Invalid delivery material");
  }
  const label = deliveryLabel(context, material.key_id);
  if (!privateKeysById || !Object.hasOwn(privateKeysById, material.key_id)) {
    throw new Error("Missing delivery private key");
  }
  const privateKeyPem = privateKeysById[material.key_id];
  if (typeof privateKeyPem !== "string" || !privateKeyPem.startsWith("-----BEGIN PRIVATE KEY-----\n")) {
    throw new Error("Invalid delivery private key");
  }
  if (typeof expectedProofDigest !== "string" || expectedProofDigest.length !== 64 ||
      /[^a-f0-9]/.test(expectedProofDigest)) {
    throw new Error("Invalid delivery proof digest");
  }
  const ciphertext = Buffer.from(material.ciphertext_base64url, "base64url");
  if (ciphertext.length !== 384 || ciphertext.toString("base64url") !== material.ciphertext_base64url) {
    throw new Error("Invalid delivery ciphertext");
  }
  const key = createPrivateKey(privateKeyPem);
  checkRsaKey(key);
  if (key.export({ type: "pkcs8", format: "pem" }).toString() !== privateKeyPem) {
    throw new Error("Invalid delivery private key");
  }
  let token: string;
  try {
    const plaintext = privateDecrypt({
      key, padding: constants.RSA_PKCS1_OAEP_PADDING, oaepHash: "sha256", oaepLabel: label,
    }, ciphertext);
    token = plaintext.toString("utf8");
    if (!plaintext.equals(tokenBytes(token))) throw new Error("Invalid delivery token");
  } catch {
    throw new Error("Invalid delivery material");
  }
  const actualDigest = createHash("sha256").update(token, "utf8").digest();
  if (!timingSafeEqual(actualDigest, Buffer.from(expectedProofDigest, "hex"))) {
    throw new Error("Delivery proof binding failed");
  }
  return token;
}

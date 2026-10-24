"""Synthetic RSA interoperability and strict issuance boundary checks."""

import hashlib
import json
import secrets
import subprocess
from pathlib import Path

import pytest
from carepulse.notifications.delivery_material import _label, encrypt_delivery_token
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

ROOT = Path(__file__).resolve().parents[2]
VECTOR = json.loads(
    (ROOT / "tests/fixtures/delivery-material-vectors.json").read_text(encoding="utf-8")
)


@pytest.fixture(scope="module")
def rsa_keys() -> tuple[bytes, bytes]:
    private_key = rsa.generate_private_key(public_exponent=65537, key_size=3072)
    return (
        private_key.public_key().public_bytes(
            serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo
        ),
        private_key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        ),
    )


def test_unicode_label_matches_cross_language_golden_vector():
    assert _label(VECTOR["context"], VECTOR["key_id"]).hex() == VECTOR["label_utf8_hex"]


@pytest.mark.parametrize(
    "token", [VECTOR["token"], "a" * 318, secrets.token_urlsafe(32)]
)
def test_python_encrypt_node_decrypt_preserves_exact_proof(rsa_keys, token):
    public_pem, private_pem = rsa_keys
    material = encrypt_delivery_token(
        token, VECTOR["context"], VECTOR["key_id"], public_pem
    )
    assert set(material) == {"version", "key_id", "ciphertext_base64url"}
    assert material["version"] == 1
    assert len(material["ciphertext_base64url"]) == 512

    script = """
        import {readFileSync} from 'node:fs';
        import {createHash} from 'node:crypto';
        import {decryptDeliveryToken} from './apps/web/mail/delivery-material.ts';
        const {material, context, keyId, privatePem, digest} =
          JSON.parse(readFileSync(0, 'utf8'));
        const token = decryptDeliveryToken(material, context, {[keyId]: privatePem}, digest);
        if (createHash('sha256').update(token, 'utf8').digest('hex') !== digest)
          throw new Error('Interop digest mismatch');
        process.stdout.write('verified');
    """
    payload = {
        "material": material,
        "context": VECTOR["context"],
        "keyId": VECTOR["key_id"],
        "privatePem": private_pem.decode("ascii"),
        "digest": hashlib.sha256(token.encode("utf-8")).hexdigest(),
    }
    completed = subprocess.run(
        [
            "node",
            "--no-warnings",
            "--experimental-strip-types",
            "--input-type=module",
            "-e",
            script,
        ],
        input=json.dumps(payload, ensure_ascii=True),
        text=True,
        capture_output=True,
        cwd=ROOT,
        check=True,
    )
    assert completed.stdout == "verified"


@pytest.mark.parametrize("token", ["", "x" * 319, "\ud800"])
def test_invalid_utf8_or_oversized_token_fails_before_encryption(rsa_keys, token):
    with pytest.raises(ValueError):
        encrypt_delivery_token(token, VECTOR["context"], VECTOR["key_id"], rsa_keys[0])


@pytest.mark.parametrize(
    "context_patch",
    [
        {"expiry_epoch_ms": "1\n"},
        {"expiry_epoch_ms": "01"},
        {"tenant_id": ""},
        {"extra": "x"},
    ],
)
def test_noncanonical_context_rejected(rsa_keys, context_patch):
    with pytest.raises(ValueError):
        encrypt_delivery_token(
            "test",
            {**VECTOR["context"], **context_patch},
            VECTOR["key_id"],
            rsa_keys[0],
        )


def test_nonpublic_or_wrong_size_rsa_key_rejected(rsa_keys):
    for candidate in [
        rsa_keys[1],
        rsa_keys[0] + b"UNEXPECTED TRAILING BYTES",
        rsa.generate_private_key(public_exponent=65537, key_size=2048)
        .public_key()
        .public_bytes(
            serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo
        ),
    ]:
        with pytest.raises(ValueError):
            encrypt_delivery_token(
                "test", VECTOR["context"], VECTOR["key_id"], candidate
            )

"""Small shared-format HMAC envelope for Pi MQTT messages."""
from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import json
import os

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM


def _canonical_payload(payload: dict) -> bytes:
    unsigned = {key: value for key, value in payload.items() if key != "signature"}
    return json.dumps(
        unsigned,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")


def sign_payload(payload: dict, signing_key: str) -> dict:
    if not isinstance(payload, dict) or not signing_key:
        raise ValueError("A payload and MQTT signing key are required.")
    key = bytes.fromhex(signing_key)
    signed = dict(payload)
    signed.pop("signature", None)
    signed["signature"] = hmac.new(
        key, _canonical_payload(signed), hashlib.sha256
    ).hexdigest()
    return signed


def _command_encryption_key(signing_key: str) -> bytes:
    try:
        signing_material = bytes.fromhex(signing_key)
    except (TypeError, ValueError) as exc:
        raise ValueError("invalid_command_encryption_key") from exc
    if len(signing_material) != 32:
        raise ValueError("invalid_command_encryption_key")
    return hmac.new(
        signing_material,
        b"mqtt-command-encryption-v1",
        hashlib.sha256,
    ).digest()


def _command_aad(envelope: dict) -> bytes:
    metadata = {
        "device_id": envelope["device_id"],
        "command_id": envelope["command_id"],
        "issued_at": envelope["issued_at"],
        "expires_at": envelope["expires_at"],
        "encryption": envelope["encryption"],
    }
    return json.dumps(
        metadata, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode("utf-8")


def encrypt_mqtt_command(command: dict, signing_key: str) -> dict:
    """Encrypt one command for this device and sign its visible routing envelope."""
    if not isinstance(command, dict):
        raise ValueError("invalid_command")
    required = ("device_id", "command_id", "issued_at", "expires_at")
    if any(not isinstance(command.get(key), str) or not command[key] for key in required):
        raise ValueError("invalid_command_metadata")
    if (
        not isinstance(command.get("command_type"), str)
        or not command["command_type"]
        or not isinstance(command.get("payload"), dict)
    ):
        raise ValueError("invalid_command_payload")

    envelope = {key: command[key] for key in required}
    envelope["encryption"] = "aes-256-gcm-v1"
    nonce = os.urandom(12)
    plaintext = json.dumps(
        {
            "command_type": command["command_type"],
            "payload": command["payload"],
        },
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")
    ciphertext = AESGCM(_command_encryption_key(signing_key)).encrypt(
        nonce, plaintext, _command_aad(envelope)
    )
    envelope["nonce"] = base64.urlsafe_b64encode(nonce).decode("ascii").rstrip("=")
    envelope["ciphertext"] = base64.urlsafe_b64encode(ciphertext).decode("ascii").rstrip("=")
    return sign_payload(envelope, signing_key)


def decrypt_mqtt_command(envelope: dict, signing_key: str) -> dict:
    """Authenticate/decrypt the per-device AES-GCM command body."""
    if not verify_payload(envelope, signing_key):
        raise ValueError("invalid_encrypted_command_signature")
    if not isinstance(envelope, dict) or envelope.get("encryption") != "aes-256-gcm-v1":
        raise ValueError("unencrypted_or_unsupported_command")
    required = ("device_id", "command_id", "issued_at", "expires_at", "nonce", "ciphertext")
    if any(not isinstance(envelope.get(key), str) or not envelope[key] for key in required):
        raise ValueError("invalid_encrypted_command")

    try:
        nonce_value = envelope["nonce"]
        ciphertext_value = envelope["ciphertext"]
        if len(nonce_value) > 32 or len(ciphertext_value) > 8192:
            raise ValueError("encrypted_command_too_large")

        def decode(value):
            padded = value + ("=" * (-len(value) % 4))
            return base64.b64decode(padded, altchars=b"-_", validate=True)

        nonce = decode(nonce_value)
        ciphertext = decode(ciphertext_value)
        if len(nonce) != 12 or len(ciphertext) < 16:
            raise ValueError("invalid_encrypted_command")

        cleartext = AESGCM(_command_encryption_key(signing_key)).decrypt(
            nonce, ciphertext, _command_aad(envelope)
        )
        command = json.loads(cleartext.decode("utf-8"))
    except (InvalidTag, binascii.Error, UnicodeDecodeError, json.JSONDecodeError, TypeError, KeyError, ValueError) as exc:
        raise ValueError("command_decryption_failed") from exc

    if (
        not isinstance(command, dict)
        or not isinstance(command.get("command_type"), str)
        or not command.get("command_type")
        or not isinstance(command.get("payload"), dict)
    ):
        raise ValueError("invalid_decrypted_command")
    return command


def verify_payload(payload: dict, signing_key: str) -> bool:
    if not isinstance(payload, dict) or not signing_key:
        return False
    signature = payload.get("signature")
    if not isinstance(signature, str) or len(signature) != 64:
        return False
    try:
        key = bytes.fromhex(signing_key)
        expected = hmac.new(
            key, _canonical_payload(payload), hashlib.sha256
        ).hexdigest()
    except (TypeError, ValueError):
        return False
    return hmac.compare_digest(signature, expected)

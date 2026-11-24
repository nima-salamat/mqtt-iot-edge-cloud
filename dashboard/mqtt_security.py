"""Per-device MQTT message authentication helpers."""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from django.conf import settings


def device_mqtt_signing_key(device) -> str:
    """Derive a stable per-device key without storing another long-lived secret.

    The key changes when the device API token is rotated or DJANGO_SECRET_KEY
    changes. Pairing and authenticated HTTPS heartbeat responses provision it
    to the paired Pi.
    """
    material = (
        f"mqtt-device-v1:{device.device_id}:{device.api_token_hash}"
    ).encode("utf-8")
    return hmac.new(
        settings.SECRET_KEY.encode("utf-8"),
        material,
        hashlib.sha256,
    ).hexdigest()


def _canonical_payload(payload: dict) -> bytes:
    unsigned = {key: value for key, value in payload.items() if key != "signature"}
    return json.dumps(
        unsigned,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")


def sign_mqtt_payload(payload: dict, signing_key: str) -> dict:
    if not isinstance(payload, dict) or not signing_key:
        raise ValueError("A payload and device signing key are required.")
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
        raise ValueError("A valid per-device signing key is required.") from exc
    if len(signing_material) != 32:
        raise ValueError("A valid per-device signing key is required.")
    # Domain-separated subkey: the HMAC identity key is never used directly
    # as an AES key, and each camera gets a different encryption key.
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
    """Encrypt command contents for one device, then HMAC-sign the envelope.

    The shared broker ACL permits all cameras to subscribe to command topics.
    AES-GCM ensures those other cameras can see routing metadata but cannot
    read the command type or its payload.
    """
    if not isinstance(command, dict):
        raise ValueError("A command envelope object is required.")
    required = ("device_id", "command_id", "issued_at", "expires_at")
    if any(not isinstance(command.get(key), str) or not command[key] for key in required):
        raise ValueError("Command routing and expiry metadata are required.")
    command_type = command.get("command_type")
    command_payload = command.get("payload")
    if not isinstance(command_type, str) or not command_type or not isinstance(command_payload, dict):
        raise ValueError("A command type and object payload are required.")

    envelope = {
        key: command[key] for key in required
    }
    envelope["encryption"] = "aes-256-gcm-v1"
    nonce = os.urandom(12)
    plaintext = json.dumps(
        {"command_type": command_type, "payload": command_payload},
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")
    ciphertext = AESGCM(_command_encryption_key(signing_key)).encrypt(
        nonce, plaintext, _command_aad(envelope)
    )
    envelope["nonce"] = base64.urlsafe_b64encode(nonce).decode("ascii").rstrip("=")
    envelope["ciphertext"] = base64.urlsafe_b64encode(ciphertext).decode("ascii").rstrip("=")
    return sign_mqtt_payload(envelope, signing_key)


def verify_mqtt_payload(payload: dict, signing_key: str) -> bool:
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

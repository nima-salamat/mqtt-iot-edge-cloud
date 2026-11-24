"""Per-device MQTT message authentication helpers."""
from __future__ import annotations

import hashlib
import hmac
import json

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

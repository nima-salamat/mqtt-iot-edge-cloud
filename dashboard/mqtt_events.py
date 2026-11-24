"""Validate and apply signed device events received from Mosquitto."""
from __future__ import annotations

import json
import logging
import math
from datetime import datetime, timedelta, timezone as datetime_timezone

from django.conf import settings
from django.utils import timezone

from .api import _merge_system_metrics, _sanitize_system_metrics
from .models import Alarm, Device, DeviceCommand
from .mqtt_security import device_mqtt_signing_key, verify_mqtt_payload

logger = logging.getLogger(__name__)


def _parse_datetime(value):
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=datetime_timezone.utc)
        return parsed
    except (TypeError, ValueError):
        return None


def _bounded_float(value, minimum=0.0, maximum=1_000_000.0, default=0.0):
    if isinstance(value, bool):
        return default
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return default
    if not math.isfinite(number) or not minimum <= number <= maximum:
        return default
    return number


def _fresh_timestamp(
    value,
    now=None,
    max_age_seconds=90,
    max_future_seconds=30,
):
    """
    Accept only recent messages.

    Pi MQTT publishers use the server-time offset learned from the authenticated
    HTTPS heartbeat. A short window prevents a queued QoS 1 packet from being
    mistaken for a fresh heartbeat when the Listener reconnects.
    """
    parsed = _parse_datetime(value)
    now = now or timezone.now()
    if parsed is None:
        return False
    return (
        now - timedelta(seconds=max_age_seconds)
        <= parsed
        <= now + timedelta(seconds=max_future_seconds)
    )


def _topic_device_and_event(topic):
    prefix_parts = settings.MQTT_TOPIC_PREFIX.strip("/").split("/")
    parts = str(topic).split("/")
    if (
        len(parts) != len(prefix_parts) + 3
        or parts[:len(prefix_parts)] != prefix_parts
        or parts[len(prefix_parts)] != "devices"
    ):
        return None, None
    device_id = parts[len(prefix_parts) + 1]
    event_type = parts[len(prefix_parts) + 2]
    if event_type not in {"alarms", "telemetry", "status", "command-acks"}:
        return None, None
    return device_id, event_type


def _apply_telemetry(device, payload, now):
    boolean_fields = (
        "model_loaded", "mqtt_connected", "mqtt_command_subscription_ready",
        "recording_supported", "rtsp_connected", "camera_ready",
        "tracking_active", "recording_enabled",
    )
    if any(field in payload and type(payload[field]) is not bool for field in boolean_fields):
        logger.warning("Ignoring malformed MQTT telemetry booleans from %s", device.device_id)
        return False

    numeric_ranges = {
        "video_fps": (0, 120),
        "inference_fps": (0, 60),
        "pending_alerts": (0, 1_000_000),
        "pending_video_segments": (0, 1_000_000),
        "tracking_seconds": (0, 86400),
    }
    for field, (minimum, maximum) in numeric_ranges.items():
        if field not in payload:
            continue
        value = _bounded_float(
            payload[field], minimum, maximum, default=None
        )
        if value is None or (
            field in {"pending_alerts", "pending_video_segments"}
            and not value.is_integer()
        ):
            logger.warning(
                "Ignoring malformed MQTT telemetry metric %s from %s",
                field, device.device_id,
            )
            return False

    reported_at = _parse_datetime(payload.get("reported_at"))
    if not _fresh_timestamp(reported_at, now=now):
        skew_seconds = (
            round((now - reported_at).total_seconds(), 1)
            if reported_at is not None else None
        )
        logger.warning(
            "Ignoring out-of-window MQTT telemetry from %s "
            "(reported_at=%r, server_now=%s, age_seconds=%s, allowed_age_seconds=90)",
            device.device_id,
            payload.get("reported_at"),
            now.isoformat(),
            skew_seconds,
        )
        return False

    state = device.current_state if isinstance(device.current_state, dict) else {}
    previous_reported_at = _parse_datetime(state.get("last_device_reported_at"))
    if previous_reported_at is not None and reported_at <= previous_reported_at:
        logger.warning(
            "Ignoring out-of-order MQTT telemetry from %s "
            "(reported_at=%s, last_accepted=%s)",
            device.device_id,
            reported_at.isoformat(),
            previous_reported_at.isoformat(),
        )
        return False

    video_fps = _bounded_float(payload.get("video_fps"), 0, 120)
    inference_fps = _bounded_float(payload.get("inference_fps"), 0, 60)
    pending_alerts = max(0, min(1_000_000, int(_bounded_float(payload.get("pending_alerts"), 0, 1_000_000))))
    pending_videos = max(0, min(1_000_000, int(_bounded_float(payload.get("pending_video_segments"), 0, 1_000_000))))
    tracking_seconds = _bounded_float(payload.get("tracking_seconds"), 0, 86400)

    device.is_online = True
    device.last_seen = now
    device.last_heartbeat = now
    device.reported_video_fps = video_fps
    device.reported_inference_fps = inference_fps
    device.model_loaded = bool(payload.get("model_loaded", False))

    state.update({
        "mqtt_connected": True,
        "mqtt_status": "online",
        "mqtt_command_subscription_ready": (
            payload.get("mqtt_command_subscription_ready") is True
        ),
        "recording_supported": payload.get("recording_supported") is True,
        "telemetry_transport": "mqtt",
        "last_mqtt_telemetry_at": now.isoformat(),
        "last_device_reported_at": reported_at.isoformat(),
        "rtsp_connected": bool(payload.get("rtsp_connected", False)),
        "camera_ready": bool(payload.get("camera_ready", False)),
        "pending_alerts": pending_alerts,
        "pending_video_segments": pending_videos,
        "tracking_active": bool(payload.get("tracking_active", False)),
        "tracking_seconds": tracking_seconds,
        "model": str(payload.get("model", "yolov5n-ncnn"))[:64],
        "software_version": str(payload.get("software_version", ""))[:64],
        "connection_state": "online",
    })
    if isinstance(payload.get("recording_enabled"), bool):
        state["recording_enabled"] = payload["recording_enabled"]
    if "last_video_upload_at" in payload:
        state["last_video_upload_at"] = str(payload.get("last_video_upload_at") or "")[:64]
    if "last_video_upload_error" in payload:
        state["last_video_upload_error"] = str(payload.get("last_video_upload_error") or "")[:500]
    system = _sanitize_system_metrics(payload.get("system"))
    if system is not None:
        merged_system = _merge_system_metrics(state.get("system"), system)
        if merged_system is not None:
            state["system"] = merged_system

    device.current_state = state
    device.save(update_fields=[
        "is_online", "last_seen", "last_heartbeat", "reported_video_fps",
        "reported_inference_fps", "model_loaded", "current_state", "updated_at",
    ])
    return True


def _apply_status(device, payload, now):
    status = payload.get("status")
    source = payload.get("source")
    if status not in {"online", "offline"}:
        return False
    if source not in {"device", "last-will"}:
        return False
    if status == "online" and (
        source != "device" or not _fresh_timestamp(payload.get("reported_at"), now=now)
    ):
        return False
    if status == "offline" and source == "device" and not _fresh_timestamp(
        payload.get("reported_at"), now=now
    ):
        # The graceful-disconnect status has a server-clock timestamp. A
        # delayed/replayed packet must not disconnect a newer MQTT session.
        return False

    state = device.current_state if isinstance(device.current_state, dict) else {}
    if status == "offline" and source == "last-will":
        last_telemetry = _parse_datetime(state.get("last_mqtt_telemetry_at"))
        last_status = _parse_datetime(state.get("last_mqtt_status_at"))
        if (
            last_telemetry is not None
            and last_telemetry >= now - timedelta(seconds=15)
        ) or (
            state.get("mqtt_status") == "online"
            and last_status is not None
            and last_status >= now - timedelta(seconds=15)
        ):
            # Broker Last Will delivery can race a reconnect. A recent telemetry
            # packet or online status from the new connection takes precedence.
            logger.info(
                "Ignoring delayed MQTT Last Will for %s after fresh online traffic",
                device.device_id,
            )
            return False
    state["mqtt_connected"] = status == "online"
    # A transport status event is not an MQTT SUBACK. Require fresh telemetry
    # from the current connection before enabling remote command controls.
    state["mqtt_command_subscription_ready"] = False
    state["mqtt_status"] = status
    state["last_mqtt_status_at"] = now.isoformat()
    state["connection_state"] = "online" if status == "online" else "mqtt-disconnected"
    if status == "online":
        device.is_online = True
        device.last_seen = now
    # A broker LWT only says MQTT was disconnected; HTTPS fallback may still
    # be working, so it must not by itself mark the whole device offline.
    device.current_state = state
    device.save(update_fields=["is_online", "last_seen", "current_state", "updated_at"])
    return True


def _apply_alarm(device, payload, now):
    alert_id = str(payload.get("alert_id", ""))[:64]
    if payload.get("device_id") != device.device_id or not alert_id:
        return False
    # The photo and authoritative detection metadata must already have arrived
    # through the per-device HTTPS API. MQTT is a low-latency notification only.
    alarm = Alarm.objects.filter(device=device, alert_id=alert_id).first()
    if alarm is None:
        return False
    if alarm.mqtt_received_at is None:
        alarm.mqtt_received_at = now
        alarm.save(update_fields=["mqtt_received_at"])
    return True


def _apply_command_ack(device, payload, now):
    command_id = str(payload.get("command_id", ""))
    status = payload.get("status")
    if len(command_id) != 32 or status not in {"applied", "failed"}:
        return False
    command = DeviceCommand.objects.filter(
        command_id=command_id, device=device
    ).first()
    if command is None:
        return False
    if command.expires_at < now:
        if command.status in {"pending", "published"}:
            command.status = "expired"
            command.save(update_fields=["status"])
        return False
    if command.status not in {"pending", "published"}:
        return False

    result = payload.get("result")
    if not isinstance(result, dict):
        result = {"message": str(result or "")[:300]}
    command.status = status
    command.result = result
    command.acknowledged_at = now
    command.save(update_fields=["status", "result", "acknowledged_at"])

    state = device.current_state if isinstance(device.current_state, dict) else {}
    state["last_remote_command"] = {
        "command_id": command_id,
        "command_type": command.command_type,
        "status": status,
        "acknowledged_at": now.isoformat(),
    }
    if command.command_type == "recording.set" and status == "applied":
        enabled = result.get("recording_enabled")
        if isinstance(enabled, bool):
            state["recording_enabled"] = enabled
    device.current_state = state
    device.save(update_fields=["current_state", "updated_at"])
    return True


def process_mqtt_message(topic, raw_payload, qos=0):
    """Validate topic/device/HMAC, then update the appropriate authoritative state."""
    if isinstance(raw_payload, bytes) and len(raw_payload) > 256 * 1024:
        logger.warning("Ignoring oversized MQTT message on %s (%d bytes)", topic, len(raw_payload))
        return False
    if isinstance(raw_payload, str) and len(raw_payload.encode("utf-8")) > 256 * 1024:
        logger.warning("Ignoring oversized MQTT text message on %s", topic)
        return False
    device_id, event_type = _topic_device_and_event(topic)
    if not device_id:
        return False
    try:
        if isinstance(raw_payload, bytes):
            payload = json.loads(raw_payload.decode("utf-8"))
        elif isinstance(raw_payload, str):
            payload = json.loads(raw_payload)
        else:
            payload = raw_payload
        if not isinstance(payload, dict):
            return False
    except (UnicodeDecodeError, json.JSONDecodeError, TypeError, ValueError):
        logger.warning("Ignoring invalid MQTT JSON on %s", topic)
        return False

    if payload.get("device_id") != device_id:
        logger.warning("MQTT topic/device mismatch on %s", topic)
        return False
    device = Device.objects.filter(device_id=device_id, api_token_hash__gt="").first()
    if device is None:
        return False
    signing_key = device_mqtt_signing_key(device)
    if not verify_mqtt_payload(payload, signing_key):
        logger.warning("Ignoring MQTT message with invalid device signature: %s", topic)
        return False

    now = timezone.now()
    if event_type == "telemetry":
        return _apply_telemetry(device, payload, now)
    if event_type == "status":
        return _apply_status(device, payload, now)
    if event_type == "alarms":
        return _apply_alarm(device, payload, now)
    if event_type == "command-acks":
        return _apply_command_ack(device, payload, now)
    return False

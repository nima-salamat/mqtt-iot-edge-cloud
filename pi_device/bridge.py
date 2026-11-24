"""Authenticated MQTT event and remote-command bridge for a Raspberry Pi."""
from __future__ import annotations

import json
import logging
import os
import re
import threading
import time
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

import paho.mqtt.client as mqtt
from paho.mqtt.enums import CallbackAPIVersion

from .atomic_files import atomic_write_json
from .mqtt_protocol import sign_payload, verify_payload

log = logging.getLogger(__name__)


def utc_now():
    return datetime.now(timezone.utc).isoformat()


class RaspberryPiMqttBridge:
    """MQTT transports small, signed events and commands; media remains off MQTT."""

    def __init__(self, device_id):
        self.device_id = device_id
        self.host = os.getenv("MQTT_HOST", "server.echonode.website")
        self.port = int(os.getenv("MQTT_PORT", "443"))
        self.ws_path = os.getenv("MQTT_WS_PATH", "/mqtt")
        self.prefix = os.getenv("MQTT_TOPIC_PREFIX", "iotlab").strip("/")
        self.username = os.getenv("MQTT_USERNAME", "camera-edge")
        self.password = os.getenv("MQTT_PASSWORD", "")
        self.connected = False
        self.last_error = ""
        self.last_publish_at = ""
        self.frame_fps = 0.0
        self.inference_fps = 0.0
        self.model_loaded = False
        self.rtsp_connected = False
        self.camera_ready = False
        self.pending_alert_count = 0
        self.pending_video_count = 0
        self.tracking_active = False
        self.tracking_seconds = 0.0
        self._lock = threading.RLock()
        self._signing_key = ""
        self._command_handler = None
        self._started = False
        self.topic_root = f"{self.prefix}/devices/{device_id}"
        self.alarm_topic = f"{self.topic_root}/alarms"
        self.telemetry_topic = f"{self.topic_root}/telemetry"
        self.status_topic = f"{self.topic_root}/status"
        self.command_topic = f"{self.topic_root}/commands"
        self.command_ack_topic = f"{self.topic_root}/command-acks"
        self.log_dir = Path(os.getenv("PI_DATA_DIR", "/var/lib/mqttiot")) / "logs"
        self.log_dir.mkdir(parents=True, exist_ok=True)

        self.client = mqtt.Client(
            callback_api_version=CallbackAPIVersion.VERSION2,
            client_id=f"camera-{device_id}",
            clean_session=False,
            protocol=mqtt.MQTTv311,
            transport="websockets",
        )
        self.client.ws_set_options(path=self.ws_path)
        self.client.reconnect_delay_set(min_delay=1, max_delay=60)
        if os.getenv("MQTT_TLS", "1").lower() in {"1", "true", "yes"}:
            self.client.tls_set()
        if self.username:
            self.client.username_pw_set(self.username, self.password)
        self.client.on_connect = self._on_connect
        self.client.on_disconnect = self._on_disconnect
        self.client.on_message = self._on_message

    def set_signing_key(self, signing_key):
        signing_key = str(signing_key or "")
        if signing_key and not re.fullmatch(r"[a-fA-F0-9]{64}", signing_key):
            log.error("Refusing an invalid per-device MQTT signing key")
            return False
        with self._lock:
            changed = signing_key != self._signing_key
            self._signing_key = signing_key.lower()
            connected = self.connected
        if changed and self._signing_key:
            self._configure_will()
            # The Will is transmitted as part of CONNECT. Reconnect once after
            # key bootstrap so an already-paired Pi also receives a signed LWT.
            if connected and self._started:
                threading.Thread(
                    target=self._reconnect_for_will,
                    name="mqtt-will-refresh",
                    daemon=True,
                ).start()
        return True

    def _signed(self, payload):
        with self._lock:
            key = self._signing_key
        if not key:
            return None
        try:
            return sign_payload(payload, key)
        except (TypeError, ValueError):
            log.exception("Could not sign MQTT message")
            return None

    def _configure_will(self):
        payload = self._signed({
            "device_id": self.device_id,
            "status": "offline",
            "source": "last-will",
        })
        if payload is None:
            return
        try:
            self.client.will_set(
                self.status_topic,
                json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
                qos=1,
                retain=True,
            )
        except Exception:
            log.exception("Could not configure the MQTT Last Will")

    def set_command_handler(self, handler):
        self._command_handler = handler

    def start(self):
        if not self.password:
            self.last_error = "MQTT_PASSWORD is empty; MQTT event delivery is disabled."
            log.error(self.last_error)
            return
        self._configure_will()
        self._started = True
        self.client.connect_async(self.host, self.port, keepalive=45)
        self.client.loop_start()

    def _reconnect_for_will(self):
        try:
            self.client.disconnect()
            time.sleep(0.25)
            self.client.reconnect()
        except Exception:
            log.exception("Could not reconnect MQTT after signing-key bootstrap")

    def stop(self):
        try:
            if self.client.is_connected():
                self._publish_status("offline")
                self.client.disconnect()
        finally:
            self.client.loop_stop()
            self._started = False
            with self._lock:
                self.connected = False

    def _on_connect(self, client, userdata, flags, reason_code, properties):
        with self._lock:
            self.connected = not reason_code.is_failure
            self.last_error = "" if self.connected else f"Broker rejected connection: {reason_code}"
            has_key = bool(self._signing_key)
        if not self.connected:
            log.error(self.last_error)
            return
        log.info("Connected to authenticated MQTT device-event channel")
        if has_key:
            try:
                client.subscribe(self.command_topic, qos=1)
                self._publish_status("online", wait_for_ack=False)
            except Exception:
                log.exception("Could not initialize MQTT command subscription/status")

    def _on_disconnect(self, client, userdata, disconnect_flags, reason_code, properties):
        with self._lock:
            self.connected = False
            self.last_error = f"MQTT disconnected: {reason_code}"
        log.warning("MQTT disconnected; automatic reconnect remains enabled: %s", reason_code)

    def _publish_signed(self, topic, payload, *, retain=False, timeout=5, wait_for_ack=True):
        if not self.client.is_connected():
            return False
        signed = self._signed(payload)
        if signed is None:
            return False
        try:
            info = self.client.publish(
                topic,
                json.dumps(signed, ensure_ascii=False, separators=(",", ":")),
                qos=1,
                retain=retain,
            )
            if info.rc != mqtt.MQTT_ERR_SUCCESS:
                return False
            # Do not block Paho's network thread from inside on_connect: it
            # must keep receiving packets to process the PUBACK we would wait for.
            if wait_for_ack:
                info.wait_for_publish(timeout=timeout)
                if not info.is_published():
                    return False
            with self._lock:
                self.last_publish_at = utc_now()
                self.last_error = ""
            return True
        except Exception as exc:
            with self._lock:
                self.last_error = str(exc)
            log.warning("MQTT publish failed on %s: %s", topic, exc)
            return False

    def _publish_status(self, status, *, wait_for_ack=True):
        return self._publish_signed(
            self.status_topic,
            {
                "device_id": self.device_id,
                "status": status,
                "reported_at": utc_now(),
                "source": "device",
            },
            retain=True,
            wait_for_ack=wait_for_ack,
        )

    def publish_telemetry(self, payload):
        message = dict(payload or {})
        message["device_id"] = self.device_id
        message.setdefault("reported_at", utc_now())
        return self._publish_signed(self.telemetry_topic, message, retain=False)

    def publish_alarm(self, payload):
        message = dict(payload or {})
        message["device_id"] = self.device_id
        return self._publish_signed(self.alarm_topic, message, retain=False)

    def _on_message(self, client, userdata, message):
        if message.topic != self.command_topic:
            return
        try:
            payload = json.loads(message.payload.decode("utf-8"))
            with self._lock:
                key = self._signing_key
            if not verify_payload(payload, key):
                log.warning("Ignoring remote command with an invalid signature")
                return
            if payload.get("device_id") != self.device_id:
                return
            command_id = str(payload.get("command_id", ""))
            if not re.fullmatch(r"[a-fA-F0-9]{32}", command_id):
                return
            issued_at = datetime.fromisoformat(
                str(payload.get("issued_at", "")).replace("Z", "+00:00")
            )
            expires_at = datetime.fromisoformat(
                str(payload.get("expires_at", "")).replace("Z", "+00:00")
            )
            now = datetime.now(timezone.utc)
            if issued_at.tzinfo is None:
                issued_at = issued_at.replace(tzinfo=timezone.utc)
            if expires_at.tzinfo is None:
                expires_at = expires_at.replace(tzinfo=timezone.utc)
            if issued_at > now + timedelta(seconds=30):
                return
            if expires_at <= now or expires_at - issued_at > timedelta(minutes=2):
                self._send_command_ack(command_id, "failed", {"error": "command_expired"})
                return
            command_type = str(payload.get("command_type", ""))
            command_payload = payload.get("payload")
            if not isinstance(command_payload, dict):
                self._send_command_ack(command_id, "failed", {"error": "invalid_payload"})
                return
            threading.Thread(
                target=self._execute_command,
                args=(command_id, command_type, command_payload),
                name=f"mqtt-command-{command_id[:8]}",
                daemon=True,
            ).start()
        except Exception:
            log.exception("Could not parse or dispatch MQTT command")

    def _execute_command(self, command_id, command_type, payload):
        try:
            if not callable(self._command_handler):
                raise RuntimeError("No remote command handler is available.")
            result = self._command_handler(command_type, payload)
            if not isinstance(result, dict):
                result = {"result": str(result or "")[:200]}
            self._send_command_ack(command_id, "applied", result)
        except Exception as exc:
            log.exception("Remote MQTT command %s failed", command_id)
            self._send_command_ack(command_id, "failed", {"error": str(exc)[:300]})

    def _send_command_ack(self, command_id, status, result):
        self._publish_signed(
            self.command_ack_topic,
            {
                "device_id": self.device_id,
                "command_id": command_id,
                "status": status,
                "result": result if isinstance(result, dict) else {"message": str(result)[:300]},
                "acknowledged_at": utc_now(),
            },
            retain=False,
        )

    def publish_log(self, level, message, details=None):
        """Persist a local log item; HTTPS remains the durable log upload path."""
        record = {
            "event_id": uuid.uuid4().hex,
            "level": level if level in {"debug", "info", "warning", "error"} else "info",
            "category": "camera",
            "message": str(message)[:500],
            "details": details if isinstance(details, dict) else {},
            "created_at": utc_now(),
        }
        target = self.log_dir / f"{record['event_id']}.json"
        try:
            atomic_write_json(target, record)
        except OSError:
            log.exception("Could not queue local device log")
        getattr(log, record["level"], log.info)("%s %s", message, details or "")

    def set_metrics(self, frame_fps, inference_fps, model_loaded, pending_alerts,
                    pending_videos=0, rtsp_connected=False, camera_ready=False):
        with self._lock:
            self.frame_fps = round(float(frame_fps), 2)
            self.inference_fps = round(float(inference_fps), 2)
            self.model_loaded = bool(model_loaded)
            self.rtsp_connected = bool(rtsp_connected)
            self.camera_ready = bool(camera_ready)
            self.pending_alert_count = int(pending_alerts)
            self.pending_video_count = int(pending_videos)

    def set_tracking(self, active, duration_seconds):
        with self._lock:
            self.tracking_active = bool(active)
            self.tracking_seconds = round(max(0.0, float(duration_seconds)), 1)

    def snapshot(self):
        with self._lock:
            return {
                "device_id": self.device_id,
                "mqtt_connected": self.connected,
                "mqtt_host": self.host,
                "mqtt_last_publish_at": self.last_publish_at,
                "last_error": self.last_error,
                "video_fps": self.frame_fps,
                "inference_fps": self.inference_fps,
                "model_loaded": self.model_loaded,
                "rtsp_connected": self.rtsp_connected,
                "camera_ready": self.camera_ready,
                "pending_alerts": self.pending_alert_count,
                "pending_video_segments": self.pending_video_count,
                "tracking_active": self.tracking_active,
                "tracking_seconds": self.tracking_seconds,
            }

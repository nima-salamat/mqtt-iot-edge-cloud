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
        self.recording_enabled = False
        self.recording_supported = False
        self.rtsp_connected = False
        self.camera_ready = False
        self.pending_alert_count = 0
        self.pending_video_count = 0
        self.tracking_active = False
        self.tracking_seconds = 0.0
        self._lock = threading.RLock()
        self._reconnect_lock = threading.Lock()
        self._signing_key = ""
        self._server_time_offset_seconds = None
        self._command_handler = None
        self._started = False
        self._command_subscription_ready = False
        self._publish_warning_at = {}
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
            clean_session=True,
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
        self.client.on_subscribe = self._on_subscribe
        self.client.on_publish = self._on_publish
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

    def set_server_time_offset(self, offset_seconds):
        """Use the HTTPS server clock for command expiry when the Pi RTC drifts."""
        try:
            offset = float(offset_seconds)
        except (TypeError, ValueError, OverflowError):
            return False
        if not (-24 * 60 * 60 <= offset <= 24 * 60 * 60):
            return False
        with self._lock:
            self._server_time_offset_seconds = offset
        log.info("Calibrated MQTT command clock against HTTPS server (offset=%+.1fs)", offset)
        return True

    def _server_now(self):
        with self._lock:
            offset = self._server_time_offset_seconds
        now = datetime.now(timezone.utc)
        return now + timedelta(seconds=offset or 0.0)

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
        if not self._signing_key:
            self.last_error = (
                "MQTT connected without a device signing key; signed telemetry and "
                "remote commands remain disabled until HTTPS credential bootstrap succeeds."
            )
            log.error(
                "MQTT signing key is not loaded for device %s; check DEVICE_CREDENTIALS_PATH "
                "and the paired credential file (key contents are never logged)",
                self.device_id,
            )
        else:
            log.info(
                "MQTT signing key loaded for device %s; broker=%s:%s path=%s topic_root=%s",
                self.device_id, self.host, self.port, self.ws_path, self.topic_root,
            )
        self._configure_will()
        self._started = True
        try:
            self.client.connect_async(self.host, self.port, keepalive=45)
            self.client.loop_start()
        except Exception as exc:
            with self._lock:
                self.last_error = f"MQTT startup failed: {exc}"[:500]
            log.exception("Could not start MQTT event channel for device %s", self.device_id)
            raise

    def _reconnect_for_will(self):
        """Restart Paho's network loop once so the new signed Will is sent on CONNECT."""
        with self._reconnect_lock:
            with self._lock:
                if not self._started:
                    return
            try:
                # A manual reconnect while loop_start's worker is still active
                # can race its automatic network handling and create duplicate
                # connections with the same persistent client ID.
                self.client.disconnect()
                self.client.loop_stop()
                with self._lock:
                    if not self._started:
                        return
                self.client.connect_async(self.host, self.port, keepalive=45)
                self.client.loop_start()
            except Exception:
                log.exception("Could not reconnect MQTT after signing-key bootstrap")
                # Keep the client recoverable after a failed reconnect attempt.
                try:
                    with self._lock:
                        should_restart = self._started
                    if should_restart:
                        self.client.connect_async(self.host, self.port, keepalive=45)
                        self.client.loop_start()
                except Exception:
                    log.exception("Could not restart MQTT network loop after reconnect failure")

    def stop(self):
        with self._reconnect_lock:
            try:
                if self.client.is_connected():
                    self._publish_status("offline")
                    self.client.disconnect()
            finally:
                self.client.loop_stop()
                self._started = False
                with self._lock:
                    self.connected = False

    def _report_publish_failure(self, topic, reason):
        message = f"MQTT publish failed for {topic}: {reason}"[:500]
        now = time.monotonic()
        with self._lock:
            self.last_error = message
            previous = self._publish_warning_at.get(topic, 0.0)
            should_log = now - previous >= 30.0
            if should_log:
                self._publish_warning_at[topic] = now
        if should_log:
            log.warning("%s", message)

    def _on_connect(self, client, userdata, flags, reason_code, properties):
        with self._lock:
            self.connected = not reason_code.is_failure
            self._command_subscription_ready = False
            has_key = bool(self._signing_key)
            self.last_error = "" if self.connected and has_key else (
                f"Broker rejected connection: {reason_code}" if not self.connected
                else "MQTT connected but the per-device signing key is not loaded."
            )
        if not self.connected:
            log.error(self.last_error)
            return
        log.info(
            "Connected to MQTT broker for device %s (broker=%s:%s, topic_root=%s)",
            self.device_id, self.host, self.port, self.topic_root,
        )
        if not has_key:
            log.error(
                "MQTT transport is connected but cannot publish signed events or receive "
                "remote commands because the device signing key is missing; check the "
                "effective DEVICE_CREDENTIALS_PATH."
            )
            return
        try:
            result, mid = client.subscribe(self.command_topic, qos=1)
            if result != mqtt.MQTT_ERR_SUCCESS:
                with self._lock:
                    self.last_error = (
                        f"MQTT command subscription could not be queued: "
                        f"{mqtt.error_string(result)}"
                    )
                log.error("%s", self.last_error)
            else:
                log.info(
                    "MQTT command subscription requested (topic=%s, mid=%s)",
                    self.command_topic, mid,
                )
        except Exception:
            log.exception("Could not request MQTT command subscription")
            return

        # Publish the retained online state from a worker thread. Waiting for a
        # PUBACK inside Paho's on_connect callback would block its network loop.
        threading.Thread(
            target=self._publish_initial_status,
            name="mqtt-online-status",
            daemon=True,
        ).start()

    def _publish_initial_status(self):
        if self._publish_status("online", wait_for_ack=True):
            log.info("MQTT retained online status was acknowledged by the broker")
        else:
            with self._lock:
                reason = self.last_error or "no PUBACK received"
            log.error("Could not publish retained MQTT online status: %s", reason)

    def _on_subscribe(self, client, userdata, mid, reason_codes, properties):
        failures = [
            str(code) for code in (reason_codes or [])
            if getattr(code, "is_failure", False)
        ]
        if failures:
            with self._lock:
                self._command_subscription_ready = False
                self.last_error = (
                    "MQTT command subscription rejected: " + ", ".join(failures)
                )[:500]
            log.error("%s", self.last_error)
            return
        with self._lock:
            self._command_subscription_ready = True
            if self.last_error.startswith("MQTT command subscription"):
                self.last_error = ""
        log.info("MQTT command subscription acknowledged (mid=%s)", mid)

    def _on_publish(self, client, userdata, mid, reason_code, properties):
        if getattr(reason_code, "is_failure", False):
            self._report_publish_failure(
                "<async>", f"broker rejected PUBACK for message {mid}: {reason_code}"
            )
            return
        with self._lock:
            self.last_publish_at = utc_now()
            if self.last_error.startswith("MQTT publish failed"):
                self.last_error = ""
        log.debug("MQTT broker acknowledged published message (mid=%s)", mid)

    def _on_disconnect(self, client, userdata, disconnect_flags, reason_code, properties):
        with self._lock:
            self.connected = False
            self._command_subscription_ready = False
            self.last_error = f"MQTT disconnected: {reason_code}"
        log.warning("MQTT disconnected; automatic reconnect remains enabled: %s", reason_code)

    def _publish_signed(self, topic, payload, *, retain=False, timeout=5, wait_for_ack=True):
        if not self.client.is_connected():
            self._report_publish_failure(topic, "client is not connected")
            return False
        signed = self._signed(payload)
        if signed is None:
            self._report_publish_failure(topic, "device signing key is missing or payload signing failed")
            return False
        try:
            info = self.client.publish(
                topic,
                json.dumps(signed, ensure_ascii=False, separators=(",", ":")),
                qos=1,
                retain=retain,
            )
            if info.rc != mqtt.MQTT_ERR_SUCCESS:
                self._report_publish_failure(
                    topic, f"publish was not queued: {mqtt.error_string(info.rc)} (rc={info.rc})"
                )
                return False
            # Never wait inside the Paho network callback. For normal telemetry
            # calls, require the QoS 1 PUBACK before reporting success.
            if wait_for_ack:
                info.wait_for_publish(timeout=timeout)
                if not info.is_published():
                    self._report_publish_failure(
                        topic, f"timed out after {timeout}s waiting for QoS 1 PUBACK"
                    )
                    return False
                with self._lock:
                    self.last_publish_at = utc_now()
                    self.last_error = ""
            return True
        except Exception as exc:
            self._report_publish_failure(topic, f"{type(exc).__name__}: {exc}")
            return False

    def _publish_status(self, status, *, wait_for_ack=True):
        return self._publish_signed(
            self.status_topic,
            {
                "device_id": self.device_id,
                "status": status,
                "reported_at": self._server_now().isoformat(),
                "source": "device",
            },
            retain=True,
            wait_for_ack=wait_for_ack,
        )

    def publish_telemetry(self, payload):
        message = dict(payload or {})
        message["device_id"] = self.device_id
        # Use the calibrated server clock rather than the Pi RTC. This lets
        # Django apply a tight freshness window and prevents old broker-queued
        # telemetry from reviving a disconnected device.
        message["reported_at"] = self._server_now().isoformat()
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
            if issued_at.tzinfo is None:
                issued_at = issued_at.replace(tzinfo=timezone.utc)
            if expires_at.tzinfo is None:
                expires_at = expires_at.replace(tzinfo=timezone.utc)
            now = self._server_now()
            if expires_at <= issued_at or expires_at - issued_at > timedelta(minutes=2):
                self._send_command_ack_async(command_id, "failed", {"error": "invalid_command_expiry"})
                return
            if issued_at > now + timedelta(seconds=30):
                self._send_command_ack_async(command_id, "failed", {"error": "command_not_yet_valid"})
                return
            if expires_at <= now:
                self._send_command_ack_async(command_id, "failed", {"error": "command_expired"})
                return
            command_type = str(payload.get("command_type", ""))
            command_payload = payload.get("payload")
            if not isinstance(command_payload, dict):
                self._send_command_ack_async(command_id, "failed", {"error": "invalid_payload"})
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

    def _send_command_ack_async(self, command_id, status, result):
        """Publish a rejection outside Paho's network callback thread."""
        thread = threading.Thread(
            target=self._send_command_ack,
            args=(command_id, status, result),
            name=f"mqtt-command-ack-{command_id[:8]}",
            daemon=True,
        )
        thread.start()

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

    def set_recording_enabled(self, enabled):
        with self._lock:
            self.recording_enabled = bool(enabled)

    def set_recording_supported(self, supported):
        with self._lock:
            self.recording_supported = bool(supported)

    def set_tracking(self, active, duration_seconds):
        with self._lock:
            self.tracking_active = bool(active)
            self.tracking_seconds = round(max(0.0, float(duration_seconds)), 1)

    def snapshot(self):
        with self._lock:
            return {
                "device_id": self.device_id,
                "mqtt_connected": self.connected,
                "mqtt_signing_key_loaded": bool(self._signing_key),
                "mqtt_server_time_offset_seconds": self._server_time_offset_seconds,
                "mqtt_command_subscription_ready": self._command_subscription_ready,
                "mqtt_host": self.host,
                "mqtt_last_publish_at": self.last_publish_at,
                "last_error": self.last_error,
                "video_fps": self.frame_fps,
                "inference_fps": self.inference_fps,
                "model_loaded": self.model_loaded,
                "recording_enabled": self.recording_enabled,
                "recording_supported": self.recording_supported,
                "rtsp_connected": self.rtsp_connected,
                "camera_ready": self.camera_ready,
                "pending_alerts": self.pending_alert_count,
                "pending_video_segments": self.pending_video_count,
                "tracking_active": self.tracking_active,
                "tracking_seconds": self.tracking_seconds,
            }

"""MQTT alarm publisher and local durable event logger."""
from __future__ import annotations
import json
import logging
import os
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path
import paho.mqtt.client as mqtt
from paho.mqtt.enums import CallbackAPIVersion

log = logging.getLogger(__name__)

def utc_now():
    return datetime.now(timezone.utc).isoformat()

class RaspberryPiMqttBridge:
    """MQTT carries alarm notifications; authoritative data travels over device-authenticated HTTPS."""
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
        self._lock = threading.RLock()
        self.topic_root = f"{self.prefix}/devices/{device_id}"
        self.alarm_topic = f"{self.topic_root}/alarms"
        self.log_dir = Path(os.getenv("PI_DATA_DIR", "/var/lib/mqttiot")) / "logs"
        self.log_dir.mkdir(parents=True, exist_ok=True)
        self.client = mqtt.Client(
            callback_api_version=CallbackAPIVersion.VERSION2,
            client_id=f"alarm-{device_id}",
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

    def start(self):
        if not self.password:
            self.last_error = "MQTT_PASSWORD is empty; MQTT alarm notifications are disabled."
            log.error(self.last_error)
            return
        self.client.connect_async(self.host, self.port, keepalive=45)
        self.client.loop_start()

    def stop(self):
        try:
            if self.client.is_connected():
                self.client.disconnect()
        finally:
            self.client.loop_stop()

    def _on_connect(self, client, userdata, flags, reason_code, properties):
        with self._lock:
            self.connected = not reason_code.is_failure
            self.last_error = "" if self.connected else f"Broker rejected connection: {reason_code}"
        if self.connected:
            log.info("Connected to MQTT alarm channel")
        else:
            log.error(self.last_error)

    def _on_disconnect(self, client, userdata, disconnect_flags, reason_code, properties):
        with self._lock:
            self.connected = False
            self.last_error = f"MQTT disconnected: {reason_code}"
        log.warning("MQTT disconnected; Paho automatic retry remains enabled: %s", reason_code)

    def publish_alarm(self, payload):
        if not self.client.is_connected():
            return False
        try:
            info = self.client.publish(
                self.alarm_topic,
                json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
                qos=1,
                retain=False,
            )
            if info.rc != mqtt.MQTT_ERR_SUCCESS:
                return False
            info.wait_for_publish(timeout=5)
            if not info.is_published():
                return False
            with self._lock:
                self.last_publish_at = utc_now()
                self.last_error = ""
            return True
        except Exception as exc:
            with self._lock:
                self.last_error = str(exc)
            log.warning("MQTT alarm publish failed: %s", exc)
            return False

    def publish_log(self, level, message, details=None):
        """Persist a local log item; the HTTPS uploader sends it to this device's owner."""
        record = {
            "event_id": uuid.uuid4().hex,
            "level": level if level in {"debug", "info", "warning", "error"} else "info",
            "category": "camera",
            "message": str(message)[:500],
            "details": details if isinstance(details, dict) else {},
            "created_at": utc_now(),
        }
        target = self.log_dir / f"{record['event_id']}.json"
        temporary = target.with_suffix(".tmp")
        try:
            temporary.write_text(json.dumps(record, ensure_ascii=False), encoding="utf-8")
            os.chmod(temporary, 0o600)
            temporary.replace(target)
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
            }

"""MQTT device client for Raspberry Pi 4 (secure WebSockets over HTTPS)."""
from __future__ import annotations

import json
import logging
import os
import random
import threading
import uuid
from datetime import datetime, timezone
from typing import Any

import paho.mqtt.client as mqtt
from paho.mqtt.enums import CallbackAPIVersion

log = logging.getLogger("pi-device.mqtt")


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


class RaspberryPiMqttBridge:
    def __init__(self) -> None:
        self.device_id = os.getenv("DEVICE_ID", "raspberry-pi-01")
        self.host = os.getenv("MQTT_HOST", "server.echonode.website")
        self.port = int(os.getenv("MQTT_PORT", "443"))
        self.ws_path = os.getenv("MQTT_WS_PATH", "/mqtt")
        self.use_tls = os.getenv("MQTT_TLS", "1").lower() in {"1", "true", "yes"}
        self.username = os.getenv("MQTT_USERNAME", "raspberry-pi-01")
        self.password = os.getenv("MQTT_PASSWORD", "")
        self.prefix = os.getenv("MQTT_TOPIC_PREFIX", "iotlab").strip("/")
        self.connected = False
        self.output_state = "off"
        self.last_publish_at: str | None = None
        self.last_error: str | None = None
        self.messages: list[dict[str, Any]] = []
        self.lock = threading.RLock()
        self.led = None
        self.gpio_enabled = os.getenv("ENABLE_GPIO", "0").lower() in {"1", "true", "yes"}

        if self.gpio_enabled:
            try:
                from gpiozero import LED
                self.led = LED(int(os.getenv("GPIO_PIN", "17")))
                self.led.off()
                log.info("GPIO output enabled on BCM pin %s", os.getenv("GPIO_PIN", "17"))
            except Exception as exc:
                self.gpio_enabled = False
                self.last_error = f"GPIO initialization failed: {exc}"
                log.exception("Could not initialize GPIO output")

        self.topic_root = f"{self.prefix}/devices/{self.device_id}"
        self.topic_events = f"{self.topic_root}/events"
        self.topic_telemetry = f"{self.topic_root}/telemetry"
        self.topic_status = f"{self.topic_root}/status"
        self.topic_command = f"{self.topic_root}/command"
        self.topic_ack = f"{self.topic_root}/ack"

        self.client = mqtt.Client(
            callback_api_version=CallbackAPIVersion.VERSION2,
            client_id=f"pi-{self.device_id}",
            protocol=mqtt.MQTTv311,
            transport="websockets",
        )
        self.client.ws_set_options(path=self.ws_path)
        if self.use_tls:
            self.client.tls_set()
        if self.username:
            self.client.username_pw_set(self.username, self.password)
        self.client.will_set(
            self.topic_status,
            payload=json.dumps({"device_id": self.device_id, "status": "offline", "reported_at": utc_now()}),
            qos=1,
            retain=True,
        )
        self.client.on_connect = self._on_connect
        self.client.on_disconnect = self._on_disconnect
        self.client.on_message = self._on_message

    def start(self) -> None:
        if not self.password:
            self.last_error = "MQTT_PASSWORD is empty. Set the Raspberry Pi broker password."
            log.error(self.last_error)
            return
        log.info("Connecting to MQTT over WSS: %s:%s%s", self.host, self.port, self.ws_path)
        self.client.connect_async(self.host, self.port, keepalive=45)
        self.client.loop_start()

    def stop(self) -> None:
        try:
            if self.client.is_connected():
                info = self.client.publish(
                    self.topic_status,
                    json.dumps({"device_id": self.device_id, "status": "offline", "reported_at": utc_now()}),
                    qos=1,
                    retain=True,
                )
                if info.rc == mqtt.MQTT_ERR_SUCCESS:
                    try:
                        info.wait_for_publish(timeout=2)
                    except (RuntimeError, ValueError):
                        log.warning("Could not wait for graceful offline status publish")
                self.client.disconnect()
            self.client.loop_stop()
        finally:
            if self.led is not None:
                self.led.off()
                self.led.close()

    def _on_connect(self, client, userdata, flags, reason_code, properties) -> None:
        if reason_code.is_failure:
            with self.lock:
                self.connected = False
                self.last_error = f"Broker rejected the connection: {reason_code}"
            log.error("MQTT connection rejected: %s", reason_code)
            return
        with self.lock:
            self.connected = True
            self.last_error = None
        log.info("Connected to central MQTT broker")
        client.subscribe(self.topic_command, qos=1)
        self._publish(self.topic_status, {"device_id": self.device_id, "status": "online", "reported_at": utc_now()}, retain=True, wait=False)
        self._record_local("connected", {"message": "اتصال امن به Broker برقرار شد"})

    def _on_disconnect(self, client, userdata, disconnect_flags, reason_code, properties) -> None:
        with self.lock:
            self.connected = False
            if reason_code and getattr(reason_code, "is_failure", False):
                self.last_error = f"MQTT disconnected: {reason_code}"
        log.warning("MQTT disconnected: %s", reason_code)

    def _on_message(self, client, userdata, message) -> None:
        try:
            payload = json.loads(message.payload.decode("utf-8"))
            if not isinstance(payload, dict):
                return
        except (UnicodeDecodeError, json.JSONDecodeError):
            log.warning("Ignoring invalid command payload")
            return

        if message.topic != self.topic_command:
            log.warning("Ignoring command on unexpected topic %s", message.topic)
            return

        command = payload.get("command")
        acknowledgement: dict[str, Any] = {
            "device_id": self.device_id,
            "command": command,
            "command_id": payload.get("command_id"),
            "result": "rejected",
            "applied_at": utc_now(),
        }

        if command == "set_output":
            state = payload.get("state")
            if state not in {"on", "off"}:
                log.warning("Ignoring invalid output command: %r", payload)
                return
            self._apply_output(state)
            acknowledgement.update({"state": state, "output_state": state, "result": "applied"})
            self._record_local("فرمان خروجی از داشبورد", acknowledgement)

        elif command == "send_message":
            text = payload.get("message")
            if not isinstance(text, str) or not text.strip() or len(text) > 500:
                log.warning("Ignoring invalid device message")
                return
            acknowledgement.update({"message": text.strip(), "result": "received"})
            self._record_local("پیام دریافت‌شده از داشبورد", {"message": text.strip(), "received_at": utc_now()})

        elif command == "ping":
            acknowledgement.update({"result": "pong", "device_time": utc_now()})
            self._record_local("درخواست Ping از داشبورد", acknowledgement)

        else:
            log.warning("Unsupported command received: %r", command)
            return

        self._publish(self.topic_ack, acknowledgement, wait=False)

    def _apply_output(self, state: str) -> None:
        with self.lock:
            self.output_state = state
            if self.led is not None:
                self.led.on() if state == "on" else self.led.off()

    def _record_local(self, title: str, payload: dict[str, Any]) -> None:
        with self.lock:
            self.messages.insert(0, {"title": title, "payload": payload, "at": utc_now()})
            del self.messages[30:]

    def _publish(self, topic: str, payload: dict[str, Any], retain: bool = False, wait: bool = True) -> bool:
        if not self.client.is_connected():
            with self.lock:
                self.last_error = "اتصال MQTT به VPS برقرار نیست."
            return False
        try:
            info = self.client.publish(
                topic,
                json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
                qos=1,
                retain=retain,
            )
            if info.rc != mqtt.MQTT_ERR_SUCCESS:
                raise RuntimeError(f"MQTT publish failed with code {info.rc}")
            if wait:
                info.wait_for_publish(timeout=6)
                if not info.is_published():
                    raise TimeoutError("Broker did not acknowledge the QoS 1 message within 6 seconds")
            with self.lock:
                self.last_publish_at = utc_now()
                self.last_error = None
            return True
        except Exception as exc:
            with self.lock:
                self.last_error = str(exc)
            log.exception("MQTT publish failed for %s", topic)
            return False

    def send_text(self, message: str) -> tuple[bool, dict[str, Any]]:
        payload = {
            "device_id": self.device_id,
            "type": "user_message",
            "message_id": str(uuid.uuid4()),
            "message": message,
            "sent_at": utc_now(),
        }
        ok = self._publish(self.topic_events, payload)
        if ok:
            self._record_local("پیام ارسال‌شده", payload)
        return ok, payload

    def send_sample(self) -> tuple[bool, dict[str, Any]]:
        payload = {
            "device_id": self.device_id,
            "type": "sensor_sample",
            "temperature_c": round(random.uniform(21.0, 28.0), 2),
            "humidity_percent": round(random.uniform(35.0, 60.0), 1),
            "sampled_at": utc_now(),
            "source": "software-simulated-demo",
        }
        ok = self._publish(self.topic_telemetry, payload)
        if ok:
            self._record_local("نمونه دما و رطوبت ارسال‌شده", payload)
        return ok, payload

    def set_local_output(self, state: str) -> tuple[bool, dict[str, Any]]:
        if state not in {"on", "off"}:
            return False, {"error": "state must be on or off"}
        self._apply_output(state)
        payload = {
            "device_id": self.device_id,
            "type": "local_output_change",
            "output_state": state,
            "gpio_enabled": self.gpio_enabled,
            "changed_at": utc_now(),
        }
        ok = self._publish(self.topic_events, payload)
        if ok:
            self._record_local("تغییر خروجی از سایت محلی", payload)
        return ok, payload

    def snapshot(self) -> dict[str, Any]:
        with self.lock:
            return {
                "device_id": self.device_id,
                "connected": self.connected,
                "output_state": self.output_state,
                "gpio_enabled": self.gpio_enabled,
                "last_publish_at": self.last_publish_at,
                "last_error": self.last_error,
                "messages": list(self.messages),
            }

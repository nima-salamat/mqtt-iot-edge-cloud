"""Publish a signed command through the private Mosquitto WebSocket listener."""
from __future__ import annotations

import json
import logging
import uuid
import threading

from django.conf import settings
import paho.mqtt.client as mqtt
from paho.mqtt.enums import CallbackAPIVersion

logger = logging.getLogger(__name__)


def publish_mqtt_command(device_id: str, signed_payload: dict, timeout: float = 8.0) -> bool:
    if not settings.MQTT_USERNAME or not settings.MQTT_PASSWORD:
        raise RuntimeError("MQTT server credentials are not configured.")

    client = mqtt.Client(
        callback_api_version=CallbackAPIVersion.VERSION2,
        client_id=f"django-command-{uuid.uuid4().hex[:12]}",
        protocol=mqtt.MQTTv311,
        transport="websockets",
    )
    client.ws_set_options(path=settings.MQTT_WS_PATH)
    if settings.MQTT_TLS:
        client.tls_set()
    client.username_pw_set(settings.MQTT_USERNAME, settings.MQTT_PASSWORD)

    connected = threading.Event()
    connection_ok = {"value": False}

    def on_connect(mqtt_client, userdata, flags, reason_code, properties):
        connection_ok["value"] = not reason_code.is_failure
        connected.set()

    client.on_connect = on_connect
    client.on_disconnect = lambda *args: logger.warning("MQTT command publisher disconnected")

    try:
        client.connect(settings.MQTT_HOST, settings.MQTT_PORT, keepalive=30)
        client.loop_start()
        if not connected.wait(timeout):
            raise TimeoutError("MQTT broker connection timed out.")
        if not connection_ok["value"]:
            raise ConnectionError("MQTT broker rejected the server publisher.")
        topic = f"{settings.MQTT_TOPIC_PREFIX.strip('/')}/devices/{device_id}/commands"
        info = client.publish(
            topic,
            json.dumps(signed_payload, ensure_ascii=False, separators=(",", ":")),
            qos=1,
            retain=False,
        )
        if info.rc != mqtt.MQTT_ERR_SUCCESS:
            raise ConnectionError(f"MQTT publish failed with code {info.rc}.")
        info.wait_for_publish(timeout=timeout)
        if not info.is_published():
            raise TimeoutError("MQTT command was not acknowledged by the broker.")
        return True
    finally:
        try:
            client.disconnect()
        except Exception:
            pass
        try:
            client.loop_stop()
        except Exception:
            pass

"""Small MQTT-over-TLS-WebSocket publisher used by Django request handlers."""
import json
import logging
import uuid

from django.conf import settings
import paho.mqtt.client as mqtt
from paho.mqtt.enums import CallbackAPIVersion

logger = logging.getLogger(__name__)


def publish_json(topic: str, payload: dict, qos: int = 1) -> None:
    """Publish one JSON message to the private broker through WSS/HTTPS."""
    if not settings.MQTT_USERNAME or not settings.MQTT_PASSWORD:
        raise RuntimeError("MQTT server credentials are not configured")

    client = mqtt.Client(
        callback_api_version=CallbackAPIVersion.VERSION2,
        client_id=f"django-publisher-{uuid.uuid4().hex[:10]}",
        protocol=mqtt.MQTTv311,
        transport="websockets",
    )
    client.ws_set_options(path=settings.MQTT_WS_PATH)
    if settings.MQTT_TLS:
        client.tls_set()
    client.username_pw_set(settings.MQTT_USERNAME, settings.MQTT_PASSWORD)
    client.connect(settings.MQTT_HOST, settings.MQTT_PORT, keepalive=20)
    client.loop_start()
    try:
        info = client.publish(
            topic,
            json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
            qos=qos,
            retain=False,
        )
        info.wait_for_publish(timeout=8)
        if not info.is_published():
            raise TimeoutError("MQTT publish did not complete before timeout")
        if info.rc != mqtt.MQTT_ERR_SUCCESS:
            raise RuntimeError(f"MQTT publish failed with code {info.rc}")
    finally:
        client.disconnect()
        client.loop_stop()

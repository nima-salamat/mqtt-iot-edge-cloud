from __future__ import annotations

import os
import threading
import time
import uuid
from typing import Any

import paho.mqtt.client as mqtt
from paho.mqtt.enums import CallbackAPIVersion


def _new_client() -> mqtt.Client:
    return mqtt.Client(
        callback_api_version=CallbackAPIVersion.VERSION2,
        client_id=f"alarm-ops-{uuid.uuid4().hex[:10]}",
        protocol=mqtt.MQTTv311,
        transport="tcp",
    )


def _credentials(client: mqtt.Client) -> None:
    username = os.getenv("MQTT_SERVER_USERNAME", "")
    password = os.getenv("MQTT_SERVER_PASSWORD", "")
    if not username or not password:
        raise RuntimeError("MQTT server credentials are not configured.")
    client.username_pw_set(username, password)


def read_broker_metrics(timeout: float = 1.5) -> dict[str, Any]:
    """Connect privately to Mosquitto and collect its published $SYS counters."""
    client = _new_client()
    _credentials(client)

    connected = threading.Event()
    result: dict[str, Any] = {"ok": False, "reason": None}
    values: dict[str, str] = {}
    topics = [
        "$SYS/broker/uptime",
        "$SYS/broker/clients/connected",
        "$SYS/broker/clients/active",
        "$SYS/broker/messages/received",
        "$SYS/broker/messages/sent",
        "$SYS/broker/bytes/received",
        "$SYS/broker/bytes/sent",
    ]

    def on_connect(mqtt_client, userdata, flags, reason_code, properties):
        result["ok"] = not reason_code.is_failure
        result["reason"] = str(reason_code)
        if result["ok"]:
            mqtt_client.subscribe([(topic, 0) for topic in topics])
        connected.set()

    def on_message(mqtt_client, userdata, message):
        values[message.topic] = message.payload.decode("utf-8", errors="replace")

    client.on_connect = on_connect
    client.on_message = on_message
    host = os.getenv("MQTT_BROKER_HOST", "broker")
    port = int(os.getenv("MQTT_BROKER_PORT", "1883"))

    try:
        client.connect(host, port, keepalive=10)
        client.loop_start()
        if not connected.wait(timeout=max(0.5, timeout)):
            return {"available": False, "error": "Broker connection timed out.", "metrics": {}}
        if not result["ok"]:
            return {
                "available": False,
                "error": f"Broker rejected the connection: {result['reason']}",
                "metrics": {},
            }
        time.sleep(min(1.5, max(0.2, timeout - 0.2)))
        return {"available": True, "error": None, "metrics": dict(values)}
    except Exception as exc:
        return {"available": False, "error": str(exc), "metrics": {}}
    finally:
        try:
            client.disconnect()
        except Exception:
            pass
        try:
            client.loop_stop()
        except Exception:
            pass

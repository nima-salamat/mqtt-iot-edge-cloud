from __future__ import annotations

import json
import os
import threading
import time
import uuid
from datetime import datetime, timezone
from typing import Any

import paho.mqtt.client as mqtt
from paho.mqtt.enums import CallbackAPIVersion


def _new_client(client_prefix: str) -> mqtt.Client:
    return mqtt.Client(
        callback_api_version=CallbackAPIVersion.VERSION2,
        client_id=f"{client_prefix}-{uuid.uuid4().hex[:10]}",
        protocol=mqtt.MQTTv311,
        transport="tcp",
    )


def _credentials(client: mqtt.Client) -> None:
    username = os.getenv("MQTT_SERVER_USERNAME", os.getenv("MQTT_USERNAME", ""))
    password = os.getenv("MQTT_SERVER_PASSWORD", os.getenv("MQTT_PASSWORD", ""))
    if not username or not password:
        raise RuntimeError("MQTT server credentials are not configured.")
    client.username_pw_set(username, password)


def _connect(client: mqtt.Client, timeout: float = 5.0) -> None:
    connected = threading.Event()
    result: dict[str, Any] = {"ok": False, "reason": None}

    def on_connect(mqtt_client, userdata, flags, reason_code, properties):
        result["ok"] = not reason_code.is_failure
        result["reason"] = str(reason_code)
        connected.set()

    client.on_connect = on_connect
    host = os.getenv("MQTT_BROKER_HOST", "broker")
    port = int(os.getenv("MQTT_BROKER_PORT", "1883"))
    client.connect(host, port, keepalive=20)
    client.loop_start()

    if not connected.wait(timeout=max(0.5, timeout)):
        client.loop_stop()
        raise TimeoutError(f"Timed out connecting to MQTT broker at {host}:{port}.")
    if not result["ok"]:
        client.loop_stop()
        raise ConnectionError(f"MQTT broker rejected the connection: {result['reason']}")


def publish_device_command(
    device_id: str,
    command: str,
    state: str | None = None,
    message: str | None = None,
) -> dict[str, Any]:
    if command not in {"set_output", "send_message", "ping"}:
        raise ValueError("Unsupported device command.")

    payload: dict[str, Any] = {
        "command": command,
        "command_id": str(uuid.uuid4()),
        "issued_at": datetime.now(timezone.utc).isoformat(),
        "source": "streamlit-cloud-panel",
    }
    if command == "set_output":
        if state not in {"on", "off"}:
            raise ValueError("Output state must be 'on' or 'off'.")
        payload["state"] = state
    elif command == "send_message":
        clean_message = (message or "").strip()
        if not clean_message or len(clean_message) > 500:
            raise ValueError("Device message must contain 1–500 characters.")
        payload["message"] = clean_message

    prefix = os.getenv("MQTT_TOPIC_PREFIX", "iotlab").strip("/")
    topic = f"{prefix}/devices/{device_id}/command"
    client = _new_client("streamlit-control")
    _credentials(client)
    _connect(client)

    try:
        info = client.publish(
            topic,
            json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
            qos=1,
            retain=False,
        )
        if info.rc != mqtt.MQTT_ERR_SUCCESS:
            raise RuntimeError(f"MQTT publish failed with code {info.rc}.")
        info.wait_for_publish(timeout=8)
        if not info.is_published():
            raise TimeoutError("The broker did not acknowledge the QoS 1 command.")
    finally:
        try:
            client.disconnect()
        finally:
            client.loop_stop()

    return {"topic": topic, "payload": payload, "qos": 1}


def read_broker_metrics(timeout: float = 1.5) -> dict[str, Any]:
    """Connect to the private broker and collect its available $SYS counters."""
    client = _new_client("streamlit-monitor")
    _credentials(client)

    connected = threading.Event()
    connection_result: dict[str, Any] = {"ok": False, "reason": None}
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
        connection_result["ok"] = not reason_code.is_failure
        connection_result["reason"] = str(reason_code)
        if connection_result["ok"]:
            mqtt_client.subscribe([(topic, 0) for topic in topics])
        connected.set()

    def on_message(mqtt_client, userdata, message):
        try:
            values[message.topic] = message.payload.decode("utf-8", errors="replace")
        except Exception:
            return

    client.on_connect = on_connect
    client.on_message = on_message

    host = os.getenv("MQTT_BROKER_HOST", "broker")
    port = int(os.getenv("MQTT_BROKER_PORT", "1883"))
    try:
        client.connect(host, port, keepalive=10)
        client.loop_start()
        if not connected.wait(timeout=max(0.5, timeout)):
            return {"available": False, "error": "Broker connection timed out.", "metrics": {}}
        if not connection_result["ok"]:
            return {
                "available": False,
                "error": f"Broker rejected the connection: {connection_result['reason']}",
                "metrics": {},
            }
        # Mosquitto publishes $SYS values periodically; collect a short sample.
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

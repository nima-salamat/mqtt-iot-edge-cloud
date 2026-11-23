import json
import logging
import time

from django.conf import settings
from django.core.management.base import BaseCommand
from django.db import close_old_connections
from django.utils import timezone
import paho.mqtt.client as mqtt
from paho.mqtt.enums import CallbackAPIVersion

from dashboard.models import Device, Telemetry

logger = logging.getLogger(__name__)


class Command(BaseCommand):
    help = "Subscribe to the Raspberry Pi MQTT topics over secure WebSockets."

    def handle(self, *args, **options):
        prefix = settings.MQTT_TOPIC_PREFIX
        device_id = "raspberry-pi-01"
        subscriptions = [
            (f"{prefix}/devices/{device_id}/telemetry", 1),
            (f"{prefix}/devices/{device_id}/events", 1),
            (f"{prefix}/devices/{device_id}/status", 1),
            (f"{prefix}/devices/{device_id}/ack", 1),
        ]

        client = mqtt.Client(
            callback_api_version=CallbackAPIVersion.VERSION2,
            client_id="django-cloud-listener",
            protocol=mqtt.MQTTv311,
            transport="websockets",
        )
        client.ws_set_options(path=settings.MQTT_WS_PATH)
        if settings.MQTT_TLS:
            client.tls_set()
        client.username_pw_set(settings.MQTT_USERNAME, settings.MQTT_PASSWORD)

        def on_connect(mqtt_client, userdata, flags, reason_code, properties):
            if reason_code.is_failure:
                logger.error("MQTT connection rejected: %s", reason_code)
                return
            logger.info("Connected to secure MQTT endpoint %s:%s%s", settings.MQTT_HOST, settings.MQTT_PORT, settings.MQTT_WS_PATH)
            mqtt_client.subscribe(subscriptions)
            logger.info("Subscribed to %s", subscriptions)

        def on_disconnect(mqtt_client, userdata, disconnect_flags, reason_code, properties):
            logger.warning("MQTT listener disconnected: %s", reason_code)

        def on_message(mqtt_client, userdata, message):
            close_old_connections()
            try:
                payload = json.loads(message.payload.decode("utf-8"))
                if not isinstance(payload, dict):
                    logger.warning("Ignoring non-object JSON payload on %s", message.topic)
                    return
                parts = message.topic.split("/")
                # Expected: <prefix>/devices/<device_id>/<telemetry|events|status|ack>
                if len(parts) != 4 or parts[0] != prefix or parts[1] != "devices":
                    logger.warning("Ignoring unexpected topic: %s", message.topic)
                    return
                incoming_device_id, channel = parts[2], parts[3]
                if incoming_device_id != device_id:
                    logger.warning("Ignoring unexpected device: %s", incoming_device_id)
                    return

                device, _ = Device.objects.get_or_create(
                    device_id=device_id,
                    defaults={"name": "Raspberry Pi 4 — دستگاه آزمایشگاه", "kind": Device.RASPBERRY_PI},
                )
                now = timezone.now()
                if channel == "status":
                    status = payload.get("status")
                    if status not in {"online", "offline"}:
                        logger.warning("Invalid status payload: %r", payload)
                        return
                    device.is_online = status == "online"
                    if device.is_online:
                        device.last_seen = now
                    device.save(update_fields=["is_online", "last_seen"])
                    return

                if channel not in {"telemetry", "events", "ack"}:
                    logger.warning("Unexpected MQTT channel: %s", channel)
                    return

                device.is_online = True
                device.last_seen = now
                current_state = device.current_state if isinstance(device.current_state, dict) else {}
                current_state.update(payload)
                device.current_state = current_state
                device.save(update_fields=["is_online", "last_seen", "current_state"])
                Telemetry.objects.create(
                    device=device,
                    topic=message.topic,
                    qos=message.qos,
                    payload=payload,
                )
            except (UnicodeDecodeError, json.JSONDecodeError, TypeError, ValueError):
                logger.exception("Invalid MQTT JSON payload on %s", message.topic)
            except Exception:
                logger.exception("Failed to store MQTT message on %s", message.topic)
            finally:
                close_old_connections()

        client.on_connect = on_connect
        client.on_disconnect = on_disconnect
        client.on_message = on_message

        self.stdout.write(self.style.SUCCESS("Starting secure MQTT-over-WebSocket listener..."))
        while True:
            try:
                client.connect(settings.MQTT_HOST, settings.MQTT_PORT, keepalive=45)
                client.loop_forever(retry_first_connection=True)
            except KeyboardInterrupt:
                break
            except Exception:
                logger.exception("MQTT listener connection failed; retrying in 5 seconds")
                time.sleep(5)
        client.disconnect()

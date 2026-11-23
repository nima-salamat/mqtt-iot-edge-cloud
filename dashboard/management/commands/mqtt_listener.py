"""Consume MQTT alarm notices after the authoritative HTTPS upload is stored."""
from __future__ import annotations

import json
import logging
import time

from django.conf import settings
from django.core.management.base import BaseCommand
from django.db import close_old_connections
from django.utils import timezone
import paho.mqtt.client as mqtt
from paho.mqtt.enums import CallbackAPIVersion

from dashboard.models import Alarm

logger = logging.getLogger(__name__)


class Command(BaseCommand):
    help = "Subscribe to authenticated Pi alarm-notification topics over private MQTT/WSS."

    def handle(self, *args, **options):
        prefix = settings.MQTT_TOPIC_PREFIX.strip("/")
        prefix_parts = prefix.split("/")
        subscription = f"{prefix}/devices/+/alarms"
        client = mqtt.Client(
            callback_api_version=CallbackAPIVersion.VERSION2,
            client_id="django-alarm-listener",
            protocol=mqtt.MQTTv311,
            transport="websockets",
        )
        client.ws_set_options(path=settings.MQTT_WS_PATH)
        if settings.MQTT_TLS:
            client.tls_set()
        if not settings.MQTT_USERNAME or not settings.MQTT_PASSWORD:
            raise RuntimeError("MQTT server listener credentials are not configured.")
        client.username_pw_set(settings.MQTT_USERNAME, settings.MQTT_PASSWORD)

        def on_connect(mqtt_client, userdata, flags, reason_code, properties):
            if reason_code.is_failure:
                logger.error("MQTT alarm listener connection rejected: %s", reason_code)
                return
            mqtt_client.subscribe(subscription, qos=1)
            logger.info("Alarm listener subscribed to %s", subscription)

        def on_disconnect(mqtt_client, userdata, disconnect_flags, reason_code, properties):
            logger.warning("MQTT alarm listener disconnected: %s", reason_code)

        def on_message(mqtt_client, userdata, message):
            close_old_connections()
            try:
                payload = json.loads(message.payload.decode("utf-8"))
                if not isinstance(payload, dict):
                    return
                parts = message.topic.split("/")
                prefix_len = len(prefix_parts)
                if (
                    len(parts) != prefix_len + 3
                    or parts[:prefix_len] != prefix_parts
                    or parts[prefix_len] != "devices"
                    or parts[prefix_len + 2] != "alarms"
                ):
                    logger.warning("Ignoring unexpected alarm topic: %s", message.topic)
                    return

                device_id = parts[prefix_len + 1]
                alert_id = str(payload.get("alert_id", ""))
                if payload.get("device_id") != device_id or not alert_id:
                    logger.warning("Alarm identity mismatch on topic %s", message.topic)
                    return

                # MQTT cannot create or overwrite alert evidence. A device-specific
                # HTTPS token must have already authenticated the image and metadata.
                alarm = Alarm.objects.filter(device_id=device_id, alert_id=alert_id).first()
                if alarm is None:
                    logger.warning("Ignoring MQTT notice for an unknown alarm %s/%s", device_id, alert_id)
                    return
                if alarm.mqtt_received_at is None:
                    alarm.mqtt_received_at = timezone.now()
                    alarm.save(update_fields=["mqtt_received_at"])
            except (UnicodeDecodeError, json.JSONDecodeError, TypeError, ValueError):
                logger.exception("Invalid MQTT alarm payload on %s", message.topic)
            except Exception:
                logger.exception("Failed to process MQTT alarm notice on %s", message.topic)
            finally:
                close_old_connections()

        client.on_connect = on_connect
        client.on_disconnect = on_disconnect
        client.on_message = on_message

        self.stdout.write(self.style.SUCCESS("Starting MQTT alarm notification listener..."))
        try:
            while True:
                try:
                    client.connect(settings.MQTT_HOST, settings.MQTT_PORT, keepalive=45)
                    client.loop_forever(retry_first_connection=True)
                except KeyboardInterrupt:
                    break
                except Exception:
                    logger.exception("MQTT listener connection failed; retrying in 5 seconds")
                    time.sleep(5)
        finally:
            client.disconnect()

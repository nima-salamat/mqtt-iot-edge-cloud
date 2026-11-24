"""Consume signed MQTT telemetry, status, alarms and command acknowledgements."""
from __future__ import annotations

import logging
import time

from django.conf import settings
from django.core.management.base import BaseCommand
from django.db import close_old_connections
import paho.mqtt.client as mqtt
from paho.mqtt.enums import CallbackAPIVersion

from dashboard.mqtt_events import process_mqtt_message

logger = logging.getLogger(__name__)


class Command(BaseCommand):
    help = "Subscribe to authenticated device telemetry, state, alarm and command-ack topics."

    def handle(self, *args, **options):
        prefix = settings.MQTT_TOPIC_PREFIX.strip("/")
        subscriptions = [
            (f"{prefix}/devices/+/alarms", 1),
            (f"{prefix}/devices/+/telemetry", 1),
            (f"{prefix}/devices/+/status", 1),
            (f"{prefix}/devices/+/command-acks", 1),
        ]
        client = mqtt.Client(
            callback_api_version=CallbackAPIVersion.VERSION2,
            client_id="django-iot-listener-v2",
            clean_session=False,
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
                logger.error("MQTT listener connection rejected: %s", reason_code)
                return
            result, mid = mqtt_client.subscribe(subscriptions)
            if result != mqtt.MQTT_ERR_SUCCESS:
                logger.error(
                    "MQTT listener could not queue subscriptions (rc=%s, topics=%s)",
                    result, subscriptions,
                )
                return
            logger.info(
                "MQTT listener subscription request queued (topics=%s, mid=%s)",
                subscriptions, mid,
            )

        def on_subscribe(mqtt_client, userdata, mid, reason_codes, properties):
            failures = [
                str(code) for code in (reason_codes or [])
                if getattr(code, "is_failure", False)
            ]
            if failures:
                logger.error(
                    "MQTT listener subscription rejected (mid=%s): %s",
                    mid, ", ".join(failures),
                )
            else:
                logger.info(
                    "MQTT listener broker subscription acknowledged (mid=%s, grants=%s)",
                    mid, reason_codes,
                )

        def on_disconnect(mqtt_client, userdata, disconnect_flags, reason_code, properties):
            logger.warning("MQTT listener disconnected: %s", reason_code)

        def on_message(mqtt_client, userdata, message):
            close_old_connections()
            try:
                applied = process_mqtt_message(message.topic, message.payload, message.qos)
                if applied:
                    logger.info("Applied MQTT event from broker: %s", message.topic)
                else:
                    logger.warning("MQTT event was ignored or rejected: %s", message.topic)
            except Exception:
                logger.exception("Failed to process MQTT event on %s", message.topic)
            finally:
                close_old_connections()

        client.on_connect = on_connect
        client.on_subscribe = on_subscribe
        client.on_disconnect = on_disconnect
        client.on_message = on_message

        self.stdout.write(self.style.SUCCESS("Starting MQTT device-event listener..."))
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
            try:
                client.disconnect()
            except Exception:
                pass

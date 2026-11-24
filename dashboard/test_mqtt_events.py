"""End-to-end unit coverage for signed MQTT telemetry and remote commands."""
from __future__ import annotations

import json
from datetime import timedelta
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.contrib.auth.hashers import make_password
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from dashboard.models import Device, DeviceCommand
from dashboard.mqtt_events import process_mqtt_message
from dashboard.mqtt_security import device_mqtt_signing_key, sign_mqtt_payload

User = get_user_model()


@override_settings(MQTT_TOPIC_PREFIX="iotlab")
class DeviceMqttEventTests(TestCase):
    def setUp(self):
        self.owner = User.objects.create_user(username="mqtt-owner", password="Long-test-pass-456")
        self.device_token = "mqtt-device-test-token-never-store-plain"
        self.device = Device.objects.create(
            device_id="cam-mqtt-events-01",
            owner=self.owner,
            name="MQTT Test Camera",
            api_token_hash=make_password(self.device_token),
        )
        self.topic = f"iotlab/devices/{self.device.device_id}"

    def signed(self, payload):
        return sign_mqtt_payload(payload, device_mqtt_signing_key(self.device))

    def send(self, event, payload):
        return process_mqtt_message(
            f"{self.topic}/{event}",
            json.dumps(self.signed(payload), ensure_ascii=False).encode("utf-8"),
            qos=1,
        )

    def test_signed_telemetry_updates_current_device_state_without_history_rows(self):
        payload = {
            "device_id": self.device.device_id,
            "reported_at": timezone.now().isoformat(),
            "video_fps": 24.5,
            "inference_fps": 5.0,
            "model_loaded": True,
            "mqtt_connected": True,
            "mqtt_command_subscription_ready": True,
            "recording_supported": True,
            "rtsp_connected": True,
            "camera_ready": True,
            "pending_alerts": 1,
            "pending_video_segments": 2,
            "tracking_active": False,
            "tracking_seconds": 3.5,
            "recording_enabled": True,
            "system": {
                "cpu_percent": 30.0,
                "temperature_c": 52.0,
                "memory": {"total_bytes": 1000, "used_bytes": 400, "available_bytes": 600, "used_percent": 40},
                "storage": {"total_bytes": 2000, "used_bytes": 500, "free_bytes": 1500, "used_percent": 25},
                "path": "/must/not/be/stored",
            },
        }
        self.assertTrue(self.send("telemetry", payload))
        self.device.refresh_from_db()
        self.assertTrue(self.device.is_online)
        self.assertTrue(self.device.model_loaded)
        self.assertAlmostEqual(self.device.reported_video_fps, 24.5)
        self.assertEqual(self.device.current_state["telemetry_transport"], "mqtt")
        self.assertTrue(self.device.current_state["mqtt_command_subscription_ready"])
        self.assertTrue(self.device.current_state["recording_supported"])
        self.assertEqual(self.device.current_state["system"]["temperature_c"], 52.0)
        self.assertEqual(self.device.current_state["system"]["cpu_percent"], 30.0)
        self.assertEqual(self.device.current_state["system"]["memory"]["used_percent"], 40.0)
        self.assertNotIn("path", self.device.current_state["system"])
        self.assertTrue(self.device.current_state["last_device_reported_at"])
        self.assertFalse(self.device.telemetry.exists())

    def test_signed_telemetry_with_calibrated_clock_updates_dashboard_health(self):
        payload = {
            "device_id": self.device.device_id,
            # Pi rewrites MQTT reported_at from the authenticated HTTPS server-time calibration.
            "reported_at": timezone.now().isoformat(),
            "video_fps": 18.0,
            "inference_fps": 4.0,
            "system": {
                "cpu_percent": 46.5,
                "memory": {
                    "total_bytes": 8_000_000,
                    "used_bytes": 3_200_000,
                    "available_bytes": 4_800_000,
                    "used_percent": 40.0,
                },
            },
        }

        self.assertTrue(self.send("telemetry", payload))
        self.device.refresh_from_db()
        self.assertEqual(self.device.current_state["system"]["cpu_percent"], 46.5)
        self.assertEqual(self.device.current_state["system"]["memory"]["used_percent"], 40.0)

        self.client.force_login(self.owner)
        overview = self.client.get(reverse("dashboard-overview"))
        self.assertEqual(overview.status_code, 200)
        device = next(
            item for item in overview.json()["devices"]
            if item["device_id"] == self.device.device_id
        )
        self.assertEqual(device["system"]["cpu_percent"], 46.5)
        self.assertEqual(device["system"]["memory"]["used_percent"], 40.0)

    def test_stale_queued_mqtt_telemetry_cannot_resurrect_an_offline_device(self):
        stale_payload = {
            "device_id": self.device.device_id,
            "reported_at": (timezone.now() - timedelta(minutes=5)).isoformat(),
            "video_fps": 18.0,
            "system": {"cpu_percent": 99.0},
        }

        self.assertFalse(self.send("telemetry", stale_payload))
        self.device.refresh_from_db()
        self.assertFalse(self.device.is_online)
        self.assertIsNone(self.device.last_heartbeat)
        self.assertNotIn("system", self.device.current_state)

    def test_partial_signed_system_sample_preserves_previous_cpu_and_ram_values(self):
        self.device.current_state = {
            "system": {
                "cpu_percent": 31.2,
                "memory": {
                    "total_bytes": 8_000,
                    "used_bytes": 3_600,
                    "available_bytes": 4_400,
                    "used_percent": 45.0,
                },
            },
        }
        self.device.save(update_fields=["current_state"])
        payload = {
            "device_id": self.device.device_id,
            "reported_at": timezone.now().isoformat(),
            "system": {
                "cpu_percent": None,
                "memory": {"total_bytes": 8_192},
            },
        }

        self.assertTrue(self.send("telemetry", payload))
        self.device.refresh_from_db()
        system = self.device.current_state["system"]
        self.assertEqual(system["cpu_percent"], 31.2)
        self.assertEqual(system["memory"]["total_bytes"], 8_192)
        self.assertEqual(system["memory"]["used_percent"], 45.0)

    def test_out_of_order_signed_telemetry_is_rejected(self):
        last_accepted = timezone.now() - timedelta(minutes=2)
        self.device.current_state = {
            "last_device_reported_at": last_accepted.isoformat(),
            "system": {"cpu_percent": 12.0},
        }
        self.device.save(update_fields=["current_state"])
        older_payload = {
            "device_id": self.device.device_id,
            "reported_at": (last_accepted - timedelta(minutes=1)).isoformat(),
            "system": {"cpu_percent": 99.0},
        }

        self.assertFalse(self.send("telemetry", older_payload))
        self.device.refresh_from_db()
        self.assertEqual(self.device.current_state["system"]["cpu_percent"], 12.0)

    def test_invalid_hmac_is_ignored(self):
        payload = self.signed({
            "device_id": self.device.device_id,
            "reported_at": timezone.now().isoformat(),
            "video_fps": 30,
        })
        payload["video_fps"] = 99
        self.assertFalse(process_mqtt_message(
            f"{self.topic}/telemetry",
            json.dumps(payload).encode("utf-8"),
            qos=1,
        ))
        self.device.refresh_from_db()
        self.assertEqual(self.device.reported_video_fps, 0)
        self.assertFalse(self.device.is_online)

    def test_delayed_last_will_cannot_override_fresh_mqtt_telemetry(self):
        self.device.current_state = {
            "mqtt_connected": True,
            "mqtt_command_subscription_ready": True,
            "last_mqtt_telemetry_at": (
                timezone.now() - timedelta(seconds=5)
            ).isoformat(),
        }
        self.device.save(update_fields=["current_state"])

        self.assertFalse(self.send("status", {
            "device_id": self.device.device_id,
            "status": "offline",
            "source": "last-will",
        }))
        self.device.refresh_from_db()
        self.assertTrue(self.device.current_state["mqtt_connected"])
        self.assertTrue(self.device.current_state["mqtt_command_subscription_ready"])

    def test_mqtt_telemetry_rejects_string_booleans_and_fractional_queue_counters(self):
        invalid_updates = (
            {"mqtt_connected": "false"},
            {"camera_ready": 1},
            {"recording_supported": "true"},
            {"pending_alerts": 1.5},
            {"pending_video_segments": True},
        )
        for invalid in invalid_updates:
            with self.subTest(invalid=invalid):
                payload = {
                    "device_id": self.device.device_id,
                    "reported_at": timezone.now().isoformat(),
                    **invalid,
                }
                self.assertFalse(self.send("telemetry", payload))

        self.device.refresh_from_db()
        self.assertFalse(self.device.is_online)
        self.assertIsNone(self.device.last_heartbeat)
        self.assertFalse(self.device.current_state.get("mqtt_connected", False))

    def test_last_will_marks_mqtt_disconnected_without_erasing_https_liveness(self):
        self.device.is_online = True
        self.device.last_seen = timezone.now()
        self.device.last_heartbeat = timezone.now()
        self.device.current_state = {
            "mqtt_connected": True,
            "mqtt_command_subscription_ready": True,
        }
        self.device.save()
        self.assertTrue(self.send("status", {
            "device_id": self.device.device_id,
            "status": "offline",
            "source": "last-will",
        }))
        self.device.refresh_from_db()
        self.assertFalse(self.device.current_state["mqtt_connected"])
        self.assertFalse(self.device.current_state["mqtt_command_subscription_ready"])
        self.assertTrue(self.device.is_online)

    def test_signed_command_ack_updates_command_and_recording_state(self):
        command_id = "a" * 32
        command = DeviceCommand.objects.create(
            command_id=command_id,
            device=self.device,
            command_type="recording.set",
            payload={"enabled": False},
            status="published",
            expires_at=timezone.now() + timedelta(seconds=45),
        )
        self.assertTrue(self.send("command-acks", {
            "device_id": self.device.device_id,
            "command_id": command_id,
            "status": "applied",
            "result": {"recording_enabled": False},
            "acknowledged_at": timezone.now().isoformat(),
        }))
        command.refresh_from_db()
        self.device.refresh_from_db()
        self.assertEqual(command.status, "applied")
        self.assertFalse(self.device.current_state["recording_enabled"])

    def test_pairing_response_provisions_a_per_device_signing_key(self):
        self.client.force_login(self.owner)
        qr = self.client.post(reverse("create-pairing")).json()
        pairing_code = qr["qr_payload"].split(":", 1)[1]
        response = self.client.post(
            reverse("pair-device"),
            data=json.dumps({
                "pairing_code": pairing_code,
                "device_id": "cam-mqtt-pair-01",
                "name": "MQTT Paired Camera",
            }),
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 201)
        key = response.json()["mqtt_signing_key"]
        self.assertRegex(key, r"^[a-f0-9]{64}$")


@override_settings(MQTT_TOPIC_PREFIX="iotlab")
class RemoteDeviceCommandApiTests(TestCase):
    def setUp(self):
        self.owner = User.objects.create_user(username="command-owner", password="Long-test-pass-456")
        self.other_owner = User.objects.create_user(username="command-other", password="Long-test-pass-456")
        self.device = Device.objects.create(
            device_id="cam-command-device-01",
            owner=self.owner,
            name="Command Camera",
            api_token_hash=make_password("command-device-token"),
            is_online=True,
            last_seen=timezone.now(),
            last_heartbeat=timezone.now(),
            current_state={
                "mqtt_connected": True,
                "mqtt_command_subscription_ready": True,
                "camera_ready": True,
                "recording_supported": True,
                "recording_enabled": True,
            },
        )

    @patch("dashboard.views.publish_mqtt_command", return_value=True)
    def test_fresh_heartbeat_overrides_stale_cached_offline_flag(self, publish):
        self.device.is_online = False
        self.device.save(update_fields=["is_online"])
        self.client.force_login(self.owner)

        overview = self.client.get(reverse("dashboard-overview"))
        self.assertEqual(overview.status_code, 200)
        device_state = next(
            item for item in overview.json()["devices"]
            if item["device_id"] == self.device.device_id
        )
        self.assertTrue(device_state["is_online"])

        response = self.client.post(
            reverse("dashboard:device-command", args=[self.device.device_id]),
            data=json.dumps({"command_type": "recording.set", "payload": {"enabled": False}}),
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 202)
        self.assertTrue(publish.called)

    @patch("dashboard.views.publish_mqtt_command", return_value=True)
    def test_remote_command_is_rejected_until_pi_subscribes_to_command_topic(self, publish):
        self.device.current_state = {
            "mqtt_connected": True,
            "mqtt_command_subscription_ready": False,
        }
        self.device.save(update_fields=["current_state"])
        self.client.force_login(self.owner)

        response = self.client.post(
            reverse("dashboard:device-command", args=[self.device.device_id]),
            data=json.dumps({"command_type": "recording.set", "payload": {"enabled": False}}),
            content_type="application/json",
        )

        self.assertEqual(response.status_code, 503)
        self.assertFalse(publish.called)
        self.assertIn("اشتراک", response.json()["error"])

    @patch("dashboard.views.publish_mqtt_command", return_value=True)
    def test_remote_recording_requires_ready_camera_and_supported_backend(self, publish):
        self.client.force_login(self.owner)
        for state, expected_text in (
            ({
                "mqtt_connected": True,
                "mqtt_command_subscription_ready": True,
                "camera_ready": False,
                "recording_supported": True,
            }, "فریم تازه"),
            ({
                "mqtt_connected": True,
                "mqtt_command_subscription_ready": True,
                "camera_ready": True,
                "recording_supported": False,
            }, "پشتیبانی"),
        ):
            with self.subTest(state=state):
                self.device.current_state = state
                self.device.save(update_fields=["current_state"])
                response = self.client.post(
                    reverse("dashboard:device-command", args=[self.device.device_id]),
                    data=json.dumps({"command_type": "recording.set", "payload": {"enabled": False}}),
                    content_type="application/json",
                )
                self.assertEqual(response.status_code, 503 if not state["camera_ready"] else 409)
                self.assertIn(expected_text, response.json()["error"])
        self.assertFalse(publish.called)

    @patch("dashboard.views.publish_mqtt_command", return_value=True)
    def test_owner_can_publish_a_short_lived_signed_recording_command(self, publish):
        self.client.force_login(self.owner)
        response = self.client.post(
            reverse("dashboard:device-command", args=[self.device.device_id]),
            data=json.dumps({"command_type": "recording.set", "payload": {"enabled": False}}),
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 202)
        command = DeviceCommand.objects.get(command_id=response.json()["command_id"])
        self.assertEqual(command.status, "published")
        self.assertEqual(command.payload, {"enabled": False})
        self.assertTrue(publish.called)
        signed_payload = publish.call_args.args[1]
        self.assertEqual(signed_payload["device_id"], self.device.device_id)
        self.assertIn("signature", signed_payload)



    @patch("dashboard.views.publish_mqtt_command")
    def test_fast_device_ack_is_not_overwritten_by_publish_return(self, publish):
        def publish_and_ack(device_id, signed_command):
            ack = sign_mqtt_payload({
                "device_id": device_id,
                "command_id": signed_command["command_id"],
                "status": "applied",
                "result": {"recording_enabled": False},
                "acknowledged_at": timezone.now().isoformat(),
            }, device_mqtt_signing_key(self.device))
            self.assertTrue(process_mqtt_message(
                f"iotlab/devices/{device_id}/command-acks",
                json.dumps(ack).encode("utf-8"),
                qos=1,
            ))
            return True

        publish.side_effect = publish_and_ack
        self.client.force_login(self.owner)
        response = self.client.post(
            reverse("dashboard:device-command", args=[self.device.device_id]),
            data=json.dumps({"command_type": "recording.set", "payload": {"enabled": False}}),
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 202)
        command = DeviceCommand.objects.get(command_id=response.json()["command_id"])
        self.assertEqual(command.status, "applied")

    def test_other_user_cannot_read_device_command_status(self):
        command = DeviceCommand.objects.create(
            command_id="b" * 32,
            device=self.device,
            command_type="recording.set",
            payload={"enabled": True},
            status="published",
            expires_at=timezone.now() + timedelta(seconds=45),
        )
        self.client.force_login(self.other_owner)
        response = self.client.get(reverse(
            "dashboard:device-command-status",
            args=[self.device.device_id, command.command_id],
        ))
        self.assertEqual(response.status_code, 404)

    def test_non_boolean_recording_payload_is_rejected(self):
        self.client.force_login(self.owner)
        response = self.client.post(
            reverse("dashboard:device-command", args=[self.device.device_id]),
            data=json.dumps({"command_type": "recording.set", "payload": {"enabled": "false"}}),
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 400)

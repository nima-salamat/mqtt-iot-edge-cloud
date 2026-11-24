"""Unit tests for Pi-side MQTT message authentication."""
import unittest

from pi_device.mqtt_protocol import sign_payload, verify_payload


class MqttProtocolTests(unittest.TestCase):
    def setUp(self):
        self.key = "a" * 64
        self.payload = {
            "device_id": "cam-test-device-01",
            "event": "telemetry",
            "value": 12.5,
            "nested": {"b": 2, "a": 1},
        }

    def test_signed_payload_verifies_after_json_round_trip(self):
        signed = sign_payload(self.payload, self.key)
        self.assertTrue(verify_payload(signed, self.key))

    def test_payload_tampering_is_detected(self):
        signed = sign_payload(self.payload, self.key)
        signed["value"] = 99
        self.assertFalse(verify_payload(signed, self.key))

    def test_wrong_device_key_does_not_verify(self):
        signed = sign_payload(self.payload, self.key)
        self.assertFalse(verify_payload(signed, "b" * 64))

    def test_signature_is_deterministic_for_same_content(self):
        first = sign_payload(self.payload, self.key)
        second = sign_payload(dict(reversed(list(self.payload.items()))), self.key)
        self.assertEqual(first["signature"], second["signature"])



class MqttBridgePublishTests(unittest.TestCase):
    def setUp(self):
        import os
        import tempfile
        from unittest.mock import patch, Mock

        self.temp_dir = tempfile.TemporaryDirectory()
        self.env_patcher = patch.dict(os.environ, {
            "PI_DATA_DIR": self.temp_dir.name,
            "MQTT_PASSWORD": "test-password",
            "MQTT_HOST": "localhost",
            "MQTT_PORT": "1883",
            "MQTT_TLS": "0",
        })
        self.env_patcher.start()
        from pi_device.bridge import RaspberryPiMqttBridge
        self.bridge = RaspberryPiMqttBridge("cam-test-device-01")
        self.bridge.client = Mock()
        self.bridge.client.is_connected.return_value = True
        self.bridge.set_signing_key("a" * 64)

    def tearDown(self):
        self.env_patcher.stop()
        self.temp_dir.cleanup()

    def test_publish_requires_connected_broker(self):
        self.bridge.client.is_connected.return_value = False

        self.assertFalse(self.bridge.publish_telemetry({"video_fps": 10}))
        self.assertIn("not connected", self.bridge.last_error)

    def test_publish_requires_signing_key(self):
        self.bridge.set_signing_key("")

        self.assertFalse(self.bridge.publish_telemetry({"video_fps": 10}))
        self.assertIn("signing key", self.bridge.last_error)

    def test_publish_queue_rejection_is_reported(self):
        import paho.mqtt.client as mqtt
        from unittest.mock import Mock

        info = Mock()
        info.rc = mqtt.MQTT_ERR_NO_CONN
        self.bridge.client.publish.return_value = info

        self.assertFalse(self.bridge.publish_telemetry({"video_fps": 10}))
        self.assertIn("not queued", self.bridge.last_error)

    def test_publish_requires_qos_one_puback(self):
        import paho.mqtt.client as mqtt
        from unittest.mock import Mock

        info = Mock()
        info.rc = mqtt.MQTT_ERR_SUCCESS
        info.is_published.return_value = False
        self.bridge.client.publish.return_value = info

        self.assertFalse(self.bridge.publish_telemetry({"video_fps": 10}))
        self.assertIn("PUBACK", self.bridge.last_error)
        info.wait_for_publish.assert_called_once_with(timeout=5)

    def test_publish_succeeds_only_after_qos_one_puback(self):
        import paho.mqtt.client as mqtt
        from unittest.mock import Mock

        info = Mock()
        info.rc = mqtt.MQTT_ERR_SUCCESS
        info.is_published.return_value = True
        self.bridge.client.publish.return_value = info

        self.assertTrue(self.bridge.publish_telemetry({"video_fps": 10}))
        self.assertTrue(self.bridge.last_publish_at)
        self.assertEqual(self.bridge.last_error, "")

    def test_connect_without_signing_key_does_not_claim_command_ready(self):
        from types import SimpleNamespace

        self.bridge.set_signing_key("")
        self.bridge._on_connect(
            self.bridge.client, None, {}, SimpleNamespace(is_failure=False), None
        )

        self.bridge.client.subscribe.assert_not_called()
        self.assertTrue(self.bridge.connected)
        self.assertFalse(self.bridge.snapshot()["mqtt_signing_key_loaded"])
        self.assertIn("signing key", self.bridge.last_error)

    def test_successful_suback_marks_remote_command_subscription_ready(self):
        class SuccessCode:
            is_failure = False

        self.bridge._on_subscribe(self.bridge.client, None, 7, [SuccessCode()], None)

        self.assertTrue(self.bridge.snapshot()["mqtt_command_subscription_ready"])

    def test_rejected_suback_is_reported(self):
        class FailureCode:
            is_failure = True

            def __str__(self):
                return "Not authorized"

        self.bridge._on_subscribe(self.bridge.client, None, 7, [FailureCode()], None)

        self.assertFalse(self.bridge.snapshot()["mqtt_command_subscription_ready"])
        self.assertIn("subscription rejected", self.bridge.last_error)


if __name__ == "__main__":
    unittest.main()

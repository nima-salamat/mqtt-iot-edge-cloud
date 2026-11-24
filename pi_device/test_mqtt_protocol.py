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


if __name__ == "__main__":
    unittest.main()

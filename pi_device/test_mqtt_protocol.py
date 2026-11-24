"""Unit tests for Pi-side MQTT message authentication."""
import unittest

from pi_device.mqtt_protocol import (
    decrypt_mqtt_command, encrypt_mqtt_command, sign_payload, verify_payload,
)


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

    def test_signing_key_refresh_restarts_network_loop_in_order(self):
        from unittest.mock import call

        self.bridge._started = True
        self.bridge.client.reset_mock()

        self.bridge._reconnect_for_will()

        self.assertEqual(self.bridge.client.mock_calls, [
            call.disconnect(),
            call.loop_stop(),
            call.connect_async("localhost", 1883, keepalive=45),
            call.loop_start(),
        ])

    def test_actual_recording_state_is_exposed_in_bridge_snapshot(self):
        self.bridge.set_recording_enabled(True)
        self.assertTrue(self.bridge.snapshot()["recording_enabled"])
        self.bridge.set_recording_enabled(False)
        self.assertFalse(self.bridge.snapshot()["recording_enabled"])

    def test_server_time_offset_is_bounded_and_applied(self):
        from datetime import datetime, timezone

        self.assertFalse(self.bridge.set_server_time_offset(25 * 60 * 60))
        before = datetime.now(timezone.utc)
        self.assertTrue(self.bridge.set_server_time_offset(300))
        after = datetime.now(timezone.utc)
        server_now = self.bridge._server_now()
        self.assertAlmostEqual((server_now - after).total_seconds(), 300, delta=1)
        self.assertAlmostEqual(
            self.bridge.snapshot()["mqtt_server_time_offset_seconds"], 300
        )

    def test_signed_command_is_accepted_when_pi_clock_is_five_minutes_behind(self):
        import json
        from datetime import datetime, timedelta, timezone
        from types import SimpleNamespace
        from unittest.mock import Mock, patch
        from pi_device.mqtt_protocol import encrypt_mqtt_command

        local_now = datetime.now(timezone.utc)
        issued_at = local_now + timedelta(minutes=5)
        command_id = "c" * 32
        payload = encrypt_mqtt_command({
            "device_id": self.bridge.device_id,
            "command_id": command_id,
            "command_type": "recording.set",
            "payload": {"enabled": False},
            "issued_at": issued_at.isoformat(),
            "expires_at": (issued_at + timedelta(seconds=45)).isoformat(),
        }, "a" * 64)
        handler = Mock(return_value={"recording_enabled": False})
        self.bridge.set_command_handler(handler)
        self.bridge.set_server_time_offset(300)
        message = SimpleNamespace(
            topic=self.bridge.command_topic,
            payload=json.dumps(payload).encode("utf-8"),
        )

        class ImmediateThread:
            def __init__(self, target, args, **kwargs):
                self.target = target
                self.args = args

            def start(self):
                self.target(*self.args)

        with patch("pi_device.bridge.threading.Thread", ImmediateThread):
            self.bridge._on_message(self.bridge.client, None, message)

        handler.assert_called_once_with("recording.set", {"enabled": False})

    def test_qos1_redelivery_executes_command_once_and_replays_cached_ack(self):
        import json
        from datetime import timedelta
        from types import SimpleNamespace
        from unittest.mock import Mock, patch

        now = self.bridge._server_now()
        command_id = "a" * 32
        envelope = encrypt_mqtt_command({
            "device_id": self.bridge.device_id,
            "command_id": command_id,
            "command_type": "recording.set",
            "payload": {"enabled": False},
            "issued_at": now.isoformat(),
            "expires_at": (now + timedelta(seconds=45)).isoformat(),
        }, "a" * 64)
        handler = Mock(return_value={"recording_enabled": False})
        ack = Mock()
        self.bridge.set_command_handler(handler)
        self.bridge._send_command_ack = ack
        message = SimpleNamespace(
            topic=self.bridge.command_topic,
            payload=json.dumps(envelope).encode("utf-8"),
        )

        class ImmediateThread:
            def __init__(self, target, args, **kwargs):
                self.target, self.args = target, args

            def start(self):
                self.target(*self.args)

        with patch("pi_device.bridge.threading.Thread", ImmediateThread):
            self.bridge._on_message(self.bridge.client, None, message)
            self.bridge._on_message(self.bridge.client, None, message)

        handler.assert_called_once_with("recording.set", {"enabled": False})
        self.assertEqual(ack.call_count, 2)
        self.assertEqual(
            ack.call_args_list[0].args,
            (command_id, "applied", {"recording_enabled": False}),
        )
        self.assertEqual(ack.call_args_list[1].args, ack.call_args_list[0].args)

    def test_qos1_redelivery_while_command_is_inflight_does_not_start_second_worker(self):
        import json
        from datetime import timedelta
        from types import SimpleNamespace
        from unittest.mock import Mock, patch

        now = self.bridge._server_now()
        command_id = "b" * 32
        envelope = encrypt_mqtt_command({
            "device_id": self.bridge.device_id,
            "command_id": command_id,
            "command_type": "recording.set",
            "payload": {"enabled": True},
            "issued_at": now.isoformat(),
            "expires_at": (now + timedelta(seconds=45)).isoformat(),
        }, "a" * 64)
        message = SimpleNamespace(
            topic=self.bridge.command_topic,
            payload=json.dumps(envelope).encode("utf-8"),
        )
        workers = []

        class DeferredThread:
            def __init__(self, target, args, **kwargs):
                self.target, self.args = target, args

            def start(self):
                workers.append((self.target, self.args))

        handler = Mock(return_value={"recording_enabled": True})
        self.bridge.set_command_handler(handler)
        with patch("pi_device.bridge.threading.Thread", DeferredThread):
            self.bridge._on_message(self.bridge.client, None, message)
            self.bridge._on_message(self.bridge.client, None, message)

        self.assertEqual(len(workers), 1)
        self.assertIn(command_id, self.bridge._command_inflight)
        workers[0][0](*workers[0][1])
        handler.assert_called_once_with("recording.set", {"enabled": True})
        self.assertNotIn(command_id, self.bridge._command_inflight)

    def test_aes_gcm_command_round_trip_and_device_isolation(self):
        command = {
            "device_id": self.bridge.device_id,
            "command_id": "f" * 32,
            "command_type": "recording.set",
            "payload": {"enabled": False},
            "issued_at": "2026-11-25T12:00:00+00:00",
            "expires_at": "2026-11-25T12:01:00+00:00",
        }
        envelope = encrypt_mqtt_command(command, "a" * 64)

        self.assertEqual(envelope["encryption"], "aes-256-gcm-v1")
        self.assertNotIn("command_type", envelope)
        self.assertNotIn("payload", envelope)
        self.assertEqual(decrypt_mqtt_command(envelope, "a" * 64), {
            "command_type": "recording.set",
            "payload": {"enabled": False},
        })
        with self.assertRaises(ValueError):
            decrypt_mqtt_command(envelope, "b" * 64)

    def test_aes_gcm_command_tampering_is_rejected(self):
        command = {
            "device_id": self.bridge.device_id,
            "command_id": "e" * 32,
            "command_type": "recording.set",
            "payload": {"enabled": True},
            "issued_at": "2026-11-25T12:00:00+00:00",
            "expires_at": "2026-11-25T12:01:00+00:00",
        }
        envelope = encrypt_mqtt_command(command, "a" * 64)
        envelope["ciphertext"] = envelope["ciphertext"][:-2] + "AA"
        with self.assertRaises(ValueError):
            decrypt_mqtt_command(envelope, "a" * 64)

    def test_bridge_does_not_execute_signed_but_unencrypted_commands(self):
        import json
        from datetime import timedelta
        from types import SimpleNamespace
        from unittest.mock import Mock, patch

        now = self.bridge._server_now()
        command_id = "d" * 32
        payload = sign_payload({
            "device_id": self.bridge.device_id,
            "command_id": command_id,
            "command_type": "recording.set",
            "payload": {"enabled": False},
            "issued_at": now.isoformat(),
            "expires_at": (now + timedelta(seconds=45)).isoformat(),
        }, "a" * 64)
        handler = Mock()
        self.bridge.set_command_handler(handler)
        message = SimpleNamespace(
            topic=self.bridge.command_topic,
            payload=json.dumps(payload).encode("utf-8"),
        )

        with patch("pi_device.bridge.threading.Thread") as thread_factory:
            self.bridge._on_message(self.bridge.client, None, message)

        handler.assert_not_called()
        thread_factory.assert_called_once()
        self.assertEqual(
            thread_factory.call_args.kwargs["args"],
            (command_id, "failed", {"error": "command_decryption_failed"}),
        )

    def test_telemetry_timestamp_uses_calibrated_server_clock(self):
        import json
        from datetime import datetime, timezone
        import paho.mqtt.client as mqtt
        from unittest.mock import Mock
        from pi_device.mqtt_protocol import verify_payload

        info = Mock()
        info.rc = mqtt.MQTT_ERR_SUCCESS
        info.is_published.return_value = True
        self.bridge.client.publish.return_value = info
        self.bridge.set_server_time_offset(300)
        before = datetime.now(timezone.utc)

        self.assertTrue(self.bridge.publish_telemetry({
            "reported_at": "2000-01-01T00:00:00+00:00",
            "video_fps": 10,
        }))

        after = datetime.now(timezone.utc)
        signed = json.loads(self.bridge.client.publish.call_args.args[1])
        self.assertTrue(verify_payload(signed, "a" * 64))
        reported_at = datetime.fromisoformat(signed["reported_at"])
        self.assertGreaterEqual(reported_at, before + __import__("datetime").timedelta(seconds=299))
        self.assertLessEqual(reported_at, after + __import__("datetime").timedelta(seconds=301))

    def test_rejected_command_ack_is_dispatched_off_the_mqtt_callback_thread(self):
        import json
        from datetime import timedelta
        from types import SimpleNamespace
        from unittest.mock import Mock, patch
        from pi_device.mqtt_protocol import sign_payload

        now = self.bridge._server_now()
        command_id = "d" * 32
        payload = sign_payload({
            "device_id": self.bridge.device_id,
            "command_id": command_id,
            "command_type": "recording.set",
            "payload": {"enabled": False},
            "issued_at": now.isoformat(),
            "expires_at": (now + timedelta(minutes=3)).isoformat(),
        }, "a" * 64)
        message = SimpleNamespace(
            topic=self.bridge.command_topic,
            payload=json.dumps(payload).encode("utf-8"),
        )

        with patch("pi_device.bridge.threading.Thread") as thread_factory:
            self.bridge._on_message(self.bridge.client, None, message)

        thread_factory.assert_called_once()
        args = thread_factory.call_args.kwargs
        self.assertEqual(args["target"], self.bridge._send_command_ack)
        self.assertEqual(args["args"], (
            command_id, "failed", {"error": "invalid_command_expiry"}
        ))
        thread_factory.return_value.start.assert_called_once()

    def test_revoked_mqtt_session_stops_and_restarts_after_new_pairing(self):
        from unittest.mock import call

        self.bridge._started = True
        self.bridge.connected = False
        self.bridge.client.is_connected.return_value = False

        self.bridge.revoke_credentials()

        self.assertFalse(self.bridge._started)
        self.assertEqual(self.bridge._signing_key, "")
        self.assertTrue(self.bridge._restart_after_pair)
        self.bridge.client.loop_stop.assert_called_once()

        self.bridge.client.reset_mock()
        self.assertTrue(self.bridge.set_signing_key("b" * 64))

        self.assertTrue(self.bridge._started)
        self.assertFalse(self.bridge._restart_after_pair)
        self.bridge.client.connect_async.assert_called_once_with(
            "localhost", 1883, keepalive=45
        )
        self.bridge.client.loop_start.assert_called_once()
        self.bridge.client.will_set.assert_called_once()

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

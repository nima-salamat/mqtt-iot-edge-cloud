"""Unit tests for the Raspberry Pi durable outbox controls."""
from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from pi_device.uploader import DurableUploader


class QueueManagementTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.env_patch = patch.dict(os.environ, {"PI_DATA_DIR": self.temp_dir.name})
        self.env_patch.start()
        self.addCleanup(self.env_patch.stop)
        self.uploader = DurableUploader(
            device_id="cam-test-device-01",
            settings_store=None,
            bridge=None,
            state_provider=lambda: {},
        )
        self.addCleanup(self.uploader._session.close)

    def _alarm(self, alert_id: str, *, published: bool):
        meta = self.uploader.alarm_dir / f"{alert_id}.json"
        image = self.uploader.alarm_dir / f"{alert_id}.jpg"
        meta.write_text(
            json.dumps({
                "alert_id": alert_id,
                "image": image.name,
                "event": "person_detected",
                "bbox": [],
            }),
            encoding="utf-8",
        )
        image.write_bytes(b"jpeg-test")
        if published:
            meta.with_suffix(".published").write_text("published", encoding="utf-8")
        else:
            # This represents an alarm whose image reached Django, but whose
            # MQTT notification is still pending.
            meta.with_suffix(".uploaded").write_text("uploaded", encoding="utf-8")
        return meta, image

    def test_queue_snapshot_counts_only_items_waiting_to_send(self):
        pending_video = self.uploader.video_dir / "pending.mp4"
        pending_video.write_bytes(b"video-pending")

        open_video = self.uploader.video_dir / "recording.mp4"
        open_video.write_bytes(b"video-open")
        open_video.with_suffix(".open").touch()

        uploaded_video = self.uploader.video_dir / "uploaded.mp4"
        uploaded_video.write_bytes(b"video-uploaded")
        uploaded_video.with_suffix(".uploaded").touch()

        self._alarm("a" * 32, published=False)
        self._alarm("b" * 32, published=True)

        pending_log = self.uploader.log_dir / "pending.json"
        pending_log.write_text("{}", encoding="utf-8")
        archived_log = self.uploader.log_archive_dir / "sent.json"
        archived_log.write_text("{}", encoding="utf-8")

        queues = self.uploader.queue_snapshot()
        self.assertEqual(queues["videos"]["count"], 1)
        self.assertEqual(queues["alarms"]["count"], 1)
        self.assertEqual(queues["logs"]["count"], 1)
        self.assertGreater(queues["videos"]["bytes"], 0)
        self.assertGreater(queues["alarms"]["bytes"], 0)

    def test_clear_pending_queues_preserves_recording_and_uploaded_data(self):
        pending_video = self.uploader.video_dir / "pending.mp4"
        pending_video.write_bytes(b"video-pending")

        open_video = self.uploader.video_dir / "recording.mp4"
        open_video.write_bytes(b"video-open")
        open_video.with_suffix(".open").touch()

        uploaded_video = self.uploader.video_dir / "uploaded.mp4"
        uploaded_video.write_bytes(b"video-uploaded")
        uploaded_video.with_suffix(".uploaded").touch()

        pending_meta, pending_image = self._alarm("c" * 32, published=False)
        published_meta, published_image = self._alarm("d" * 32, published=True)

        pending_log = self.uploader.log_dir / "pending.json"
        pending_log.write_text("{}", encoding="utf-8")
        archived_log = self.uploader.log_archive_dir / "sent.json"
        archived_log.write_text("{}", encoding="utf-8")

        cleared_video = self.uploader.clear_pending_queue("videos")
        self.assertEqual(cleared_video["removed_count"], 1)
        self.assertFalse(pending_video.exists())
        self.assertTrue(open_video.exists())
        self.assertTrue(open_video.with_suffix(".open").exists())
        self.assertTrue(uploaded_video.exists())
        self.assertTrue(uploaded_video.with_suffix(".uploaded").exists())

        cleared_alarm = self.uploader.clear_pending_queue("alarms")
        self.assertEqual(cleared_alarm["removed_count"], 1)
        self.assertFalse(pending_meta.exists())
        self.assertFalse(pending_image.exists())
        self.assertTrue(published_meta.exists())
        self.assertTrue(published_image.exists())

        cleared_logs = self.uploader.clear_pending_queue("logs")
        self.assertEqual(cleared_logs["removed_count"], 1)
        self.assertFalse(pending_log.exists())
        self.assertTrue(archived_log.exists())

    def test_pending_video_upload_is_paused_when_recording_is_disabled(self):
        from types import SimpleNamespace
        from unittest.mock import Mock

        path = self.uploader.video_dir / "1234567890000-pending.mp4"
        path.write_bytes(b"recorded-video")
        self.uploader.settings_store = SimpleNamespace(
            get=lambda: {"recording_enabled": False, "segment_seconds": 5}
        )
        self.uploader._session.post = Mock()

        self.uploader._upload_segments()

        self.uploader._session.post.assert_not_called()
        self.assertTrue(path.exists())
        self.assertFalse(path.with_suffix(".uploaded").exists())

    def test_pending_video_upload_resumes_when_recording_is_enabled(self):
        from types import SimpleNamespace
        from unittest.mock import Mock
        import time

        path = self.uploader.video_dir / "1234567890000-pending.mp4"
        path.write_bytes(b"recorded-video")
        os.utime(path, (time.time() - 3, time.time() - 3))
        self.uploader.settings_store = SimpleNamespace(
            get=lambda: {"recording_enabled": True, "segment_seconds": 5}
        )
        response = Mock()
        response.raise_for_status.return_value = None
        self.uploader._session.post = Mock(return_value=response)

        self.uploader._upload_segments()

        self.uploader._session.post.assert_called_once()
        self.assertTrue(path.with_suffix(".uploaded").exists())
        self.assertTrue(self.uploader.last_video_upload_at)
        self.assertEqual(self.uploader.last_video_upload_error, "")

    def test_video_upload_error_is_reported_without_marking_segment_uploaded(self):
        from types import SimpleNamespace
        from unittest.mock import Mock
        import requests

        path = self.uploader.video_dir / "1234567890000-failed.mp4"
        path.write_bytes(b"recorded-video")
        import time
        os.utime(path, (time.time() - 3, time.time() - 3))
        self.uploader.settings_store = SimpleNamespace(
            get=lambda: {"recording_enabled": True, "segment_seconds": 5}
        )
        response = Mock()
        response.raise_for_status.side_effect = requests.HTTPError("413 upload too large")
        self.uploader._session.post = Mock(return_value=response)

        with self.assertRaises(requests.HTTPError):
            self.uploader._upload_segments()

        self.assertEqual(self.uploader.last_video_upload_error, "413 upload too large")
        self.assertFalse(path.with_suffix(".uploaded").exists())
        self.assertTrue(path.exists())

    def test_heartbeat_includes_temperature_cpu_memory_and_storage(self):
        from types import SimpleNamespace
        from unittest.mock import Mock

        self.uploader.bridge = SimpleNamespace(snapshot=lambda: {
            "video_fps": 25.0,
            "inference_fps": 5.0,
            "model_loaded": True,
            "mqtt_connected": True,
            "rtsp_connected": True,
            "camera_ready": True,
            "pending_alerts": 0,
            "pending_video_segments": 1,
            "tracking_active": False,
            "tracking_seconds": 0.0,
        })
        response = Mock(status_code=200)
        self.uploader._session.post = Mock(return_value=response)
        system = {
            "cpu_percent": 20.0,
            "temperature_c": 49.5,
            "memory": {"total_bytes": 1000, "used_bytes": 400, "available_bytes": 600, "used_percent": 40.0},
            "storage": {"total_bytes": 2000, "used_bytes": 800, "free_bytes": 1200, "used_percent": 40.0},
        }
        with patch("pi_device.uploader.collect_system_stats", return_value=system):
            self.uploader._send_heartbeat()

        payload = self.uploader._session.post.call_args.kwargs["json"]
        self.assertEqual(payload["system"]["temperature_c"], 49.5)
        self.assertEqual(payload["system"]["memory"]["used_percent"], 40.0)

    def test_delete_alarm_removes_one_record_and_delivery_markers(self):
        alert_id = "f" * 32
        meta, image = self._alarm(alert_id, published=False)
        marker = meta.with_suffix(".uploaded")
        result = self.uploader.delete_alarm(alert_id)
        self.assertEqual(result["removed_count"], 1)
        self.assertGreater(result["removed_bytes"], 0)
        self.assertFalse(meta.exists())
        self.assertFalse(image.exists())
        self.assertFalse(marker.exists())

    def test_cleanup_all_local_alarms_removes_pending_and_published_records(self):
        pending_meta, pending_image = self._alarm("1" * 32, published=False)
        published_meta, published_image = self._alarm("2" * 32, published=True)
        result = self.uploader.clear_all_local_alarms()
        self.assertEqual(result["removed_count"], 2)
        self.assertGreater(result["removed_bytes"], 0)
        self.assertFalse(pending_meta.exists())
        self.assertFalse(pending_image.exists())
        self.assertFalse(published_meta.exists())
        self.assertFalse(published_image.exists())
        self.assertEqual(result["remaining"]["alarms"]["count"], 0)

    def test_disconnect_revokes_server_token_then_removes_local_credential_file(self):
        from unittest.mock import Mock

        credentials_path = Path(self.temp_dir.name) / "device.json"
        credentials_path.write_text(
            json.dumps({"api_token": "unit-test-secret-token", "stream_path": "cam-test-device-01"}),
            encoding="utf-8",
        )
        self.uploader.state_provider = lambda: {
            "api_token": "unit-test-secret-token",
            "stream_path": "cam-test-device-01",
        }
        response = Mock(status_code=200)
        with patch.dict(os.environ, {"DEVICE_CREDENTIALS_PATH": str(credentials_path)}):
            self.uploader._session.post = Mock(return_value=response)
            result = self.uploader.disconnect_from_server()
        self.assertEqual(result["status"], "disconnected")
        self.assertFalse(credentials_path.exists())
        self.assertFalse(self.uploader._token())
        self.assertTrue(self.uploader.credentials_revoked)
        self.uploader._session.post.assert_called_once()

    def test_successful_response_checker_calls_raise_for_status_once(self):
        from unittest.mock import Mock

        response = Mock(status_code=200)
        self.uploader._check_response(response)
        response.raise_for_status.assert_called_once_with()

    def test_alarm_snapshot_path_rejects_path_traversal(self):
        alert_id = "e" * 32
        metadata = self.uploader.alarm_dir / f"{alert_id}.json"
        with self.assertRaises(ValueError):
            self.uploader._alarm_snapshot_path(
                metadata,
                {"alert_id": alert_id, "image": "../../outside.jpg"},
            )

    def test_clear_rejects_unknown_queue(self):
        with self.assertRaises(ValueError):
            self.uploader.clear_pending_queue("everything")


    def test_heartbeat_uses_signed_mqtt_as_primary_path_when_connected(self):
        from types import SimpleNamespace
        from unittest.mock import Mock

        snapshot = {
            "video_fps": 25.0,
            "inference_fps": 5.0,
            "model_loaded": True,
            "mqtt_connected": True,
            "rtsp_connected": True,
            "camera_ready": True,
            "pending_alerts": 0,
            "pending_video_segments": 0,
            "tracking_active": False,
            "tracking_seconds": 0.0,
        }
        publisher = Mock(return_value=True)
        self.uploader.bridge = SimpleNamespace(
            snapshot=lambda: snapshot,
            publish_telemetry=publisher,
        )
        self.uploader._session.post = Mock()

        self.uploader._send_heartbeat()

        publisher.assert_called_once()
        self.uploader._session.post.assert_not_called()

    def test_https_heartbeat_bootstraps_and_persists_mqtt_signing_key(self):
        from types import SimpleNamespace
        from unittest.mock import Mock

        credentials = {"api_token": "bootstrap-token", "stream_path": "camera-path"}
        snapshot = {
            "video_fps": 25.0,
            "inference_fps": 5.0,
            "model_loaded": True,
            "mqtt_connected": False,
            "rtsp_connected": True,
            "camera_ready": True,
            "pending_alerts": 0,
            "pending_video_segments": 0,
            "tracking_active": False,
            "tracking_seconds": 0.0,
        }
        bridge = SimpleNamespace(
            snapshot=lambda: snapshot,
            publish_telemetry=Mock(return_value=False),
            set_signing_key=Mock(),
        )
        self.uploader.bridge = bridge
        self.uploader.state_provider = lambda: credentials
        response = Mock(status_code=200)
        signing_key = "c" * 64
        response.json.return_value = {"mqtt_signing_key": signing_key}
        self.uploader._session.post = Mock(return_value=response)
        credentials_path = Path(self.temp_dir.name) / "device.json"

        with patch.dict(os.environ, {"DEVICE_CREDENTIALS_PATH": str(credentials_path)}):
            self.uploader._send_heartbeat()

        stored = json.loads(credentials_path.read_text(encoding="utf-8"))
        self.assertEqual(stored["mqtt_signing_key"], signing_key)
        self.assertEqual(credentials_path.stat().st_mode & 0o777, 0o600)
        bridge.set_signing_key.assert_called_once_with(signing_key)


if __name__ == "__main__":
    unittest.main()

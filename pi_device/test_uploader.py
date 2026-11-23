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


if __name__ == "__main__":
    unittest.main()

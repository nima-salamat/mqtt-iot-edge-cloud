"""Unit tests for persisted Raspberry Pi camera settings migrations."""
from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from pi_device.settings_store import SettingsStore


class VideoBitrateMigrationTests(unittest.TestCase):
    def test_old_factory_bitrate_is_migrated_to_one_megabit(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(
            os.environ, {"VIDEO_BITRATE": "4000000"}
        ):
            path = Path(directory) / "settings.json"
            path.write_text(
                json.dumps({"video_bitrate": 4_000_000, "video_fps": 30}),
                encoding="utf-8",
            )

            settings = SettingsStore(str(path))

            self.assertEqual(settings.get()["video_bitrate"], 1_000_000)
            stored = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(stored["video_bitrate"], 1_000_000)
            self.assertEqual(settings.get()["video_fps"], 30)

    def test_worker_count_is_bounded_and_requires_restart(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "settings.json"
            settings = SettingsStore(str(path))

            updated, restart_required = settings.update({"inference_workers": 8})
            self.assertEqual(updated["inference_workers"], 8)
            self.assertEqual(restart_required, ["inference_workers"])

            updated, restart_required = settings.update({"inference_workers": 99})
            self.assertEqual(updated["inference_workers"], 8)
            self.assertEqual(restart_required, [])

    def test_fps_values_are_not_capped_by_a_fixed_ui_ceiling(self):
        with tempfile.TemporaryDirectory() as directory:
            settings = SettingsStore(str(Path(directory) / "settings.json"))
            updated, restart_required = settings.update({
                "video_fps": 240,
                "video_fps_auto": "false",
                "inference_fps": 180,
                "camera_backend": "opencv",
                "camera_source": "/dev/video2",
            })
            self.assertEqual(updated["video_fps"], 240)
            self.assertFalse(updated["video_fps_auto"])
            self.assertEqual(updated["inference_fps"], 180)
            self.assertEqual(updated["camera_backend"], "opencv")
            self.assertEqual(updated["camera_source"], "/dev/video2")
            self.assertIn("camera_backend", restart_required)
            self.assertIn("camera_source", restart_required)

    def test_recording_pause_is_persisted_without_restarting_camera(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "settings.json"
            settings = SettingsStore(str(path))

            updated, restart_required = settings.update({"recording_enabled": False})
            self.assertFalse(updated["recording_enabled"])
            self.assertEqual(restart_required, [])

            reloaded = SettingsStore(str(path))
            self.assertFalse(reloaded.get()["recording_enabled"])

    def test_operator_selected_bitrate_is_preserved(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "settings.json"
            path.write_text(json.dumps({"video_bitrate": 1_500_000}), encoding="utf-8")
            settings = SettingsStore(str(path))
            self.assertEqual(settings.get()["video_bitrate"], 1_500_000)


if __name__ == "__main__":
    unittest.main()

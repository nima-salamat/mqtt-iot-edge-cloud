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

    def test_operator_selected_bitrate_is_preserved(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "settings.json"
            path.write_text(json.dumps({"video_bitrate": 1_500_000}), encoding="utf-8")
            settings = SettingsStore(str(path))
            self.assertEqual(settings.get()["video_bitrate"], 1_500_000)


if __name__ == "__main__":
    unittest.main()

"""Unit tests for automatic Picamera2 sensor mode selection."""
from __future__ import annotations

import unittest

from pi_device.camera_modes import select_sensor_mode


class SensorModeSelectionTests(unittest.TestCase):
    MODES = [
        {"size": (640, 480), "bit_depth": 10, "fps": 120.0},
        {"size": (1296, 972), "bit_depth": 10, "fps": 58.92},
        {"size": (1920, 1080), "bit_depth": 10, "fps": 30.0},
    ]

    def test_auto_picks_fastest_mode_that_meets_output_size(self):
        mode, fps = select_sensor_mode(self.MODES, 1280, 720, 30, auto=True)
        self.assertEqual(mode["size"], (1296, 972))
        self.assertEqual(fps, 58.92)

    def test_auto_does_not_choose_fast_low_resolution_mode(self):
        mode, fps = select_sensor_mode(self.MODES, 1280, 720, 30, auto=True)
        self.assertGreaterEqual(mode["size"][0], 1280)
        self.assertGreaterEqual(mode["size"][1], 720)
        self.assertNotEqual(mode["size"], (640, 480))

    def test_manual_picks_smallest_mode_that_meets_requested_fps(self):
        mode, fps = select_sensor_mode(self.MODES, 1280, 720, 30, auto=False)
        self.assertEqual(mode["size"], (1920, 1080))
        self.assertEqual(fps, 30)

    def test_manual_clamps_to_fastest_supported_mode(self):
        mode, fps = select_sensor_mode(self.MODES, 1280, 720, 90, auto=False)
        self.assertEqual(mode["size"], (1296, 972))
        self.assertEqual(fps, 58.92)

    def test_empty_modes_returns_fallback(self):
        mode, fps = select_sensor_mode([], 1280, 720, 30, auto=True)
        self.assertIsNone(mode)
        self.assertEqual(fps, 30)


if __name__ == "__main__":
    unittest.main()

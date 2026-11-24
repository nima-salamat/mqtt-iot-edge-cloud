"""Unit tests for automatic Picamera2 sensor mode selection."""
from __future__ import annotations

import unittest

from pi_device.camera_modes import parse_v4l2_modes, select_sensor_mode


class SensorModeSelectionTests(unittest.TestCase):
    MODES = [
        {"size": (640, 480), "bit_depth": 10, "fps": 120.0},
        {"size": (1296, 972), "bit_depth": 10, "fps": 58.92},
        {"size": (1920, 1080), "bit_depth": 10, "fps": 30.0},
    ]

    def test_auto_picks_fastest_sensor_mode_but_keeps_requested_stream_fps(self):
        mode, fps = select_sensor_mode(self.MODES, 1280, 720, 30, auto=True)
        self.assertEqual(mode["size"], (1296, 972))
        self.assertEqual(fps, 30)

    def test_auto_clamps_requested_stream_fps_to_sensor_maximum(self):
        mode, fps = select_sensor_mode(self.MODES, 1280, 720, 90, auto=True)
        self.assertEqual(mode["size"], (1296, 972))
        self.assertEqual(fps, 58.92)

    def test_auto_does_not_choose_fast_low_resolution_mode(self):
        mode, fps = select_sensor_mode(self.MODES, 1280, 720, 30, auto=True)
        self.assertGreaterEqual(mode["size"][0], 1280)
        self.assertGreaterEqual(mode["size"][1], 720)
        self.assertNotEqual(mode["size"], (640, 480))

    def test_manual_picks_smallest_mode_that_meets_requested_fps(self):
        mode, fps = select_sensor_mode(self.MODES, 1280, 720, 30, auto=False)
        self.assertEqual(mode["size"], (1296, 972))
        self.assertEqual(fps, 30)

    def test_manual_clamps_to_fastest_mode_without_changing_selected_resolution(self):
        mode, fps = select_sensor_mode(self.MODES, 1280, 720, 90, auto=False)
        self.assertEqual(mode["size"], (1296, 972))
        self.assertEqual(fps, 58.92)

    def test_manual_uses_fastest_compatible_mode_when_target_is_too_high(self):
        mode, fps = select_sensor_mode(self.MODES, 1280, 720, 150, auto=False)
        self.assertEqual(mode["size"], (1296, 972))
        self.assertEqual(fps, 58.92)

    def test_smaller_selected_resolution_can_use_its_higher_fps_mode(self):
        mode, fps = select_sensor_mode(self.MODES, 640, 480, 90, auto=False)
        self.assertEqual(mode["size"], (640, 480))
        self.assertEqual(fps, 90)

    def test_empty_modes_returns_fallback(self):
        mode, fps = select_sensor_mode([], 1280, 720, 30, auto=True)
        self.assertIsNone(mode)
        self.assertEqual(fps, 30)

    def test_unsupported_resolution_does_not_fall_back_to_a_smaller_mode(self):
        mode, fps = select_sensor_mode(self.MODES, 3840, 2160, 30, auto=False)
        self.assertIsNone(mode)
        self.assertEqual(fps, 30)

    def test_parse_v4l2_discrete_formats_and_frame_rates(self):
        output = """
        [0]: 'MJPG' (Motion-JPEG, compressed)
            Size: Discrete 1920x1080
                Interval: Discrete 0.033s (30.000 fps)
                Interval: Discrete 0.017s (60.000 fps)
            Size: Discrete 1280x720
                Interval: Discrete 0.016s (60.000 fps)
        [1]: 'YUYV' (YUYV 4:2:2)
            Size: Discrete 640x480
                Interval: Discrete 0.033s (30.000 fps)
        """
        modes = parse_v4l2_modes(output)
        self.assertEqual(len(modes), 3)
        by_size = {mode["size"]: mode["fps"] for mode in modes}
        self.assertEqual(by_size[(1920, 1080)], 60.0)
        self.assertEqual(by_size[(1280, 720)], 60.0)
        self.assertEqual(by_size[(640, 480)], 30.0)
        self.assertTrue(all(mode["fourcc"] in {"MJPG", "YUYV"} for mode in modes))
        available = {(mode["size"], mode["fourcc"]) for mode in modes}
        self.assertIn(((1920, 1080), "MJPG"), available)
        self.assertIn(((640, 480), "YUYV"), available)


if __name__ == "__main__":
    unittest.main()

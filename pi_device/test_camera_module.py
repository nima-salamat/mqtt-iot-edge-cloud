"""Regression tests for the camera pipeline import boundary and tracking timer."""
from __future__ import annotations

import importlib
import sys
import types
import unittest
from collections import deque
from types import SimpleNamespace
from unittest.mock import MagicMock, patch


class CameraPipelineStructureTests(unittest.TestCase):
    def setUp(self):
        sys.modules.pop("pi_device.camera", None)

    def tearDown(self):
        sys.modules.pop("pi_device.camera", None)

    def _load_camera_module(self):
        cv2_stub = types.ModuleType("cv2")
        pyzbar_package = types.ModuleType("pyzbar")
        pyzbar_package.__path__ = []
        pyzbar_module = types.ModuleType("pyzbar.pyzbar")
        pyzbar_module.decode = MagicMock(return_value=[])
        with patch.dict(sys.modules, {
            "cv2": cv2_stub,
            "pyzbar": pyzbar_package,
            "pyzbar.pyzbar": pyzbar_module,
        }):
            return importlib.import_module("pi_device.camera")

    def test_camera_pipeline_class_and_lifecycle_methods_are_defined(self):
        module = self._load_camera_module()
        self.assertTrue(hasattr(module, "CameraPipeline"))
        self.assertTrue(callable(module.CameraPipeline.start))
        self.assertTrue(callable(module.CameraPipeline.is_running))
        self.assertTrue(callable(module.CameraPipeline._capture_opencv_loop))

    def test_bridge_queue_and_worker_metrics_are_throttled_to_once_per_second(self):
        module = self._load_camera_module()
        camera = object.__new__(module.CameraPipeline)
        camera._last_metrics_update_at = 0.0
        camera.inference_pool = None
        camera.video_fps = 29.0
        camera.inference_fps = 9.5
        camera.live_output = None
        camera.bridge = MagicMock()
        camera._pending_alarm_count = MagicMock(return_value=0)
        camera._pending_video_count = MagicMock(return_value=4)
        camera._is_camera_ready = MagicMock(return_value=True)

        camera._publish_bridge_metrics(100.0)
        camera._publish_bridge_metrics(100.5)
        camera._publish_bridge_metrics(101.1)

        self.assertEqual(camera.bridge.set_metrics.call_count, 2)
        self.assertEqual(camera._pending_alarm_count.call_count, 2)
        self.assertEqual(camera._pending_video_count.call_count, 2)

    def test_tracking_duration_is_finalized_after_detection_grace_period(self):
        module = self._load_camera_module()
        camera = object.__new__(module.CameraPipeline)
        camera._detection_history = deque([True, True], maxlen=3)
        camera._tracking_active = False
        camera._tracking_started_at = 0.0
        camera._tracking_seconds = 0.0
        camera._last_person_at = 0.0
        camera._last_alert_at = 10.0
        camera._alert_active = False
        camera.settings_store = SimpleNamespace(get=lambda: {
            "alert_clear_seconds": 2,
            "alert_cooldown_seconds": 30,
        })
        camera.bridge = MagicMock()
        camera._create_alarm = MagicMock()

        camera._process_detections(None, [{"confidence": 0.9}], 10.0)
        camera._process_detections(None, [{"confidence": 0.9}], 11.0)
        self.assertTrue(camera._tracking_active)
        self.assertAlmostEqual(camera._tracking_seconds, 1.0)

        camera._process_detections(None, [], 12.9)
        self.assertTrue(camera._tracking_active)
        camera._process_detections(None, [], 13.1)
        self.assertFalse(camera._tracking_active)
        self.assertAlmostEqual(camera._tracking_seconds, 1.0)


if __name__ == "__main__":
    unittest.main()

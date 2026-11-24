"""Regression tests for the camera pipeline import boundary and tracking timer."""
from __future__ import annotations

import importlib
import json
import os
import sys
import tempfile
import types
import unittest
from collections import deque
from pathlib import Path
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
        self.assertIn(
            "tracking_seconds",
            module.CameraPipeline._draw_boxes.__code__.co_varnames,
        )

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

    def test_pairing_credentials_are_written_atomically_with_private_permissions(self):
        module = self._load_camera_module()
        camera = object.__new__(module.CameraPipeline)
        camera.device_id = "cam-pair-test-01"
        camera.uploader = SimpleNamespace(
            state_provider=lambda: {},
            credentials_revoked=True,
        )
        camera.bridge = MagicMock()
        camera.live_output = None
        camera._pair_status = "submitting"
        camera._pair_lock = __import__("threading").RLock()
        camera._pair_in_progress = True

        credentials = {
            "device_id": camera.device_id,
            "api_token": "paired-api-token",
            "mqtt_signing_key": "a" * 64,
            "stream_path": camera.device_id,
        }
        response = MagicMock()
        response.json.return_value = credentials
        response.raise_for_status.return_value = None

        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {
            "DEVICE_CREDENTIALS_PATH": str(Path(directory) / "device.json"),
            "API_BASE_URL": "https://example.invalid",
        }), patch("requests.post", return_value=response):
            camera._pair_request("one-time-pairing-code")
            credential_path = Path(directory) / "device.json"
            stored = json.loads(credential_path.read_text(encoding="utf-8"))

            self.assertEqual(stored, credentials)
            self.assertEqual(credential_path.stat().st_mode & 0o777, 0o600)
            self.assertEqual(camera._pair_status, "paired")
            self.assertFalse(camera.uploader.credentials_revoked)
            camera.bridge.set_signing_key.assert_called_once_with("a" * 64)

    def test_pairing_credentials_refuse_symlink_destination(self):
        module = self._load_camera_module()
        camera = object.__new__(module.CameraPipeline)
        camera.device_id = "cam-pair-test-02"
        camera.uploader = SimpleNamespace(
            state_provider=lambda: {},
            credentials_revoked=True,
        )
        camera.bridge = MagicMock()
        camera.live_output = None
        camera._pair_status = "submitting"
        camera._pair_lock = __import__("threading").RLock()
        camera._pair_in_progress = True

        credentials = {
            "device_id": camera.device_id,
            "api_token": "paired-api-token",
            "mqtt_signing_key": "b" * 64,
        }
        response = MagicMock()
        response.json.return_value = credentials
        response.raise_for_status.return_value = None

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            protected = root / "protected.json"
            protected.write_text("keep-this-file", encoding="utf-8")
            target = root / "device.json"
            target.symlink_to(protected)
            with patch.dict(os.environ, {
                "DEVICE_CREDENTIALS_PATH": str(target),
                "API_BASE_URL": "https://example.invalid",
            }), patch("requests.post", return_value=response):
                camera._pair_request("one-time-pairing-code")

            self.assertEqual(protected.read_text(encoding="utf-8"), "keep-this-file")
            self.assertTrue(target.is_symlink())
            self.assertTrue(camera._pair_status.startswith("failed:"))
            camera.bridge.set_signing_key.assert_not_called()

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

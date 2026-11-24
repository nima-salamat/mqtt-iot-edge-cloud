"""Regression tests for the camera pipeline import boundary and tracking timer."""
from __future__ import annotations

import importlib
import json
import os
import sys
import tempfile
import threading
import time
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

    def _load_local_app_module(self):
        cv2_stub = types.ModuleType("cv2")
        pyzbar_package = types.ModuleType("pyzbar")
        pyzbar_package.__path__ = []
        pyzbar_module = types.ModuleType("pyzbar.pyzbar")
        pyzbar_module.decode = MagicMock(return_value=[])
        sys.modules.pop("pi_device.app", None)
        sys.modules.pop("pi_device.camera", None)
        with patch.dict(sys.modules, {
            "cv2": cv2_stub,
            "pyzbar": pyzbar_package,
            "pyzbar.pyzbar": pyzbar_module,
        }):
            return importlib.import_module("pi_device.app")

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

    def test_local_alarm_list_skips_corrupt_non_object_metadata(self):
        module = self._load_local_app_module()

        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {
            "PI_DATA_DIR": directory,
            "FLASK_SECRET_KEY": "unit-test-flask-secret",
        }):
            alarm_dir = Path(directory) / "alarms"
            alarm_dir.mkdir(parents=True)
            (alarm_dir / f'{"a" * 32}.json').write_text("[]", encoding="utf-8")
            (alarm_dir / f'{"b" * 32}.json').write_text("{broken-json", encoding="utf-8")

            camera = SimpleNamespace(device_id="cam-local-test-01")
            app = module.create_local_app(
                camera,
                SimpleNamespace(get=lambda: {}),
                MagicMock(),
                SimpleNamespace(),
            )
            client = app.test_client()
            with client.session_transaction() as session:
                session["authenticated"] = True
                session["csrf_token"] = "csrf-test"

            response = client.get("/api/alarms/")

            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.get_json(), {"alarms": []})

    def test_local_settings_reject_boolean_and_fractional_camera_resolutions(self):
        module = self._load_local_app_module()
        from pi_device.settings_store import SettingsStore

        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {
            "PI_DATA_DIR": directory,
            "FLASK_SECRET_KEY": "unit-test-flask-secret",
        }):
            settings = SettingsStore(str(Path(directory) / "settings.json"))
            camera = SimpleNamespace(device_id="cam-local-test-02", available_video_modes=[])
            app = module.create_local_app(
                camera,
                settings,
                MagicMock(),
                SimpleNamespace(),
            )
            client = app.test_client()
            with client.session_transaction() as session:
                session["authenticated"] = True
                session["csrf_token"] = "csrf-test"

            for value in (True, 1920.5, 0, -720):
                with self.subTest(value=value):
                    response = client.post(
                        "/api/settings/",
                        json={"video_width": value},
                        headers={"X-CSRF-Token": "csrf-test"},
                    )
                    self.assertEqual(response.status_code, 400)

            self.assertEqual(settings.get()["video_width"], 1280)

    def test_opencv_camera_is_ready_when_it_produces_fresh_frames(self):
        module = self._load_camera_module()
        camera = object.__new__(module.CameraPipeline)
        camera.camera_backend = "opencv"
        camera._opencv_capture = SimpleNamespace(isOpened=lambda: True)
        camera.picam2 = None
        camera._frames_lock = threading.RLock()
        camera.latest_jpeg = b"jpeg-frame"
        camera.latest_frame_at = 100.0

        with patch("pi_device.camera.time.time", return_value=101.0):
            self.assertTrue(camera._is_camera_ready())

        camera.latest_frame_at = 90.0
        with patch("pi_device.camera.time.time", return_value=101.0):
            self.assertFalse(camera._is_camera_ready())

        camera.latest_frame_at = 100.0
        camera._opencv_capture = SimpleNamespace(isOpened=lambda: False)
        with patch("pi_device.camera.time.time", return_value=101.0):
            self.assertFalse(camera._is_camera_ready())

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

    def test_failed_old_mp4_finalize_keeps_new_output_active_and_reports_failure(self):
        module = self._load_camera_module()
        package = types.ModuleType("picamera2")
        package.__path__ = []
        outputs_module = types.ModuleType("picamera2.outputs")

        class BaseOutput:
            def start(self):
                self.started = True

            def _add_stream(self, encoder_stream, codec_name, **kwargs):
                pass

            def outputframe(self, frame, keyframe=True, timestamp=None, packet=None, audio=False):
                pass

            def stop(self):
                pass

        outputs_module.Output = BaseOutput

        class ChildOutput:
            def __init__(self, fail_stop=False):
                self.fail_stop = fail_stop
                self.started = False
                self.stopped = False

            def start(self):
                self.started = True

            def _add_stream(self, encoder_stream, codec_name, **kwargs):
                pass

            def outputframe(self, frame, keyframe=True, timestamp=None, packet=None, audio=False):
                pass

            def stop(self):
                self.stopped = True
                if self.fail_stop:
                    raise OSError("simulated MP4 finalize failure")

        with patch.dict(sys.modules, {
            "picamera2": package,
            "picamera2.outputs": outputs_module,
        }):
            output_class = module._switchable_output_class()
            previous = ChildOutput(fail_stop=True)
            replacement = ChildOutput()
            switchable = output_class(previous)
            switchable.start()

            result = []
            worker = threading.Thread(
                target=lambda: result.append(
                    switchable.switch_output(replacement, timeout=2)
                )
            )
            worker.start()
            deadline = time.monotonic() + 1
            while not switchable._switch_pending and time.monotonic() < deadline:
                time.sleep(0.001)
            self.assertTrue(switchable._switch_pending)

            switchable.outputframe(None, keyframe=True)
            worker.join(timeout=2)

            self.assertFalse(worker.is_alive())
            self.assertEqual(result, [False])
            self.assertTrue(previous.stopped)
            self.assertTrue(replacement.started)
            self.assertIs(switchable._output, replacement)

    def test_manual_pause_keeps_marker_when_mp4_finalize_fails(self):
        module = self._load_camera_module()
        package = types.ModuleType("picamera2")
        package.__path__ = []
        outputs_module = types.ModuleType("picamera2.outputs")
        outputs_module.PyavOutput = MagicMock()
        camera = object.__new__(module.CameraPipeline)
        camera.camera_backend = "picamera2"
        camera._recording_lock = threading.RLock()
        camera.picam2 = object()
        camera.local_output = SimpleNamespace(switch_output=MagicMock(return_value=False))
        camera.recording_enabled = True
        camera._local_recording_attached = True
        camera.settings_store = SimpleNamespace(update=MagicMock())
        camera.uploader = SimpleNamespace(_queue_lock=threading.RLock())
        camera.bridge = SimpleNamespace(set_recording_enabled=MagicMock())
        camera._publish_recording_state = lambda: camera.bridge.set_recording_enabled(
            bool(camera.picam2 is not None and camera._local_recording_attached and camera.recording_enabled)
        )

        with tempfile.TemporaryDirectory() as directory, patch.dict(sys.modules, {
            "picamera2": package,
            "picamera2.outputs": outputs_module,
        }):
            segment = Path(directory) / "partial.mp4"
            marker_path = segment.with_suffix(".open")
            segment.write_bytes(b"partial-mp4")
            marker_path.touch()
            camera._current_segment = segment
            camera._current_marker = marker_path

            result = module.CameraPipeline.set_recording_enabled(camera, False)

            self.assertFalse(result["recording_enabled"])
            self.assertTrue(segment.exists())
            self.assertTrue(marker_path.exists())
            camera.local_output.switch_output.assert_called_once_with(None)
            camera.bridge.set_recording_enabled.assert_called_once_with(False)

    def test_switchable_output_reports_child_finalize_failures_on_shutdown(self):
        module = self._load_camera_module()
        package = types.ModuleType("picamera2")
        package.__path__ = []
        outputs_module = types.ModuleType("picamera2.outputs")

        class BaseOutput:
            def start(self):
                pass

            def stop(self):
                pass

        outputs_module.Output = BaseOutput

        class FailingOutput:
            def start(self):
                pass

            def stop(self):
                raise OSError("simulated close failure")

        with patch.dict(sys.modules, {
            "picamera2": package,
            "picamera2.outputs": outputs_module,
        }):
            output_class = module._switchable_output_class()
            switchable = output_class(FailingOutput())
            switchable.stop()
            self.assertTrue(switchable.stop_failed)

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

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

    def test_flask_session_secret_is_generated_private_and_persistent(self):
        module = self._load_local_app_module()
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {
            "PI_DATA_DIR": directory,
            "FLASK_SECRET_KEY": "",
            "LOCAL_WEB_SECRET_PATH": "",
        }):
            first = module._load_local_flask_secret()
            secret_path = Path(directory) / "local-web-secret"
            second = module._load_local_flask_secret()

            self.assertEqual(len(first), len(second))
            self.assertEqual(first, second)
            self.assertGreaterEqual(len(first), 32)
            self.assertEqual(secret_path.stat().st_mode & 0o777, 0o600)

            app = module.create_local_app(
                SimpleNamespace(device_id="cam-secret-test"),
                SimpleNamespace(get=lambda: {}),
                MagicMock(),
                SimpleNamespace(),
            )
            self.assertEqual(app.secret_key, first)

    def test_flask_session_secret_refuses_symlink_path(self):
        module = self._load_local_app_module()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            external = root / "external-secret"
            external.write_text("external-secret-value-that-must-not-be-used", encoding="utf-8")
            secret_path = root / "local-web-secret"
            secret_path.symlink_to(external)
            with patch.dict(os.environ, {
                "PI_DATA_DIR": directory,
                "FLASK_SECRET_KEY": "",
                "LOCAL_WEB_SECRET_PATH": str(secret_path),
            }):
                with self.assertRaisesRegex(RuntimeError, "symlink"):
                    module._load_local_flask_secret()
            self.assertEqual(
                external.read_text(encoding="utf-8"),
                "external-secret-value-that-must-not-be-used",
            )

    def test_short_configured_flask_session_secret_is_rejected(self):
        module = self._load_local_app_module()
        with patch.dict(os.environ, {
            "FLASK_SECRET_KEY": "too-short",
        }):
            with self.assertRaisesRegex(RuntimeError, "at least 32"):
                module._load_local_flask_secret()

    def test_local_login_requires_csrf_and_throttles_failed_password_attempts(self):
        module = self._load_local_app_module()
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {
            "PI_DATA_DIR": directory,
            "FLASK_SECRET_KEY": "unit-test-flask-secret-0123456789abcdef",
            "LOCAL_WEB_PASSWORD": "correct-local-password",
        }):
            app = module.create_local_app(
                SimpleNamespace(device_id="cam-login-test"),
                SimpleNamespace(get=lambda: {}),
                MagicMock(),
                SimpleNamespace(),
            )
            client = app.test_client()
            login_page = client.get("/login/")
            self.assertEqual(login_page.status_code, 200)
            with client.session_transaction() as session:
                csrf_token = session["csrf_token"]

            csrf_missing = client.post(
                "/login/", data={"password": "correct-local-password"}
            )
            self.assertEqual(csrf_missing.status_code, 403)

            for attempt in range(5):
                response = client.post(
                    "/login/",
                    data={
                        "password": "wrong-password",
                        "csrf_token": csrf_token,
                    },
                    environ_base={"REMOTE_ADDR": "192.0.2.44"},
                )
                self.assertEqual(response.status_code, 401)

            blocked = client.post(
                "/login/",
                data={
                    "password": "correct-local-password",
                    "csrf_token": csrf_token,
                },
                environ_base={"REMOTE_ADDR": "192.0.2.44"},
            )
            self.assertEqual(blocked.status_code, 429)
            self.assertEqual(blocked.headers["Retry-After"], "300")

    def test_local_login_rotates_session_after_valid_credentials(self):
        module = self._load_local_app_module()
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {
            "PI_DATA_DIR": directory,
            "FLASK_SECRET_KEY": "unit-test-flask-secret-0123456789abcdef",
            "LOCAL_WEB_PASSWORD": "correct-local-password",
        }):
            app = module.create_local_app(
                SimpleNamespace(device_id="cam-login-test-02"),
                SimpleNamespace(get=lambda: {}),
                MagicMock(),
                SimpleNamespace(),
            )
            client = app.test_client()
            client.get("/login/")
            with client.session_transaction() as session:
                csrf_token = session["csrf_token"]
                old_cookie_token = csrf_token

            response = client.post(
                "/login/",
                data={
                    "password": "correct-local-password",
                    "csrf_token": csrf_token,
                },
            )
            self.assertEqual(response.status_code, 302)
            self.assertTrue(response.headers["Location"].endswith("/"))
            with client.session_transaction() as session:
                self.assertTrue(session["authenticated"])
                self.assertNotEqual(session["csrf_token"], old_cookie_token)

    def test_local_alarm_list_skips_corrupt_non_object_metadata(self):
        module = self._load_local_app_module()

        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {
            "PI_DATA_DIR": directory,
            "FLASK_SECRET_KEY": "unit-test-flask-secret-0123456789abcdef",
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
            "FLASK_SECRET_KEY": "unit-test-flask-secret-0123456789abcdef",
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

    def test_switchable_output_closes_child_when_base_stop_raises(self):
        module = self._load_camera_module()
        package = types.ModuleType("picamera2")
        package.__path__ = []
        outputs_module = types.ModuleType("picamera2.outputs")

        class BaseOutput:
            def __init__(self):
                self.base_stopped = False

            def stop(self):
                self.base_stopped = True
                raise OSError("simulated base output stop failure")

        outputs_module.Output = BaseOutput

        class ChildOutput:
            def __init__(self):
                self.stopped = False

            def stop(self):
                self.stopped = True

        with patch.dict(sys.modules, {
            "picamera2": package,
            "picamera2.outputs": outputs_module,
        }):
            output_class = module._switchable_output_class()
            child = ChildOutput()
            switchable = output_class(child)
            switchable.stop()

            self.assertTrue(switchable.stop_failed)
            self.assertTrue(switchable.base_stopped)
            self.assertTrue(child.stopped)

    def test_switchable_output_closes_new_muxer_when_stream_setup_fails(self):
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

        class BrokenOutput:
            def __init__(self):
                self.started = False
                self.stopped = False

            def start(self):
                self.started = True

            def _add_stream(self, *args, **kwargs):
                raise RuntimeError("simulated codec setup failure")

            def stop(self):
                self.stopped = True

        with patch.dict(sys.modules, {
            "picamera2": package,
            "picamera2.outputs": outputs_module,
        }):
            output_class = module._switchable_output_class()
            switchable = output_class()
            switchable._streams = [(object(), "h264", {})]
            broken = BrokenOutput()

            with self.assertRaisesRegex(RuntimeError, "codec setup failure"):
                switchable.switch_output(broken)

            self.assertTrue(broken.started)
            self.assertTrue(broken.stopped)

    def test_failed_recording_resume_keeps_incomplete_mp4_marker(self):
        module = self._load_camera_module()
        package = types.ModuleType("picamera2")
        package.__path__ = []
        outputs_module = types.ModuleType("picamera2.outputs")
        outputs_module.PyavOutput = MagicMock()
        camera = object.__new__(module.CameraPipeline)
        camera.camera_backend = "picamera2"
        camera._recording_lock = threading.RLock()
        camera.picam2 = object()
        camera.local_output = SimpleNamespace(
            switch_output=MagicMock(side_effect=TimeoutError("no keyframe"))
        )
        camera.recording_enabled = False
        camera._local_recording_attached = False
        camera.settings_store = SimpleNamespace(update=MagicMock())
        camera.bridge = SimpleNamespace(set_recording_enabled=MagicMock())
        camera._publish_recording_state = lambda: camera.bridge.set_recording_enabled(
            bool(camera.picam2 is not None and camera._local_recording_attached and camera.recording_enabled)
        )

        with tempfile.TemporaryDirectory() as directory, patch.dict(sys.modules, {
            "picamera2": package,
            "picamera2.outputs": outputs_module,
        }):
            expected_segment = Path(directory) / "failed-resume.mp4"
            camera._segment_path = lambda: expected_segment

            with self.assertRaisesRegex(TimeoutError, "no keyframe"):
                module.CameraPipeline.set_recording_enabled(camera, True)

            self.assertTrue(expected_segment.with_suffix(".open").exists())
            camera.local_output.switch_output.assert_called_once()
            self.assertFalse(camera.recording_enabled)

    def test_camera_stop_continues_cleanup_when_rtsps_output_fails(self):
        module = self._load_camera_module()
        camera = object.__new__(module.CameraPipeline)
        camera._stop = threading.Event()
        camera._threads = []
        camera.inference_pool = None
        camera._inflight_frames = {}
        camera._recording_lock = threading.RLock()
        live_output = SimpleNamespace(
            stop=MagicMock(side_effect=OSError("simulated RTSPS stop failure")),
            _thread=SimpleNamespace(is_alive=lambda: True),
        )
        camera.live_output = live_output
        camera._opencv_capture = None
        camera.picam2 = MagicMock()
        camera.encoder = object()
        camera.local_output = SimpleNamespace(
            stop_failed=False,
            stop=MagicMock(),
        )
        camera._current_marker = None
        camera._current_segment = None
        camera._local_recording_attached = False
        camera.recording_enabled = True
        camera._tracking_seconds = 0.0
        camera.inference_fps = 5.0
        camera.inference_latency_ms = 10.0
        camera.bridge = SimpleNamespace(
            set_tracking=MagicMock(),
            set_recording_supported=MagicMock(),
            set_recording_enabled=MagicMock(),
            model_loaded=True,
        )
        camera._publish_recording_state = lambda: camera.bridge.set_recording_enabled(
            bool(camera.picam2 is not None and camera._local_recording_attached and camera.recording_enabled)
        )
        camera._frames_lock = threading.RLock()
        camera.latest_jpeg = b"frame"
        camera.latest_frame_at = 1.0
        camera._latest_bgr = object()
        camera._last_detections = [{"bbox": [0, 0, 1, 1]}]
        camera._last_detection_at = 1.0
        picam2 = camera.picam2
        encoder = camera.encoder
        local_output = camera.local_output

        module.CameraPipeline.stop(camera)

        picam2.stop.assert_called_once()
        picam2.stop_encoder.assert_called_once_with(encoder)
        picam2.close.assert_called_once()
        local_output.stop.assert_called_once()
        camera.bridge.set_recording_supported.assert_called_once_with(False)
        self.assertIsNone(camera.picam2)
        self.assertIs(camera.live_output, live_output)
        self.assertTrue(camera.is_running())
        self.assertIsNone(camera.latest_jpeg)

    def test_camera_stop_retains_live_capture_worker_and_blocks_restart(self):
        module = self._load_camera_module()
        camera = object.__new__(module.CameraPipeline)
        worker = SimpleNamespace(
            name="stuck-camera-capture",
            join=MagicMock(),
            is_alive=lambda: True,
        )
        camera._stop = threading.Event()
        camera._threads = [worker]
        camera.inference_pool = None
        camera._inflight_frames = {}
        camera._recording_lock = threading.RLock()
        camera.live_output = None
        camera._opencv_capture = None
        camera.picam2 = None
        camera.encoder = None
        camera.local_output = None
        camera._current_marker = None
        camera._current_segment = None
        camera._local_recording_attached = False
        camera.recording_enabled = False
        camera._tracking_seconds = 0.0
        camera.inference_fps = 0.0
        camera.inference_latency_ms = 0.0
        camera.bridge = SimpleNamespace(
            set_tracking=MagicMock(),
            set_recording_supported=MagicMock(),
            set_recording_enabled=MagicMock(),
            model_loaded=False,
        )
        camera._publish_recording_state = lambda: None
        camera._frames_lock = threading.RLock()
        camera.latest_jpeg = None
        camera.latest_frame_at = 0.0
        camera._latest_bgr = None
        camera._last_detections = []
        camera._last_detection_at = 0.0

        module.CameraPipeline.stop(camera)

        worker.join.assert_called_once_with(timeout=14)
        self.assertEqual(camera._threads, [worker])
        self.assertTrue(camera.is_running())

    def test_rtsps_stop_timeout_is_reported_so_supervisor_can_retry(self):
        module = self._load_camera_module()
        output = object.__new__(module.ReconnectingRtspOutput)
        output._stop = threading.Event()
        output._wake = threading.Event()
        output._thread = SimpleNamespace(
            join=MagicMock(),
            is_alive=lambda: True,
        )
        output.error = ""

        with self.assertRaisesRegex(TimeoutError, "did not stop"):
            module.ReconnectingRtspOutput.stop(output)

        self.assertTrue(output._stop.is_set())
        self.assertTrue(output._wake.is_set())
        output._thread.join.assert_called_once_with(timeout=4)
        self.assertIn("did not stop", output.error)

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

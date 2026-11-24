"""Picamera2 pipeline: hardware H.264, rotating offline spool, YOLOv5n NCNN and QR pairing."""
from __future__ import annotations
import json
import logging
import os
import re
import subprocess
import threading
import time
import uuid
from collections import deque
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote
import cv2
import requests

try:
    # Debian/Raspberry Pi OS supplies the native libzbar0 shared library.
    # pyzbar uses it for QR decoding when OpenCV was built without QUIRC.
    from pyzbar.pyzbar import decode as _zbar_decode
except (ImportError, OSError):
    _zbar_decode = None

from .atomic_files import atomic_write_bytes, atomic_write_json
from .inference_pool import AdaptiveInferencePool
from .camera_modes import parse_v4l2_modes, select_sensor_mode

log = logging.getLogger(__name__)

class ReconnectingRtspOutput:
    """Retry RTSP-over-TLS publishing without stopping local video recording."""
    def __init__(self, device_id, credentials_provider):
        from picamera2.outputs import SplittableOutput
        self.device_id = device_id
        self.credentials_provider = credentials_provider
        self.host = os.getenv("RTSPS_HOST", os.getenv("DOMAIN", "server.echonode.website"))
        self.port = int(os.getenv("RTSPS_PORT", "8322"))
        self.output = SplittableOutput()
        self.connected = False
        self.error = ""
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._thread = None
        self._backoff = 2

    def _new_output(self):
        from picamera2.outputs import PyavOutput
        credentials = self.credentials_provider() or {}
        token = credentials.get("api_token", "")
        stream_path = credentials.get("stream_path", self.device_id)
        if not token:
            return None
        user = quote(self.device_id, safe="")
        password = quote(token, safe="")
        url = f"rtsps://{user}:{password}@{self.host}:{self.port}/{stream_path}"
        output = PyavOutput(url, format="rtsp", options={"rtsp_transport": "tcp", "rw_timeout": "5000000"})
        output.error_callback = self._on_error
        return output

    def _redact_error(self, error):
        message = str(error)
        credentials = self.credentials_provider() or {}
        token = str(credentials.get("api_token", ""))
        if token:
            message = message.replace(token, "[device-token-redacted]")
        return message

    def _on_error(self, error):
        self.connected = False
        self.error = self._redact_error(error)
        self._wake.set()
        log.warning("RTSPS output failed; local recording continues: %s", self.error)

    def start(self):
        self._thread = threading.Thread(target=self._reconnect_loop, name="rtsps-reconnect", daemon=True)
        self._thread.start()

    def credentials_changed(self):
        self._wake.set()

    def _reconnect_loop(self):
        while not self._stop.is_set():
            if self.connected:
                if not (self.credentials_provider() or {}).get("api_token"):
                    try:
                        # Revoke an already-authorized publish session immediately,
                        # without terminating the local camera or its H.264 encoder.
                        self.output.split_output(None)
                    except Exception:
                        log.exception("Could not detach revoked RTSPS output")
                    self.connected = False
                    self.error = "Device pairing has been revoked"
                    continue
                self._wake.wait(2)
                self._wake.clear()
                continue
            try:
                replacement = self._new_output()
                if replacement is None:
                    self._wake.wait(5)
                    self._wake.clear()
                    continue
                # SplittableOutput changes outputs at the next keyframe, without stopping H.264.
                self.output.split_output(replacement)
                self.connected = True
                self.error = ""
                self._backoff = 2
                log.info("Live RTSPS publishing resumed for device %s", self.device_id)
            except Exception as exc:
                self.connected = False
                self.error = self._redact_error(exc)
                log.info("RTSPS unavailable; retry in %ss: %s", self._backoff, self.error)
                self._stop.wait(self._backoff)
                self._backoff = min(60, self._backoff * 2)

    def stop(self):
        self._stop.set()
        self._wake.set()
        if self._thread:
            # split_output can be waiting for a keyframe; it is a daemon thread.
            self._thread.join(timeout=4)
        # The encoder owns this SplittableOutput and closes its active child
        # when Picamera2 stops the encoder. Do not stop it while frames are
        # still being delivered.



def _switchable_output_class():
    """Create a Picamera2 Output that can attach/detach MP4 recording at keyframes."""
    from picamera2.outputs import Output

    class SwitchableOutput(Output):
        def __init__(self, initial_output=None):
            super().__init__()
            self._output = initial_output
            self._pending_output = None
            self._switch_pending = False
            self._switch_done = threading.Event()
            self._lock = threading.RLock()
            self._streams = []

        def start(self):
            super().start()
            with self._lock:
                output = self._output
            if output is not None:
                output.start()

        def _add_stream(self, encoder_stream, codec_name, **kwargs):
            stream = (encoder_stream, codec_name, dict(kwargs))
            with self._lock:
                self._streams.append(stream)
                output = self._output
            if output is not None:
                output._add_stream(encoder_stream, codec_name, **kwargs)

        def switch_output(self, new_output, timeout=12.0):
            # Prepare the new muxer and its stream definitions before asking
            # outputframe() to switch on an H.264 keyframe.
            with self._lock:
                if self._switch_pending:
                    raise RuntimeError("A local recording transition is already in progress")
                old_output = self._output
                streams = list(self._streams)

            if new_output is not None:
                new_output.start()
                try:
                    for encoder_stream, codec_name, kwargs in streams:
                        new_output._add_stream(encoder_stream, codec_name, **kwargs)
                except Exception:
                    new_output.stop()
                    raise

            with self._lock:
                self._pending_output = new_output
                self._switch_pending = True
                self._switch_done.clear()

            completed = self._switch_done.wait(timeout=max(1.0, float(timeout)))
            if not completed:
                with self._lock:
                    # A keyframe can race the timeout boundary. Treat an already
                    # completed pointer swap as success instead of closing the
                    # output that the encoder has just begun using.
                    completed = not self._switch_pending and self._output is new_output
                    if not completed and self._switch_pending and self._pending_output is new_output:
                        self._pending_output = None
                        self._switch_pending = False
                if not completed:
                    if new_output is not None:
                        new_output.stop()
                    raise TimeoutError("Timed out waiting for an H.264 keyframe to change recording state")

            # outputframe has stopped referencing the previous output, so it is
            # now safe to finalize its MP4 container on this control thread.
            if old_output is not None:
                old_output.stop()

        def outputframe(self, frame, keyframe=True, timestamp=None, packet=None, audio=False):
            with self._lock:
                if keyframe and self._switch_pending:
                    self._output = self._pending_output
                    self._pending_output = None
                    self._switch_pending = False
                    self._switch_done.set()
                output = self._output
            if output is not None:
                output.outputframe(frame, keyframe, timestamp, packet, audio)

        def stop(self):
            super().stop()
            with self._lock:
                outputs = (self._output, self._pending_output if self._switch_pending else None)
                self._output = None
                self._pending_output = None
                self._switch_pending = False
            seen = set()
            for output in outputs:
                if output is not None and id(output) not in seen:
                    seen.add(id(output))
                    try:
                        output.stop()
                    except Exception:
                        log.exception("Could not close a local recording output")

    return SwitchableOutput


class CameraPipeline:
    def __init__(self, device_id, settings_store, bridge, uploader):
        self.device_id = device_id
        self.settings_store = settings_store
        self.bridge = bridge
        self.uploader = uploader
        self.data_dir = Path(os.getenv("PI_DATA_DIR", "/var/lib/mqttiot"))
        self.video_dir = self.data_dir / "video"
        self.alarm_dir = self.data_dir / "alarms"
        self.video_dir.mkdir(parents=True, exist_ok=True)
        self.alarm_dir.mkdir(parents=True, exist_ok=True)
        self.picam2 = None
        self._opencv_capture = None
        self.camera_backend = str(settings_store.get().get("camera_backend", "picamera2"))
        self.camera_source = str(settings_store.get().get("camera_source", "0"))
        self.camera_model = ""
        self.camera_resolution = ""
        self.stream_width = int(settings_store.get().get("video_width", 1280))
        self.stream_height = int(settings_store.get().get("video_height", 720))
        self.camera_max_fps = 0.0
        self.target_video_fps = float(settings_store.get().get("video_fps", 30))
        self.available_video_modes = []
        self.video_fps_options = []
        self.encoder = None
        self.local_output = None
        self.live_output = None
        self.inference_pool = None
        self.latest_jpeg = None
        self.latest_frame_at = 0.0
        self._latest_bgr = None
        self._latest_bgr_at = 0.0
        self._last_detection_at = 0.0
        self._last_preview_at = 0.0
        self._last_encoder_count = 0
        self._last_metrics_update_at = 0.0
        self.video_fps = 0.0
        self.inference_fps = 0.0
        self.inference_latency_ms = 0.0
        self.inference_dropped_frames = 0
        self._inference_completed_at = deque(maxlen=100)
        self._inference_pool_started_at = 0.0
        self._last_processed_frame_sequence = 0
        self._inflight_frames = {}
        self._frame_sequence = 0
        self._latest_frame_sequence = 0
        self.model_error = ""
        self.camera_error = ""
        self._stop = threading.Event()
        self._pair_requested_until = 0.0
        self._pair_status = "paired" if uploader.state_provider().get("api_token") else "not_paired"
        self._pair_lock = threading.RLock()
        self._pair_in_progress = False
        self._threads = []
        self._alert_active = False
        self._last_person_at = 0.0
        self._tracking_started_at = 0.0
        self._tracking_active = False
        self._tracking_seconds = 0.0
        self._last_alert_at = 0.0
        self._last_detections = []
        self._detection_history = deque(maxlen=3)
        self._frame_count = 0
        self._fps_started = time.monotonic()
        self._frames_lock = threading.RLock()
        self._current_segment = None
        self._current_marker = None
        self._recording_lock = threading.RLock()
        self.recording_enabled = bool(settings_store.get().get("recording_enabled", True))
        self._local_recording_attached = False

    def _publish_recording_state(self):
        """Report whether an MP4 writer is actually attached to the active encoder."""
        active = bool(
            self.picam2 is not None
            and self._local_recording_attached
            and self.recording_enabled
        )
        setter = getattr(self.bridge, "set_recording_enabled", None)
        if callable(setter):
            setter(active)

    def _pending_alarm_count(self):
        return sum(
            1 for item in self.alarm_dir.glob("*.json")
            if not item.with_suffix(".published").exists()
        )

    def _pending_video_count(self):
        return sum(
            1 for item in self.video_dir.glob("*.mp4")
            if not item.with_suffix(".open").exists()
            and not item.with_suffix(".uploaded").exists()
        )

    def _publish_bridge_metrics(self, now, pending_videos=None):
        """Publish slow-changing queue/worker diagnostics at most once per second."""
        if now - self._last_metrics_update_at < 1.0:
            return

        pool = self.inference_pool
        pool_stats = pool.snapshot() if pool is not None else {}
        model_ready = bool(
            pool is not None
            and pool_stats.get("ready_workers", 0) == pool.worker_count
        )
        if pending_videos is None:
            pending_videos = self._pending_video_count()

        self.bridge.set_metrics(
            self.video_fps,
            self.inference_fps,
            model_ready,
            self._pending_alarm_count(),
            pending_videos=pending_videos,
            rtsp_connected=bool(self.live_output and self.live_output.connected),
            camera_ready=self._is_camera_ready(),
        )
        self._last_metrics_update_at = now

    def _segment_path(self):
        return self.video_dir / f"{int(time.time() * 1000)}-{uuid.uuid4().hex[:8]}.mp4"

    def _mark_open(self, path):
        marker = path.with_suffix(".open")
        marker.touch()
        return marker

    def is_running(self):
        """Whether the selected camera backend currently owns an open device."""
        if self.picam2 is not None:
            return True
        return bool(self._opencv_capture is not None and self._opencv_capture.isOpened())

    def _reset_frame_state(self):
        self._stop.clear()
        with self._frames_lock:
            self.latest_jpeg = None
            self.latest_frame_at = 0.0
            self._latest_bgr = None
            self._latest_bgr_at = 0.0
            self._frame_sequence = 0
            self._latest_frame_sequence = 0
            self._last_processed_frame_sequence = 0
            self._inflight_frames.clear()
            self._inference_completed_at.clear()
            self.inference_fps = 0.0
            self.inference_latency_ms = 0.0
            self.inference_dropped_frames = 0
            self._last_detections = []
            self._last_detection_at = 0.0
        self._frame_count = 0
        self._fps_started = time.monotonic()
        self._last_metrics_update_at = 0.0
        self._tracking_started_at = 0.0
        self._tracking_active = False
        self._tracking_seconds = 0.0
        self._last_person_at = 0.0
        self.model_error = ""
        self.camera_error = ""

    def _start_inference_pool(self, values):
        """Start the bounded NCNN worker pool for either camera backend."""
        try:
            worker_count = int(values.get("inference_workers", 2))
            cpu_count = os.cpu_count() or 4
            ncnn_threads = max(1, min(3, (cpu_count - 1) // worker_count))
            pool = AdaptiveInferencePool(
                worker_count=worker_count,
                model_dir=os.getenv("MODEL_DIR", str(Path(__file__).parent / "models")),
                confidence=float(values["alert_confidence"]),
                ncnn_threads=ncnn_threads,
            )
            try:
                pool.start(timeout=75)
            except Exception:
                pool.stop()
                raise
            self.inference_pool = pool
            self._inference_pool_started_at = time.monotonic()
            self.bridge.model_loaded = True
            self.bridge.publish_log("info", "YOLOv5n NCNN adaptive worker pool loaded", {
                "model": "yolov5n-ncnn",
                "workers": worker_count,
                "ncnn_threads_per_worker": ncnn_threads,
            })
        except Exception as exc:
            self.inference_pool = None
            self.model_error = str(exc)
            self.bridge.model_loaded = False
            self.bridge.publish_log(
                "error", "YOLOv5n NCNN worker pool could not be loaded", {"error": self.model_error}
            )
            log.exception("YOLOv5n worker pool unavailable; camera preview remains active")

        inference_thread = threading.Thread(
            target=self._ai_loop, name="camera-yolov5n-inference", daemon=True
        )
        self._threads.append(inference_thread)
        inference_thread.start()

    @staticmethod
    def _probe_v4l2_modes(raw_source):
        """Read a USB camera's discrete formats/FPS when v4l2-ctl is installed."""
        if "://" in raw_source:
            return []
        if raw_source.isdigit():
            device = f"/dev/video{raw_source}"
        elif raw_source.startswith("/dev/video"):
            device = raw_source
        else:
            return []
        try:
            result = subprocess.run(
                ["v4l2-ctl", "--device", device, "--list-formats-ext"],
                capture_output=True,
                text=True,
                timeout=3,
                check=False,
            )
            if result.returncode != 0:
                return []
            return parse_v4l2_modes(result.stdout)
        except (OSError, subprocess.SubprocessError):
            log.debug("V4L2 mode discovery is unavailable for %s", device, exc_info=True)
            return []

    def _start_opencv(self, values):
        """Open a UVC/V4L2 webcam or network camera for preview and YOLO."""
        self._reset_frame_state()
        raw_source = str(values.get("camera_source", "0")).strip() or "0"
        source = int(raw_source) if raw_source.isdigit() else raw_source
        capture = (
            cv2.VideoCapture(source, cv2.CAP_V4L2)
            if isinstance(source, int)
            else cv2.VideoCapture(source)
        )
        if not capture.isOpened():
            capture.release()
            raise RuntimeError("OpenCV could not open the selected camera source")

        auto_fps = bool(values.get("video_fps_auto", True))
        requested_fps = float(values.get("video_fps", 30))
        sensor_modes = self._probe_v4l2_modes(raw_source)
        output_width = int(values["video_width"])
        output_height = int(values["video_height"])
        self.available_video_modes = sensor_modes
        compatible_modes = [
            mode for mode in sensor_modes
            if int(mode.get("size", (0, 0))[0]) >= output_width
            and int(mode.get("size", (0, 0))[1]) >= output_height
            and float(mode.get("fps", 0) or 0) > 0
        ]
        if sensor_modes and not compatible_modes:
            supported = ", ".join(
                sorted({f'{m["size"][0]}x{m["size"][1]}' for m in sensor_modes})
            )
            raise RuntimeError(
                f"Selected resolution {output_width}x{output_height} is not supported. "
                f"Available resolutions: {supported}"
            )
        selected_mode, selected_fps = select_sensor_mode(
            compatible_modes,
            output_width,
            output_height,
            requested_fps,
            auto=auto_fps,
        )
        self.video_fps_options = sorted({
            round(float(mode["fps"]), 2)
            for mode in compatible_modes
            if float(mode.get("fps", 0) or 0) > 0
        })
        selected_size = (output_width, output_height)
        try:
            capture.set(cv2.CAP_PROP_BUFFERSIZE, 1)
            if selected_mode and len(str(selected_mode.get("fourcc", ""))) == 4:
                capture.set(
                    cv2.CAP_PROP_FOURCC,
                    cv2.VideoWriter_fourcc(*str(selected_mode["fourcc"])),
                )
            capture.set(cv2.CAP_PROP_FRAME_WIDTH, int(selected_size[0]))
            capture.set(cv2.CAP_PROP_FRAME_HEIGHT, int(selected_size[1]))
            if selected_mode:
                capture.set(cv2.CAP_PROP_FPS, float(selected_fps))
            elif not auto_fps:
                capture.set(cv2.CAP_PROP_FPS, requested_fps)
        except Exception:
            log.debug("The selected OpenCV camera ignored one or more capture hints", exc_info=True)

        self._opencv_capture = capture
        self.picam2 = None
        self.encoder = None
        self.local_output = None
        self.live_output = None
        self._current_segment = None
        self._current_marker = None
        self.recording_enabled = False
        self._local_recording_attached = False
        self.bridge.set_recording_enabled(False)
        self.camera_backend = "opencv"
        self.camera_source = raw_source
        self.camera_model = (
            "OpenCV network source" if "://" in raw_source
            else f"OpenCV device {raw_source}"
        )
        reported_rate = float(capture.get(cv2.CAP_PROP_FPS) or 0)
        available_rates = [
            float(mode["fps"]) for mode in compatible_modes
            if float(mode.get("fps", 0) or 0) > 0
        ]
        self.camera_max_fps = max(available_rates, default=max(0.0, reported_rate))
        self.target_video_fps = (
            selected_fps if selected_mode
            else (reported_rate if auto_fps else requested_fps)
        )
        if self.camera_max_fps > 0:
            changes = {}
            max_setting = max(1, int(self.camera_max_fps))
            if not auto_fps and requested_fps > self.camera_max_fps:
                changes["video_fps"] = max_setting
                self.target_video_fps = float(max_setting)
            if int(values.get("inference_fps", 10)) > max_setting:
                changes["inference_fps"] = max_setting
            if changes:
                values, _ = self.settings_store.update(changes)
        if self.camera_max_fps <= 0 and self.target_video_fps > 0:
            self.camera_max_fps = self.target_video_fps
        self.stream_width = int(capture.get(cv2.CAP_PROP_FRAME_WIDTH) or selected_size[0])
        self.stream_height = int(capture.get(cv2.CAP_PROP_FRAME_HEIGHT) or selected_size[1])
        self.camera_resolution = f"{self.stream_width}x{self.stream_height}"
        self._threads = [
            threading.Thread(target=self._capture_loop, name="camera-capture-preview", daemon=True),
        ]
        for thread in self._threads:
            thread.start()
        self.bridge.publish_log("info", "Camera opened through OpenCV", {
            "source_type": "network" if "://" in raw_source else "local_device",
            "mode_discovery": "v4l2" if sensor_modes else "driver_default",
            "reported_fps": reported_rate,
            "selected_fps": self.target_video_fps,
            "media_output": "not_supported_by_opencv_backend",
        })
        self._start_inference_pool(values)

    def start(self):
        values = self.settings_store.get()
        if self.is_running():
            return

        self.camera_backend = str(values.get("camera_backend", "picamera2"))
        self.camera_source = str(values.get("camera_source", "0"))
        if self.camera_backend == "opencv":
            self._start_opencv(values)
            return

        from picamera2 import Picamera2
        from picamera2.encoders import H264Encoder
        from picamera2.outputs import PyavOutput

        self._reset_frame_state()
        for stale_marker in self.video_dir.glob("*.open"):
            stale_marker.with_suffix(".mp4").unlink(missing_ok=True)
            stale_marker.unlink(missing_ok=True)

        self.recording_enabled = bool(values.get("recording_enabled", True))
        try:
            camera_num = int(self.camera_source)
        except (TypeError, ValueError):
            camera_num = 0
            self.camera_source = "0"
        picam2 = Picamera2(camera_num=camera_num)
        marker = None
        local_output = None
        live_output = None
        encoder = None
        encoder_started = False
        sensor_configuration = None
        sensor_modes = []
        requested_fps = float(values.get("video_fps", 30))
        auto_fps = bool(values.get("video_fps_auto", True))
        try:
            sensor_modes = list(picam2.sensor_modes or [])
            output_width = int(values["video_width"])
            output_height = int(values["video_height"])
            self.available_video_modes = [
                mode for mode in sensor_modes
                if float(mode.get("fps", 0) or 0) > 0
            ]
            compatible_modes = [
                mode for mode in self.available_video_modes
                if int(mode.get("size", (0, 0))[0]) >= output_width
                and int(mode.get("size", (0, 0))[1]) >= output_height
            ]
            if self.available_video_modes and not compatible_modes:
                supported = ", ".join(
                    sorted({
                        f'{int(m["size"][0])}x{int(m["size"][1])}'
                        for m in self.available_video_modes
                    })
                )
                raise RuntimeError(
                    f"Selected resolution {output_width}x{output_height} is not supported. "
                    f"Available resolutions: {supported}"
                )
            selected_mode, selected_fps = select_sensor_mode(
                compatible_modes,
                output_width,
                output_height,
                requested_fps,
                auto=auto_fps,
            )
            self.video_fps_options = sorted({
                round(float(mode["fps"]), 2)
                for mode in compatible_modes
                if float(mode.get("fps", 0) or 0) > 0
            })
            if selected_mode:
                sensor_configuration = {
                    "output_size": tuple(selected_mode["size"]),
                    "bit_depth": int(selected_mode["bit_depth"]),
                }
            available_rates = [
                float(mode.get("fps", 0) or 0)
                for mode in compatible_modes
            ]
            self.camera_max_fps = max(available_rates, default=selected_fps)
            self.target_video_fps = selected_fps
            # Output resolution is explicit and never silently reduced to
            # satisfy FPS; only a sensor mode compatible with this size can
            # be selected above.
            self.stream_width, self.stream_height = output_width, output_height
            if self.camera_max_fps > 0:
                max_setting = max(1, int(self.camera_max_fps))
                changes = {}
                if not auto_fps and requested_fps > self.camera_max_fps:
                    changes["video_fps"] = max_setting
                    self.target_video_fps = float(max_setting)
                if int(values.get("inference_fps", 10)) > max_setting:
                    changes["inference_fps"] = max_setting
                if changes:
                    values, _ = self.settings_store.update(changes)
            effective_fps = max(1.0, float(self.target_video_fps or requested_fps))
            frame_duration = max(1, int(round(1_000_000 / effective_fps)))
            config_options = {
                "main": {"size": (self.stream_width, self.stream_height), "format": "YUV420"},
                "lores": {"size": (640, 360), "format": "YUV420"},
                "controls": {"FrameDurationLimits": (frame_duration, frame_duration)},
                "buffer_count": 8,
            }
            if sensor_configuration:
                config_options["sensor"] = sensor_configuration
            config = picam2.create_video_configuration(**config_options)
            picam2.configure(config)

            try:
                camera_info = Picamera2.global_camera_info()
                if 0 <= camera_num < len(camera_info):
                    self.camera_model = str(camera_info[camera_num].get("Model") or f"Picamera2 camera {camera_num}")
                else:
                    self.camera_model = f"Picamera2 camera {camera_num}"
            except Exception:
                self.camera_model = f"Picamera2 camera {camera_num}"
            self.camera_resolution = f"{self.stream_width}x{self.stream_height}"

            segment = None
            first_segment = None
            if self.recording_enabled:
                segment = self._segment_path()
                marker = self._mark_open(segment)
                first_segment = PyavOutput(str(segment), format="mp4")
                first_segment.error_callback = lambda error: log.error("Local MP4 segment failed: %s", error)
            local_output = _switchable_output_class()(first_segment)
            live_output = ReconnectingRtspOutput(self.device_id, self.uploader.state_provider)
            encoder_fps = max(1, int(round(effective_fps)))
            encoder = H264Encoder(
                bitrate=values["video_bitrate"],
                repeat=True,
                iperiod=encoder_fps,
                framerate=encoder_fps,
            )
            if hasattr(encoder, "use_hw"):
                encoder.use_hw = True
                if not encoder.use_hw:
                    raise RuntimeError("Picamera2 did not enable the Pi 4 VC4 H.264 encoder")
            encoder.output = [local_output, live_output.output]
            picam2.start_encoder(encoder)
            encoder_started = True
            picam2.start()
        except Exception:
            if marker:
                marker.unlink(missing_ok=True)
            try:
                if encoder_started:
                    picam2.stop()
                    picam2.stop_encoder(encoder)
            except Exception:
                log.exception("Could not stop partially-started Picamera2 encoder")
            try:
                picam2.close()
            except Exception:
                log.exception("Could not close partially-started Picamera2 instance")
            if live_output:
                live_output.stop()
            self.picam2 = None
            self.encoder = None
            self.local_output = None
            self.live_output = None
            self._current_segment = None
            self._current_marker = None
            raise

        with self._recording_lock:
            self.picam2 = picam2
            self.encoder = encoder
            self.local_output = local_output
            self.live_output = live_output
            self.camera_backend = "picamera2"
            self._opencv_capture = None
            self._current_segment = segment
            self._current_marker = marker
            self._local_recording_attached = bool(segment)
            self._publish_recording_state()
            desired_recording = bool(
                self.settings_store.get().get("recording_enabled", self.recording_enabled)
            )
            self.recording_enabled = desired_recording
            try:
                self.set_recording_enabled(desired_recording)
            except Exception:
                log.exception("Could not reconcile local recording state during camera startup")

        self.live_output.start()
        self._threads = [
            threading.Thread(target=self._capture_loop, name="camera-capture-preview", daemon=True),
            threading.Thread(target=self._segment_loop, name="local-video-segmenter", daemon=True),
        ]
        for thread in self._threads:
            thread.start()
        self._start_inference_pool(values)

    def set_recording_enabled(self, enabled: bool) -> dict:
        """Pause/resume only the local MP4 spool; camera, preview and RTSPS stay live."""
        if self.camera_backend != "picamera2":
            raise RuntimeError("ضبط و ارسال RTSPS برای دوربین OpenCV هنوز پشتیبانی نمی‌شود؛ از Picamera2 استفاده کن.")
        from picamera2.outputs import PyavOutput

        enabled = bool(enabled)
        with self._recording_lock:
            if self.picam2 is None or self.local_output is None:
                self.recording_enabled = enabled
                self.settings_store.update({"recording_enabled": enabled})
                self._publish_recording_state()
                return {"recording_enabled": enabled}
            if enabled == self._local_recording_attached:
                self.recording_enabled = enabled
                self.settings_store.update({"recording_enabled": enabled})
                self._publish_recording_state()
                return {"recording_enabled": enabled}

            if enabled:
                next_path = self._segment_path()
                next_marker = self._mark_open(next_path)
                output = PyavOutput(str(next_path), format="mp4")
                output.error_callback = lambda error: log.error("Local MP4 segment failed: %s", error)
                try:
                    self.local_output.switch_output(output)
                except Exception:
                    next_marker.unlink(missing_ok=True)
                    self.recording_enabled = self._local_recording_attached
                    self.settings_store.update({
                        "recording_enabled": self._local_recording_attached
                    })
                    raise
                self._current_segment = next_path
                self._current_marker = next_marker
                self._local_recording_attached = True
                self.recording_enabled = True
                self.settings_store.update({"recording_enabled": True})
                self._publish_recording_state()
                log.info("Local video recording resumed; RTSPS live output was not restarted")
            else:
                old_marker = self._current_marker
                old_segment = self._current_segment
                # Pause the video uploader before waiting for the encoder's next
                # keyframe. If switching fails, restore the prior upload state.
                self.settings_store.update({"recording_enabled": False})
                try:
                    # Detach only the local MP4 writer. Camera, YOLO and RTSPS
                    # remain active and receive every subsequent frame.
                    self.local_output.switch_output(None)
                except Exception:
                    self.recording_enabled = self._local_recording_attached
                    self.settings_store.update({
                        "recording_enabled": self._local_recording_attached
                    })
                    raise

                self._current_segment = None
                self._current_marker = None
                self._local_recording_attached = False
                self.recording_enabled = False

                # The uploader uses this same lock. Delete the open partial file
                # before removing its .open marker, so it can never race and
                # upload a segment that the operator asked us to discard. If
                # deletion fails, leave the marker in place to keep it unqueued.
                try:
                    with self.uploader._queue_lock:
                        if old_segment:
                            old_segment.unlink(missing_ok=True)
                        if old_marker:
                            old_marker.unlink(missing_ok=True)
                except OSError:
                    log.exception(
                        "Could not remove the partial recording; its open marker is retained if present"
                    )

                self._publish_recording_state()
                log.info("Local recording paused; partial segment discarded; video queue delivery paused")

        return {"recording_enabled": self.recording_enabled}

    def _segment_loop(self):
        from picamera2.outputs import PyavOutput
        while not self._stop.wait(int(self.settings_store.get()["segment_seconds"])):
            if not self.recording_enabled:
                continue
            try:
                with self._recording_lock:
                    if not self.recording_enabled or self.local_output is None:
                        continue
                    next_path = self._segment_path()
                    next_marker = self._mark_open(next_path)
                    output = PyavOutput(str(next_path), format="mp4")
                    output.error_callback = lambda error: log.error("MP4 spool output error: %s", error)
                    old_marker = self._current_marker
                    try:
                        self.local_output.switch_output(output)
                    except Exception:
                        next_marker.unlink(missing_ok=True)
                        raise
                    # Old output has now closed and is safe for the HTTP uploader to read.
                    if old_marker:
                        old_marker.unlink(missing_ok=True)
                    self._current_segment, self._current_marker = next_path, next_marker
            except Exception:
                log.exception("Could not rotate local video segment")

    @staticmethod
    def _decode_pairing_qr(detector, frame):
        """Decode pairing QR with ZBar; use OpenCV only when ZBar is unavailable."""
        if frame is None:
            return ""

        gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY) if frame.ndim == 3 else frame
        enlarged = cv2.resize(
            gray, None, fx=1.75, fy=1.75, interpolation=cv2.INTER_CUBIC
        )
        adaptive = cv2.adaptiveThreshold(
            enlarged, 255, cv2.ADAPTIVE_THRESH_GAUSSIAN_C,
            cv2.THRESH_BINARY, 31, 7,
        )

        def extract_code(value):
            if isinstance(value, bytes):
                value = value.decode("utf-8", errors="strict")
            value = str(value).strip()
            prefix = "iot-alarm-pair:"
            if not value.startswith(prefix):
                return ""
            code = value[len(prefix):].strip()
            return code if re.fullmatch(r"[A-Za-z0-9_-]{32,128}", code) else ""

        if _zbar_decode is not None:
            # ZBar is explicitly installed on the Pi; unlike this system's
            # OpenCV build, it does not depend on the optional QUIRC module.
            for candidate in (gray, enlarged, adaptive):
                try:
                    for symbol in _zbar_decode(candidate):
                        code = extract_code(symbol.data)
                        if code:
                            return code
                except Exception:
                    log.debug("ZBar QR decode attempt failed", exc_info=True)
            return ""

        # Fallback for systems where libzbar/pyzbar could not be installed.
        for candidate in (gray, enlarged, adaptive):
            try:
                decoded, _, _ = detector.detectAndDecode(candidate)
                code = extract_code(decoded) if decoded else ""
                if code:
                    return code
            except (cv2.error, UnicodeDecodeError):
                log.debug("OpenCV QR decode attempt failed", exc_info=True)

            multi_decode = getattr(detector, "detectAndDecodeMulti", None)
            if multi_decode is not None:
                try:
                    result = multi_decode(candidate)
                    if result and result[0]:
                        for decoded in result[1]:
                            code = extract_code(decoded)
                            if code:
                                return code
                except (cv2.error, UnicodeDecodeError):
                    log.debug("OpenCV multi-QR decode attempt failed", exc_info=True)
        return ""

    @staticmethod
    def _i420_to_bgr(frame, width, height, name):
        if frame is None:
            raise RuntimeError(f"Camera returned an empty {name} frame")
        if frame.ndim == 3 and frame.shape[-1] == 1:
            frame = frame.reshape((height * 3 // 2, width))
        expected = (height * 3 // 2, width)
        if frame.shape != expected:
            if frame.size != width * height * 3 // 2:
                raise RuntimeError(
                    f"Unexpected {name} YUV420 shape: {frame.shape}; expected {expected}"
                )
            frame = frame.reshape(expected)
        return cv2.cvtColor(frame, cv2.COLOR_YUV2BGR_I420)

    def _capture_loop(self):
        if self.camera_backend == "opencv":
            self._capture_opencv_loop()
            return

        last_qr_scan = 0.0
        qr_detector = cv2.QRCodeDetector()
        if _zbar_decode is None:
            log.warning("pyzbar/libzbar is unavailable; QR scanning will fall back to OpenCV")
        # A full-resolution BGR conversion plus JPEG encoding on every source
        # frame starves the capture loop on a Pi 4. Keep the high-res preview
        # useful, but refresh it independently at 15 FPS.
        preview_interval = 1.0 / 15.0
        values = self.settings_store.get()
        main_width, main_height = self.stream_width, self.stream_height
        while not self._stop.is_set():
            try:
                request = self.picam2.capture_request()
                try:
                    lores_yuv = request.make_array("lores")
                    now = time.monotonic()
                    update_preview = now - self._last_preview_at >= preview_interval
                    main_yuv = request.make_array("main") if update_preview else None
                finally:
                    request.release()
                frame = self._i420_to_bgr(lores_yuv, 640, 360, "lores")
                self.camera_error = ""

                if self._pair_requested_until and now >= self._pair_requested_until:
                    self._pair_requested_until = 0.0
                    self._pair_status = "expired"
                if self._pair_requested_until > now and now - last_qr_scan >= 0.35:
                    last_qr_scan = now
                    pairing_code = self._decode_pairing_qr(qr_detector, frame)
                    if pairing_code:
                        self._complete_pairing(pairing_code)

                with self._frames_lock:
                    self._latest_bgr = frame.copy()
                    self._frame_sequence += 1
                    self._latest_frame_sequence = self._frame_sequence
                    self._latest_bgr_at = now
                    detections = list(self._last_detections)
                    detection_age = now - self._last_detection_at if self._last_detection_at else 999.0

                if update_preview:
                    # The main stream is only read/converted when a new MJPEG
                    # preview frame is due; H.264 recording/live publishing is
                    # driven separately by the camera encoder at target FPS.
                    preview = self._i420_to_bgr(main_yuv, main_width, main_height, "main")
                    if detections and detection_age < 1.0:
                        sx, sy = preview.shape[1] / 640.0, preview.shape[0] / 360.0
                        scaled = []
                        for item in detections:
                            mapped = dict(item)
                            x1, y1, x2, y2 = item["bbox"]
                            mapped["bbox"] = [
                                int(round(x1 * sx)), int(round(y1 * sy)),
                                int(round(x2 * sx)), int(round(y2 * sy)),
                            ]
                            scaled.append(mapped)
                        self._draw_boxes(preview, scaled, self._tracking_seconds)

                    ok, encoded = cv2.imencode(
                        ".jpg", preview, [cv2.IMWRITE_JPEG_QUALITY, 84]
                    )
                    with self._frames_lock:
                        self.latest_jpeg = encoded.tobytes() if ok else None
                        self.latest_frame_at = time.time()
                    self._last_preview_at = now

                self.camera_resolution = f"{main_width}x{main_height}"
                self._frame_count += 1
                if now - self._fps_started >= 2.0:
                    elapsed = now - self._fps_started
                    self.video_fps = self._frame_count / elapsed
                    self._frame_count = 0
                    self._fps_started = now

                self._publish_bridge_metrics(now)
            except Exception as exc:
                self.camera_error = str(exc)
                log.exception("Picamera2 capture/preview error")
                self.bridge.publish_log("error", "Camera capture or preview failed", {"error": str(exc)})
                self._stop.wait(0.5)

    def _capture_opencv_loop(self):
        last_qr_scan = 0.0
        qr_detector = cv2.QRCodeDetector()
        preview_interval = 1.0 / 15.0
        while not self._stop.is_set():
            try:
                capture = self._opencv_capture
                if capture is None or not capture.isOpened():
                    raise RuntimeError("OpenCV camera source is no longer open")
                ok, preview_source = capture.read()
                if not ok or preview_source is None:
                    self.camera_error = "OpenCV did not return a frame from the selected source"
                    self._stop.wait(0.1)
                    continue
                height, width = preview_source.shape[:2]
                if width <= 0 or height <= 0:
                    continue
                frame = (
                    preview_source if (width, height) == (640, 360)
                    else cv2.resize(preview_source, (640, 360), interpolation=cv2.INTER_AREA)
                )
                now = time.monotonic()
                self.camera_error = ""
                if self._pair_requested_until and now >= self._pair_requested_until:
                    self._pair_requested_until = 0.0
                    self._pair_status = "expired"
                if self._pair_requested_until > now and now - last_qr_scan >= 0.35:
                    last_qr_scan = now
                    pairing_code = self._decode_pairing_qr(qr_detector, frame)
                    if pairing_code:
                        self._complete_pairing(pairing_code)
                with self._frames_lock:
                    self._latest_bgr = frame.copy()
                    self._frame_sequence += 1
                    self._latest_frame_sequence = self._frame_sequence
                    self._latest_bgr_at = now
                    detections = list(self._last_detections)
                    detection_age = now - self._last_detection_at if self._last_detection_at else 999.0
                update_preview = now - self._last_preview_at >= preview_interval
                if update_preview:
                    preview = preview_source.copy()
                    if detections and detection_age < 1.0:
                        sx, sy = width / 640.0, height / 360.0
                        scaled = []
                        for item in detections:
                            mapped = dict(item)
                            x1, y1, x2, y2 = item["bbox"]
                            mapped["bbox"] = [
                                int(round(x1 * sx)), int(round(y1 * sy)),
                                int(round(x2 * sx)), int(round(y2 * sy)),
                            ]
                            scaled.append(mapped)
                        self._draw_boxes(preview, scaled, self._tracking_seconds)

                    ok, encoded = cv2.imencode(
                        ".jpg", preview, [cv2.IMWRITE_JPEG_QUALITY, 84]
                    )
                    with self._frames_lock:
                        self.latest_jpeg = encoded.tobytes() if ok else None
                        self.latest_frame_at = time.time()
                    self._last_preview_at = now
                self.camera_resolution = f"{width}x{height}"
                self._frame_count += 1
                if now - self._fps_started >= 2.0:
                    elapsed = now - self._fps_started
                    self.video_fps = self._frame_count / elapsed
                    self._frame_count = 0
                    self._fps_started = now
                self._publish_bridge_metrics(now, pending_videos=0)
            except Exception as exc:
                self.camera_error = str(exc)
                log.warning("OpenCV camera capture error: %s", exc)
                self._stop.wait(0.25)

    def _ai_loop(self):
        """Dispatch fresh frames to idle workers and drop work rather than queue latency."""
        next_dispatch_at = 0.0
        last_seen_sequence = 0

        while not self._stop.is_set():
            pool = self.inference_pool
            if pool is None:
                self._stop.wait(0.25)
                continue

            try:
                # Harvest completions before dispatching the next frame.
                for result in pool.collect(timeout=0.01):
                    sequence = int(result.get("frame_sequence", 0))
                    frame = self._inflight_frames.pop(sequence, None)
                    if frame is None:
                        continue

                    error = str(result.get("error", ""))
                    if error:
                        self.model_error = error
                        log.warning(
                            "YOLO worker %s failed frame %s: %s",
                            result.get("worker_id"), sequence, error,
                        )
                        continue

                    now = time.monotonic()
                    self.model_error = ""
                    self._inference_completed_at.append(now)
                    while (
                        self._inference_completed_at
                        and now - self._inference_completed_at[0] > 5.0
                    ):
                        self._inference_completed_at.popleft()
                    window = min(5.0, max(0.5, now - self._inference_pool_started_at))
                    self.inference_fps = len(self._inference_completed_at) / window
                    self.inference_latency_ms = float(result.get("elapsed_seconds", 0.0)) * 1000.0

                    # Workers can finish out of order. Older results never replace
                    # detections already produced for a newer source frame.
                    if sequence <= self._last_processed_frame_sequence:
                        continue
                    self._last_processed_frame_sequence = sequence
                    detections = result.get("detections", [])
                    if not isinstance(detections, list):
                        detections = []

                    with self._frames_lock:
                        self._last_detections = detections
                        self._last_detection_at = now

                    self._detection_history.append(bool(detections))
                    self._process_detections(frame, detections, now)

                # Recalculate the rolling completion rate even when no new
                # result arrives, so a stalled worker pool visibly decays to 0 FPS.
                measurement_now = time.monotonic()
                while (
                    self._inference_completed_at
                    and measurement_now - self._inference_completed_at[0] > 5.0
                ):
                    self._inference_completed_at.popleft()
                measurement_window = min(
                    5.0, max(0.5, measurement_now - self._inference_pool_started_at)
                )
                self.inference_fps = len(self._inference_completed_at) / measurement_window

                values = self.settings_store.get()
                pool_stats = pool.snapshot()
                estimated_capacity = float(pool_stats.get("estimated_capacity_fps", 0.0))
                configured_cap = max(1.0, float(values["inference_fps"]))
                # Recompute the dispatch interval continuously from the EWMA
                # service time of every worker. This starts conservatively,
                # ramps up as real throughput is measured, and backs off after
                # thermal throttling or CPU contention.
                if estimated_capacity <= 0:
                    self._stop.wait(0.05)
                    continue
                camera_cap = self.camera_max_fps if self.camera_max_fps > 0 else configured_cap
                effective_rate = min(configured_cap, estimated_capacity, camera_cap)
                interval = 1.0 / effective_rate
                now = time.monotonic()
                if now >= next_dispatch_at:
                    with self._frames_lock:
                        frame = self._latest_bgr.copy() if self._latest_bgr is not None else None
                        frame_sequence = self._latest_frame_sequence
                        frame_timestamp = self._latest_bgr_at

                    if frame is not None and frame_sequence > last_seen_sequence:
                        last_seen_sequence = frame_sequence
                        age = now - frame_timestamp if frame_timestamp else 999.0
                        if age <= 1.0:
                            worker_id = pool.submit(
                                frame,
                                frame_sequence,
                                frame_timestamp,
                                float(values["alert_confidence"]),
                            )
                            if worker_id is None:
                                # No FIFO backlog: the next scheduler tick will use
                                # a newer camera frame instead of queueing this one.
                                self.inference_dropped_frames += 1
                            else:
                                # Retain the exact source image only while its job
                                # is in flight, for a matching alarm snapshot.
                                self._inflight_frames[frame_sequence] = frame
                        else:
                            self.inference_dropped_frames += 1
                    next_dispatch_at = now + interval

                self._stop.wait(min(0.01, max(0.001, next_dispatch_at - time.monotonic())))
            except Exception as exc:
                self.model_error = str(exc)
                log.exception("Adaptive YOLO inference scheduler failed")
                self.bridge.publish_log(
                    "error", "Adaptive YOLO inference scheduler failed", {"error": str(exc)}
                )
                self._stop.wait(0.5)

    def _process_detections(self, frame, detections, now):
        confirmed = sum(self._detection_history) >= 2
        if detections:
            if not self._tracking_active:
                self._tracking_started_at = now
                self._tracking_active = True
            self._last_person_at = now
            self._tracking_seconds = max(0.0, now - self._tracking_started_at)
        elif self._tracking_active:
            if now - self._last_person_at >= self.settings_store.get()["alert_clear_seconds"]:
                self._tracking_seconds = max(
                    0.0, self._last_person_at - self._tracking_started_at
                )
                self._tracking_active = False
                self._tracking_started_at = 0.0
        self.bridge.set_tracking(self._tracking_active, self._tracking_seconds)

        if confirmed and detections:
            values = self.settings_store.get()
            if not self._alert_active and now - self._last_alert_at >= values["alert_cooldown_seconds"]:
                self._create_alarm(frame, detections)
                self._alert_active = True
                self._last_alert_at = now
        elif not detections and self._alert_active:
            if now - self._last_person_at >= self.settings_store.get()["alert_clear_seconds"]:
                self._alert_active = False

    @staticmethod
    def _draw_boxes(frame, detections, tracking_seconds=0.0):
        for item in detections:
            x1, y1, x2, y2 = item["bbox"]
            cv2.rectangle(frame, (x1, y1), (x2, y2), (40, 220, 120), 2)
            label = f"PERSON {item['confidence']:.2f}"
            if tracking_seconds > 0:
                label += f" · {tracking_seconds:.1f}s"
            cv2.putText(frame, label,
                        (x1, max(18, y1 - 8)), cv2.FONT_HERSHEY_SIMPLEX,
                        0.48, (40, 220, 120), 1, cv2.LINE_AA)

    def _create_alarm(self, frame, detections):
        alert_id = uuid.uuid4().hex
        image_name = f"{alert_id}.jpg"
        image_path = self.alarm_dir / image_name
        alarm_frame = frame.copy()
        self._draw_boxes(alarm_frame, detections, self._tracking_seconds)
        ok, encoded = cv2.imencode(".jpg", alarm_frame, [cv2.IMWRITE_JPEG_QUALITY, 90])
        if not ok:
            return
        atomic_write_bytes(image_path, encoded.tobytes())
        height, width = frame.shape[:2]
        boxes = []
        for item in detections:
            x1, y1, x2, y2 = item["bbox"]
            boxes.append({
                "x1": round(x1 / width, 6), "y1": round(y1 / height, 6),
                "x2": round(x2 / width, 6), "y2": round(y2 / height, 6),
                "confidence": item["confidence"],
            })
        payload = {
            "alert_id": alert_id, "device_id": self.device_id, "event": "person_detected",
            "detected_at": datetime.now(timezone.utc).isoformat(),
            "confidence": max(item["confidence"] for item in detections),
            "bbox": boxes, "image": image_name, "model": "yolov5n-ncnn",
        }
        meta = self.alarm_dir / f"{alert_id}.json"
        atomic_write_json(meta, payload)
        self.bridge.publish_log("warning", "Person detected by YOLOv5n", {
            "alert_id": alert_id, "confidence": payload["confidence"], "bbox": boxes,
        })

    def request_pairing(self):
        with self._pair_lock:
            if self._pair_in_progress:
                return {"status": "submitting", "expires_in": 0}
            self._pair_requested_until = time.monotonic() + 120
            self._pair_status = "scanning"
        log.info("Pairing QR scan started; waiting up to 120 seconds for a supported code")
        return {"status": "scanning", "expires_in": 120}

    def submit_manual_pairing_code(self, value):
        """Accept either the raw one-time token or the complete QR payload."""
        value = str(value or "").strip()
        prefix = "iot-alarm-pair:"
        if value.startswith(prefix):
            value = value[len(prefix):].strip()

        # The backend creates URL-safe 256-bit tokens. Reject arbitrary text
        # before making a network request and never include the secret in logs.
        import re
        if not re.fullmatch(r"[A-Za-z0-9_-]{32,128}", value):
            raise ValueError("کد اتصال معتبر نیست؛ متن زیر QR را کامل کپی کن.")

        if not self._complete_pairing(value):
            raise RuntimeError("یک درخواست Pairing دیگر در حال انجام است.")
        return {"status": "submitting"}

    def _complete_pairing(self, pairing_code):
        with self._pair_lock:
            if self._pair_in_progress:
                return False
            self._pair_in_progress = True
            self._pair_requested_until = 0
            self._pair_status = "submitting"
        try:
            thread = threading.Thread(
                target=self._pair_request,
                args=(pairing_code,),
                name="device-pairing",
                daemon=True,
            )
            thread.start()
            return True
        except Exception:
            with self._pair_lock:
                self._pair_in_progress = False
                self._pair_status = "failed_to_start"
            raise

    def _pair_request(self, pairing_code):
        api_base = os.getenv("API_BASE_URL", "https://server.echonode.website").rstrip("/")
        try:
            response = requests.post(
                f"{api_base}/api/device/pair/",
                json={
                    "pairing_code": pairing_code,
                    "device_id": self.device_id,
                    "name": os.getenv("DEVICE_NAME", f"Camera {self.device_id[:8]}"),
                },
                timeout=(10, 30),
            )
            response.raise_for_status()
            credentials = response.json()
            if not isinstance(credentials, dict):
                raise ValueError("Pairing response must be a JSON object.")
            if (
                credentials.get("device_id") != self.device_id
                or not isinstance(credentials.get("api_token"), str)
                or not credentials.get("api_token")
                or not re.fullmatch(
                    r"[a-fA-F0-9]{64}",
                    str(credentials.get("mqtt_signing_key", "")),
                )
            ):
                raise ValueError("Pairing response did not contain valid credentials for this device.")
            target = Path(os.getenv("DEVICE_CREDENTIALS_PATH", "/var/lib/mqttiot/device.json"))
            atomic_write_json(target, credentials, indent=2)
            self.uploader.state_provider = lambda: credentials
            self.uploader.credentials_revoked = False
            self.bridge.set_signing_key(credentials.get("mqtt_signing_key", ""))
            if self.live_output:
                self.live_output.credentials_changed()
            self._pair_status = "paired"
            self.bridge.publish_log("info", "Device pairing completed", {"device_id": self.device_id})
            log.info("Device %s paired successfully", self.device_id)
        except Exception as exc:
            self._pair_status = f"failed: {exc}"
            self.bridge.publish_log("error", "Device pairing failed", {"error": str(exc)})
            log.exception("QR pairing failed")
        finally:
            with self._pair_lock:
                self._pair_in_progress = False

    def _is_camera_ready(self):
        with self._frames_lock:
            return bool(
                self.picam2 is not None
                and self.latest_jpeg is not None
                and self.latest_frame_at
                and time.time() - self.latest_frame_at < 2.5
            )

    def snapshot(self):
        preview_ready = self._is_camera_ready()
        values = self.settings_store.get()
        paired = bool((self.uploader.state_provider() or {}).get("api_token"))
        if not paired and self._pair_status == "paired":
            self._pair_status = "not_paired"
        return {
            "device_id": self.device_id,
            "camera_ready": preview_ready,
            "preview_ready": preview_ready,
            "camera_backend": self.camera_backend,
            "camera_source": self.camera_source,
            "camera_model": self.camera_model,
            "camera_resolution": self.camera_resolution,
            "camera_max_fps": round(self.camera_max_fps, 2),
            "video_fps_options": self.video_fps_options,
            "video_modes": [
                {
                    "resolution": f"{int(mode.get('size', (0, 0))[0])}x{int(mode.get('size', (0, 0))[1])}",
                    "fps": round(float(mode.get("fps", 0) or 0), 2),
                    "fourcc": str(mode.get("fourcc", "")),
                }
                for mode in self.available_video_modes
                if mode.get("size") and float(mode.get("fps", 0) or 0) > 0
            ],
            "video_fps_auto": bool(values.get("video_fps_auto", True)),
            "target_video_fps": round(self.target_video_fps or values["video_fps"], 2),
            "recording_supported": self.camera_backend == "picamera2",
            "live_publish_supported": self.camera_backend == "picamera2",
            "video_fps": round(self.video_fps, 2),
            "inference_fps": round(self.inference_fps, 2),
            "inference_fps_limit": round(self.camera_max_fps, 2),
            "inference_latency_ms": round(self.inference_latency_ms, 1),
            "inference_dropped_frames": self.inference_dropped_frames,
            "inference_pool": self.inference_pool.snapshot() if self.inference_pool else {
                "workers": int(values.get("inference_workers", 2)),
                "ready_workers": 0,
                "busy_workers": 0,
                "average_latency_ms": 0,
                "worker_latency_ms": [],
                "last_error": self.model_error,
            },
            "model_loaded": bool(
                self.inference_pool
                and self.inference_pool.started
                and self.inference_pool.snapshot()["ready_workers"] == self.inference_pool.worker_count
            ),
            "recording_enabled": self.recording_enabled,
            "model_error": (
                self.model_error
                or (self.inference_pool.last_error if self.inference_pool else "")
            ),
            "camera_error": self.camera_error,
            "rtsp_connected": bool(self.live_output and self.live_output.connected),
            "rtsp_error": self.live_output.error if self.live_output else "",
            "pairing_status": self._pair_status if paired else "not_paired",
            "tracking_active": self._tracking_active,
            "tracking_seconds": round(self._tracking_seconds, 1),
            "local_video_retention_minutes": values["local_video_retention_minutes"],
            "local_alarm_retention_days": values["local_alarm_retention_days"],
            "local_log_retention_days": values["local_log_retention_days"],
            "pending_video_segments": self._pending_video_count(),
            "pending_alarms": self._pending_alarm_count(),
        }

    def get_preview(self):
        with self._frames_lock:
            if not self.latest_frame_at or time.time() - self.latest_frame_at >= 2.5:
                return None
            return self.latest_jpeg

    def stop(self):
        self._stop.set()
        for thread in self._threads:
            thread.join(timeout=5)
        self._threads = []
        if self.inference_pool:
            try:
                self.inference_pool.stop()
            except Exception:
                log.exception("Could not stop YOLO inference workers cleanly")
            self.inference_pool = None
        self._inflight_frames.clear()
        if self.live_output:
            self.live_output.stop()
        if self._opencv_capture is not None:
            try:
                self._opencv_capture.release()
            except Exception:
                log.exception("Could not release OpenCV camera capture")
            self._opencv_capture = None
        stopped_cleanly = True
        try:
            if self.picam2:
                self.picam2.stop()
                self.picam2.stop_encoder(self.encoder)
                self.picam2.close()
        except Exception:
            stopped_cleanly = False
            log.exception("Could not stop Picamera2 cleanly")
        if stopped_cleanly and self._current_marker:
            # Removing the marker makes a complete, finalized MP4 eligible for
            # retry upload. If close failed, keep it so startup discards it.
            self._current_marker.unlink(missing_ok=True)

        self.picam2 = None
        self.encoder = None
        self.local_output = None
        self.live_output = None
        if self.camera_backend == "opencv":
            self.recording_enabled = False
        self.bridge.set_tracking(False, self._tracking_seconds)
        self.bridge.model_loaded = False
        self.inference_fps = 0.0
        self.inference_latency_ms = 0.0
        self._current_segment = None
        self._current_marker = None
        self._local_recording_attached = False
        self._publish_recording_state()
        with self._frames_lock:
            self.latest_jpeg = None
            self.latest_frame_at = 0.0
            self._latest_bgr = None
            self._last_detections = []
            self._last_detection_at = 0.0

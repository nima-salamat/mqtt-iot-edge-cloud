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
        self.model_error = ""
        self.camera_error = ""

    def _start_inference_pool(self, values):
        """Start the same bounded YOLO pool for either camera capture backend."""
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
        """Return discrete USB camera formats when v4l2-ctl is installed."""
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
        """Open a UVC/V4L2 webcam or a network video source through OpenCV.

        V4L2 mode tables are used when available; actual delivered FPS is
        measured in the capture loop rather than trusting driver metadata.
        Hardware H.264 recording and RTSPS publishing remain Picamera2-only.
        """
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
        selected_mode, selected_fps = select_sensor_mode(
            sensor_modes,
            int(values["video_width"]),
            int(values["video_height"]),
            requested_fps,
            auto=auto_fps,
        )
        try:
            capture.set(cv2.CAP_PROP_BUFFERSIZE, 1)
            selected_fourcc = str(selected_mode.get("fourcc", "")) if selected_mode else ""
            if len(selected_fourcc) == 4:
                capture.set(
                    cv2.CAP_PROP_FOURCC,
                    cv2.VideoWriter_fourcc(*selected_fourcc),
                )
            selected_size = (
                tuple(selected_mode["size"])
                if selected_mode
                else (int(values["video_width"]), int(values["video_height"]))
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
        self.camera_backend = "opencv"
        self.camera_source = raw_source
        self.camera_model = ("OpenCV network source" if "://" in raw_source else f"OpenCV device {raw_source}")
        reported_rate = float(capture.get(cv2.CAP_PROP_FPS) or 0)
        compatible_rates = [
            float(mode["fps"])
            for mode in sensor_modes
            if int(mode["size"][0]) >= int(values["video_width"])
            and int(mode["size"][1]) >= int(values["video_height"])
        ]
        self.camera_max_fps = max(compatible_rates, default=max(0.0, reported_rate))
        self.target_video_fps = (
            selected_fps if selected_mode
            else (reported_rate if auto_fps else requested_fps)
        )
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
        # Incomplete MP4s from a power loss have no final container index.
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
            try:
                sensor_modes = list(picam2.sensor_modes or [])
                selected_mode, selected_fps = select_sensor_mode(
                    sensor_modes,
                    int(values["video_width"]),
                    int(values["video_height"]),
                    requested_fps,
                    auto=auto_fps,
                )
                if selected_mode:
                    sensor_configuration = {
                        "output_size": tuple(selected_mode["size"]),
                        "bit_depth": int(selected_mode["bit_depth"]),
                    }
                compatible_rates = []
                for mode in sensor_modes:
                    try:
                        mode_size = mode.get("size", ())
                        if (
                            len(mode_size) >= 2
                            and int(mode_size[0]) >= int(values["video_width"])
                            and int(mode_size[1]) >= int(values["video_height"])
                        ):
                            compatible_rates.append(float(mode.get("fps", 0) or 0))
                    except (TypeError, ValueError):
                        continue
                self.camera_max_fps = max(compatible_rates, default=selected_fps)
                self.target_video_fps = selected_fps
            except Exception:
                log.exception("Could not inspect Picamera2 sensor modes; using configured FPS")
                self.target_video_fps = requested_fps

            effective_fps = max(1.0, float(self.target_video_fps or requested_fps))
            frame_duration = max(1, int(round(1_000_000 / effective_fps)))
            config_options = {
                "main": {"size": (values["video_width"], values["video_height"]), "format": "YUV420"},
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
            self.camera_resolution = f"{values['video_width']}x{values['video_height']}"

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
            # Newer Picamera2 releases expose the VC4 hardware selection on
            # their LibavH264Encoder wrapper; older releases select hardware
            # directly in H264Encoder. Never silently use software on this Pi 4.
            if hasattr(encoder, "use_hw"):
                encoder.use_hw = True
                if not encoder.use_hw:
                    raise RuntimeError("Picamera2 did not enable the Pi 4 VC4 H.264 encoder")
            # One hardware encoder feeds the rolling MP4 files and RTSPS stream.
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
                return {"recording_enabled": enabled}
            if enabled == self._local_recording_attached:
                self.recording_enabled = enabled
                self.settings_store.update({"recording_enabled": enabled})
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
    def _i420_to_bgr(frame, width, height, stream_name):
        if frame is None:
            raise RuntimeError(f"Camera returned an empty {stream_name} frame")
        if frame.ndim == 3 and frame.shape[-1] == 1:
            frame = frame.reshape((height * 3 // 2, width))
        expected = (height * 3 // 2, width)
        if frame.shape != expected:
            if frame.size != width * height * 3 // 2:
                raise RuntimeError(
                    f"Unexpected {stream_name} YUV420 shape: {frame.shape}; expected {expected}"
                )
            frame = frame.reshape(expected)
        return cv2.cvtColor(frame, cv2.COLOR_YUV2BGR_I420)

    def _capture_loop(self):
        """Read synchronized high-resolution preview and low-resolution AI frames."""
        if self.camera_backend == "opencv":
            self._capture_opencv_loop()
            return

        last_qr_scan = 0.0
        qr_detector = cv2.QRCodeDetector()
        if _zbar_decode is None:
            log.warning(
                "pyzbar/libzbar is unavailable; QR scanning will fall back to OpenCV, "
                "which may be built without QUIRC support"
            )
        preview_interval = 1.0 / 20.0
        values = self.settings_store.get()
        main_width = int(values["video_width"])
        main_height = int(values["video_height"])
        while not self._stop.is_set():
            try:
                request = self.picam2.capture_request()
                try:
                    main_yuv = request.make_array("main")
                    lores_yuv = request.make_array("lores")
                finally:
                    request.release()

                # Both arrays come from the same libcamera request. The main
                # stream drives the operator preview; lores is reserved for
                # QR scanning and NCNN to keep its CPU budget low.
                preview_source = self._i420_to_bgr(main_yuv, main_width, main_height, "main")
                frame = self._i420_to_bgr(lores_yuv, 640, 360, "lores")
                now = time.monotonic()
                self.camera_error = ""

                if self._pair_requested_until and now >= self._pair_requested_until:
                    self._pair_requested_until = 0.0
                    self._pair_status = "expired"
                    log.warning(
                        "Pairing QR scan expired without decoding a supported QR code; "
                        "use manual code entry or try again with the QR closer to the camera"
                    )
                if self._pair_requested_until > now and now - last_qr_scan >= 0.35:
                    last_qr_scan = now
                    pairing_code = self._decode_pairing_qr(qr_detector, frame)
                    if pairing_code:
                        log.info("Pairing QR detected; submitting one-time code")
                        self._complete_pairing(pairing_code)

                with self._frames_lock:
                    self._latest_bgr = frame.copy()
                    self._frame_sequence += 1
                    self._latest_frame_sequence = self._frame_sequence
                    self._latest_bgr_at = now
                    detections = list(self._last_detections)
                    detection_age = now - self._last_detection_at if self._last_detection_at else 999.0

                preview = preview_source.copy()
                if detections and detection_age < 1.0:
                    sx = preview.shape[1] / max(1, frame.shape[1])
                    sy = preview.shape[0] / max(1, frame.shape[0])
                    scaled = []
                    for item in detections:
                        mapped = dict(item)
                        x1, y1, x2, y2 = item["bbox"]
                        mapped["bbox"] = [
                            int(round(x1 * sx)), int(round(y1 * sy)),
                            int(round(x2 * sx)), int(round(y2 * sy)),
                        ]
                        scaled.append(mapped)
                    self._draw_boxes(preview, scaled)

                if now - self._last_preview_at >= preview_interval:
                    ok, encoded = cv2.imencode(
                        ".jpg", preview, [cv2.IMWRITE_JPEG_QUALITY, 92]
                    )
                    with self._frames_lock:
                        self.latest_jpeg = encoded.tobytes() if ok else None
                        self.latest_frame_at = time.time()
                    self._last_preview_at = now

                self.camera_resolution = f"{preview.shape[1]}x{preview.shape[0]}"
                self._frame_count += 1
                if now - self._fps_started >= 2.0:
                    elapsed = now - self._fps_started
                    self.video_fps = self._frame_count / elapsed
                    self._frame_count = 0
                    self._fps_started = now

                inference_pool_ready = bool(
                    self.inference_pool
                    and self.inference_pool.snapshot()["ready_workers"] == self.inference_pool.worker_count
                )
                self.bridge.set_metrics(
                    self.video_fps,
                    self.inference_fps,
                    inference_pool_ready,
                    self._pending_alarm_count(),
                    pending_videos=self._pending_video_count(),
                    rtsp_connected=bool(self.live_output and self.live_output.connected),
                    camera_ready=self._is_camera_ready(),
                )
            except Exception as exc:
                self.camera_error = str(exc)
                log.exception("Picamera2 capture/preview error")
                self.bridge.publish_log(
                    "error", "Camera capture or preview failed", {"error": str(exc)}
                )
                self._stop.wait(0.5)

    def _capture_opencv_loop(self):
        """Capture a USB/UVC/V4L2 webcam or network video source with OpenCV."""
        last_qr_scan = 0.0
        qr_detector = cv2.QRCodeDetector()
        preview_interval = 1.0 / 20.0
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

                preview = preview_source.copy()
                if detections and detection_age < 1.0:
                    sx = width / 640.0
                    sy = height / 360.0
                    scaled = []
                    for item in detections:
                        mapped = dict(item)
                        x1, y1, x2, y2 = item["bbox"]
                        mapped["bbox"] = [
                            int(round(x1 * sx)), int(round(y1 * sy)),
                            int(round(x2 * sx)), int(round(y2 * sy)),
                        ]
                        scaled.append(mapped)
                    self._draw_boxes(preview, scaled)

                if now - self._last_preview_at >= preview_interval:
                    ok, encoded = cv2.imencode(
                        ".jpg", preview, [cv2.IMWRITE_JPEG_QUALITY, 92]
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

                self.bridge.set_metrics(
                    self.video_fps,
                    self.inference_fps,
                    bool(
                        self.inference_pool
                        and self.inference_pool.snapshot()["ready_workers"] == self.inference_pool.worker_count
                    ),
                    self._pending_alarm_count(),
                    pending_videos=0,
                    rtsp_connected=False,
                    camera_ready=self._is_camera_ready(),
                )
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
                    if detections or self._alert_active:
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
                effective_rate = min(configured_cap, estimated_capacity)
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
            self._last_person_at = now
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
    def _draw_boxes(frame, detections):
        for item in detections:
            x1, y1, x2, y2 = item["bbox"]
            cv2.rectangle(frame, (x1, y1), (x2, y2), (40, 220, 120), 2)
            cv2.putText(frame, f"PERSON {item['confidence']:.2f}",
                        (x1, max(18, y1 - 8)), cv2.FONT_HERSHEY_SIMPLEX,
                        0.48, (40, 220, 120), 1, cv2.LINE_AA)

    def _create_alarm(self, frame, detections):
        alert_id = uuid.uuid4().hex
        image_name = f"{alert_id}.jpg"
        image_path = self.alarm_dir / image_name
        alarm_frame = frame.copy()
        self._draw_boxes(alarm_frame, detections)
        ok, encoded = cv2.imencode(".jpg", alarm_frame, [cv2.IMWRITE_JPEG_QUALITY, 90])
        if not ok:
            return
        image_path.write_bytes(encoded.tobytes())
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
        temporary = meta.with_suffix(".tmp")
        temporary.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        os.chmod(temporary, 0o600)
        temporary.replace(meta)
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
            target = Path(os.getenv("DEVICE_CREDENTIALS_PATH", "/var/lib/mqttiot/device.json"))
            target.parent.mkdir(parents=True, exist_ok=True)
            temporary = target.with_suffix(".tmp")
            temporary.write_text(json.dumps(credentials, indent=2), encoding="utf-8")
            os.chmod(temporary, 0o600)
            temporary.replace(target)
            self.uploader.state_provider = lambda: credentials
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
                self.is_running()
                and self.latest_jpeg is not None
                and self.latest_frame_at
                and time.time() - self.latest_frame_at < 2.5
            )

    def snapshot(self):
        preview_ready = self._is_camera_ready()
        values = self.settings_store.get()
        return {
            "device_id": self.device_id,
            "camera_ready": preview_ready,
            "preview_ready": preview_ready,
            "camera_backend": self.camera_backend,
            "camera_source": self.camera_source,
            "camera_model": self.camera_model,
            "camera_resolution": self.camera_resolution,
            "camera_max_fps": round(self.camera_max_fps, 2),
            "video_fps_auto": bool(values.get("video_fps_auto", True)),
            "recording_supported": self.camera_backend == "picamera2",
            "live_publish_supported": self.camera_backend == "picamera2",
            "video_fps": round(self.video_fps, 2),
            "target_video_fps": round(self.target_video_fps or values["video_fps"], 2),
            "inference_fps": round(self.inference_fps, 2),
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
            "pairing_status": self._pair_status,
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
        self.bridge.model_loaded = False
        self.inference_fps = 0.0
        self.inference_latency_ms = 0.0
        self._current_segment = None
        self._current_marker = None
        with self._frames_lock:
            self.latest_jpeg = None
            self.latest_frame_at = 0.0
            self._latest_bgr = None
            self._last_detections = []
            self._last_detection_at = 0.0

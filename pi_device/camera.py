"""Picamera2 pipeline: hardware H.264, rotating offline spool, YOLOv5n NCNN and QR pairing."""
from __future__ import annotations
import json
import logging
import os
import re
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

from .detector import YoloV5nDetector

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
        self.encoder = None
        self.local_output = None
        self.live_output = None
        self.detector = None
        self.latest_jpeg = None
        self.latest_frame_at = 0.0
        self._latest_bgr = None
        self._last_detection_at = 0.0
        self._last_preview_at = 0.0
        self._last_encoder_count = 0
        self.video_fps = 0.0
        self.inference_fps = 0.0
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
        self._last_alert_at = 0.0
        self._last_detections = []
        self._detection_history = deque(maxlen=3)
        self._frame_count = 0
        self._fps_started = time.monotonic()
        self._frames_lock = threading.RLock()
        self._current_segment = None
        self._current_marker = None

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

    def _segment_path(self):
        return self.video_dir / f"{int(time.time() * 1000)}-{uuid.uuid4().hex[:8]}.mp4"

    def _mark_open(self, path):
        marker = path.with_suffix(".open")
        marker.touch()
        return marker

    def start(self):
        from picamera2 import Picamera2
        from picamera2.encoders import H264Encoder
        from picamera2.outputs import PyavOutput, SplittableOutput

        if self.picam2 is not None:
            return
        self._stop.clear()
        with self._frames_lock:
            self.latest_jpeg = None
            self.latest_frame_at = 0.0
            self._latest_bgr = None
            self._last_detections = []
            self._last_detection_at = 0.0
        self.model_error = ""
        self.camera_error = ""
        # Incomplete MP4s from a power loss have no final container index.
        for stale_marker in self.video_dir.glob("*.open"):
            stale_marker.with_suffix(".mp4").unlink(missing_ok=True)
            stale_marker.unlink(missing_ok=True)

        values = self.settings_store.get()
        picam2 = Picamera2()
        marker = None
        local_output = None
        live_output = None
        encoder = None
        encoder_started = False
        try:
            config = picam2.create_video_configuration(
                main={"size": (values["video_width"], values["video_height"]), "format": "YUV420"},
                lores={"size": (640, 360), "format": "YUV420"},
                controls={"FrameDurationLimits": (
                    int(1_000_000 / values["video_fps"]),
                    int(1_000_000 / values["video_fps"]),
                )},
                buffer_count=8,
            )
            picam2.configure(config)

            segment = self._segment_path()
            marker = self._mark_open(segment)
            first_segment = PyavOutput(str(segment), format="mp4")
            first_segment.error_callback = lambda error: log.error("Local MP4 segment failed: %s", error)
            local_output = SplittableOutput(first_segment)
            live_output = ReconnectingRtspOutput(self.device_id, self.uploader.state_provider)
            encoder = H264Encoder(
                bitrate=values["video_bitrate"],
                repeat=True,
                iperiod=values["video_fps"],
                framerate=values["video_fps"],
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

        self.picam2 = picam2
        self.encoder = encoder
        self.local_output = local_output
        self.live_output = live_output
        self._current_segment = segment
        self._current_marker = marker
        self.live_output.start()

        try:
            self.detector = YoloV5nDetector(confidence=values["alert_confidence"])
            self.bridge.model_loaded = True
            self.bridge.publish_log("info", "YOLOv5n NCNN model loaded", {"model": "yolov5n-ncnn"})
        except Exception as exc:
            self.detector = None
            self.model_error = str(exc)
            self.bridge.model_loaded = False
            self.bridge.publish_log(
                "error", "YOLOv5n NCNN model could not be loaded", {"error": self.model_error}
            )
            log.exception("YOLOv5n unavailable; recording and streaming remain active")

        self._threads = [
            threading.Thread(target=self._capture_loop, name="camera-capture-preview", daemon=True),
            threading.Thread(target=self._ai_loop, name="camera-yolov5n-inference", daemon=True),
            threading.Thread(target=self._segment_loop, name="local-video-segmenter", daemon=True),
        ]
        for thread in self._threads:
            thread.start()

    def _segment_loop(self):
        from picamera2.outputs import PyavOutput
        while not self._stop.wait(int(self.settings_store.get()["segment_seconds"])):
            try:
                next_path = self._segment_path()
                next_marker = self._mark_open(next_path)
                output = PyavOutput(str(next_path), format="mp4")
                output.error_callback = lambda error: log.error("MP4 spool output error: %s", error)
                old_marker = self._current_marker
                self.local_output.split_output(output)
                # Old output has now closed and is safe for the HTTP uploader to read.
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

    def _capture_loop(self):
        """Keep camera capture and the local MJPEG preview independent of AI inference."""
        last_qr_scan = 0.0
        qr_detector = cv2.QRCodeDetector()
        if _zbar_decode is None:
            log.warning(
                "pyzbar/libzbar is unavailable; QR scanning will fall back to OpenCV, "
                "which may be built without QUIRC support"
            )
        preview_interval = 1.0 / 15.0
        while not self._stop.is_set():
            try:
                lores_yuv = self.picam2.capture_array("lores")
                if lores_yuv is None:
                    continue

                # Picamera2 requires the lores stream to use YUV420. Its
                # capture_array representation is a packed I420 frame:
                # height * 3/2 rows by width columns. Convert once to BGR so
                # OpenCV preview, QR decoding, snapshots and YOLO share the
                # same correctly formatted frame.
                if lores_yuv.ndim == 3 and lores_yuv.shape[2] == 1:
                    lores_yuv = lores_yuv.reshape((540, 640))
                if lores_yuv.shape != (540, 640):
                    raise RuntimeError(
                        "Unexpected lores YUV420 frame shape: "
                        f"{lores_yuv.shape}; expected (540, 640)"
                    )
                frame = cv2.cvtColor(lores_yuv, cv2.COLOR_YUV2BGR_I420)

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

                # frame is now BGR, as expected by OpenCV drawing,
                # JPEG encoding, QR decoding and snapshot generation.
                with self._frames_lock:
                    self._latest_bgr = frame.copy()
                    detections = list(self._last_detections)
                    detection_age = now - self._last_detection_at if self._last_detection_at else 999.0

                preview = frame.copy()
                if detections and detection_age < 1.0:
                    self._draw_boxes(preview, detections)
                if now - self._last_preview_at >= preview_interval:
                    ok, encoded = cv2.imencode(
                        ".jpg", preview, [cv2.IMWRITE_JPEG_QUALITY, 78]
                    )
                    with self._frames_lock:
                        self.latest_jpeg = encoded.tobytes() if ok else None
                        self.latest_frame_at = time.time()
                    self._last_preview_at = now

                # Measure the camera's capture cadence, not the slower 15 FPS
                # browser preview or independent NCNN inference cadence.
                self._frame_count += 1
                if now - self._fps_started >= 2.0:
                    elapsed = now - self._fps_started
                    self.video_fps = self._frame_count / elapsed
                    self._frame_count = 0
                    self._fps_started = now

                self.bridge.set_metrics(
                    self.video_fps,
                    self.inference_fps,
                    self.detector is not None,
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

    def _ai_loop(self):
        """Run YOLO at its own configured cadence without blocking the camera stream."""
        inference_count = 0
        inference_started = time.monotonic()
        while not self._stop.is_set():
            try:
                values = self.settings_store.get()
                interval = 1.0 / float(values["inference_fps"])
                loop_started = time.monotonic()
                with self._frames_lock:
                    frame = self._latest_bgr.copy() if self._latest_bgr is not None else None
                    frame_timestamp = self.latest_frame_at
                if frame is None:
                    self._stop.wait(0.1)
                    continue

                # Ignore stale samples if inference fell behind camera capture.
                if frame_timestamp and time.time() - frame_timestamp > 1.0:
                    self._stop.wait(0.05)
                    continue

                rgb_frame = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
                if self.detector:
                    # The local panel may change the confidence threshold without
                    # restarting the camera or reloading the NCNN graph.
                    self.detector.confidence = float(values["alert_confidence"])
                    detections = self.detector.detect(rgb_frame)
                else:
                    detections = []
                now = time.monotonic()
                with self._frames_lock:
                    self._last_detections = detections
                    self._last_detection_at = now

                self._detection_history.append(bool(detections))
                inference_count += 1
                elapsed = now - inference_started
                if elapsed >= 2.0:
                    self.inference_fps = inference_count / elapsed
                    inference_count = 0
                    inference_started = now

                if detections:
                    self._process_detections(frame, detections, now)
                elif self._alert_active:
                    self._process_detections(frame, detections, now)

                remaining = interval - (time.monotonic() - loop_started)
                if remaining > 0:
                    self._stop.wait(remaining)
            except Exception as exc:
                self.camera_error = str(exc)
                log.exception("YOLOv5n inference loop failed")
                self.bridge.publish_log(
                    "error", "YOLOv5n inference failed", {"error": str(exc)}
                )
                self._stop.wait(1)

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
                self.picam2 is not None
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
            "video_fps": round(self.video_fps, 2),
            "target_video_fps": values["video_fps"],
            "inference_fps": round(self.inference_fps, 2),
            "model_loaded": self.detector is not None,
            "model_error": self.model_error,
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
        if self.live_output:
            self.live_output.stop()
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
        self.detector = None
        self.bridge.model_loaded = False
        self._current_segment = None
        self._current_marker = None
        with self._frames_lock:
            self.latest_jpeg = None
            self.latest_frame_at = 0.0
            self._latest_bgr = None
            self._last_detections = []
            self._last_detection_at = 0.0

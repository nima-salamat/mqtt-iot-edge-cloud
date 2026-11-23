"""Durable HTTPS outbox for heartbeats, alarms, video segments and device logs."""
from __future__ import annotations
import hashlib
import json
import logging
import os
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
import requests

log = logging.getLogger(__name__)

def sha256_file(path):
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()

class DurableUploader:
    def __init__(self, device_id, settings_store, bridge, state_provider):
        self.device_id = device_id
        self.settings_store = settings_store
        self.bridge = bridge
        self.state_provider = state_provider
        self.api_base = os.getenv("API_BASE_URL", "https://server.echonode.website").rstrip("/")
        self.data_dir = Path(os.getenv("PI_DATA_DIR", "/var/lib/mqttiot"))
        self.video_dir = self.data_dir / "video"
        self.alarm_dir = self.data_dir / "alarms"
        self.log_dir = self.data_dir / "logs"
        self.log_archive_dir = self.log_dir / "archive"
        for folder in (self.video_dir, self.alarm_dir, self.log_dir, self.log_archive_dir):
            folder.mkdir(parents=True, exist_ok=True)
        self._stop = threading.Event()
        self._thread = None
        self._session = requests.Session()
        self.last_upload_at = ""
        self.last_upload_error = ""
        self.last_heartbeat_at = ""
        self.uploaded_segments = 0
        self._last_heartbeat_monotonic = 0.0

    def start(self):
        self._thread = threading.Thread(target=self._run, name="durable-uploader", daemon=True)
        self._thread.start()

    def stop(self):
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=6)
        self._session.close()

    def _token(self):
        return self.state_provider().get("api_token", "")

    def _headers(self):
        return {"Authorization": f"Bearer {self._token()}", "X-Device-ID": self.device_id}

    def _run(self):
        backoff = 2
        while not self._stop.is_set():
            if not self._token():
                self._stop.wait(5)
                continue

            failures = []
            # Keep queues independent: a failed heartbeat must not prevent an
            # alert snapshot or an already-recorded video segment from uploading.
            for task in (
                self._send_heartbeat,
                self._upload_alarms,
                self._upload_logs,
                self._upload_segments,
            ):
                if self._stop.is_set():
                    break
                try:
                    task()
                except Exception as exc:
                    failures.append(f"{task.__name__}: {exc}")
                    log.warning("%s failed; the durable outbox will retry: %s", task.__name__, exc)

            try:
                self._cleanup()
            except Exception as exc:
                failures.append(f"cleanup: {exc}")
                log.exception("Local retention cleanup failed")

            self.last_upload_error = " | ".join(failures)[:1000] if failures else ""
            if failures:
                self._stop.wait(backoff)
                backoff = min(60, backoff * 2)
            else:
                backoff = 2
                self._stop.wait(2)

    def _send_heartbeat(self):
        if time.monotonic() - self._last_heartbeat_monotonic < 15:
            return
        state = self.bridge.snapshot()
        payload = {
            "reported_at": datetime.now(timezone.utc).isoformat(),
            "video_fps": state["video_fps"],
            "inference_fps": state["inference_fps"],
            "model_loaded": state["model_loaded"],
            "mqtt_connected": state["mqtt_connected"],
            "rtsp_connected": state["rtsp_connected"],
            "camera_ready": state["camera_ready"],
            "pending_alerts": state["pending_alerts"],
            "pending_video_segments": state["pending_video_segments"],
            "model": "yolov5n-ncnn",
            "software_version": os.getenv("DEVICE_SOFTWARE_VERSION", "alarm-camera-0.1"),
        }
        response = self._session.post(
            f"{self.api_base}/api/device/heartbeat/",
            headers=self._headers(),
            json=payload,
            timeout=(5, 15),
        )
        response.raise_for_status()
        self._last_heartbeat_monotonic = time.monotonic()
        self.last_heartbeat_at = datetime.now(timezone.utc).isoformat()

    def _upload_alarms(self):
        for meta_path in sorted(self.alarm_dir.glob("*.json"), key=lambda p: p.stat().st_mtime):
            if self._stop.is_set():
                return
            try:
                payload = json.loads(meta_path.read_text(encoding="utf-8"))
                image_path = self.alarm_dir / payload["image"]
                uploaded_marker = meta_path.with_suffix(".uploaded")
                published_marker = meta_path.with_suffix(".published")
                if not uploaded_marker.exists():
                    if not image_path.is_file():
                        raise FileNotFoundError(f"Alarm snapshot missing: {image_path}")
                    with image_path.open("rb") as image_file:
                        response = self._session.post(
                            f"{self.api_base}/api/device/alarms/",
                            headers=self._headers(),
                            data={"payload": json.dumps(payload, ensure_ascii=False)},
                            files={"snapshot": (image_path.name, image_file, "image/jpeg")},
                            timeout=(10, 60),
                        )
                    response.raise_for_status()
                    uploaded_marker.write_text(datetime.now(timezone.utc).isoformat(), encoding="utf-8")
                if not published_marker.exists():
                    event = {
                        "device_id": self.device_id,
                        "event": "person_detected",
                        "alert_id": payload["alert_id"],
                        "confidence": payload["confidence"],
                        "bbox": payload["bbox"],
                        "detected_at": payload["detected_at"],
                        "snapshot_id": payload["alert_id"],
                        "source": "pi-yolov5n-ncnn",
                    }
                    if not self.bridge.publish_alarm(event):
                        self._stop.wait(1)
                        continue
                    published_marker.write_text(datetime.now(timezone.utc).isoformat(), encoding="utf-8")
                self.last_upload_at = datetime.now(timezone.utc).isoformat()
            except requests.RequestException as exc:
                self.last_upload_error = str(exc)
                log.info("Alarm %s remains in local outbox: %s", meta_path.name, exc)
                raise
            except Exception:
                log.exception("Could not sync alarm %s", meta_path)

    def _upload_logs(self):
        for path in sorted(self.log_dir.glob("*.json"), key=lambda p: p.stat().st_mtime):
            if self._stop.is_set():
                return
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
                response = self._session.post(
                    f"{self.api_base}/api/device/logs/",
                    headers=self._headers(),
                    json=payload,
                    timeout=(5, 20),
                )
                response.raise_for_status()
                archive_target = self.log_archive_dir / path.name
                path.replace(archive_target)
            except requests.RequestException as exc:
                self.last_upload_error = str(exc)
                log.info("Device log %s remains queued: %s", path.name, exc)
                raise
            except Exception:
                log.exception("Could not upload log %s", path)

    def _upload_segments(self):
        for path in sorted(self.video_dir.glob("*.mp4"), key=lambda p: p.stat().st_mtime):
            if self._stop.is_set():
                return
            if path.with_suffix(".open").exists() or path.with_suffix(".uploaded").exists():
                continue
            if time.time() - path.stat().st_mtime < 1:
                continue
            checksum = sha256_file(path)
            try:
                started_ms = int(path.stem.split("-", 1)[0])
                started = datetime.fromtimestamp(started_ms / 1000.0, tz=timezone.utc).isoformat()
            except (ValueError, IndexError):
                started = datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc).isoformat()
            with path.open("rb") as handle:
                response = self._session.post(
                    f"{self.api_base}/api/device/segments/",
                    headers=self._headers(),
                    data={
                        "segment_id": path.stem,
                        "sha256": checksum,
                        "started_at": started,
                        "duration_seconds": str(self.settings_store.get()["segment_seconds"]),
                    },
                    files={"file": (path.name, handle, "video/mp4")},
                    timeout=(15, 180),
                )
            response.raise_for_status()
            path.with_suffix(".uploaded").write_text(datetime.now(timezone.utc).isoformat(), encoding="utf-8")
            self.uploaded_segments += 1
            self.last_upload_at = datetime.now(timezone.utc).isoformat()

    def _cleanup(self):
        values = self.settings_store.get()
        now = time.time()
        local_video_seconds = int(values["local_video_retention_minutes"]) * 60
        queue_seconds = int(values["upload_queue_retention_hours"]) * 3600
        max_bytes = int(values["upload_queue_max_gb"]) * 1024**3
        videos = sorted(self.video_dir.glob("*.mp4"), key=lambda p: p.stat().st_mtime)
        total_bytes = sum(p.stat().st_size for p in videos)
        for path in videos:
            if path.with_suffix(".open").exists():
                continue
            age = now - path.stat().st_mtime
            uploaded = path.with_suffix(".uploaded").exists()
            expired_pending = not uploaded and age > queue_seconds
            over_cap = not uploaded and total_bytes > max_bytes
            expired_local_copy = uploaded and (local_video_seconds == 0 or age > local_video_seconds)
            if expired_pending or over_cap or expired_local_copy:
                size = path.stat().st_size
                path.unlink(missing_ok=True)
                path.with_suffix(".uploaded").unlink(missing_ok=True)
                total_bytes -= size
                if expired_pending or over_cap:
                    self.bridge.publish_log(
                        "warning", "Pending video removed from the local retry queue",
                        {"segment": path.name, "age_seconds": int(age)},
                    )

        alarm_days = int(values["local_alarm_retention_days"])
        alarm_files = sorted(self.alarm_dir.glob("*.json"), key=lambda p: p.stat().st_mtime)
        alarm_bytes = 0
        for meta_path in alarm_files:
            if meta_path.with_suffix(".published").exists():
                continue
            try:
                item = json.loads(meta_path.read_text(encoding="utf-8"))
                image_path = self.alarm_dir / item.get("image", "")
                if image_path.is_file():
                    alarm_bytes += image_path.stat().st_size
            except Exception:
                log.exception("Could not inspect queued alarm %s", meta_path)
        alarm_queue_cap = max(128 * 1024 * 1024, max_bytes // 3)
        for meta_path in alarm_files:
            try:
                item = json.loads(meta_path.read_text(encoding="utf-8"))
                image_path = self.alarm_dir / item.get("image", "")
                uploaded = meta_path.with_suffix(".uploaded").exists()
                published = meta_path.with_suffix(".published").exists()
                age = now - meta_path.stat().st_mtime
                expired_local = uploaded and published and age > alarm_days * 86400
                expired_pending = not published and age > queue_seconds
                over_cap = not published and alarm_bytes > alarm_queue_cap
                if expired_local or expired_pending or over_cap:
                    image_size = image_path.stat().st_size if image_path.is_file() else 0
                    image_path.unlink(missing_ok=True)
                    meta_path.unlink(missing_ok=True)
                    meta_path.with_suffix(".uploaded").unlink(missing_ok=True)
                    meta_path.with_suffix(".published").unlink(missing_ok=True)
                    if not published:
                        alarm_bytes = max(0, alarm_bytes - image_size)
                    if expired_pending or over_cap:
                        self.bridge.publish_log(
                            "error", "An alarm snapshot expired in the local retry queue",
                            {"alert_id": item.get("alert_id"), "age_seconds": int(age)},
                        )
            except Exception:
                log.exception("Could not prune local alarm %s", meta_path)

        log_days = int(values["local_log_retention_days"])
        for path in self.log_dir.glob("*.json"):
            if now - path.stat().st_mtime > queue_seconds:
                path.unlink(missing_ok=True)
        for path in self.log_archive_dir.glob("*.json"):
            if now - path.stat().st_mtime > log_days * 86400:
                path.unlink(missing_ok=True)

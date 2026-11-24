"""Durable HTTPS outbox for heartbeats, alarms, video segments and device logs."""
from __future__ import annotations
import hashlib
import json
import logging
import math
import os
import re
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
import requests

from .atomic_files import atomic_write_json, atomic_write_text
from .system_stats import collect_system_stats

log = logging.getLogger(__name__)

def sha256_file(path):
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()

class DurableUploader:
    # MQTT PUBACK confirms broker receipt, not Django processing. Keep a
    # periodic authenticated HTTPS heartbeat as an application-level fallback.
    HTTPS_HEARTBEAT_INTERVAL_SECONDS = 30

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
        self.quarantine_dir = self.data_dir / "quarantine"
        for folder in (self.video_dir, self.alarm_dir, self.log_dir, self.log_archive_dir, self.quarantine_dir):
            folder.mkdir(parents=True, exist_ok=True)
        self._stop = threading.Event()
        self._thread = None
        # Serialize queue item uploads, retention and user-requested queue clearing.
        self._queue_lock = threading.RLock()
        self._session = requests.Session()
        self.last_upload_at = ""
        self.last_upload_error = ""
        self.last_video_upload_at = ""
        self.last_video_upload_error = ""
        self.last_heartbeat_at = ""
        self.last_mqtt_telemetry_at = ""
        self.uploaded_segments = 0
        self._last_heartbeat_monotonic = 0.0
        self._last_https_heartbeat_monotonic = 0.0
        self.credentials_revoked = False

    def start(self):
        self._thread = threading.Thread(target=self._run, name="durable-uploader", daemon=True)
        self._thread.start()

    def stop(self):
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=6)
            if self._thread.is_alive():
                # requests cannot safely cancel an in-flight multipart upload.
                # Closing the shared Session here races that request; keep the
                # session alive until process exit so an interrupted upload can
                # leave its queue file unmarked and retry after the next boot.
                log.warning(
                    "Uploader is still finishing an HTTP request during shutdown; "
                    "leaving the session open and keeping queue items retryable"
                )
                return
        self._session.close()

    def _token(self):
        return self.state_provider().get("api_token", "")

    def _headers(self):
        return {"Authorization": f"Bearer {self._token()}", "X-Device-ID": self.device_id}

    def clear_local_credentials(self):
        """Forget revoked pairing credentials without deleting local recordings."""
        path = Path(os.getenv("DEVICE_CREDENTIALS_PATH", "/var/lib/mqttiot/device.json"))
        try:
            if path.is_file() and not path.is_symlink():
                path.unlink()
            path.with_suffix(".tmp").unlink(missing_ok=True)
        except OSError:
            log.exception("Could not remove revoked device credentials")
            raise
        self.state_provider = lambda: {}
        self.credentials_revoked = True
        if self.bridge is not None:
            revoke_session = getattr(self.bridge, "revoke_credentials", None)
            if callable(revoke_session):
                try:
                    revoke_session()
                except Exception:
                    log.exception("Could not stop the revoked MQTT session")
            elif hasattr(self.bridge, "set_signing_key"):
                self.bridge.set_signing_key("")
        self.last_heartbeat_at = ""
        self.last_mqtt_telemetry_at = ""
        self._last_heartbeat_monotonic = 0.0
        self._last_https_heartbeat_monotonic = 0.0

    def _check_response(self, response):
        if response.status_code in {401, 403}:
            self.clear_local_credentials()
            raise requests.HTTPError(
                "The server revoked this camera pairing; local credentials were cleared.",
                response=response,
            )
        response.raise_for_status()

    def disconnect_from_server(self):
        """Revoke server access first, then clear the local saved API token."""
        token = self._token()
        if not token:
            self.clear_local_credentials()
            return {"status": "already_disconnected"}
        response = self._session.post(
            f"{self.api_base}/api/device/disconnect/",
            headers=self._headers(),
            json={},
            timeout=(5, 20),
        )
        if response.status_code not in {200, 204}:
            self._check_response(response)
            raise RuntimeError(f"Device disconnect returned HTTP {response.status_code}")
        self.clear_local_credentials()
        return {"status": "disconnected", "device_id": self.device_id}

    def delete_alarm(self, alert_id):
        """Delete one local detection and all local delivery markers."""
        if not re.fullmatch(r"[a-fA-F0-9]{32}", str(alert_id or "")):
            raise ValueError("شناسه تشخیص معتبر نیست.")
        with self._queue_lock:
            paths = [
                self.alarm_dir / f"{alert_id}.json",
                self.alarm_dir / f"{alert_id}.jpg",
                self.alarm_dir / f"{alert_id}.uploaded",
                self.alarm_dir / f"{alert_id}.published",
            ]
            removed_bytes = 0
            removed_any = False
            for path in paths:
                if path.is_symlink():
                    continue
                if not path.is_file():
                    continue
                removed_bytes += self._file_size(path)
                path.unlink(missing_ok=True)
                removed_any = True
            return {
                "removed_count": 1 if removed_any else 0,
                "removed_bytes": removed_bytes,
            }

    def clear_all_local_alarms(self):
        """Delete every local detection, including items already synced to the server."""
        removed_count = 0
        removed_bytes = 0
        with self._queue_lock:
            for meta_path in self.alarm_dir.glob("*.json"):
                alert_id = meta_path.stem
                if not re.fullmatch(r"[a-fA-F0-9]{32}", alert_id):
                    continue
                if not meta_path.is_file() or meta_path.is_symlink():
                    continue
                result = self.delete_alarm(alert_id)
                removed_count += result["removed_count"]
                removed_bytes += result["removed_bytes"]
        return {
            "removed_count": removed_count,
            "removed_bytes": removed_bytes,
            "remaining": self.queue_snapshot(),
        }

    def _sorted_queue_files(self, folder: Path, pattern: str):
        # Snapshot mtimes under the same lock used by queue clearing and cleanup.
        with self._queue_lock:
            items = []
            for path in folder.glob(pattern):
                try:
                    items.append((path.stat().st_mtime, path))
                except OSError:
                    continue
            return [path for _mtime, path in sorted(items, key=lambda item: item[0])]

    def _alarm_snapshot_path(self, meta_path: Path, payload: dict) -> Path:
        alert_id = meta_path.stem
        if (
            not re.fullmatch(r"[a-fA-F0-9]{32}", alert_id)
            or payload.get("alert_id") != alert_id
            or payload.get("image") != f"{alert_id}.jpg"
        ):
            raise ValueError(f"Invalid or unsafe local alarm metadata: {meta_path.name}")
        return self.alarm_dir / f"{alert_id}.jpg"

    def _validate_alarm_metadata(self, meta_path: Path, payload: dict) -> None:
        """Reject malformed detections before they can retry forever against Django."""
        alert_id = meta_path.stem
        if (
            payload.get("alert_id") != alert_id
            or payload.get("device_id") != self.device_id
            or payload.get("event") != "person_detected"
        ):
            raise ValueError("Alarm identity or event type is invalid")

        def finite_number(value, minimum, maximum):
            if isinstance(value, bool):
                return None
            try:
                number = float(value)
            except (TypeError, ValueError, OverflowError):
                return None
            if not math.isfinite(number) or number < minimum or number > maximum:
                return None
            return number

        confidence = finite_number(payload.get("confidence"), 0, 1)
        if confidence is None:
            raise ValueError("Alarm confidence must be finite and between 0 and 1")

        detected_at = payload.get("detected_at")
        if not isinstance(detected_at, str):
            raise ValueError("Alarm detected_at timestamp is missing")
        try:
            datetime.fromisoformat(detected_at.replace("Z", "+00:00"))
        except ValueError as exc:
            raise ValueError("Alarm detected_at timestamp is invalid") from exc

        boxes = payload.get("bbox")
        if not isinstance(boxes, list) or len(boxes) > 100:
            raise ValueError("Alarm bounding boxes must be a list with at most 100 entries")
        for index, box in enumerate(boxes):
            if not isinstance(box, dict):
                raise ValueError(f"Alarm bounding box {index} is not an object")
            values = {
                key: finite_number(box.get(key), 0, 1)
                for key in ("x1", "y1", "x2", "y2")
            }
            box_confidence = finite_number(box.get("confidence", confidence), 0, 1)
            if (
                any(value is None for value in values.values())
                or box_confidence is None
                or values["x2"] <= values["x1"]
                or values["y2"] <= values["y1"]
            ):
                raise ValueError(f"Alarm bounding box {index} contains invalid coordinates")

    def _quarantine_queue_file(self, path: Path, queue_name: str, reason: Exception) -> bool:
        """Move an unreadable queue item (and alarm companions) out of the retry loop."""
        if path.is_symlink() or not path.is_file():
            return False

        target_dir = self.quarantine_dir / queue_name
        target_dir.mkdir(parents=True, exist_ok=True)
        sources = [path]
        if queue_name == "alarms":
            sources.extend(
                path.with_suffix(suffix)
                for suffix in (".jpg", ".uploaded", ".published")
            )

        moved = []
        primary_destination = None
        for source in sources:
            if source.is_symlink() or not source.is_file():
                continue
            destination = target_dir / source.name
            if destination.exists():
                destination = target_dir / f"{source.stem}.{time.time_ns()}{source.suffix}"
            try:
                source.replace(destination)
                moved.append(destination.name)
                if source == path:
                    primary_destination = destination
            except OSError:
                log.exception("Could not quarantine queue file %s", source)

        if primary_destination is not None:
            reason_path = target_dir / f"{primary_destination.name}.reason.txt"
            if reason_path.exists():
                reason_path = target_dir / f"{primary_destination.stem}.{time.time_ns()}.reason.txt"
            try:
                atomic_write_text(
                    reason_path,
                    f"Quarantined at {datetime.now(timezone.utc).isoformat()}\n"
                    f"Reason: {type(reason).__name__}\n",
                )
            except OSError:
                log.exception("Could not write quarantine reason for %s", path.name)
            log.warning(
                "Quarantined malformed %s queue item %s; moved %d related file(s)",
                queue_name, path.name, len(moved),
            )
            return True
        return False

    def _read_queue_json(self, path: Path, queue_name: str):
        """Read one queue JSON object; quarantine permanently malformed files."""
        if path.is_symlink() or not path.is_file():
            return None
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            self._quarantine_queue_file(path, queue_name, exc)
            return None

        if not isinstance(payload, dict):
            self._quarantine_queue_file(
                path, queue_name, ValueError("Queue JSON root must be an object")
            )
            return None

        if queue_name == "alarms":
            try:
                self._alarm_snapshot_path(path, payload)
                self._validate_alarm_metadata(path, payload)
            except ValueError as exc:
                self._quarantine_queue_file(path, queue_name, exc)
                return None
        return payload

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
                if self._stop.is_set() or not self._token():
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
        now_monotonic = time.monotonic()
        if now_monotonic - self._last_heartbeat_monotonic < 15:
            return
        state = self.bridge.snapshot()
        payload = {
            "reported_at": datetime.now(timezone.utc).isoformat(),
            "video_fps": state["video_fps"],
            "inference_fps": state["inference_fps"],
            "model_loaded": state["model_loaded"],
            "mqtt_connected": state["mqtt_connected"],
            "mqtt_command_subscription_ready": bool(
                state.get("mqtt_command_subscription_ready", False)
            ),
            "recording_supported": bool(state.get("recording_supported", False)),
            "rtsp_connected": state["rtsp_connected"],
            "camera_ready": state["camera_ready"],
            "pending_alerts": state["pending_alerts"],
            "pending_video_segments": state["pending_video_segments"],
            "tracking_active": state.get("tracking_active", False),
            "tracking_seconds": state.get("tracking_seconds", 0.0),
            "model": "yolov5n-ncnn",
            "software_version": os.getenv("DEVICE_SOFTWARE_VERSION", "alarm-camera-0.1"),
            "recording_enabled": (
                state["recording_enabled"]
                if isinstance(state.get("recording_enabled"), bool)
                else (
                    bool(self.settings_store.get().get("recording_enabled", True))
                    if self.settings_store is not None else True
                )
            ),
            "last_video_upload_error": self.last_video_upload_error[:500],
        }
        try:
            payload["system"] = collect_system_stats(self.data_dir)
        except Exception:
            log.warning("Could not collect Raspberry Pi system metrics for heartbeat", exc_info=True)
        if self.last_video_upload_at:
            payload["last_video_upload_at"] = self.last_video_upload_at

        # Publish frequent telemetry over MQTT, but do not confuse a broker
        # PUBACK with successful processing by the Django listener.
        publish_telemetry = getattr(self.bridge, "publish_telemetry", None)
        if callable(publish_telemetry):
            if publish_telemetry(payload):
                self._last_heartbeat_monotonic = time.monotonic()
                self.last_mqtt_telemetry_at = datetime.now(timezone.utc).isoformat()
                if (
                    self._last_https_heartbeat_monotonic > 0
                    and time.monotonic() - self._last_https_heartbeat_monotonic
                    < self.HTTPS_HEARTBEAT_INTERVAL_SECONDS
                ):
                    return
            else:
                bridge_state = self.bridge.snapshot() if self.bridge is not None else {}
                mqtt_reason = (
                    bridge_state.get("last_error", "")
                    if isinstance(bridge_state, dict) else ""
                )
                log.warning(
                    "Signed MQTT heartbeat was not acknowledged; using authenticated HTTPS fallback. "
                    "Reason: %s",
                    mqtt_reason or "MQTT publisher is unavailable or not connected.",
                )

        # This endpoint applies metrics directly in Django and is the recovery
        # path when an MQTT message is broker-acknowledged but rejected by the
        # application (for example because of device clock skew).
        request_started_at = datetime.now(timezone.utc)
        response = self._session.post(
            f"{self.api_base}/api/device/heartbeat/",
            headers=self._headers(),
            json=payload,
            timeout=(5, 15),
        )
        response_received_at = datetime.now(timezone.utc)
        self._check_response(response)
        try:
            response_payload = response.json()
        except (ValueError, AttributeError):
            response_payload = {}
        # The Django response carries its authoritative UTC clock. Estimate
        # network delay symmetrically and calibrate the Pi clock for short-lived
        # MQTT commands without changing the OS clock.
        server_time_value = (
            response_payload.get("server_time", "")
            if isinstance(response_payload, dict) else ""
        )
        if server_time_value:
            try:
                server_time = datetime.fromisoformat(
                    str(server_time_value).replace("Z", "+00:00")
                )
                if server_time.tzinfo is None:
                    server_time = server_time.replace(tzinfo=timezone.utc)
                round_trip = response_received_at - request_started_at
                local_midpoint = request_started_at + round_trip / 2
                offset_seconds = (server_time - local_midpoint).total_seconds()
                set_time_offset = getattr(self.bridge, "set_server_time_offset", None)
                if callable(set_time_offset):
                    set_time_offset(offset_seconds)
            except (TypeError, ValueError, OverflowError):
                log.warning("Ignoring invalid server_time in heartbeat response")
        signing_key = (
            response_payload.get("mqtt_signing_key", "")
            if isinstance(response_payload, dict) else ""
        )
        if re.fullmatch(r"[a-fA-F0-9]{64}", str(signing_key or "")):
            self._persist_mqtt_signing_key(signing_key)
        self._last_https_heartbeat_monotonic = time.monotonic()
        self._last_heartbeat_monotonic = self._last_https_heartbeat_monotonic
        self.last_heartbeat_at = datetime.now(timezone.utc).isoformat()

    def _persist_mqtt_signing_key(self, signing_key):
        """Persist the per-device event key beside the existing mode-0600 credentials."""
        current = self.state_provider()
        if not isinstance(current, dict) or not current.get("api_token"):
            return
        credentials = dict(current)
        credentials["mqtt_signing_key"] = str(signing_key).lower()
        target = Path(os.getenv("DEVICE_CREDENTIALS_PATH", "/var/lib/mqttiot/device.json"))
        target.parent.mkdir(parents=True, exist_ok=True)
        if target.is_symlink():
            raise OSError("Refusing to replace a symlinked device credential file.")
        atomic_write_json(target, credentials, indent=2)
        self.state_provider = lambda: credentials
        if self.bridge is not None and hasattr(self.bridge, "set_signing_key"):
            self.bridge.set_signing_key(signing_key)

    def _upload_alarms(self):
        for meta_path in self._sorted_queue_files(self.alarm_dir, "*.json"):
            if self._stop.is_set():
                return
            # Lock one item at a time so the local panel can clear the remaining
            # queue without racing a file that is currently being uploaded.
            with self._queue_lock:
                if not meta_path.is_file() or meta_path.is_symlink():
                    continue
                try:
                    payload = self._read_queue_json(meta_path, "alarms")
                    if payload is None:
                        continue
                    image_path = self._alarm_snapshot_path(meta_path, payload)
                    uploaded_marker = meta_path.with_suffix(".uploaded")
                    published_marker = meta_path.with_suffix(".published")
                    if not uploaded_marker.exists():
                        if not image_path.is_file() or image_path.is_symlink():
                            raise FileNotFoundError(f"Alarm snapshot missing or unsafe: {image_path.name}")
                        with image_path.open("rb") as image_file:
                            response = self._session.post(
                                f"{self.api_base}/api/device/alarms/",
                                headers=self._headers(),
                                data={"payload": json.dumps(payload, ensure_ascii=False)},
                                files={"snapshot": (image_path.name, image_file, "image/jpeg")},
                                timeout=(10, 60),
                            )
                        self._check_response(response)
                        atomic_write_text(uploaded_marker, datetime.now(timezone.utc).isoformat())
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
                        atomic_write_text(published_marker, datetime.now(timezone.utc).isoformat())
                    self.last_upload_at = datetime.now(timezone.utc).isoformat()
                except requests.RequestException as exc:
                    self.last_upload_error = str(exc)
                    log.info("Alarm %s remains in local outbox: %s", meta_path.name, exc)
                    raise
                except Exception:
                    log.exception("Could not sync alarm %s", meta_path)

    def _upload_logs(self):
        for path in self._sorted_queue_files(self.log_dir, "*.json"):
            if self._stop.is_set():
                return
            with self._queue_lock:
                if not path.is_file() or path.is_symlink():
                    continue
                try:
                    payload = self._read_queue_json(path, "logs")
                    if payload is None:
                        continue
                    response = self._session.post(
                        f"{self.api_base}/api/device/logs/",
                        headers=self._headers(),
                        json=payload,
                        timeout=(5, 20),
                    )
                    self._check_response(response)
                    archive_target = self.log_archive_dir / path.name
                    path.replace(archive_target)
                except requests.RequestException as exc:
                    self.last_upload_error = str(exc)
                    log.info("Device log %s remains queued: %s", path.name, exc)
                    raise
                except Exception:
                    log.exception("Could not upload log %s", path)

    def _upload_segments(self):
        # Pausing recording also pauses video delivery. Existing pending files
        # remain on disk and resume when the operator starts recording again.
        if self.settings_store is not None:
            settings = self.settings_store.get()
            if not settings.get("recording_enabled", True):
                return

        for path in self._sorted_queue_files(self.video_dir, "*.mp4"):
            if self._stop.is_set():
                return
            if self.settings_store is not None and not self.settings_store.get().get("recording_enabled", True):
                return
            with self._queue_lock:
                if self.settings_store is not None and not self.settings_store.get().get("recording_enabled", True):
                    return
                if not path.is_file() or path.is_symlink():
                    continue
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
                try:
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
                    self._check_response(response)
                except Exception as exc:
                    self.last_video_upload_error = str(exc)[:500]
                    raise
                uploaded_at = datetime.now(timezone.utc).isoformat()
                atomic_write_text(path.with_suffix(".uploaded"), uploaded_at)
                self.uploaded_segments += 1
                self.last_upload_at = uploaded_at
                self.last_video_upload_at = uploaded_at
                self.last_video_upload_error = ""

    @staticmethod
    def _file_size(path):
        try:
            return path.stat().st_size if path.is_file() and not path.is_symlink() else 0
        except OSError:
            return 0

    def queue_snapshot(self):
        """Return counts and byte sizes for data that is still waiting to send."""
        with self._queue_lock:
            videos = [
                path for path in self.video_dir.glob("*.mp4")
                if path.is_file() and not path.is_symlink()
                and not path.with_suffix(".open").exists()
                and not path.with_suffix(".uploaded").exists()
            ]
            alarms = []
            alarm_bytes = 0
            for meta_path in self.alarm_dir.glob("*.json"):
                if (
                    meta_path.is_symlink()
                    or not meta_path.is_file()
                    or meta_path.with_suffix(".published").exists()
                ):
                    continue
                # The local status endpoint calls queue_snapshot independently
                # of the upload worker. Validate the same metadata schema here,
                # otherwise a valid JSON list/string can raise AttributeError
                # and make the entire local diagnostics API return HTTP 500.
                item = self._read_queue_json(meta_path, "alarms")
                if item is None:
                    continue
                alarms.append(meta_path)
                alarm_bytes += self._file_size(meta_path)
                image_path = self._alarm_snapshot_path(meta_path, item)
                alarm_bytes += self._file_size(image_path)

            logs = [
                path for path in self.log_dir.glob("*.json")
                if path.is_file() and not path.is_symlink()
            ]

            def summary(items, size):
                return {"count": len(items), "bytes": size}

            return {
                "videos": summary(videos, sum(self._file_size(path) for path in videos)),
                "alarms": summary(alarms, alarm_bytes),
                "logs": summary(logs, sum(self._file_size(path) for path in logs)),
            }

    def clear_pending_queue(self, queue_name):
        """Delete only unsent files in one queue; retain uploaded and archived data."""
        if queue_name not in {"videos", "alarms", "logs"}:
            raise ValueError("Unknown queue. Choose videos, alarms or logs.")

        removed_count = 0
        removed_bytes = 0
        with self._queue_lock:
            if queue_name == "videos":
                for path in self.video_dir.glob("*.mp4"):
                    if (
                        not path.is_file() or path.is_symlink()
                        or path.with_suffix(".open").exists()
                        or path.with_suffix(".uploaded").exists()
                    ):
                        continue
                    size = self._file_size(path)
                    try:
                        path.unlink()
                    except OSError:
                        log.exception("Could not clear pending video %s", path.name)
                        continue
                    removed_count += 1
                    removed_bytes += size

            elif queue_name == "alarms":
                for meta_path in self.alarm_dir.glob("*.json"):
                    if (
                        not meta_path.is_file() or meta_path.is_symlink()
                        or meta_path.with_suffix(".published").exists()
                    ):
                        continue
                    # Alarm IDs are UUID hex strings. Derive the snapshot filename
                    # from the ID instead of trusting a path stored inside JSON.
                    if not re.fullmatch(r"[a-fA-F0-9]{32}", meta_path.stem):
                        continue
                    image_path = self.alarm_dir / f"{meta_path.stem}.jpg"
                    size = self._file_size(meta_path) + self._file_size(image_path)
                    try:
                        meta_path.unlink()
                        if image_path.is_file() and not image_path.is_symlink():
                            image_path.unlink()
                        meta_path.with_suffix(".uploaded").unlink(missing_ok=True)
                        meta_path.with_suffix(".published").unlink(missing_ok=True)
                    except OSError:
                        log.exception("Could not clear pending alarm %s", meta_path.name)
                        continue
                    removed_count += 1
                    removed_bytes += size

            else:
                for path in self.log_dir.glob("*.json"):
                    if not path.is_file() or path.is_symlink():
                        continue
                    size = self._file_size(path)
                    try:
                        path.unlink()
                    except OSError:
                        log.exception("Could not clear pending log %s", path.name)
                        continue
                    removed_count += 1
                    removed_bytes += size

        return {
            "queue": queue_name,
            "removed_count": removed_count,
            "removed_bytes": removed_bytes,
            "remaining": self.queue_snapshot(),
        }

    def _cleanup(self):
        with self._queue_lock:
            self._cleanup_locked()

    def _cleanup_locked(self):
        values = self.settings_store.get()
        now = time.time()
        local_video_seconds = int(values["local_video_retention_minutes"]) * 60
        queue_seconds = int(values["upload_queue_retention_hours"]) * 3600
        max_bytes = int(values["upload_queue_max_gb"]) * 1024**3
        video_entries = []
        for path in self.video_dir.glob("*.mp4"):
            try:
                if path.is_symlink() or not path.is_file():
                    continue
                stat_result = path.stat()
                video_entries.append((stat_result.st_mtime, stat_result.st_size, path))
            except OSError:
                log.warning("Could not inspect queued video file %s", path.name, exc_info=True)
        video_entries.sort(key=lambda item: item[0])
        total_bytes = sum(size for _mtime, size, _path in video_entries)
        for modified_at, original_size, path in video_entries:
            try:
                if path.is_symlink() or not path.is_file():
                    continue
                stat_result = path.stat()
            except OSError:
                log.warning("Could not stat queued video file %s", path.name, exc_info=True)
                continue
            if path.with_suffix(".open").exists():
                continue
            age = now - stat_result.st_mtime
            uploaded = path.with_suffix(".uploaded").exists()
            expired_pending = not uploaded and age > queue_seconds
            over_cap = not uploaded and total_bytes > max_bytes
            expired_local_copy = uploaded and (
                local_video_seconds == 0 or age > local_video_seconds
            )
            if expired_pending or over_cap or expired_local_copy:
                size = stat_result.st_size
                try:
                    path.unlink(missing_ok=True)
                    path.with_suffix(".uploaded").unlink(missing_ok=True)
                except OSError:
                    log.exception("Could not remove expired queued video %s", path.name)
                    continue
                total_bytes = max(0, total_bytes - size)
                if expired_pending or over_cap:
                    self.bridge.publish_log(
                        "warning", "Pending video removed from the local retry queue",
                        {"segment": path.name, "age_seconds": int(age)},
                    )

        alarm_days = int(values["local_alarm_retention_days"])
        alarm_entries = []
        for meta_path in self.alarm_dir.glob("*.json"):
            try:
                if meta_path.is_symlink() or not meta_path.is_file():
                    continue
                alarm_entries.append((meta_path.stat().st_mtime, meta_path))
            except OSError:
                log.warning("Could not inspect queued alarm %s", meta_path.name, exc_info=True)
        alarm_files = [
            path for _modified_at, path in sorted(alarm_entries, key=lambda item: item[0])
        ]
        alarm_bytes = 0
        for meta_path in alarm_files:
            if meta_path.with_suffix(".published").exists():
                continue
            try:
                item = self._read_queue_json(meta_path, "alarms")
                if item is None:
                    continue
                image_path = self._alarm_snapshot_path(meta_path, item)
                if image_path.is_file() and not image_path.is_symlink():
                    alarm_bytes += image_path.stat().st_size
            except OSError:
                log.exception("Could not inspect queued alarm %s", meta_path)
        alarm_queue_cap = max(128 * 1024 * 1024, max_bytes // 3)
        for meta_path in alarm_files:
            if not meta_path.is_file() or meta_path.is_symlink():
                continue
            try:
                item = self._read_queue_json(meta_path, "alarms")
                if item is None:
                    continue
                image_path = self._alarm_snapshot_path(meta_path, item)
                uploaded = meta_path.with_suffix(".uploaded").exists()
                published = meta_path.with_suffix(".published").exists()
                age = now - meta_path.stat().st_mtime
                expired_local = uploaded and published and age > alarm_days * 86400
                expired_pending = not published and age > queue_seconds
                over_cap = not published and alarm_bytes > alarm_queue_cap
                if expired_local or expired_pending or over_cap:
                    image_size = (
                        image_path.stat().st_size
                        if image_path.is_file() and not image_path.is_symlink()
                        else 0
                    )
                    if image_path.is_file() and not image_path.is_symlink():
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
            if path.is_symlink() or not path.is_file():
                continue
            if now - path.stat().st_mtime > queue_seconds:
                path.unlink(missing_ok=True)
        for path in self.log_archive_dir.glob("*.json"):
            try:
                if path.is_symlink() or not path.is_file():
                    continue
                expired = now - path.stat().st_mtime > log_days * 86400
                if expired:
                    path.unlink(missing_ok=True)
            except OSError:
                log.warning("Could not prune archived log %s", path.name, exc_info=True)

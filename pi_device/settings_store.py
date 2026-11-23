"""Thread-safe, range-checked settings for the Raspberry Pi camera agent."""
from __future__ import annotations

import json
import os
import threading
from pathlib import Path
from typing import Any


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        return default


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        return default


def _video_bitrate_default() -> int:
    configured = _env_int("VIDEO_BITRATE", 1_000_000)
    # 4 Mbps was the old factory default, not a deliberate user override.
    return 1_000_000 if configured == 4_000_000 else configured


DEFAULTS: dict[str, Any] = {
    "video_width": 1280,
    "video_height": 720,
    "video_fps": _env_int("VIDEO_FPS", 30),
    "video_bitrate": _video_bitrate_default(),
    "segment_seconds": _env_int("VIDEO_SEGMENT_SECONDS", 5),
    "inference_fps": _env_int("INFERENCE_FPS", 10),
    "inference_workers": _env_int("INFERENCE_WORKERS", 2),
    "recording_enabled": True,
    "alert_confidence": _env_float("ALERT_CONFIDENCE", 0.45),
    "alert_cooldown_seconds": _env_int("ALERT_COOLDOWN_SECONDS", 15),
    "alert_clear_seconds": _env_int("ALERT_CLEAR_SECONDS", 4),
    "local_video_retention_minutes": _env_int("LOCAL_VIDEO_RETENTION_MINUTES", 10),
    "upload_queue_retention_hours": _env_int("UPLOAD_QUEUE_RETENTION_HOURS", 72),
    "upload_queue_max_gb": _env_int("UPLOAD_QUEUE_MAX_GB", 8),
    "local_alarm_retention_days": _env_int("LOCAL_ALARM_RETENTION_DAYS", 7),
    "local_log_retention_days": _env_int("LOCAL_LOG_RETENTION_DAYS", 7),
}

RANGES = {
    "video_fps": (5, 30),
    "video_bitrate": (500_000, 8_000_000),
    "segment_seconds": (2, 15),
    "inference_fps": (1, 10),
    "inference_workers": (1, 3),
    "alert_confidence": (0.10, 0.95),
    "alert_cooldown_seconds": (1, 300),
    "alert_clear_seconds": (1, 60),
    "local_video_retention_minutes": (0, 1440),
    "upload_queue_retention_hours": (1, 168),
    "upload_queue_max_gb": (1, 64),
    "local_alarm_retention_days": (1, 90),
    "local_log_retention_days": (1, 90),
}


def normalize_settings(value: dict[str, Any]) -> dict[str, Any]:
    result = dict(DEFAULTS)
    for key, raw in value.items():
        if key not in DEFAULTS or key in {"video_width", "video_height"}:
            continue
        if key == "recording_enabled":
            if isinstance(raw, bool):
                result[key] = raw
            elif isinstance(raw, str) and raw.strip().lower() in {"true", "false", "1", "0"}:
                result[key] = raw.strip().lower() in {"true", "1"}
            continue
        low, high = RANGES.get(key, (None, None))
        try:
            number = float(raw) if key == "alert_confidence" else int(raw)
        except (TypeError, ValueError):
            continue
        if low is not None:
            number = max(low, min(high, number))
        result[key] = round(number, 3) if key == "alert_confidence" else int(number)
    return result


class SettingsStore:
    def __init__(self, path: str | None = None):
        self.path = Path(path or os.getenv("PI_SETTINGS_PATH", "/var/lib/mqttiot/settings.json"))
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._value = normalize_settings({})
        self._load()

    def _load(self) -> None:
        try:
            stored = json.loads(self.path.read_text(encoding="utf-8"))
            if isinstance(stored, dict):
                # Migrate the previous factory default once. Preserve any other
                # operator-selected bitrate, while reducing the old 4 Mbps default.
                migrated = stored.get("video_bitrate") == 4_000_000
                if migrated:
                    stored["video_bitrate"] = DEFAULTS["video_bitrate"]
                # Persisted UI changes override environment-provided first-run defaults.
                self._value = normalize_settings({**DEFAULTS, **stored})
                if migrated:
                    self._save_locked()
        except FileNotFoundError:
            self._save_locked()
        except (OSError, json.JSONDecodeError):
            self._save_locked()

    def _save_locked(self) -> None:
        temporary = self.path.with_suffix(".tmp")
        temporary.write_text(
            json.dumps(self._value, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        os.chmod(temporary, 0o600)
        temporary.replace(self.path)

    def get(self) -> dict[str, Any]:
        with self._lock:
            return dict(self._value)

    def update(self, changes: dict[str, Any]) -> tuple[dict[str, Any], list[str]]:
        with self._lock:
            old = dict(self._value)
            self._value = normalize_settings({**old, **changes})
            self._save_locked()
            restart_keys = {"video_fps", "video_bitrate", "segment_seconds", "inference_workers"}
            restart_required = [
                key for key in changes
                if key in restart_keys and self._value[key] != old[key]
            ]
            return dict(self._value), restart_required

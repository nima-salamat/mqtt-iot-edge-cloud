from __future__ import annotations

import os
import threading
from datetime import timedelta
from typing import Any

import django
from django.db import close_old_connections, connection
from django.utils import timezone

_DJANGO_READY = False
_DJANGO_LOCK = threading.Lock()


def setup_django() -> None:
    global _DJANGO_READY
    if _DJANGO_READY:
        return
    with _DJANGO_LOCK:
        if _DJANGO_READY:
            return
        os.environ.setdefault("DJANGO_SETTINGS_MODULE", "iotlab.settings")
        django.setup()
        _DJANGO_READY = True


def _db_call(callback):
    setup_django()
    close_old_connections()
    try:
        return callback()
    finally:
        close_old_connections()


def get_snapshot() -> dict[str, Any]:
    def query():
        from dashboard.models import Alarm, Device, DeviceLog, VideoSegment

        devices = list(Device.objects.select_related("owner").order_by("name", "device_id"))
        alarms = Alarm.objects.select_related("device", "device__owner")
        logs = DeviceLog.objects.select_related("device", "device__owner")
        segments = VideoSegment.objects.select_related("device")
        now = timezone.now()
        online_count = Device.objects.filter(
            is_online=True, last_heartbeat__gte=now - timedelta(seconds=50)
        ).count()
        return {
            "devices": [
                {
                    "device_id": device.device_id,
                    "name": device.name,
                    "owner": device.owner.get_username() if device.owner_id else "Unassigned",
                    "is_online": bool(device.is_online and device.last_heartbeat and
                                      device.last_heartbeat >= now - timedelta(seconds=50)),
                    "last_seen": device.last_seen,
                    "last_heartbeat": device.last_heartbeat,
                    "video_fps": device.reported_video_fps,
                    "inference_fps": device.reported_inference_fps,
                    "model_loaded": device.model_loaded,
                    "state": device.current_state if isinstance(device.current_state, dict) else {},
                }
                for device in devices
            ],
            "device_count": len(devices),
            "online_count": online_count,
            "offline_count": max(0, len(devices) - online_count),
            "alarm_count": alarms.count(),
            "unacknowledged_count": alarms.filter(acknowledged=False).count(),
            "log_count": logs.count(),
            "error_count": logs.filter(level="error").count(),
            "video_segment_count": segments.count(),
            "video_bytes": sum(segments.values_list("size_bytes", flat=True)),
            "latest_alarms": [
                {
                    "id": row.pk,
                    "alert_id": row.alert_id,
                    "device_id": row.device_id,
                    "device_name": row.device.name,
                    "owner": row.device.owner.get_username() if row.device.owner_id else "Unassigned",
                    "event_type": row.event_type,
                    "confidence": row.confidence,
                    "detected_at": row.detected_at,
                    "acknowledged": row.acknowledged,
                    "has_snapshot": bool(row.snapshot),
                }
                for row in alarms.order_by("-detected_at")[:12]
            ],
            "latest_logs": [
                {
                    "created_at": row.created_at,
                    "device_id": row.device_id,
                    "device_name": row.device.name,
                    "owner": row.device.owner.get_username() if row.device.owner_id else "Unassigned",
                    "level": row.level,
                    "category": row.category,
                    "message": row.message,
                }
                for row in logs.order_by("-created_at")[:20]
            ],
            "latest_segments": [
                {
                    "segment_id": row.segment_id,
                    "device_id": row.device_id,
                    "device_name": row.device.name,
                    "owner": row.device.owner.get_username() if row.device.owner_id else "Unassigned",
                    "started_at": row.started_at,
                    "duration_seconds": row.duration_seconds,
                    "size_bytes": row.size_bytes,
                    "uploaded_at": row.uploaded_at,
                }
                for row in segments.order_by("-started_at")[:12]
            ],
        }

    return _db_call(query)


def get_alarms(limit: int = 300, only_unacknowledged: bool = False) -> list[dict[str, Any]]:
    limit = max(10, min(int(limit), 2000))

    def query():
        from dashboard.models import Alarm
        rows = Alarm.objects.select_related("device", "device__owner")
        if only_unacknowledged:
            rows = rows.filter(acknowledged=False)
        return [
            {
                "alert_id": row.alert_id,
                "device_id": row.device_id,
                "device_name": row.device.name,
                "owner": row.device.owner.get_username() if row.device.owner_id else "Unassigned",
                "event_type": row.event_type,
                "confidence": row.confidence,
                "detected_at": row.detected_at,
                "acknowledged": row.acknowledged,
                "has_snapshot": bool(row.snapshot),
                "bbox_count": len(row.bbox) if isinstance(row.bbox, list) else 0,
                "model": (row.details or {}).get("model", "—"),
            }
            for row in rows.order_by("-detected_at")[:limit]
        ]

    return _db_call(query)


def get_logs(limit: int = 300, minimum_level: str = "all") -> list[dict[str, Any]]:
    limit = max(10, min(int(limit), 2000))

    def query():
        from dashboard.models import DeviceLog
        rows = DeviceLog.objects.select_related("device", "device__owner")
        if minimum_level != "all":
            levels = {
                "debug": ["debug", "info", "warning", "error"],
                "info": ["info", "warning", "error"],
                "warning": ["warning", "error"],
                "error": ["error"],
            }.get(minimum_level, ["debug", "info", "warning", "error"])
            rows = rows.filter(level__in=levels)
        return [
            {
                "created_at": row.created_at,
                "device_id": row.device_id,
                "device_name": row.device.name,
                "owner": row.device.owner.get_username() if row.device.owner_id else "Unassigned",
                "level": row.level,
                "category": row.category,
                "message": row.message,
                "details": row.details if isinstance(row.details, dict) else {},
            }
            for row in rows.order_by("-created_at")[:limit]
        ]

    return _db_call(query)


def get_database_status() -> tuple[bool, str]:
    def query():
        with connection.cursor() as cursor:
            cursor.execute("SELECT 1")
            cursor.fetchone()
        return True, f"{connection.vendor} connected"

    try:
        return _db_call(query)
    except Exception as exc:
        return False, str(exc)

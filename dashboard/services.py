"""Shared, ownership-scoped deletion of stored IoT evidence."""
from __future__ import annotations

from typing import Any

from .models import Alarm, DeviceLog, VideoSegment


def _delete_queryset_files(queryset, field_name: str) -> tuple[int, int]:
    rows = list(queryset.only("pk", field_name))
    removed_bytes = 0
    for row in rows:
        stored_file = getattr(row, field_name)
        if not stored_file or not stored_file.name:
            continue
        try:
            removed_bytes += int(stored_file.size or 0)
        except (OSError, ValueError):
            pass
        stored_file.storage.delete(stored_file.name)
    if rows:
        queryset.model.objects.filter(pk__in=[row.pk for row in rows]).delete()
    return len(rows), removed_bytes


def cleanup_iot_data(kind: str, devices) -> dict[str, Any]:
    """Delete selected records only for the supplied, already-authorized devices."""
    if kind not in {"alarms", "videos", "logs", "all"}:
        raise ValueError("نوع داده برای پاک‌سازی معتبر نیست.")

    result: dict[str, Any] = {
        "removed_alarms": 0,
        "removed_videos": 0,
        "removed_logs": 0,
        "removed_bytes": 0,
    }

    if kind in {"alarms", "all"}:
        result["removed_alarms"], size = _delete_queryset_files(
            Alarm.objects.filter(device__in=devices), "snapshot"
        )
        result["removed_bytes"] += size

    if kind in {"videos", "all"}:
        result["removed_videos"], size = _delete_queryset_files(
            VideoSegment.objects.filter(device__in=devices), "file"
        )
        result["removed_bytes"] += size

    if kind in {"logs", "all"}:
        logs = DeviceLog.objects.filter(device__in=devices)
        result["removed_logs"] = logs.count()
        logs.delete()

    result["removed_count"] = (
        result["removed_alarms"] + result["removed_videos"] + result["removed_logs"]
    )
    return result

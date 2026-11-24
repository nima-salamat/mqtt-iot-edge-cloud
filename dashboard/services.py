"""Shared, ownership-scoped deletion of stored IoT evidence."""
from __future__ import annotations

import logging
from typing import Any

from .models import Alarm, DeviceLog, VideoSegment

logger = logging.getLogger(__name__)


def _delete_queryset_files(queryset, field_name: str) -> tuple[int, int]:
    rows = list(queryset.only("pk", field_name))
    removed_ids = []
    removed_bytes = 0
    failures = []

    for row in rows:
        stored_file = getattr(row, field_name)
        if not stored_file or not stored_file.name:
            removed_ids.append(row.pk)
            continue

        file_name = stored_file.name
        try:
            try:
                removed_bytes += max(0, int(stored_file.size or 0))
            except (AttributeError, NotImplementedError, OSError, TypeError, ValueError):
                logger.debug(
                    "Could not determine size for stored IoT file %s",
                    file_name,
                    exc_info=True,
                )

            # FileField.delete(save=False) mutates the model field before the
            # row is deleted. Use storage directly so failed removals keep a
            # valid database reference for the next cleanup attempt.
            stored_file.storage.delete(file_name)
            removed_ids.append(row.pk)
        except FileNotFoundError:
            # A missing object is already in the desired state.
            removed_ids.append(row.pk)
        except Exception:
            failures.append(row.pk)
            logger.exception(
                "Could not delete stored IoT file %s for row %s",
                file_name,
                row.pk,
            )

    if removed_ids:
        queryset.model.objects.filter(pk__in=removed_ids).delete()

    if failures:
        raise OSError(
            f"Could not delete {len(failures)} stored file(s); "
            "their database rows were retained for retry."
        )

    return len(removed_ids), removed_bytes


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

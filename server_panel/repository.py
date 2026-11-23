from __future__ import annotations

import json
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
    """Initialize Django once so Streamlit can read the existing project models."""
    global _DJANGO_READY
    if _DJANGO_READY:
        return

    with _DJANGO_LOCK:
        if _DJANGO_READY:
            return
        os.environ.setdefault("DJANGO_SETTINGS_MODULE", "iotlab.settings")
        django.setup()
        # WAL allows the Streamlit reader and MQTT listener to access the same
        # small SQLite database with fewer reader/writer lock conflicts.
        if connection.vendor == "sqlite":
            with connection.cursor() as cursor:
                cursor.execute("PRAGMA journal_mode=WAL")
                cursor.execute("PRAGMA busy_timeout=30000")
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
        from dashboard.models import Device, Telemetry

        devices = list(
            Device.objects.values(
                "device_id", "name", "kind", "is_online", "last_seen", "current_state"
            )
        )
        total = Telemetry.objects.count()
        telemetry_count = Telemetry.objects.filter(topic__endswith="/telemetry").count()
        event_count = Telemetry.objects.filter(topic__endswith="/events").count()
        ack_count = Telemetry.objects.filter(topic__endswith="/ack").count()
        latest = list(Telemetry.objects.select_related("device").order_by("-received_at")[:12])
        recent = [
            {
                "device_id": row.device_id,
                "device_name": row.device.name,
                "topic": row.topic,
                "channel": row.topic.rsplit("/", 1)[-1],
                "payload": row.payload,
                "qos": row.qos,
                "received_at": row.received_at,
            }
            for row in latest
        ]
        last_seen_values = [d["last_seen"] for d in devices if d["last_seen"]]
        latest_device_seen = max(last_seen_values) if last_seen_values else None
        return {
            "devices": devices,
            "total_messages": total,
            "telemetry_count": telemetry_count,
            "event_count": event_count,
            "ack_count": ack_count,
            "recent_messages": recent,
            "last_seen": latest_device_seen,
        }

    return _db_call(query)


def get_telemetry(hours: int = 24, limit: int = 1000) -> list[dict[str, Any]]:
    hours = max(1, min(int(hours), 24 * 30))
    limit = max(10, min(int(limit), 5000))

    def query():
        from dashboard.models import Telemetry

        since = timezone.now() - timedelta(hours=hours)
        rows = (
            Telemetry.objects.filter(
                topic__endswith="/telemetry",
                received_at__gte=since,
            )
            .order_by("-received_at")[:limit]
        )
        values = []
        for row in reversed(list(rows)):
            payload = row.payload if isinstance(row.payload, dict) else {}
            temperature = payload.get("temperature_c")
            humidity = payload.get("humidity_percent")
            if not isinstance(temperature, (int, float)) and not isinstance(humidity, (int, float)):
                continue
            values.append(
                {
                    "time": row.received_at,
                    "temperature_c": temperature,
                    "humidity_percent": humidity,
                    "device_id": row.device_id,
                    "topic": row.topic,
                }
            )
        return values

    return _db_call(query)


def get_messages(
    channel: str = "all",
    search: str = "",
    limit: int = 300,
) -> list[dict[str, Any]]:
    limit = max(10, min(int(limit), 2000))

    def query():
        from dashboard.models import Telemetry

        rows = Telemetry.objects.select_related("device").order_by("-received_at")
        if channel in {"events", "telemetry", "ack"}:
            rows = rows.filter(topic__endswith=f"/{channel}")

        results = []
        for row in rows[:2000]:
            payload = row.payload if isinstance(row.payload, dict) else {}
            searchable = f"{row.topic} {json.dumps(payload, ensure_ascii=False)}".casefold()
            if search and search.casefold() not in searchable:
                continue
            results.append(
                {
                    "device_id": row.device_id,
                    "device": row.device.name,
                    "channel": row.topic.rsplit("/", 1)[-1],
                    "topic": row.topic,
                    "payload": payload,
                    "qos": row.qos,
                    "received_at": row.received_at,
                }
            )
            if len(results) >= limit:
                break
        return results

    return _db_call(query)


def get_database_status() -> tuple[bool, str]:
    def query():
        with connection.cursor() as cursor:
            cursor.execute("SELECT 1")
            cursor.fetchone()
        return True, "SQLite connected"

    try:
        return _db_call(query)
    except Exception as exc:
        return False, str(exc)

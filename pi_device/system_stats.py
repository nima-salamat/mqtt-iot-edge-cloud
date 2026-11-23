"""Lightweight Raspberry Pi health metrics using only the Python standard library."""
from __future__ import annotations

import os
import shutil
from pathlib import Path
from typing import Any


def _read_temperature(
    thermal_root: Path = Path("/sys/class/thermal"),
    hwmon_root: Path = Path("/sys/class/hwmon"),
) -> float | None:
    candidates = list(thermal_root.glob("thermal_zone*/temp"))
    candidates.extend(hwmon_root.glob("hwmon*/temp*_input"))
    for path in candidates:
        try:
            raw = float(path.read_text(encoding="ascii").strip())
            value = raw / 1000.0 if abs(raw) > 1000 else raw
            if -10.0 <= value <= 150.0:
                return round(value, 1)
        except (OSError, ValueError):
            continue
    return None


def _read_memory(meminfo_path: Path = Path("/proc/meminfo")) -> dict[str, Any] | None:
    values: dict[str, int] = {}
    try:
        for line in meminfo_path.read_text(encoding="ascii").splitlines():
            key, separator, rest = line.partition(":")
            if not separator:
                continue
            fields = rest.strip().split()
            if fields:
                values[key] = int(fields[0]) * 1024
    except (OSError, ValueError):
        return None

    total = values.get("MemTotal", 0)
    available = values.get("MemAvailable")
    if available is None:
        available = sum(values.get(key, 0) for key in ("MemFree", "Buffers", "Cached"))
    if total <= 0:
        return None
    available = max(0, min(total, available))
    used = total - available
    return {
        "total_bytes": total,
        "used_bytes": used,
        "available_bytes": available,
        "used_percent": round(used * 100.0 / total, 1),
    }


def _read_storage(data_path: str | os.PathLike[str] | None = None) -> dict[str, Any] | None:
    target = Path(data_path or os.getenv("PI_DATA_DIR", "/var/lib/mqttiot"))
    if not target.exists():
        target = Path("/")
    try:
        usage = shutil.disk_usage(target)
    except OSError:
        return None
    return {
        "path": str(target),
        "total_bytes": usage.total,
        "used_bytes": usage.used,
        "free_bytes": usage.free,
        "used_percent": round(usage.used * 100.0 / usage.total, 1) if usage.total else 0.0,
    }


def collect_system_stats(
    data_path: str | os.PathLike[str] | None = None,
    *,
    meminfo_path: Path = Path("/proc/meminfo"),
    thermal_root: Path = Path("/sys/class/thermal"),
    hwmon_root: Path = Path("/sys/class/hwmon"),
) -> dict[str, Any]:
    """Return best-effort metrics; missing sysfs/procfs data never breaks the camera API."""
    return {
        "temperature_c": _read_temperature(thermal_root, hwmon_root),
        "memory": _read_memory(meminfo_path),
        "storage": _read_storage(data_path),
    }

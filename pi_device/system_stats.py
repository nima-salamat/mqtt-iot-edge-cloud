"""Lightweight Raspberry Pi health metrics using only the Python standard library."""
from __future__ import annotations

import os
import shutil
import threading
from pathlib import Path
from typing import Any


_CPU_LOCK = threading.Lock()
_CPU_LAST_SAMPLE: tuple[int, int] | None = None


def _cpu_totals(stat_line: str) -> tuple[int, int] | None:
    """Return total and idle CPU ticks from the first /proc/stat line."""
    fields = str(stat_line or "").split()
    if not fields or fields[0] != "cpu" or len(fields) < 5:
        return None
    try:
        values = [int(value) for value in fields[1:]]
    except ValueError:
        return None
    total = sum(values)
    idle = values[3] + (values[4] if len(values) > 4 else 0)
    return total, idle


def _cpu_percent_from_samples(
    previous: tuple[int, int] | None,
    current: tuple[int, int] | None,
) -> float | None:
    if previous is None or current is None:
        return None
    total_delta = current[0] - previous[0]
    idle_delta = current[1] - previous[1]
    if total_delta <= 0:
        return None
    return round(max(0.0, min(100.0, 100.0 * (1.0 - idle_delta / total_delta))), 1)


def _read_cpu_usage(stat_path: Path = Path("/proc/stat")) -> float | None:
    global _CPU_LAST_SAMPLE
    try:
        lines = stat_path.read_text(encoding="ascii").splitlines()
        sample = _cpu_totals(lines[0] if lines else "")
    except (OSError, ValueError):
        return None
    if sample is None:
        return None
    with _CPU_LOCK:
        percent = _cpu_percent_from_samples(_CPU_LAST_SAMPLE, sample)
        _CPU_LAST_SAMPLE = sample
        return percent


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
    cpu_stat_path: Path = Path("/proc/stat"),
) -> dict[str, Any]:
    """Return best-effort metrics; missing sysfs/procfs data never breaks the camera API."""
    return {
        "cpu_percent": _read_cpu_usage(cpu_stat_path),
        "temperature_c": _read_temperature(thermal_root, hwmon_root),
        "memory": _read_memory(meminfo_path),
        "storage": _read_storage(data_path),
    }

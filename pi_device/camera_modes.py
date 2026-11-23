"""Sensor mode selection for Picamera2 cameras."""
from __future__ import annotations

from typing import Any


def select_sensor_mode(
    sensor_modes: list[dict[str, Any]],
    width: int,
    height: int,
    requested_fps: float,
    auto: bool = True,
) -> tuple[dict[str, Any] | None, float]:
    """Choose a sensor mode that meets the output size and requested cadence.

    Auto mode chooses the fastest compatible sensor mode. Manual mode chooses
    the smallest mode that can sustain the requested FPS, or the fastest
    compatible mode when the requested cadence is unsupported.
    """
    usable: list[tuple[dict[str, Any], int, int, float]] = []
    for raw in sensor_modes or []:
        try:
            size = raw.get("size")
            mode_width, mode_height = int(size[0]), int(size[1])
            fps = float(raw.get("fps", 0))
        except (TypeError, ValueError, IndexError, KeyError):
            continue
        if mode_width <= 0 or mode_height <= 0 or fps <= 0:
            continue
        usable.append((dict(raw), mode_width, mode_height, fps))

    if not usable:
        return None, max(1.0, float(requested_fps or 30.0))

    compatible = [
        item for item in usable
        if item[1] >= int(width) and item[2] >= int(height)
    ] or usable

    if auto:
        selected = max(
            compatible,
            key=lambda item: (item[3], -item[1] * item[2]),
        )
        return selected[0], selected[3]

    target = max(1.0, float(requested_fps))
    supporting = [item for item in compatible if item[3] >= target]
    if supporting:
        selected = min(
            supporting,
            key=lambda item: (item[1] * item[2], item[3]),
        )
        return selected[0], target

    selected = max(
        compatible,
        key=lambda item: (item[3], -item[1] * item[2]),
    )
    return selected[0], selected[3]

"""Sensor mode selection for Picamera2 cameras."""
from __future__ import annotations

from typing import Any
import re


def select_sensor_mode(
    sensor_modes: list[dict[str, Any]],
    width: int,
    height: int,
    requested_fps: float,
    auto: bool = True,
) -> tuple[dict[str, Any] | None, float]:
    """Choose a sensor mode that meets the output size and requested cadence.

    Auto mode selects the fastest compatible sensor readout mode while capping
    the stream/encoder cadence at the requested FPS. Manual mode selects the
    smallest mode that can sustain the requested FPS, or the fastest compatible
    mode when the requested cadence is unsupported.
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

    target = max(1.0, float(requested_fps))
    if auto:
        selected = max(
            compatible,
            key=lambda item: (
                item[3],
                -item[1] * item[2],
                2 if item[0].get("fourcc") in {"MJPG", "H264", "AVC1"} else 1,
            ),
        )
        # Auto mode may use the sensor's fastest compatible readout mode,
        # but it must not silently raise the requested stream/encoder cadence.
        return selected[0], min(target, selected[3])

    supporting = [item for item in compatible if item[3] >= target]
    if supporting:
        selected = min(
            supporting,
            key=lambda item: (
                item[1] * item[2],
                -item[3],
                -(
                    2 if item[0].get("fourcc") in {"MJPG", "H264", "AVC1"}
                    else 1
                ),
            ),
        )
        return selected[0], target

    selected = max(
        compatible,
        key=lambda item: (item[3], -item[1] * item[2]),
    )
    return selected[0], selected[3]



def parse_v4l2_modes(output: str) -> list[dict[str, Any]]:
    """Parse discrete formats, resolutions and FPS entries from v4l2-ctl."""
    found: dict[tuple[tuple[int, int], str], float] = {}
    format_pattern = re.compile(
        r"^\s*\[\d+\]:\s*'([^']+)'[^\n]*\n(.*?)(?=^\s*\[\d+\]:|\Z)",
        re.IGNORECASE | re.DOTALL | re.MULTILINE,
    )
    size_pattern = re.compile(
        r"Size:\s*Discrete\s+(\d+)\s*x\s*(\d+)(.*?)(?=\n\s*Size:|\Z)",
        re.IGNORECASE | re.DOTALL,
    )
    for format_match in format_pattern.finditer(str(output or "")):
        fourcc = format_match.group(1).strip().upper()
        for match in size_pattern.finditer(format_match.group(2)):
            try:
                width, height = int(match.group(1)), int(match.group(2))
            except (TypeError, ValueError):
                continue
            section = match.group(3)
            rates = [
                float(value)
                for value in re.findall(
                    r"\(([0-9]+(?:\.[0-9]+)?)\s*fps\)",
                    section,
                    re.IGNORECASE,
                )
            ]
            if not rates:
                intervals = re.findall(
                    r"Interval:\s*Discrete\s+([0-9]+(?:\.[0-9]+)?)s",
                    section,
                    re.IGNORECASE,
                )
                rates = [1.0 / float(value) for value in intervals if float(value) > 0]
            if width > 0 and height > 0 and rates:
                key = ((width, height), fourcc)
                found[key] = max(found.get(key, 0.0), max(rates))
    return [
        {"size": size, "fps": fps, "fourcc": fourcc, "source": "v4l2"}
        for ((size, fourcc), fps) in sorted(
            found.items(),
            key=lambda item: (item[0][0][0] * item[0][0][1], item[1], item[0][1]),
        )
    ]

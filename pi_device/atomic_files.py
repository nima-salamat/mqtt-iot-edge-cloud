"""Crash-safe local file writes used by the Raspberry Pi agent."""
from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Any


def atomic_write_bytes(path: str | Path, data: bytes, *, mode: int | None = 0o600) -> None:
    """Write bytes to a same-directory temporary file, then atomically replace."""
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.is_symlink():
        raise OSError(f"Refusing to replace symlink: {target}")

    fd, temporary_name = tempfile.mkstemp(
        prefix=f".{target.name}.",
        suffix=".tmp",
        dir=str(target.parent),
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            if mode is not None:
                os.fchmod(handle.fileno(), mode)
            os.fsync(handle.fileno())

        os.replace(temporary, target)
        try:
            directory_fd = os.open(
                str(target.parent),
                os.O_RDONLY | getattr(os, "O_DIRECTORY", 0),
            )
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        except OSError:
            # Some filesystems do not support fsync on directories.
            pass
    finally:
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass


def atomic_write_text(
    path: str | Path,
    value: str,
    *,
    encoding: str = "utf-8",
    mode: int | None = 0o600,
) -> None:
    atomic_write_bytes(path, value.encode(encoding), mode=mode)


def atomic_write_json(
    path: str | Path,
    value: Any,
    *,
    indent: int | None = None,
    mode: int | None = 0o600,
) -> None:
    atomic_write_text(
        path,
        json.dumps(value, ensure_ascii=False, indent=indent),
        mode=mode,
    )

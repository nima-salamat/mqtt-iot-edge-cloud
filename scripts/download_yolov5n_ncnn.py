#!/usr/bin/env python3
"""Download the pinned YOLOv5n NCNN assets and verify their Git blob SHA-1."""
from __future__ import annotations

import hashlib
import os
import tempfile
import urllib.request
from pathlib import Path

REVISION = "85e5a7cd8236b71391a3f7ffd021a076f102024c"
BASE_URL = (
    "https://raw.githubusercontent.com/nihui/ncnn-webassembly-yolov5/"
    f"{REVISION}/assets"
)
ASSETS = {
    "yolov5n.ncnn.param": {
        "git_blob_sha1": "cec838e1e6446bfc722fb9eaa869ffeb84603cb4",
        "size": 11796,
    },
    "yolov5n.ncnn.bin": {
        "git_blob_sha1": "5572c14a35bea6f8be492978d3bc1923bb476008",
        "size": 3746084,
    },
}


def git_blob_sha1(path: Path) -> str:
    size = path.stat().st_size
    digest = hashlib.sha1()
    digest.update(f"blob {size}\0".encode("ascii"))
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def fetch_asset(name: str, destination: Path, expected: dict) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.is_file():
        if (
            destination.stat().st_size == expected["size"]
            and git_blob_sha1(destination) == expected["git_blob_sha1"]
        ):
            print(f"Already verified: {destination}")
            return

    temporary_path = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="wb", prefix=f".{name}.", suffix=".download",
            dir=destination.parent, delete=False,
        ) as temporary:
            temporary_path = Path(temporary.name)
            request = urllib.request.Request(
                f"{BASE_URL}/{name}",
                headers={"User-Agent": "mqtt-iot-alarm-camera-installer"},
            )
            with urllib.request.urlopen(request, timeout=60) as response:
                while True:
                    block = response.read(1024 * 1024)
                    if not block:
                        break
                    temporary.write(block)
            temporary.flush()
            os.fsync(temporary.fileno())

        actual_size = temporary_path.stat().st_size
        actual_sha = git_blob_sha1(temporary_path)
        if actual_size != expected["size"] or actual_sha != expected["git_blob_sha1"]:
            raise RuntimeError(
                f"Integrity check failed for {name}: "
                f"size={actual_size}, sha1={actual_sha}"
            )

        os.chmod(temporary_path, 0o644)
        temporary_path.replace(destination)
        print(f"Downloaded and verified {destination} ({actual_size:,} bytes)")
    finally:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)


def main() -> int:
    target = Path(
        os.getenv(
            "MODEL_DIR",
            Path(__file__).resolve().parents[1] / "pi_device" / "models",
        )
    )
    target.mkdir(parents=True, exist_ok=True)
    for filename, metadata in ASSETS.items():
        fetch_asset(filename, target / filename, metadata)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

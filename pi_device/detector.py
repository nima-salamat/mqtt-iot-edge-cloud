"""YOLOv5n NCNN person-only detector matching the pinned upstream 320px graph."""
from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Any

import cv2
import ncnn
import numpy as np

log = logging.getLogger(__name__)

TARGET_SIZE = 320
MAX_STRIDE = 64
OUTPUTS = (
    ("out0", 8, ((10, 13), (16, 30), (33, 23))),
    ("out1", 16, ((30, 61), (62, 45), (59, 119))),
    ("out2", 32, ((116, 90), (156, 198), (373, 326))),
)


def sigmoid(values: np.ndarray) -> np.ndarray:
    return 1.0 / (1.0 + np.exp(-np.clip(values, -40.0, 40.0)))


def letterbox_rgb(frame: np.ndarray) -> tuple[np.ndarray, float, int, int]:
    """Match the upstream C++: scale the long edge to 320, then pad to stride 64."""
    height, width = frame.shape[:2]
    if width < 1 or height < 1:
        raise ValueError("Frame dimensions must be non-zero")
    scale = TARGET_SIZE / float(max(width, height))
    resized_w = max(1, int(width * scale))
    resized_h = max(1, int(height * scale))
    resized = cv2.resize(frame, (resized_w, resized_h), interpolation=cv2.INTER_LINEAR)

    padded_w = ((resized_w + MAX_STRIDE - 1) // MAX_STRIDE) * MAX_STRIDE
    padded_h = ((resized_h + MAX_STRIDE - 1) // MAX_STRIDE) * MAX_STRIDE
    pad_x = (padded_w - resized_w) // 2
    pad_y = (padded_h - resized_h) // 2

    canvas = np.full((padded_h, padded_w, 3), 114, dtype=np.uint8)
    canvas[pad_y:pad_y + resized_h, pad_x:pad_x + resized_w] = resized
    return canvas, scale, pad_x, pad_y


class YoloV5nDetector:
    """CPU inference wrapper for the official NCNN YOLOv5n COCO model."""

    def __init__(self, model_dir=None, confidence=0.45, nms=0.45, num_threads=None):
        root = Path(model_dir or os.getenv("MODEL_DIR", Path(__file__).parent / "models"))
        self.param_path = root / "yolov5n.ncnn.param"
        self.bin_path = root / "yolov5n.ncnn.bin"
        if not self.param_path.is_file() or not self.bin_path.is_file():
            raise FileNotFoundError(
                f"YOLOv5n NCNN weights are missing from {root}; run scripts/install_pi.sh."
            )

        self.confidence = float(confidence)
        self.nms_threshold = float(nms)
        self.net = ncnn.Net()
        self.net.opt.use_vulkan_compute = False
        self.net.opt.num_threads = int(
            num_threads or max(1, min(3, (os.cpu_count() or 2) - 1))
        )
        self.net.opt.use_fp16_packed = True
        self.net.opt.use_fp16_storage = True
        self.net.opt.use_fp16_arithmetic = False

        if self.net.load_param(str(self.param_path)) != 0:
            raise RuntimeError("NCNN failed to load yolov5n.ncnn.param")
        if self.net.load_model(str(self.bin_path)) != 0:
            raise RuntimeError("NCNN failed to load yolov5n.ncnn.bin")
        log.info("Loaded YOLOv5n NCNN model from %s (threads=%s)", root, self.net.opt.num_threads)

    @staticmethod
    def _output_array(output, expected_channels: int) -> np.ndarray:
        # NCNN channels can have cstep padding. Mat.numpy() exposes the actual
        # strides; making that view contiguous before reshaping avoids reading
        # padding bytes as model values (np.asarray(...).reshape() is unsafe).
        array = np.asarray(output.numpy(), dtype=np.float32)
        if array.ndim != 3 or array.shape[0] != expected_channels:
            raise RuntimeError(
                f"Unexpected YOLO output layout {array.shape}; expected "
                f"({expected_channels}, grid_h, grid_w)"
            )
        return np.ascontiguousarray(array)

    def detect(self, rgb_frame: np.ndarray) -> list[dict[str, Any]]:
        if rgb_frame is None or rgb_frame.size == 0:
            return []
        if rgb_frame.ndim != 3 or rgb_frame.shape[2] != 3:
            raise ValueError("YOLOv5n expects an HxWx3 RGB frame")

        source_h, source_w = rgb_frame.shape[:2]
        canvas, scale, pad_x, pad_y = letterbox_rgb(rgb_frame)
        input_h, input_w = canvas.shape[:2]
        input_pixels = np.ascontiguousarray(canvas)
        input_mat = ncnn.Mat.from_pixels(
            input_pixels, ncnn.Mat.PixelType.PIXEL_RGB, input_w, input_h
        )
        input_mat.substract_mean_normalize(
            [0.0, 0.0, 0.0],
            [1.0 / 255.0, 1.0 / 255.0, 1.0 / 255.0],
        )

        extractor = self.net.create_extractor()
        if extractor.input("in0", input_mat) != 0:
            raise RuntimeError("NCNN rejected YOLOv5n input blob 'in0'")

        boxes: list[list[float]] = []
        scores: list[float] = []
        for output_name, stride, anchors in OUTPUTS:
            result = extractor.extract(output_name)
            if not isinstance(result, tuple) or result[0] != 0:
                code = result[0] if isinstance(result, tuple) else "unknown"
                raise RuntimeError(f"NCNN could not extract {output_name}: {code}")

            output = result[1]
            channels = self._output_array(output, 255)
            feature = channels.reshape(3, 85, output.h, output.w)

            for anchor_index, (anchor_w, anchor_h) in enumerate(anchors):
                class_logits = feature[anchor_index, 5:85]
                best_class = np.argmax(class_logits, axis=0)
                person_scores = (
                    sigmoid(feature[anchor_index, 4])
                    * sigmoid(feature[anchor_index, 5])
                )
                # The NCNN C++ reference emits the highest-scoring COCO class.
                candidate_y, candidate_x = np.where(
                    (person_scores >= self.confidence) & (best_class == 0)
                )
                for grid_y, grid_x in zip(candidate_y.tolist(), candidate_x.tolist()):
                    dx, dy, dw, dh = (
                        sigmoid(feature[anchor_index, channel, grid_y, grid_x])
                        for channel in range(4)
                    )
                    center_x = (dx * 2.0 - 0.5 + grid_x) * stride
                    center_y = (dy * 2.0 - 0.5 + grid_y) * stride
                    box_w = (dw * 2.0) ** 2 * anchor_w
                    box_h = (dh * 2.0) ** 2 * anchor_h

                    x1 = (center_x - box_w / 2.0 - pad_x) / scale
                    y1 = (center_y - box_h / 2.0 - pad_y) / scale
                    x2 = (center_x + box_w / 2.0 - pad_x) / scale
                    y2 = (center_y + box_h / 2.0 - pad_y) / scale

                    x1 = max(0.0, min(source_w - 1.0, x1))
                    y1 = max(0.0, min(source_h - 1.0, y1))
                    x2 = max(0.0, min(source_w - 1.0, x2))
                    y2 = max(0.0, min(source_h - 1.0, y2))
                    if x2 <= x1 or y2 <= y1:
                        continue

                    boxes.append([x1, y1, x2 - x1, y2 - y1])
                    scores.append(float(person_scores[grid_y, grid_x]))

        if not boxes:
            return []
        selected = cv2.dnn.NMSBoxes(
            boxes, scores, self.confidence, self.nms_threshold
        )
        if selected is None or len(selected) == 0:
            return []

        detections = []
        for index in np.asarray(selected).reshape(-1).tolist():
            x, y, width, height = boxes[index]
            detections.append({
                "bbox": [
                    int(round(x)),
                    int(round(y)),
                    int(round(x + width)),
                    int(round(y + height)),
                ],
                "confidence": round(scores[index], 4),
                "class_id": 0,
                "class": "person",
            })
        return detections

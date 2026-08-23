"""YOLOv5 post-processing over raw RKNN head outputs.

Calibrated against the C demo (scripts/verify_detector_m2.py, bus.jpg
ground truth). Findings baked in:

- The ReLU-activation RK3576 export bakes the sigmoid into xy/obj/class —
  outputs arrive already in 0..1, so no sigmoid is applied here (the
  apply_sigmoid experiment lost 1.476 vs 0.366 and was deleted).
- wh is NOT exp-decoded: the C demo's disassembly shows wh = (2*wh)^2 * anchor
  (YOLOv8-style), verified empirically (IoU 0.74-0.95, conf within 0.013).
- The model expects RGB input; frames arrive BGR from the ingest pipeline,
  so detect() flips channels before inference.
"""

import logging
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import numpy as np

log = logging.getLogger(__name__)

_LABELS_PATH = Path(__file__).resolve().parent / "labels_coco80.txt"
COCO80_LABELS = [
    line.strip() for line in _LABELS_PATH.read_text(encoding="utf-8").splitlines() if line.strip()
]

# Standard YOLOv5 anchors, P3/8 -> P5/32 (public architecture constants).
ANCHORS = [
    [(10, 13), (16, 30), (33, 23)],
    [(30, 61), (62, 45), (59, 119)],
    [(116, 90), (156, 198), (373, 326)],
]
STRIDES = [8, 16, 32]
_INPUT_SIZE = 640


@dataclass
class Detection:
    class_name: str
    confidence: float
    bbox_xyxy: tuple[float, float, float, float]  # (x1, y1, x2, y2) in original frame


class ObjectDetector:
    def __init__(
        self,
        npu_pool,
        conf_threshold: float = 0.25,
        nms_threshold: float = 0.45,
    ):
        self.npu_pool = npu_pool
        self.conf_threshold = conf_threshold
        self.nms_threshold = nms_threshold

    # -- preprocessing ------------------------------------------------------

    def _letterbox(self, frame: np.ndarray) -> tuple[np.ndarray, float, tuple[int, int]]:
        """Aspect-preserving resize + pad to 640x640, nearest-neighbor.

        Fast paths for the cases the ingest actually produces: identity when
        the frame is already 640-wide (1-D row gather, ~0.5 ms vs ~20 ms for
        the general 2-D gather) and stride slicing for integer downscales
        (720p sources, ~free).
        """
        h, w = frame.shape[:2]
        scale = min(_INPUT_SIZE / w, _INPUT_SIZE / h)
        new_w, new_h = int(round(w * scale)), int(round(h * scale))

        if new_w == w and new_h == h:
            resized = frame
        elif w % new_w == 0 and h % new_h == 0 and new_w <= w:
            resized = frame[:: h // new_h, :: w // new_w]
        elif new_w == w:
            ys = (np.linspace(0, h - 1, new_h)).astype(int)
            resized = frame[ys]  # 1-D row gather
        else:
            ys = (np.linspace(0, h - 1, new_h)).astype(int)
            xs = (np.linspace(0, w - 1, new_w)).astype(int)
            resized = frame[ys][:, xs]

        pad_x = (_INPUT_SIZE - new_w) // 2
        pad_y = (_INPUT_SIZE - new_h) // 2
        canvas = np.full((_INPUT_SIZE, _INPUT_SIZE, 3), 114, dtype=np.uint8)
        canvas[pad_y : pad_y + new_h, pad_x : pad_x + new_w] = resized
        return canvas, scale, (pad_x, pad_y)

    # -- decode -------------------------------------------------------------

    def _decode_head(self, output: np.ndarray, anchors, stride: int) -> list[Detection]:
        # output: (1, 255, H, W) with 255 = 3 anchors x (5 + num_classes).
        # xy/obj/class arrive post-sigmoid (baked into the model graph).
        num_classes = len(COCO80_LABELS)
        _, _, h, w = output.shape
        pred = output[0]  # (255, H, W); layout per anchor: xywh, obj, 80 classes

        grid_y, grid_x = np.meshgrid(np.arange(h), np.arange(w), indexing="ij")

        detections: list[Detection] = []
        for a in range(3):
            base = a * (5 + num_classes)
            xy = pred[base : base + 2]  # (2, H, W), post-sigmoid
            # wh decode from the C demo's disassembly: (2*wh)^2 * anchor.
            wh = (pred[base + 2 : base + 4] * 2.0) ** 2
            obj_a = pred[base + 4]
            cls_a = pred[base + 5 : base + 5 + num_classes]  # (C, H, W)
            aw, ah = anchors[a]

            cx = (xy[0] + grid_x) * stride
            cy = (xy[1] + grid_y) * stride
            bw = wh[0] * aw
            bh = wh[1] * ah

            # Filter by objectness first (cheap), then per-cell class argmax
            # over the few surviving cells — avoids materializing the full
            # (C, H, W) obj*cls product and its double reduction per head.
            ys, xs = np.nonzero(obj_a >= self.conf_threshold)
            for y, x in zip(ys, xs):
                scores = obj_a[y, x] * cls_a[:, y, x]
                c = int(np.argmax(scores))
                conf = float(scores[c])
                if conf < self.conf_threshold:
                    continue
                x1 = cx[y, x] - bw[y, x] / 2.0
                y1 = cy[y, x] - bh[y, x] / 2.0
                x2 = cx[y, x] + bw[y, x] / 2.0
                y2 = cy[y, x] + bh[y, x] / 2.0
                detections.append(
                    Detection(
                        class_name=COCO80_LABELS[c],
                        confidence=conf,
                        bbox_xyxy=(x1, y1, x2, y2),
                    )
                )
        return detections

    # -- NMS ----------------------------------------------------------------

    def _nms(self, detections: list[Detection]) -> list[Detection]:
        kept: list[Detection] = []
        by_class: dict[str, list[Detection]] = {}
        for d in detections:
            by_class.setdefault(d.class_name, []).append(d)

        for cls, cands in by_class.items():
            cands.sort(key=lambda d: d.confidence, reverse=True)
            while cands:
                best = cands.pop(0)
                kept.append(best)
                cands = [
                    c
                    for c in cands
                    if self._iou(best.bbox_xyxy, c.bbox_xyxy) <= self.nms_threshold
                ]
        return kept

    @staticmethod
    def _iou(a, b) -> float:
        ax1, ay1, ax2, ay2 = a
        bx1, by1, bx2, by2 = b
        ix1, iy1 = max(ax1, bx1), max(ay1, by1)
        ix2, iy2 = min(ax2, bx2), min(ay2, by2)
        iw, ih = ix2 - ix1, iy2 - iy1
        if iw <= 0 or ih <= 0:
            return 0.0
        inter = iw * ih
        union = (ax2 - ax1) * (ay2 - ay1) + (bx2 - bx1) * (by2 - by1) - inter
        return inter / union if union > 0 else 0.0

    # -- entry point ----------------------------------------------------------

    def detect(
        self, frame: np.ndarray, core: int = 0, profile: bool = False
    ) -> list[Detection] | tuple[list[Detection], dict[str, float]]:
        """Letterbox -> NPU (on ``core``) -> decode all heads -> NMS -> map.

        ``frame`` is expected in BGR (ffmpeg/ingest convention); the model
        was trained on RGB, so channels are flipped before inference.

        Pass ``profile=True`` to also receive a per-stage timing breakdown.
        """
        t0 = time.perf_counter()
        h, w = frame.shape[:2]
        letterboxed, scale, (pad_x, pad_y) = self._letterbox(frame[:, :, ::-1])
        inputs = letterboxed[np.newaxis, :, :, :].astype(np.uint8)
        t1 = time.perf_counter()

        outputs = self.npu_pool.run_detection(inputs, core=core)
        t2 = time.perf_counter()

        raw: list[Detection] = []
        for output, anchors, stride in zip(outputs, ANCHORS, STRIDES):
            raw.extend(self._decode_head(output, anchors, stride))
        t3 = time.perf_counter()

        # Cap before NMS (reference YOLOv5 keeps the top 300): keeps NMS
        # quadratic cost bounded even when a bad mode passes everything.
        raw.sort(key=lambda d: d.confidence, reverse=True)
        raw = raw[:300]

        kept = self._nms(raw)
        t4 = time.perf_counter()

        for d in kept:
            x1, y1, x2, y2 = d.bbox_xyxy
            d.bbox_xyxy = (
                max(0.0, (x1 - pad_x) / scale),
                max(0.0, (y1 - pad_y) / scale),
                min(float(w), (x2 - pad_x) / scale),
                min(float(h), (y2 - pad_y) / scale),
            )
        t5 = time.perf_counter()

        if profile:
            timings = {
                "letterbox_ms": (t1 - t0) * 1000,
                "npu_inference_ms": (t2 - t1) * 1000,
                "decode_ms": (t3 - t2) * 1000,
                "nms_ms": (t4 - t3) * 1000,
                "map_to_frame_ms": (t5 - t4) * 1000,
                "total_ms": (t5 - t0) * 1000,
            }
            return kept, timings
        return kept

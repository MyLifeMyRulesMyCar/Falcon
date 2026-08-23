"""Tests for nvr.inference.detector post-processing — synthetic head outputs,
no NPU calls, no rknnlite import."""

import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from nvr.inference.detector import (
    ANCHORS,
    STRIDES,
    ObjectDetector,
    _LABELS_PATH,
)


class _FakePool:
    def __init__(self, outputs):
        self._outputs = outputs

    def run_detection(self, inputs):
        return self._outputs

    def run_pose(self, crop):
        raise NotImplementedError


def _make_head(h: int, w: int, anchor: int, cls_idx: int, cx_frac: float, cy_frac: float):
    """Build one head output with a single hot cell.

    Sets cell (grid_y, grid_x) -> anchor `anchor`'s objectness and class score
    high (0.99), everything else low (0.01). Box offsets centered at the cell
    with wh logits 0.5 (decodes to (2*0.5)^2 = 1.0 * anchor size).
    """
    num_classes = len(_LABELS_PATH.read_text(encoding="utf-8").splitlines())
    out = np.full((1, 3 * (5 + num_classes), h, w), 0.01, dtype=np.float32)
    grid_y, grid_x = int(round(cy_frac * (h - 1))), int(round(cx_frac * (w - 1)))
    base = anchor * (5 + num_classes)
    out[0, base + 4, grid_y, grid_x] = 0.99              # objectness
    out[0, base + 5 + cls_idx, grid_y, grid_x] = 0.99    # class score
    out[0, base + 0, grid_y, grid_x] = 0.5               # x center (cell center)
    out[0, base + 1, grid_y, grid_x] = 0.5               # y center
    out[0, base + 2, grid_y, grid_x] = 0.5               # wh.x logit -> 1.0
    out[0, base + 3, grid_y, grid_x] = 0.5               # wh.y logit -> 1.0
    return out


def test_decode_head_finds_hot_anchor_cell():
    num_classes = len(_LABELS_PATH.read_text(encoding="utf-8").splitlines())
    # P3 head, 80x80 grid, anchor 1, class "car" (idx 2), cell (20, 30).
    out = _make_head(80, 80, anchor=1, cls_idx=2, cx_frac=30 / 79.0, cy_frac=20 / 79.0)
    dets = ObjectDetector(_FakePool([]))._decode_head(out, ANCHORS[0], STRIDES[0])

    assert len(dets) == 1
    d = dets[0]
    assert d.class_name == "car"
    assert d.confidence > 0.9
    aw, ah = ANCHORS[0][1]
    cx = (30 + 0.5) * STRIDES[0]
    cy = (20 + 0.5) * STRIDES[0]
    x1, y1, x2, y2 = d.bbox_xyxy
    assert abs((x1 + x2) / 2 - cx) < 1e-3
    assert abs((y1 + y2) / 2 - cy) < 1e-3
    # wh logits were 0.5 -> (2*0.5)^2 = 1.0 -> box size = 1.0 * anchor.
    assert abs((x2 - x1) - aw) < 1e-3
    assert abs((y2 - y1) - ah) < 1e-3


def _det(cls, conf, box):
    from nvr.inference.detector import Detection

    return Detection(class_name=cls, confidence=conf, bbox_xyxy=box)


def test_nms_suppresses_duplicate_and_keeps_distinct():
    dets = [
        _det("person", 0.9, (10, 10, 50, 50)),
        _det("person", 0.8, (12, 12, 52, 52)),
        _det("bus", 0.7, (100, 100, 200, 200)),
    ]
    kept = ObjectDetector(_FakePool([]))._nms(dets)
    assert len(kept) == 2
    assert {d.class_name for d in kept} == {"person", "bus"}
    assert kept[0].confidence == 0.9


def test_iou_basic():
    det = ObjectDetector(_FakePool([]))
    a = (0, 0, 10, 10)
    b = (5, 5, 15, 15)
    assert det._iou(a, b) == 25.0 / 175.0
    assert det._iou(a, (20, 20, 30, 30)) == 0.0


def test_letterbox_identity_and_scale():
    det = ObjectDetector(_FakePool([]))
    # 640x640 input -> identity letterbox.
    frame = np.zeros((640, 640, 3), dtype=np.uint8)
    boxed, scale, (px, py) = det._letterbox(frame)
    assert boxed.shape == (640, 640, 3)
    assert scale == 1.0 and (px, py) == (0, 0)
    # 320x180 input -> scaled to 320x180 centered in 640x640.
    frame2 = np.zeros((180, 320, 3), dtype=np.uint8)
    boxed2, scale2, (px2, py2) = det._letterbox(frame2)
    assert scale2 == 2.0
    assert (px2, py2) == (0, 140)

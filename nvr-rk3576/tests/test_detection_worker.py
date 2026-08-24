"""Tests for nvr.inference.detection_worker — pure-function stats and the
per-camera feeder step, no NPU involved."""

import queue
import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from nvr.inference.detection_worker import _feed_camera, _update_stats
from nvr.motion.motion_gate import MotionGate


class _StubManager:
    def __init__(self):
        self.queues = {}

    def get_queue(self, name):
        return self.queues.get(name)


def _push(queue_obj, n=3):
    for i in range(n):
        queue_obj.put(np.zeros((360, 640, 3), dtype=np.uint8))


def test_feed_camera_skips_disabled_camera():
    mgr = _StubManager()
    q = queue.Queue(maxsize=8)
    _push(q)
    mgr.queues["cam_a"] = q
    flags = {"cam_a": False}
    work = queue.Queue()
    stats = {}

    seen = _feed_camera("cam_a", flags, mgr, MotionGate(), work, lambda *a: None)
    assert seen is False
    assert work.empty()  # never enqueued
    assert stats == {}  # no stats writes at all

    seen = _feed_camera("cam_a", flags, mgr, MotionGate(), work, lambda *a: None)
    assert seen is False
    assert work.empty()
    assert stats == {}


def test_feed_camera_resumes_after_flag_flip():
    mgr = _StubManager()
    q = queue.Queue(maxsize=8)
    _push(q)
    mgr.queues["cam_a"] = q
    flags = {"cam_a": False}
    work = queue.Queue()
    stats = {}

    _feed_camera("cam_a", flags, mgr, MotionGate(), work, lambda *a: None)
    assert work.empty()

    flags["cam_a"] = True  # mid-run flip
    seen = _feed_camera("cam_a", flags, mgr, MotionGate(), work, lambda *a: None)
    assert seen is True
    assert not work.empty()  # first frame always passes the gate
    name, frame, skipped = work.get()
    assert name == "cam_a"
    assert frame.shape == (360, 640, 3)


def test_update_stats_stores_json_safe_detections():
    detections = [{"class_name": "person", "confidence": 0.88, "bbox": [10, 20, 30, 40]}]
    stats = _update_stats(None, elapsed=0.1, detections=detections, skipped=False)

    assert stats["total"] == 1
    assert stats["skipped"] == 0
    assert stats["last_detections"] == [
        {"class_name": "person", "confidence": 0.88, "bbox": [10, 20, 30, 40]}
    ]
    assert stats["inference_fps"] == 10.0
    assert stats["skip_ratio"] == 0.0


def test_update_stats_skipped_does_not_touch_detections():
    stats = _update_stats(None, elapsed=0.0, detections=[], skipped=True)

    assert stats["skipped"] == 1
    assert stats["total"] == 0
    assert stats["last_detections"] == []
    assert stats["skip_ratio"] == 1.0


def test_update_stats_skip_ratio_mixed():
    stats = _update_stats(None, elapsed=0.1, detections=[{"class_name": "car", "confidence": 0.5}], skipped=False)
    stats = _update_stats(stats, elapsed=0.0, detections=[], skipped=True)
    stats = _update_stats(stats, elapsed=0.0, detections=[], skipped=True)

    assert stats["total"] == 1
    assert stats["skipped"] == 2
    assert stats["skip_ratio"] == 2 / 3
    assert stats["last_detections"] == [{"class_name": "car", "confidence": 0.5}]


def test_update_stats_caps_detections_at_three():
    detections = [
        {"class_name": "person", "confidence": 0.9},
        {"class_name": "person", "confidence": 0.8},
        {"class_name": "bird", "confidence": 0.7},
        {"class_name": "cow", "confidence": 0.6},
    ]
    stats = _update_stats(None, elapsed=0.1, detections=detections, skipped=False)

    assert len(stats["last_detections"]) == 3

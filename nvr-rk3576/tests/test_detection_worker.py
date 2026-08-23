"""Tests for nvr.inference.detection_worker — pure-function stats only, no NPU."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from nvr.inference.detection_worker import _update_stats


def test_update_stats_stores_json_safe_detections():
    detections = [{"class_name": "person", "confidence": 0.88}]
    stats = _update_stats(None, elapsed=0.1, detections=detections, skipped=False)

    assert stats["total"] == 1
    assert stats["skipped"] == 0
    assert stats["last_detections"] == [{"class_name": "person", "confidence": 0.88}]
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

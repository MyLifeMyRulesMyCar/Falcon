"""Tests for nvr.tracking.centroid_tracker — pure logic, no NPU involved."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from nvr.inference.detector import Detection
from nvr.tracking.centroid_tracker import CentroidTracker


def _det(bbox, cls="person", conf=0.8):
    return Detection(class_name=cls, confidence=conf, bbox_xyxy=bbox)


def test_single_detection_drift_keeps_id():
    tr = CentroidTracker(max_distance_px=80)
    a, b, c = _det((10, 10, 30, 30)), _det((12, 12, 32, 32)), _det((14, 14, 34, 34))
    t1 = tr.update([a], now=1.0)[0]
    t2 = tr.update([b], now=1.5)[0]
    t3 = tr.update([c], now=2.0)[0]
    assert t1.track_id == t2.track_id == t3.track_id
    assert t3.bbox_xyxy == c.bbox_xyxy
    assert t1.first_seen_ts == 1.0
    assert t3.last_seen_ts == 2.0


def test_brief_gap_retains_id():
    tr = CentroidTracker(max_missed_frames=5)
    t1 = tr.update([_det((10, 10, 30, 30))], now=1.0)[0]
    for n in range(3):  # under the limit
        assert tr.update([], now=2.0 + n) == []
    reappear = tr.update([_det((14, 12, 34, 32))], now=5.0)
    assert len(reappear) == 1
    assert reappear[0].track_id == t1.track_id


def test_long_gap_drops_and_reassigns():
    tr = CentroidTracker(max_missed_frames=2)
    t1 = tr.update([_det((10, 10, 30, 30))], now=1.0)[0]
    for n in range(3):  # 3 consecutive misses > max_missed_frames(2)
        tr.update([], now=2.0 + n)
    reappear = tr.update([_det((12, 12, 32, 32))], now=5.0)
    assert len(reappear) == 1
    assert reappear[0].track_id != t1.track_id


def test_far_detections_never_cross_match():
    tr = CentroidTracker(max_distance_px=80)
    a = _det((10, 10, 30, 30), cls="person")
    t1 = tr.update([a], now=1.0)[0]
    # a new detection far from the existing track is a new track, and the
    # existing track is not updated
    res = tr.update([_det((300, 300, 320, 320), cls="bird")], now=1.1)
    assert len(res) == 1
    assert res[0].track_id != t1.track_id
    # the old track is still alive for a nearby detection
    res = tr.update([_det((11, 11, 31, 31))], now=1.2)
    assert any(t.track_id == t1.track_id for t in res)

"""Tests for nvr.output.annotate — pure rendering, no processes."""

import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from nvr.config import ZoneConfig
from nvr.inference.detector import Detection
from nvr.output.annotate import draw_annotations


def _frame():
    return np.zeros((360, 640, 3), dtype=np.uint8)


def _detection():
    return Detection(class_name="person", confidence=0.9, bbox_xyxy=(100.0, 100.0, 200.0, 200.0))


def _zone():
    return ZoneConfig(
        name="entry_path",
        polygon=[(160.0, 260.0), (480.0, 260.0), (480.0, 360.0), (160.0, 360.0)],
        trigger_classes=["person"],
        dwell_time_sec=2.0,
        cooldown_sec=30.0,
    )


def test_draw_annotations_actually_draws_something():
    frame = _frame()
    out = draw_annotations(frame, [_detection()], [_zone()])
    assert isinstance(out, np.ndarray)
    assert out.shape == frame.shape
    # It drew something — pixels differ from the blank input.
    assert not np.array_equal(out, frame)


def test_empty_input_returns_unchanged_pixels():
    frame = _frame()
    out = draw_annotations(frame, [], [])
    # Same pixel values (no detections/zones to draw)...
    assert np.array_equal(out, frame)
    # ...but a different object: the caller's frame is never mutated.
    assert out is not frame
    assert not np.shares_memory(out, frame)


def test_highlight_zone_changes_output():
    frame = _frame()
    normal = draw_annotations(frame, [], [_zone()])
    highlighted = draw_annotations(frame, [], [_zone()], highlight_zone="entry_path")
    assert not np.array_equal(normal, highlighted)


def test_unknown_highlight_name_is_a_noop():
    frame = _frame()
    normal = draw_annotations(frame, [_detection()], [_zone()])
    other = draw_annotations(frame, [_detection()], [_zone()], highlight_zone="nope")
    assert np.array_equal(normal, other)

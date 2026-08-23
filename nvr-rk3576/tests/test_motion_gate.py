"""Tests for nvr.motion.motion_gate — pure numpy logic, no NPU involved."""

import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from nvr.motion.motion_gate import MotionGate


def _frame(h=360, w=640, color=128):
    return np.full((h, w, 3), color, dtype=np.uint8)


def test_identical_frames_do_not_trigger():
    gate = MotionGate()
    frame = _frame()
    assert gate.should_run_inference(frame) is True   # first frame always runs
    assert gate.should_run_inference(frame) is False  # identical -> no motion


def test_bright_block_triggers():
    gate = MotionGate()
    frame = _frame()
    assert gate.should_run_inference(frame) is True
    noisy = frame.copy()
    noisy[150:200, 150:250, :] = 255  # ~4.3% of 360x640 -> above 1.5%
    assert gate.should_run_inference(noisy) is True


def test_small_change_below_threshold_does_not_trigger():
    gate = MotionGate()
    frame = _frame()
    assert gate.should_run_inference(frame) is True
    tweak = frame.copy()
    tweak[178:182, 318:322, :] = 255  # 16 px of 230k -> ~0.007%
    assert gate.should_run_inference(tweak) is False


def test_skip_counter_forces_inference_on_static_scene():
    gate = MotionGate(max_skip_frames=90)
    frame = _frame()
    assert gate.should_run_inference(frame) is True  # first frame
    for _ in range(90):
        assert gate.should_run_inference(frame) is False
    assert gate.should_run_inference(frame) is True  # 91st identical frame


def test_gate_handles_odd_dimensions():
    gate = MotionGate()
    frame = np.zeros((101, 333, 3), dtype=np.uint8)
    assert gate.should_run_inference(frame) is True
    assert gate.should_run_inference(frame) is False

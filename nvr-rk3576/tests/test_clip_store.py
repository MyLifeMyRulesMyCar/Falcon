"""Tests for nvr.output.clip_store — capture + ffmpeg mux with a fake frame
store (ffmpeg is the only real subprocess; encoder is mpeg4 software so the
tests are hermetic — the h264_rkmpp path is validated live on the board)."""

import glob
import os
import sys
import time
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from nvr.output.clip_store import ClipStore


class _StubFrameStore:
    """Returns a fresh (frame, gen) per read; gen advances each call."""

    def __init__(self, ready=True):
        self.ready = ready
        self.gens = {"cam_a": 0}

    def read(self, name):
        if not self.ready:
            return None
        self.gens[name] += 1
        return np.zeros((360, 640, 3), dtype=np.uint8), self.gens[name]


def _pump(store: ClipStore, timeout=8.0) -> None:
    """Poll until the store has no active clips (or timeout)."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        store.poll()
        if not store._active:
            return
        time.sleep(0.02)
    raise AssertionError("clip never finalized within timeout")


def _make_store(tmp_path, **kw):
    return ClipStore(
        str(tmp_path), _StubFrameStore(), max_per_camera=10, duration_sec=0.15,
        encoder="mpeg4", **kw,
    )


def test_poll_finalizes_exactly_one_mp4(tmp_path):
    store = _make_store(tmp_path)
    store.start_clip("cam_a", "entry", 1, time.time())
    _pump(store)
    files = glob.glob(os.path.join(str(tmp_path), "cam_a", "*.mp4"))
    assert len(files) == 1
    assert os.path.getsize(files[0]) > 0
    assert "entry_" in os.path.basename(files[0])


def test_second_start_while_active_is_noop(tmp_path):
    store = _make_store(tmp_path)
    store.start_clip("cam_a", "entry", 1, time.time())
    store.start_clip("cam_a", "other", 2, time.time() + 0.01)
    _pump(store)
    files = glob.glob(os.path.join(str(tmp_path), "cam_a", "*.mp4"))
    assert len(files) == 1  # one clip, not two


def test_too_few_frames_skips_ffmpeg(tmp_path):
    store = _make_store(tmp_path)
    store.frame_store = _StubFrameStore(ready=False)  # never yields a frame
    store.start_clip("cam_a", "entry", 1, time.time())
    _pump(store)
    assert glob.glob(os.path.join(str(tmp_path), "cam_a", "*.mp4")) == []


def test_rotation_caps_at_max_per_camera(tmp_path):
    store = ClipStore(
        str(tmp_path), _StubFrameStore(), max_per_camera=3, duration_sec=0.05,
        encoder="mpeg4",
    )
    for i in range(6):
        store.start_clip("cam_a", "entry", i, time.time())
        _pump(store)
    files = sorted(glob.glob(os.path.join(str(tmp_path), "cam_a", "*.mp4")))
    assert len(files) == 3
    # Oldest clip files removed; the last three survive.
    for f in files:
        assert os.path.getsize(f) > 0

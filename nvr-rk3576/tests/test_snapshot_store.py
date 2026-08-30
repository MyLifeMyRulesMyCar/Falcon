"""Tests for nvr.output.snapshot_store — rotation by max file count."""

import glob
import os
import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from nvr.output.snapshot_store import SnapshotStore


def _frame():
    return np.zeros((360, 640, 3), dtype=np.uint8)


def _files(tmp_path: Path) -> list:
    return sorted(glob.glob(os.path.join(str(tmp_path), "cam_a", "*.jpg")))


def test_save_writes_jpeg_and_returns_rel_path(tmp_path):
    store = SnapshotStore(str(tmp_path), max_per_camera=10)
    rel = store.save("cam_a", "entry", 1, _frame(), 1700000000.0)
    assert rel == "cam_a/entry_1700000000_1.jpg"
    out = os.path.join(str(tmp_path), rel)
    assert os.path.isfile(out)
    assert open(out, "rb").read(2) == b"\xff\xd8"


def test_rotation_caps_at_max_per_camera_removing_oldest(tmp_path):
    store = SnapshotStore(str(tmp_path), max_per_camera=10)
    saved = [
        store.save("cam_a", "entry", track_id, _frame(), 1700000000.0 + track_id)
        for track_id in range(15)
    ]
    remaining = _files(tmp_path)
    assert len(remaining) == 10
    # Oldest five (track ids 0..4) are gone; the most recent survive.
    for rel in saved[:5]:
        assert not os.path.isfile(os.path.join(str(tmp_path), rel))
    for rel in saved[5:]:
        assert os.path.isfile(os.path.join(str(tmp_path), rel))


def test_rotation_is_per_camera(tmp_path):
    store = SnapshotStore(str(tmp_path), max_per_camera=5)
    for track_id in range(8):
        store.save("cam_a", "entry", track_id, _frame(), 1700000000.0 + track_id)
    for track_id in range(3):
        store.save("cam_b", "entry", track_id, _frame(), 1800000000.0 + track_id)
    assert len(glob.glob(os.path.join(str(tmp_path), "cam_a", "*.jpg"))) == 5
    # cam_b never hit the cap.
    assert len(glob.glob(os.path.join(str(tmp_path), "cam_b", "*.jpg"))) == 3

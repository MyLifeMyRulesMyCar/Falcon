"""Tests for nvr.output.rotation — max-count file rotation by mtime."""

import glob
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from nvr.output.rotation import rotate_by_count


def _make(tmp_path: Path, name: str, n: int) -> list:
    d = tmp_path / name
    d.mkdir(exist_ok=True)
    paths = []
    base = 1700000000
    for i in range(n):
        p = d / f"{i:03d}.jpg"
        p.write_bytes(b"x")
        # Deterministic mtime order (tight-loop writes can share mtimes).
        os.utime(p, (base + i, base + i))
        paths.append(p)
    return paths


def test_rotate_caps_count_and_removes_oldest(tmp_path):
    paths = _make(tmp_path, "cam_a", 8)
    rotate_by_count(str(tmp_path / "cam_a"), 3, "*.jpg")
    remaining = glob.glob(str(tmp_path / "cam_a" / "*.jpg"))
    assert len(remaining) == 3
    # Oldest (000, 001, ...) removed; the last three written survive.
    for p in paths[5:]:
        assert p.exists()
    assert not any(p.exists() for p in paths[:5])


def test_rotate_respects_pattern(tmp_path):
    _make(tmp_path, "cam_a", 5)
    (tmp_path / "cam_a" / "note.txt").write_text("keep me")
    rotate_by_count(str(tmp_path / "cam_a"), 2, "*.jpg")
    assert (tmp_path / "cam_a" / "note.txt").exists()
    assert len(glob.glob(str(tmp_path / "cam_a" / "*.jpg"))) == 2


def test_rotate_is_per_directory(tmp_path):
    _make(tmp_path, "cam_a", 6)
    _make(tmp_path, "cam_b", 2)
    rotate_by_count(str(tmp_path / "cam_a"), 3, "*")
    assert len(glob.glob(str(tmp_path / "cam_a" / "*"))) == 3
    # cam_b never hit its cap threshold.
    assert len(glob.glob(str(tmp_path / "cam_b" / "*"))) == 2


def test_rotate_under_cap_is_noop(tmp_path):
    _make(tmp_path, "cam_a", 2)
    rotate_by_count(str(tmp_path / "cam_a"), 5, "*.jpg")
    assert len(glob.glob(str(tmp_path / "cam_a" / "*.jpg"))) == 2

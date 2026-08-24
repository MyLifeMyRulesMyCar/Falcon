"""Tests for nvr.ingest.frame_broadcast — shared-memory latest-frame store."""

import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from nvr.ingest.frame_broadcast import LatestFrameStore


def test_read_before_first_write_is_none():
    store = LatestFrameStore(["cam_a"])
    try:
        assert store.read("cam_a") is None
        assert store.read("nope") is None
    finally:
        store.unlink_all()


def test_write_read_roundtrip_and_generation():
    store = LatestFrameStore(["cam_a", "cam_b"])
    try:
        frame = np.arange(360 * 640 * 3, dtype=np.uint8).reshape(360, 640, 3)
        store.write("cam_a", frame)
        out, gen = store.read("cam_a")
        assert gen == 1
        assert out.shape == (360, 640, 3)
        assert np.array_equal(out, frame)
        assert out is not frame  # a copy, not a view

        # Mutating the returned copy must not corrupt the store.
        out[:] = 0
        out2, _ = store.read("cam_a")
        assert np.array_equal(out2, frame)

        # Unknown camera -> None.
        assert store.read("nope") is None
    finally:
        store.unlink_all()


def test_write_bumps_generation_only_for_that_camera():
    store = LatestFrameStore(["cam_a", "cam_b"])
    try:
        store.write("cam_a", np.zeros((4, 4, 3), dtype=np.uint8))
        _, gen_a = store.read("cam_a")
        assert gen_a == 1
        assert store.read("cam_b") is None  # never written -> no block yet
    finally:
        store.unlink_all()


def test_lazy_per_camera_shapes_are_isolated():
    store = LatestFrameStore(["cam_a", "cam_b"])
    try:
        # Different cameras, different resolutions (360p vs 720p sources).
        small = np.zeros((360, 640, 3), dtype=np.uint8)
        big = np.zeros((720, 1280, 3), dtype=np.uint8)
        store.write("cam_a", small)
        store.write("cam_b", big)
        out_a, _ = store.read("cam_a")
        out_b, _ = store.read("cam_b")
        assert out_a.shape == (360, 640, 3)
        assert out_b.shape == (720, 1280, 3)
    finally:
        store.unlink_all()


def test_attach_by_name_across_processes():
    """The reader may be a different process than the writer: the block is
    created under a deterministic name and attached on the reader side."""
    import multiprocessing

    store = LatestFrameStore(["cam_a"])
    frame = np.arange(360 * 640 * 3, dtype=np.uint8).reshape(360, 640, 3)
    try:
        store.write("cam_a", frame)

        def reader_probe(store, results):
            out, gen = store.read("cam_a")
            results.append((out.shape, gen, bool(np.array_equal(out, frame))))

        results = multiprocessing.Manager().list()
        p = multiprocessing.Process(target=reader_probe, args=(store, results))
        p.start()
        p.join(timeout=10)
        assert p.exitcode == 0
        assert list(results) == [((360, 640, 3), 1, True)]
    finally:
        store.unlink_all()

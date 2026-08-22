"""Tests for nvr.ingest.manager — fake worker injected via worker_factory,
no live streams, no subprocesses."""

import multiprocessing
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from nvr.config import CameraConfig
from nvr.ingest.manager import IngestManager

CAMERAS = [
    CameraConfig(name="cam_a", url="rtsp://a"),
    CameraConfig(name="cam_b", url="rtsp://b"),
]


class FakeWorker:
    """Minimal multiprocessing.Process-compatible stub."""

    def __init__(self, camera, frame_queue, restart_counter=None, last_error=None):
        self.camera = camera
        self.frame_queue = frame_queue
        self.restart_counter = restart_counter
        self.last_error = last_error
        self.started = False
        self.terminated = False
        self.killed = False
        self._alive = True

    def start(self):
        self.started = True

    def terminate(self):
        self.terminated = True
        self._alive = False

    def kill(self):
        self.killed = True
        self._alive = False

    def join(self, timeout=None):
        pass

    def is_alive(self):
        return self._alive


def test_start_creates_one_worker_and_queue_per_camera():
    created = []

    def factory(camera, queue, restart_counter, last_error):
        created.append((camera, queue, restart_counter, last_error))
        return FakeWorker(camera, queue, restart_counter, last_error)

    mgr = IngestManager(CAMERAS, worker_factory=factory)
    mgr.start()

    assert [camera.name for camera, _, _, _ in created] == ["cam_a", "cam_b"]
    for camera, queue, restart_counter, last_error in created:
        assert queue._maxsize == 16
        assert restart_counter.value == 0
        assert last_error is not None
        assert camera.name in ("cam_a", "cam_b")


def test_stop_terminates_every_worker_and_does_not_hang_on_dead_worker():
    mgr = IngestManager(CAMERAS, worker_factory=FakeWorker)
    mgr.start()
    mgr._workers["cam_a"]._alive = False  # already dead before stop

    started = time.monotonic()
    mgr.stop(timeout_sec=1.0)
    elapsed = time.monotonic() - started

    assert mgr._workers["cam_a"].terminated is False  # skipped, already dead
    assert mgr._workers["cam_b"].terminated is True
    assert elapsed < 5.0


def test_stop_escalates_to_kill_when_terminate_is_ignored():
    class StubbornWorker(FakeWorker):
        def terminate(self):
            self.terminated = True  # pretend terminate, but stay alive

    mgr = IngestManager(CAMERAS, worker_factory=StubbornWorker)
    mgr.start()
    mgr.stop(timeout_sec=0.1)

    assert mgr._workers["cam_a"].killed is True
    assert mgr._workers["cam_b"].killed is True


def test_stats_reflects_exited_worker():
    mgr = IngestManager(CAMERAS, worker_factory=FakeWorker)
    mgr.start()

    assert mgr.stats()["cam_a"]["alive"] is True

    mgr._workers["cam_a"].terminate()
    stats = mgr.stats()["cam_a"]
    assert stats["alive"] is False
    assert stats["frames_received"] == 0
    assert stats["restart_count"] == 0
    assert stats["last_frame_ts"] is None


def _put_from_child(queue, items):
    """Put via a real child process (the production path), flushing fully
    before exit — same-process puts race CPython's async queue feeder."""
    for item in items:
        queue.put(item)
    queue.close()
    queue.join_thread()


def test_consume_updates_frames_and_last_frame_ts():
    mgr = IngestManager(CAMERAS, worker_factory=FakeWorker)
    mgr.start()

    procs = [
        multiprocessing.Process(target=_put_from_child, args=(mgr.get_queue("cam_a"), [1, 2])),
        multiprocessing.Process(target=_put_from_child, args=(mgr.get_queue("cam_b"), [3])),
    ]
    for p in procs:
        p.start()
    for p in procs:
        p.join(timeout=5)

    received = {}
    deadline = time.monotonic() + 2.0
    while sum(received.values()) < 3 and time.monotonic() < deadline:
        received = mgr.consume(timeout=0.05)
        time.sleep(0.01)

    assert received == {"cam_a": 2, "cam_b": 1}

    stats = mgr.stats()
    assert stats["cam_a"]["frames_received"] == 2
    assert stats["cam_b"]["frames_received"] == 1
    assert stats["cam_a"]["last_frame_ts"] is not None


def test_restart_counter_visible_in_stats():
    mgr = IngestManager(CAMERAS, worker_factory=FakeWorker)
    mgr.start()

    mgr._restart_counters["cam_b"].value = 3
    assert mgr.stats()["cam_b"]["restart_count"] == 3


def test_get_queue_raises_keyerror_for_unknown_camera():
    mgr = IngestManager(CAMERAS, worker_factory=FakeWorker)
    mgr.start()

    with pytest.raises(KeyError):
        mgr.get_queue("nope")


def test_is_alive_raises_keyerror_for_unknown_camera():
    mgr = IngestManager(CAMERAS, worker_factory=FakeWorker)
    mgr.start()

    with pytest.raises(KeyError):
        mgr.is_alive("nope")


def test_restart_counter_is_shared_multiprocessing_value():
    value = multiprocessing.Value("i", 0)
    worker = FakeWorker(CAMERAS[0], multiprocessing.Queue(), value)
    worker.restart_counter.value += 1
    assert value.value == 1


def test_stats_tolerates_unstarted_cameras():
    mgr = IngestManager(CAMERAS, worker_factory=FakeWorker)
    stats = mgr.stats()
    assert stats["cam_a"] == {
        "alive": False,
        "frames_received": 0,
        "restart_count": 0,
        "last_frame_ts": None,
        "last_error": "",
    }


def test_stats_surfaces_worker_last_error():
    mgr = IngestManager(CAMERAS, worker_factory=FakeWorker)
    mgr.start_one("cam_a")
    mgr._errors["cam_a"].value = b"probe failed: connection timed out"
    assert mgr.stats()["cam_a"]["last_error"] == "probe failed: connection timed out"


def test_start_one_spawns_only_that_camera():
    mgr = IngestManager(CAMERAS, worker_factory=FakeWorker)
    mgr.start_one("cam_a")

    assert "cam_a" in mgr._workers
    assert "cam_b" not in mgr._workers
    assert mgr.is_alive("cam_a") is True
    assert mgr.is_alive("cam_b") is False


def test_start_one_is_noop_when_already_running():
    mgr = IngestManager(CAMERAS, worker_factory=FakeWorker)
    mgr.start_one("cam_a")
    first = mgr._workers["cam_a"]
    mgr.start_one("cam_a")
    assert mgr._workers["cam_a"] is first


def test_stop_one_noop_when_not_running():
    mgr = IngestManager(CAMERAS, worker_factory=FakeWorker)
    mgr.start_one("cam_a")
    mgr.stop_one("cam_a")
    mgr.stop_one("cam_a")  # already dead: must not raise or hang
    mgr.stop_one("nope")  # never started: no-op
    assert mgr.is_alive("cam_a") is False


def test_stop_one_closes_old_queue_start_one_creates_fresh():
    mgr = IngestManager(CAMERAS, worker_factory=FakeWorker)
    mgr.start_one("cam_a")
    old = mgr.get_queue("cam_a")
    mgr.stop_one("cam_a")
    assert "cam_a" not in mgr._queues  # old queue closed and dropped
    mgr.start_one("cam_a")
    fresh = mgr.get_queue("cam_a")
    assert fresh is not old
    mgr.stop_one("cam_a")
    assert "cam_a" not in mgr._queues


def test_update_camera_raises_while_running():
    mgr = IngestManager(CAMERAS, worker_factory=FakeWorker)
    mgr.start_one("cam_a")
    with pytest.raises(RuntimeError):
        mgr.update_camera("cam_a", CameraConfig(name="cam_a", url="rtsp://new"))


def test_update_camera_replaces_config_after_stop():
    mgr = IngestManager(CAMERAS, worker_factory=FakeWorker)
    mgr.start_one("cam_a")
    mgr.stop_one("cam_a")
    mgr.update_camera("cam_a", CameraConfig(name="cam_a", url="rtsp://new"))

    mgr.start_one("cam_a")
    worker = mgr._workers["cam_a"]
    assert worker.camera.url == "rtsp://new"


def test_update_camera_rename_reroutes_stats():
    mgr = IngestManager(CAMERAS, worker_factory=FakeWorker)
    mgr.start_one("cam_a")
    mgr.consume()
    mgr.stop_one("cam_a")
    mgr._frames["cam_a"] = 42
    mgr.update_camera("cam_a", CameraConfig(name="cam_x", url="rtsp://x"))

    assert "cam_a" not in mgr.stats()
    assert mgr.stats()["cam_x"]["frames_received"] == 42
    with pytest.raises(KeyError):
        mgr.start_one("cam_a")
    mgr.start_one("cam_x")

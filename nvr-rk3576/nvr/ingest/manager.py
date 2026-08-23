"""Multi-camera ingest manager: run one StreamWorker per camera and expose
cross-camera visibility (liveness, frame counts, restart counts).

The manager does not touch StreamWorker internals; it just spawns N of them,
each with its own bounded frame queue and shared restart counter, and tracks
frames as the caller drains the queues via :meth:`consume`.
"""

import multiprocessing
import time
from typing import Callable, Optional

from nvr.config import CameraConfig
from nvr.ingest.stream_worker import StreamWorker

# 640x360 frames are ~0.7 MB; 16-deep queues keep the worst-case buffer
# small (~45 MB across 4 cameras) while drop-oldest smooths consumer hiccups.
_QUEUE_MAXSIZE = 16
_ERROR_BUFFER_SIZE = 512
_WorkerFactory = Callable[
    [CameraConfig, multiprocessing.Queue, Optional[multiprocessing.Value]],
    multiprocessing.Process,
]


class IngestManager:
    """Own one :class:`StreamWorker` per camera, plus its queue and restart counter."""

    def __init__(self, cameras: list[CameraConfig], worker_factory: _WorkerFactory = StreamWorker):
        self._cameras = {c.name: c for c in cameras}
        self._worker_factory = worker_factory
        self._workers: dict[str, multiprocessing.Process] = {}
        self._queues: dict[str, multiprocessing.Queue] = {}
        self._restart_counters: dict[str, multiprocessing.Value] = {}
        self._errors: dict[str, multiprocessing.Array] = {}
        self._frames: dict[str, int] = {c.name: 0 for c in cameras}
        self._last_frame_ts: dict[str, Optional[float]] = {c.name: None for c in cameras}

    def start(self) -> None:
        """Spawn one worker per camera; each gets its own queue and restart counter."""
        for name, camera in self._cameras.items():
            self._spawn_one(name, camera)

    def start_one(self, name: str) -> None:
        """Start just one camera's worker; no-op if it is already running.

        Raises:
            KeyError: if ``name`` is not a configured camera.
        """
        if self.is_alive(name):
            return
        self._spawn_one(name, self._cameras[name])

    def _spawn_one(self, name: str, camera: CameraConfig) -> None:
        queue: multiprocessing.Queue = multiprocessing.Queue(maxsize=_QUEUE_MAXSIZE)
        # RawValue: a worker that dies mid-increment would otherwise leave
        # a plain Value's semaphore held, blocking stats() forever.
        restart_counter = multiprocessing.RawValue("i", 0)
        last_error = multiprocessing.RawArray("c", _ERROR_BUFFER_SIZE)
        worker = self._worker_factory(camera, queue, restart_counter, last_error)
        worker.start()
        self._workers[name] = worker
        self._queues[name] = queue
        self._restart_counters[name] = restart_counter
        self._errors[name] = last_error

    def stop_one(self, name: str, timeout_sec: float = 5.0) -> None:
        """Stop just one camera's worker; no-op if it is not running.

        The worker's queue is closed and dropped afterwards: a worker
        terminated mid-write leaves a partial frame in the pipe, and an
        orphaned queue whose write end is inherited by the next worker
        would block the drain thread's read forever.
        """
        worker = self._workers.get(name)
        if worker is None or not worker.is_alive():
            return
        worker.terminate()
        worker.join(timeout=timeout_sec)
        if worker.is_alive():
            worker.kill()
            worker.join(timeout=timeout_sec)
        self._close_queue(name)

    def _close_queue(self, name: str) -> None:
        queue = self._queues.pop(name, None)
        if queue is not None:
            try:
                queue.close()
            except Exception:
                pass

    def update_camera(self, name: str, new_config: CameraConfig) -> None:
        """Replace a camera's stored config, used by the next ``start_one``.

        Raises:
            RuntimeError: if the camera's worker is currently running.
            KeyError: if ``name`` is not a configured camera.
        """
        if name not in self._cameras:
            raise KeyError(f"unknown camera: {name}")
        if self.is_alive(name):
            raise RuntimeError(f"{name} is running, stop it before editing")
        old_config = self._cameras.pop(name)
        self._cameras[new_config.name] = new_config
        if new_config.name != old_config.name:
            self._frames[new_config.name] = self._frames.pop(name, 0)
            self._last_frame_ts[new_config.name] = self._last_frame_ts.pop(name, None)
            self._workers.pop(name, None)
            self._restart_counters.pop(name, None)
            self._errors.pop(name, None)
            self._close_queue(name)

    def consume(self, timeout: float = 0.05) -> dict[str, int]:
        """Drain every camera queue non-blocking; return frames received per camera.

        Frame accounting lives here (not in StreamWorker): the caller loop is
        expected to call this repeatedly.
        """
        received = {name: 0 for name in self._cameras}
        for name, queue in self._queues.items():
            deadline = time.monotonic() + timeout
            while True:
                try:
                    queue.get_nowait()
                except (multiprocessing.queues.Empty, EOFError, OSError):
                    break
                self._frames[name] = self._frames.get(name, 0) + 1
                self._last_frame_ts[name] = time.monotonic()
                received[name] += 1
                if time.monotonic() >= deadline:
                    break
        return received

    def stop(self, timeout_sec: float = 5.0) -> None:
        """SIGTERM every worker, join up to ``timeout_sec``, SIGKILL survivors.

        Already-dead workers are skipped; this never blocks longer than
        ``timeout_sec`` per worker.
        """
        for name, worker in self._workers.items():
            if not worker.is_alive():
                continue
            worker.terminate()
        for name, worker in self._workers.items():
            worker.join(timeout=timeout_sec)
        for name, worker in self._workers.items():
            if worker.is_alive():
                worker.kill()
                worker.join(timeout=timeout_sec)
        for name in list(self._workers):
            self._close_queue(name)

    def get_queue(self, camera_name: str) -> Optional[multiprocessing.Queue]:
        """Return the camera's current frame queue, or ``None`` if the camera
        is configured but not started (its queue is dropped on stop).

        Raises:
            KeyError: if ``camera_name`` is not a configured camera.
        """
        if camera_name not in self._cameras:
            raise KeyError(f"unknown camera: {camera_name}")
        return self._queues.get(camera_name)

    def is_alive(self, camera_name: str) -> bool:
        if camera_name not in self._cameras:
            raise KeyError(f"unknown camera: {camera_name}")
        worker = self._workers.get(camera_name)
        return worker is not None and worker.is_alive()

    def stats(self) -> dict[str, dict]:
        """Return per-camera ``{alive, frames_received, restart_count,
        last_frame_ts, last_error}``.

        Cameras that were never started report ``alive: False`` and zero
        counters rather than raising.
        """
        stats = {}
        for name in self._cameras:
            worker = self._workers.get(name)
            counter = self._restart_counters.get(name)
            error = self._errors.get(name)
            last_error = ""
            if error is not None:
                try:
                    last_error = error.value.decode("utf-8", errors="replace").rstrip("\x00")
                except Exception:
                    pass
            stats[name] = {
                "alive": worker.is_alive() if worker is not None else False,
                "frames_received": self._frames.get(name, 0),
                "restart_count": counter.value if counter is not None else 0,
                "last_frame_ts": self._last_frame_ts.get(name, None),
                "last_error": last_error,
            }
        return stats

"""Single-owner detection process: two internal threads, one per NPU core,
round-robin motion-gated frames from all cameras through a shared work queue.

NPU cores are single hardware resources, so this one process owns both of
them. Frames are NOT tied to a camera's core: whichever core frees up first
grabs the next ready frame from the shared queue. The GIL releases during
rknn inference (measured ~1.6x threaded-vs-sequential under load), so two
threads genuinely parallelize; the shared queue.Queue is in-process.

Queues are fetched fresh from the IngestManager every pass. The worker
process is re-forked by the panel whenever cameras start/stop/rename: a
fork snapshots the manager's queue dict at that moment, so each worker
lifetime sees exactly the currently running cameras (multiprocessing.Queue
objects can only be shared through fork inheritance, not a Manager dict).
"""

import logging
import multiprocessing
import queue
import threading
import time
from pathlib import Path
from typing import Optional

import numpy as np

from nvr.inference.detector import ObjectDetector
from nvr.inference.npu_pool import NpuCorePool
from nvr.ingest.manager import IngestManager
from nvr.motion.motion_gate import MotionGate

log = logging.getLogger(__name__)

# File-relative: the worker may be launched from any cwd (panel/smoke/tests).
MODEL_PATH = str(Path(__file__).resolve().parent / "model" / "yolov5s_relu_rk3576.rknn")
_RING_SIZE = 30
_EMPTY_LOOP_SLEEP = 0.002
_MAX_BATCH = 8
_WORK_QUEUE_MAXSIZE = 8


def get_nowait_or_none(queue_obj: multiprocessing.Queue) -> Optional[np.ndarray]:
    try:
        return queue_obj.get_nowait()
    except (multiprocessing.queues.Empty, EOFError, OSError):
        return None


def _update_stats(prev, elapsed: float, detections, skipped: int) -> dict:
    """Rolling-window stats, kept JSON-serializable (plain dicts/floats/strs).

    ``detections`` must already be plain ``{"class_name", "confidence"}``
    dicts — never dataclass objects, so the API can jsonify them directly.
    """
    if prev is None:
        prev = {
            "infer_times": [],
            "total": 0,
            "skipped": 0,
            "last_detections": [],
            "inference_fps": 0.0,
            "skip_ratio": 0.0,
        }
    if skipped:
        prev["skipped"] += skipped
    else:
        prev["total"] += 1
        prev["infer_times"].append(elapsed)
        if len(prev["infer_times"]) > _RING_SIZE:
            prev["infer_times"].pop(0)
        prev["last_detections"] = detections[:3]
    ring = prev["infer_times"]
    prev["inference_fps"] = 1.0 / (sum(ring) / len(ring)) if ring else 0.0
    seen = prev["total"] + prev["skipped"]
    prev["skip_ratio"] = prev["skipped"] / seen if seen else 0.0
    return prev


class DetectionWorker(multiprocessing.Process):
    """Owns both NPU cores; motion-gates and detects frames for all cameras."""

    def __init__(
        self,
        camera_names: list[str],
        ingest_manager: IngestManager,
        stats: dict,
    ):
        super().__init__(name="detection-worker")
        self.camera_names = camera_names
        self.ingest_manager = ingest_manager
        self.stats = stats

    def run(self) -> None:
        gates = {name: MotionGate() for name in self.camera_names}
        pool = NpuCorePool(MODEL_PATH)
        work_queue: queue.Queue = queue.Queue(maxsize=_WORK_QUEUE_MAXSIZE)
        # Both core threads may update the same camera's stats (shared queue
        # means a camera's frames can land on either core); the Manager dict
        # read-modify-write is not atomic, so serialize per camera.
        stats_locks = {name: threading.Lock() for name in self.camera_names}
        log.info("detection worker started for %s", ", ".join(self.camera_names))

        def update_stats(name, elapsed, detections, skipped):
            with stats_locks[name]:
                self.stats[name] = _update_stats(
                    self.stats.get(name), elapsed, detections, skipped
                )

        def feeder() -> None:
            while True:
                for name in self.camera_names:
                    queue_obj = self.ingest_manager.get_queue(name)
                    if queue_obj is None:
                        continue  # camera not started, or was stopped
                    # Batch-read so a slow detect() doesn't throttle the
                    # count: count every frame, gate only the newest.
                    newest = None
                    count = 0
                    for _ in range(_MAX_BATCH):
                        frame = get_nowait_or_none(queue_obj)
                        if frame is None:
                            break
                        newest = frame
                        count += 1
                    if count == 0:
                        continue
                    if gates[name].should_run_inference(newest):
                        # backpressure: block when both cores are busy;
                        # drop-oldest on the ingest queue absorbs overflow.
                        work_queue.put((name, newest, count - 1))
                    else:
                        update_stats(name, 0.0, [], skipped=count)

        def core_worker(core: int) -> None:
            detector = ObjectDetector(pool)
            while True:
                name, frame, skipped = work_queue.get()
                t0 = time.monotonic()
                detections = detector.detect(frame, core=core)
                elapsed = time.monotonic() - t0
                update_stats(
                    name,
                    elapsed,
                    [
                        {"class_name": d.class_name, "confidence": round(d.confidence, 2)}
                        for d in detections
                    ],
                    skipped=skipped,
                )

        threading.Thread(target=feeder, name="feeder", daemon=True).start()
        threading.Thread(target=core_worker, args=(0,), name="core-0", daemon=True).start()
        threading.Thread(target=core_worker, args=(1,), name="core-1", daemon=True).start()

        while True:
            time.sleep(60)  # block forever; workers are daemons

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
import multiprocessing.queues
import queue
import threading
import time
from pathlib import Path
from typing import Optional

import numpy as np

from nvr.config import ZoneConfig
from nvr.inference.detector import ObjectDetector
from nvr.inference.npu_pool import NpuCorePool
from nvr.ingest.manager import IngestManager
from nvr.motion.motion_gate import MotionGate
from nvr.output.annotate import draw_annotations
from nvr.output.clip_store import ClipStore
from nvr.output.dispatcher import OutputDispatcher
from nvr.output.snapshot_store import SnapshotStore
from nvr.tracking.centroid_tracker import CentroidTracker
from nvr.zones.zone_engine import ZoneEngine, event_to_dict

log = logging.getLogger(__name__)

# File-relative: the worker may be launched from any cwd (panel/smoke/tests).
MODEL_PATH = str(Path(__file__).resolve().parent / "model" / "yolov5s_relu_rk3576.rknn")
_RING_SIZE = 30
_EMPTY_LOOP_SLEEP = 0.002
_MAX_BATCH = 8
_WORK_QUEUE_MAXSIZE = 8


def get_nowait_or_none(queue_obj: multiprocessing.Queue) -> Optional[np.ndarray]:
    # multiprocessing.queues.Empty and queue.Empty are distinct classes;
    # catch both so unit-test stubs using queue.Queue behave identically.
    try:
        return queue_obj.get_nowait()
    except (queue.Empty, multiprocessing.queues.Empty, EOFError, OSError):
        return None


def _update_stats(prev, elapsed: float, detections, skipped: int, zone_events=None) -> dict:
    """Rolling-window stats, kept JSON-serializable (plain dicts/floats/strs).

    ``detections`` must already be plain ``{"class_name", "confidence"}``
    dicts — never dataclass objects, so the API can jsonify them directly.
    ``zone_events`` (optional list of ``event_to_dict`` dicts) is appended to
    a ``recent_zone_events`` ring capped at 20.
    """
    if prev is None:
        prev = {
            "infer_times": [],
            "total": 0,
            "skipped": 0,
            "last_detections": [],
            "inference_fps": 0.0,
            "skip_ratio": 0.0,
            "recent_zone_events": [],
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
    if zone_events:
        recent = prev.setdefault("recent_zone_events", [])
        recent.extend(zone_events)
        del recent[:-20]
    return prev


def _feed_camera(
    name: str,
    detection_flags: dict,
    ingest_manager: IngestManager,
    gate: MotionGate,
    work_queue: queue.Queue,
    update_stats,
) -> bool:
    """One camera's feeder step; returns True if frames were seen.

    A camera with detection disabled is skipped entirely — no queue drain,
    no stats writes (drop-oldest on the ingest queue absorbs overflow, and
    the panel drain keeps counting its frames).
    """
    if not detection_flags.get(name, True):
        return False
    queue_obj = ingest_manager.get_queue(name)
    if queue_obj is None:
        return False  # camera not started, or was stopped
    # Batch-read so a slow detect() doesn't throttle the count: count every
    # frame, gate only the newest.
    newest = None
    count = 0
    for _ in range(_MAX_BATCH):
        frame = get_nowait_or_none(queue_obj)
        if frame is None:
            break
        newest = frame
        count += 1
    if count == 0:
        return False
    if gate.should_run_inference(newest):
        # backpressure: block when both cores are busy; drop-oldest on the
        # ingest queue absorbs overflow.
        work_queue.put((name, newest, count - 1))
    else:
        update_stats(name, 0.0, [], skipped=count)
    return True


class DetectionWorker(multiprocessing.Process):
    """Owns both NPU cores; motion-gates and detects frames for all cameras."""

    def __init__(
        self,
        camera_names: list[str],
        ingest_manager: IngestManager,
        stats: dict,
        detection_flags: Optional[dict] = None,
        zone_configs: Optional[dict[str, list[ZoneConfig]]] = None,
        dispatcher: Optional[OutputDispatcher] = None,
        publish_configs: Optional[dict] = None,
        publish_zone_events_flags: Optional[dict] = None,
        publish_detections_flags: Optional[dict] = None,
        snapshot_store: Optional[SnapshotStore] = None,
        clip_store: Optional[ClipStore] = None,
    ):
        super().__init__(name="detection-worker")
        self.camera_names = camera_names
        self.ingest_manager = ingest_manager
        self.stats = stats
        # Shared (Manager) dict; read live every feeder pass, so toggling
        # detection never requires re-forking. Absent -> all enabled.
        self.detection_flags = detection_flags if detection_flags is not None else {}
        # Per-camera M4 zone configs (empty list -> no zones). Absent -> none.
        self.zone_configs = (
            zone_configs if zone_configs is not None else {name: [] for name in camera_names}
        )
        # M5 output. dispatcher None -> no publishing (smoke test etc.).
        self.dispatcher = dispatcher
        self.publish_configs = publish_configs or {}
        # Live-toggleable per-camera content switches (Manager dicts, same
        # pattern as detection_flags); absent -> config defaults.
        self.publish_zone_events_flags = (
            publish_zone_events_flags if publish_zone_events_flags is not None else {}
        )
        self.publish_detections_flags = (
            publish_detections_flags if publish_detections_flags is not None else {}
        )
        # v1.1 event snapshots. None -> no snapshot files (smoke tests etc.).
        self.snapshot_store = snapshot_store
        # v1.3 post-roll event clips. None -> no clips.
        self.clip_store = clip_store

    def run(self) -> None:
        gates = {name: MotionGate() for name in self.camera_names}
        pool = NpuCorePool(MODEL_PATH)
        work_queue: queue.Queue = queue.Queue(maxsize=_WORK_QUEUE_MAXSIZE)
        # Both core threads may update the same camera's stats (shared queue
        # means a camera's frames can land on either core); the Manager dict
        # read-modify-write is not atomic, so serialize per camera.
        stats_locks = {name: threading.Lock() for name in self.camera_names}
        # Per-camera M4 state, built post-fork so every worker lifetime starts
        # with fresh tracking/zone state. core_worker guards access to these
        # with the same stats_locks[name] (see the M4 note in core_worker).
        trackers = {name: CentroidTracker() for name in self.camera_names}
        zone_engines = {
            name: ZoneEngine(self.zone_configs.get(name, [])) for name in self.camera_names
        }
        # M5: per-camera last detection-summary publish (wall clock).
        last_detection_publish = {name: 0.0 for name in self.camera_names}
        log.info("detection worker started for %s", ", ".join(self.camera_names))

        def update_stats(name, elapsed, detections, skipped):
            with stats_locks[name]:
                self.stats[name] = _update_stats(
                    self.stats.get(name), elapsed, detections, skipped
                )

        def feeder() -> None:
            while True:
                for name in self.camera_names:
                    _feed_camera(
                        name,
                        self.detection_flags,
                        self.ingest_manager,
                        gates[name],
                        work_queue,
                        update_stats,
                    )
                # v1.3: keep clip capture advancing independent of any single
                # camera's events (no-op when nothing is recording).
                if self.clip_store is not None:
                    self.clip_store.poll()

        def core_worker(core: int) -> None:
            detector = ObjectDetector(pool)
            while True:
                name, frame, skipped = work_queue.get()
                t0 = time.monotonic()
                detections = detector.detect(frame, core=core)
                elapsed = time.monotonic() - t0
                det_dicts = [
                    {
                        "class_name": d.class_name,
                        "confidence": round(d.confidence, 2),
                        "bbox": [round(v, 1) for v in d.bbox_xyxy],
                    }
                    for d in detections
                ]
                # M4 tracking/zone state is the same concurrency hazard the
                # M2.2 fix addressed for stats: the shared work queue means
                # both core threads can touch one camera's tracker, zone
                # engine and stats concurrently. Reuse that per-camera lock —
                # a corrupted track dict would silently wrong track IDs.
                with stats_locks[name]:
                    tracked = trackers[name].update(detections)
                    events = zone_engines[name].evaluate(name, tracked)
                    self.stats[name] = _update_stats(
                        self.stats.get(name),
                        elapsed,
                        det_dicts,
                        skipped,
                        zone_events=[event_to_dict(ev) for ev in events],
                    )
                    if self.dispatcher is not None:
                        # Non-blocking: publish() is a bounded-queue put with
                        # drop-oldest, so a down broker/endpoint can't stall
                        # this NPU-adjacent thread.
                        if self.publish_zone_events_flags.get(name, True):
                            zones = self.zone_configs.get(name, [])
                            for ev in events:
                                # Snapshot write is deliberately a blocking
                                # disk save here; event frequency is dwell/
                                # cooldown-limited (~1 per 30s), so an async
                                # writer would be over-engineering for this
                                # rate.
                                snapshot_path = None
                                if self.snapshot_store is not None:
                                    annotated = draw_annotations(
                                        frame, detections, zones, highlight_zone=ev.zone
                                    )
                                    snapshot_path = self.snapshot_store.save(
                                        name, ev.zone, ev.track_id, annotated, ev.timestamp
                                    )
                                # v1.3: post-roll clip for this event (a
                                # second event while one is recording simply
                                # extends the current clip — see ClipStore).
                                if self.clip_store is not None:
                                    self.clip_store.start_clip(
                                        name, ev.zone, ev.track_id, ev.timestamp
                                    )
                                self.dispatcher.publish_zone_event(
                                    name, ev, snapshot_path=snapshot_path
                                )
                        if self.publish_detections_flags.get(name, False) and detections:
                            interval = self.publish_configs.get(name, {}).get(
                                "detection_publish_interval_sec", 5.0
                            )
                            pub_now = time.time()
                            if pub_now - last_detection_publish[name] >= interval:
                                self.dispatcher.publish_detection_summary(
                                    name, detections, pub_now
                                )
                                last_detection_publish[name] = pub_now
                for ev in events:
                    log.warning(
                        "ZONE EVENT: %s/%s track=%s class=%s dwell=%.1fs",
                        ev.camera,
                        ev.zone,
                        ev.track_id,
                        ev.class_name,
                        ev.dwell_time_sec,
                    )

        threading.Thread(target=feeder, name="feeder", daemon=True).start()
        threading.Thread(target=core_worker, args=(0,), name="core-0", daemon=True).start()
        threading.Thread(target=core_worker, args=(1,), name="core-1", daemon=True).start()

        while True:
            time.sleep(60)  # block forever; workers are daemons

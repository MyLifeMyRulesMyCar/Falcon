"""M1.1 control panel: Flask dev server over an IngestManager.

All cameras begin stopped; the panel is what starts them. Camera edits
stay in-memory only — a restart of this process resets all state.

Usage:
    python scripts/run_control_panel.py [--host 127.0.0.1] [--port 5050] [--config PATH]
"""

import argparse
import atexit
import multiprocessing
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from nvr.config import load_config
from nvr.control.api import create_app
from nvr.control.preview_encoder import PreviewEncoder, make_slots
from nvr.inference.detection_worker import DetectionWorker
from nvr.ingest.frame_broadcast import LatestFrameStore
from nvr.ingest.manager import IngestManager
from nvr.output.dispatcher import OutputDispatcher
from nvr.output.clip_store import ClipStore
from nvr.output.http_publisher import HttpPublisher
from nvr.output.mqtt_publisher import MqttPublisher
from nvr.output.snapshot_store import SnapshotStore


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", type=str, default="127.0.0.1")
    parser.add_argument("--port", type=int, default=5050)
    parser.add_argument("--config", type=str, default="config/config.yaml")
    args = parser.parse_args()

    config = load_config(args.config)
    cameras = {c.name: c for c in config.cameras}
    # v1.1 event snapshots: one annotated .jpg per zone event, count-capped
    # per camera (rotation happens on save, inside the worker).
    snap_cfg = config.snapshots
    snapshot_store = SnapshotStore(
        snap_cfg.base_dir if snap_cfg else "snapshots",
        snap_cfg.max_per_camera if snap_cfg else 200,
    )
    # Must exist before start(): stream workers inherit the shared blocks
    # at fork time. Blocks are allocated lazily per camera on first write,
    # with that camera's own probed frame shape.
    frame_store = LatestFrameStore([c.name for c in config.cameras])
    atexit.register(frame_store.unlink_all)
    manager = IngestManager(
        config.cameras, frame_store=frame_store
    )  # cameras start stopped
    # v1.3 post-roll event clips: reads the shared preview blocks from the
    # worker process (frame_store's RawValues are fork-shared).
    clip_cfg = config.clips
    clip_store = ClipStore(
        clip_cfg.base_dir if clip_cfg else "clips",
        frame_store,
        clip_cfg.max_per_camera if clip_cfg else 30,
        clip_cfg.duration_sec if clip_cfg else 10.0,
    )
    stats = multiprocessing.Manager().dict()
    detection_flags = multiprocessing.Manager().dict()

    # M5 output: publishers own their network connections and drain bounded
    # queues in this (stable, never re-forked) process; the DetectionWorker
    # inherits them at fork and just enqueues. enabled flags are fork-shared
    # so the panel's live toggles reach the worker immediately.
    mqtt_publisher = MqttPublisher(config.mqtt) if config.mqtt else None
    http_publisher = HttpPublisher(config.http_output) if config.http_output else None
    dispatcher = OutputDispatcher(
        mqtt_publisher,
        http_publisher,
        config.mqtt.topic_prefix if config.mqtt else "nvr",
    )
    publish_zone_events_flags = multiprocessing.Manager().dict(
        {name: c.publish_zone_events for name, c in cameras.items()}
    )
    publish_detections_flags = multiprocessing.Manager().dict(
        {name: c.publish_detections for name, c in cameras.items()}
    )
    publish_configs = {
        name: {
            "publish_zone_events": c.publish_zone_events,
            "publish_detections": c.publish_detections,
            "detection_publish_interval_sec": c.detection_publish_interval_sec,
        }
        for name, c in cameras.items()
    }

    # Fresh on every call: cameras are renamed/edited live, so zones must be
    # recomputed from the current dict whenever a worker/encoder re-forks.
    def current_zone_configs() -> dict:
        return {name: c.zones for name, c in cameras.items()}

    worker = DetectionWorker(
        list(cameras),
        manager,
        stats,
        detection_flags,
        current_zone_configs(),
        dispatcher,
        publish_configs,
        publish_zone_events_flags,
        publish_detections_flags,
        snapshot_store=snapshot_store,
        clip_store=clip_store,
    )
    worker.start()

    # Dedicated preview encoders: all JPEG work leaves the panel's threads.
    # Two processes (2 cameras each), each pinned to its own A72 core, so
    # the encode load can't be preempted into jitter.
    slots = make_slots(list(cameras))

    def spawn_encoders() -> list:
        names = list(cameras)
        half = len(names) // 2
        zones = current_zone_configs()
        return [
            PreviewEncoder(names[:half], frame_store, slots, stats, detection_flags, a72_cores={4, 5}, zones=zones),
            PreviewEncoder(names[half:], frame_store, slots, stats, detection_flags, a72_cores={6, 7}, zones=zones),
        ]

    encoders = spawn_encoders()
    for enc in encoders:
        enc.start()
        atexit.register(enc.terminate)

    def restart_worker() -> None:
        """Re-fork the DetectionWorker so it inherits the current queue
        snapshot (multiprocessing.Queue objects are fork-only shared)."""
        nonlocal worker
        worker.terminate()
        worker.join(timeout=5)
        worker = DetectionWorker(
            list(cameras),
            manager,
            stats,
            detection_flags,
            current_zone_configs(),
            dispatcher,
            publish_configs,
            publish_zone_events_flags,
            publish_detections_flags,
            snapshot_store=snapshot_store,
            clip_store=clip_store,
        )
        worker.start()

    def restart_encoders() -> None:
        """Re-fork the PreviewEncoders so zone/name/url edits take effect
        (the encoder holds zones at construction time, like the worker)."""
        nonlocal encoders
        for enc in encoders:
            enc.terminate()
            enc.join(timeout=5)
        encoders = spawn_encoders()
        for enc in encoders:
            enc.start()
            atexit.register(enc.terminate)

    app = create_app(
        manager,
        cameras,
        detection_stats=stats,
        restart_detection=restart_worker,
        detection_flags=detection_flags,
        frame_store=frame_store,
        preview_slots=slots,
        restart_encoders=restart_encoders,
        config_path=args.config,
        mqtt_publisher=mqtt_publisher,
        http_publisher=http_publisher,
        publish_zone_events_flags=publish_zone_events_flags,
        publish_detections_flags=publish_detections_flags,
        mqtt_config=config.mqtt,
        http_output_config=config.http_output,
        snapshot_store=snapshot_store,
        snapshot_config=config.snapshots,
        clip_store=clip_store,
        clip_config=config.clips,
    )
    print(f"control panel: http://{args.host}:{args.port}  ({len(cameras)} cameras)")
    app.run(host=args.host, port=args.port, threaded=True)


if __name__ == "__main__":
    main()

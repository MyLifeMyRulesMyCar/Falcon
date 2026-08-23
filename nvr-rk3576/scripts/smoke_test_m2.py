"""M2 smoke test: ingest (M1) + one DetectionWorker on NPU core 0.

Every ``--interval`` seconds prints per camera:
    ingest_fps | inference_fps | skip_ratio | last_detections

Usage:
    python scripts/smoke_test_m2.py [--duration SECONDS] [--config PATH]
"""

import argparse
import logging
import multiprocessing
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from nvr.config import load_config
from nvr.inference.detection_worker import DetectionWorker
from nvr.ingest.manager import IngestManager


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--duration", type=float, default=120.0)
    parser.add_argument("--config", type=str, default="config/config.yaml")
    parser.add_argument("--interval", type=float, default=10.0)
    args = parser.parse_args()

    config = load_config(args.config)
    print(f"cameras: {', '.join(c.name for c in config.cameras)}")

    manager = IngestManager(config.cameras)
    manager.start()

    stats = multiprocessing.Manager().dict()
    worker = DetectionWorker([c.name for c in config.cameras], manager, stats)
    worker.start()

    prev_frames = {c.name: 0 for c in config.cameras}
    prev_totals = {c.name: 0 for c in config.cameras}
    last_report = time.monotonic()
    deadline = time.monotonic() + args.duration

    def report(label: str) -> None:
        st = dict(stats)
        print(f"\n--- {label} ---", flush=True)
        print(
            f"{'name':<6} | {'ingest_fps':>10} | {'infer_fps':>10} | "
            f"{'skip_ratio':>10} | last_detections",
            flush=True,
        )
        combined = 0.0
        for cam in manager.stats():
            s = manager.stats()[cam]
            d = st.get(cam, {})
            # True ingest fps: decoder production (immune to queue consumers).
            decoded = int(s.get("frames_decoded", 0))
            ingest_fps = (decoded - prev_frames[cam]) / max(args.interval, 0.001)
            prev_frames[cam] = decoded
            total = d.get("total", 0)
            combined += (total - prev_totals[cam]) / max(args.interval, 0.001)
            prev_totals[cam] = total
            infer_fps = float(d.get("inference_fps", 0.0))
            skip_ratio = float(d.get("skip_ratio", 0.0))
            last = ", ".join(
                f"{x['class_name']}:{x['confidence']:.2f}"
                for x in d.get("last_detections", [])
            )[:60]
            print(
                f"{cam:<6} | {ingest_fps:>10.1f} | {infer_fps:>10.1f} | "
                f"{skip_ratio:>10.2f} | {last}",
                flush=True,
            )
        print(f"combined detection throughput: {combined:.1f} detections/s", flush=True)

    try:
        while time.monotonic() < deadline:
            manager.consume(timeout=0.02)
            now = time.monotonic()
            if now - last_report >= args.interval:
                report(f"t+{now - (deadline - args.duration):.0f}s")
                last_report = now
    except KeyboardInterrupt:
        print("\ninterrupted")
    finally:
        report("final")
        worker.terminate()
        worker.join(timeout=5)
        manager.stop()


if __name__ == "__main__":
    main()

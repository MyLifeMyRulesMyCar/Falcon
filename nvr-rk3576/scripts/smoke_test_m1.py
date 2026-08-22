"""M1 smoke test: run all cameras through IngestManager and report per-camera stats.

Usage:
    python scripts/smoke_test_m1.py [--duration SECONDS] [--config PATH] [--interval SECONDS]

Prints a status line per camera every ``--interval`` seconds:
    name | alive | frames | fps | restarts
and a final summary table on exit.
"""

import argparse
import logging
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from nvr.config import load_config
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
    print(f"cameras: {', '.join(c.name for c in config.cameras)}", flush=True)

    manager = IngestManager(config.cameras)
    manager.start()

    prev_frames = {c.name: 0 for c in config.cameras}
    last_report = time.monotonic()
    deadline = time.monotonic() + args.duration

    def report(label: str) -> None:
        stats = manager.stats()
        print(f"\n--- {label} ---", flush=True)
        print(f"{'name':<6} | {'alive':<5} | {'frames':>9} | {'fps':>6} | {'restarts':>8}", flush=True)
        for name, s in sorted(stats.items()):
            interval = max(args.interval, 0.001)
            fps = (s["frames_received"] - prev_frames[name]) / interval
            print(
                f"{name:<6} | {str(s['alive']):<5} | {s['frames_received']:>9} | "
                f"{fps:>6.1f} | {s['restart_count']:>8}",
                flush=True,
            )
            prev_frames[name] = s["frames_received"]

    try:
        while time.monotonic() < deadline:
            manager.consume(timeout=0.05)
            now = time.monotonic()
            if now - last_report >= args.interval:
                report(f"t+{now - (deadline - args.duration):.0f}s")
                last_report = now
    except KeyboardInterrupt:
        print("\ninterrupted")
    finally:
        report("final")
        manager.stop()
        print("\nsummary:", flush=True)
        for name, s in sorted(manager.stats().items()):
            print(
                f"  {name}: frames={s['frames_received']} "
                f"restarts={s['restart_count']} alive={s['alive']}",
                flush=True,
            )


if __name__ == "__main__":
    main()

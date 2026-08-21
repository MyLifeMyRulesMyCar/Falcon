"""M0 smoke test: pull frames from one StreamWorker for a fixed duration.

Usage:
    python scripts/smoke_test_m0.py [--duration SECONDS] [--config PATH]

Prints total frames, measured FPS and sample frame paths. Sample frames are
written as 24-bit BMP files to /tmp/m0_frame_<n>.bmp.
"""

import argparse
import logging
import multiprocessing
import struct
import time
from pathlib import Path

import numpy as np

from nvr.config import load_config
from nvr.ingest.stream_worker import StreamWorker

SAMPLE_EVERY = 300
SAMPLE_DIR = Path("/tmp")


def write_bmp(path: Path, frame: np.ndarray) -> None:
    """Write an (h, w, 3) BGR uint8 array as a 24-bit BMP file."""
    height, width = frame.shape[:2]
    row_padding = (4 - (width * 3) % 4) % 4
    row_size = width * 3 + row_padding
    pixel_offset = 54
    file_size = pixel_offset + row_size * height
    with open(path, "wb") as f:
        f.write(struct.pack("<2sIHHI", b"BM", file_size, 0, 0, pixel_offset))
        f.write(struct.pack("<IiiHHIIiiII", 40, width, height, 1, 24, 0,
                            row_size * height, 2835, 2835, 0, 0))
        for row in frame[::-1]:
            f.write(row.tobytes())
            f.write(b"\x00" * row_padding)


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--duration", type=float, default=30.0)
    parser.add_argument("--config", type=str, default="config/config.yaml")
    args = parser.parse_args()

    config = load_config(args.config)
    camera = config.cameras[0]
    logging.getLogger("nvr").info("camera: %s (%s)", camera.name, camera.url)

    frame_queue: multiprocessing.Queue = multiprocessing.Queue(maxsize=256)
    worker = StreamWorker(camera, frame_queue)
    worker.start()

    deadline = time.monotonic() + args.duration
    frame_count = 0
    samples: list[str] = []
    while time.monotonic() < deadline:
        try:
            frame = frame_queue.get(timeout=1.0)
        except multiprocessing.queues.Empty:
            if not worker.is_alive():
                logging.getLogger("nvr").error("worker died; exiting")
                return
            continue
        frame_count += 1
        if frame_count == 1 or frame_count % SAMPLE_EVERY == 0:
            path = SAMPLE_DIR / f"m0_frame_{frame_count}.bmp"
            write_bmp(path, frame)
            samples.append(str(path))
            logging.getLogger("nvr").info("sample written: %s", path)

    worker.terminate()
    worker.join(timeout=10)
    if worker.is_alive():
        worker.kill()
        worker.join(timeout=10)

    fps = frame_count / args.duration
    print(f"frames received: {frame_count}")
    print(f"duration: {args.duration:.1f}s")
    print(f"measured fps: {fps:.2f}")
    print("sample frames:")
    for s in samples:
        print(f"  {s}")


if __name__ == "__main__":
    main()

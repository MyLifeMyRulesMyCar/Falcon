"""M2 calibration + correctness gate (board-only, needs the NPU).

Runs ObjectDetector on the demo's bus.jpg and scores it against the C demo's
ground truth (best-IoU match per entry, summed |confidence delta|).

With ``--profile`` it runs repeated inferences and prints averaged per-stage
timings so the Python pre/post-processing bottleneck can be isolated from NPU
compute.

Usage:
    python scripts/verify_detector_m2.py [--profile --reps 20]
"""

import argparse
import subprocess
import sys
import tempfile
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from nvr.inference.detector import ObjectDetector
from nvr.inference.npu_pool import NpuCorePool

MODEL = "nvr/inference/model/yolov5s_relu_rk3576.rknn"
BUS_JPG = Path.home() / "rk3576_rknn_yolov5_demo/model/bus.jpg"

GROUND_TRUTH = [
    {"class": "person", "bbox": (209, 243, 286, 510), "conf": 0.880},
    {"class": "person", "bbox": (479, 238, 560, 526), "conf": 0.871},
    {"class": "person", "bbox": (109, 237, 232, 534), "conf": 0.832},
    {"class": "bus", "bbox": (93, 129, 553, 464), "conf": 0.705},
    {"class": "person", "bbox": (79, 353, 122, 517), "conf": 0.301},
]


def load_bus_bgr() -> np.ndarray:
    """Decode bus.jpg to raw bgr24 via ffmpeg (same path StreamWorker uses)."""
    with tempfile.NamedTemporaryFile(suffix=".raw") as f:
        subprocess.run(
            [
                "ffmpeg", "-y", "-loglevel", "error", "-i", str(BUS_JPG),
                "-f", "rawvideo", "-pix_fmt", "bgr24", f.name,
            ],
            check=True,
        )
        data = np.fromfile(f.name, dtype=np.uint8)
    h, w = 640, 640
    return data.reshape((h, w, 3))


def iou(a, b):
    ax1, ay1, ax2, ay2 = a
    bx1, by1, bx2, by2 = b
    ix1, iy1 = max(ax1, bx1), max(ay1, by1)
    ix2, iy2 = min(ax2, bx2), min(ay2, by2)
    iw, ih = ix2 - ix1, iy2 - iy1
    if iw <= 0 or ih <= 0:
        return 0.0
    inter = iw * ih
    union = (ax2 - ax1) * (ay2 - ay1) + (bx2 - bx1) * (by2 - by1) - inter
    return inter / union if union > 0 else 0.0


def compare(detections, label: str) -> float:
    print(f"\n--- {label} ---")
    score = 0.0
    for gt in GROUND_TRUTH:
        best = None
        for d in detections:
            if d.class_name != gt["class"]:
                continue
            ov = iou(gt["bbox"], d.bbox_xyxy)
            if best is None or ov > best[0]:
                best = (ov, d.confidence)
        if best is None:
            print(f"  {gt['class']:<8} MISSING (ground truth {gt['conf']:.3f})")
            score += 1.0
            continue
        ov, conf = best
        delta = abs(conf - gt["conf"])
        score += delta
        print(
            f"  {gt['class']:<8} IoU={ov:.3f} conf={conf:.3f} "
            f"(gt {gt['conf']:.3f}, delta {delta:+.3f})"
        )
    print(f"  score: {score:.3f}")
    return score


def _profile_loop(det, frame, reps):
    """Run detect() ``reps`` times with profile=True and print averaged timings."""
    timings = []
    for _ in range(reps):
        _, t = det.detect(frame, profile=True)
        timings.append(t)

    print(f"\n--- profile ({reps} reps) ---")
    keys = [
        "letterbox_ms",
        "npu_inference_ms",
        "decode_ms",
        "nms_ms",
        "map_to_frame_ms",
        "total_ms",
    ]
    for k in keys:
        vals = [t[k] for t in timings]
        print(f"  {k:<20} mean={sum(vals) / len(vals):6.2f} ms  "
              f"min={min(vals):6.2f} ms  max={max(vals):6.2f} ms")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--profile", action="store_true", help="run repeated inferences and print timings")
    parser.add_argument("--reps", type=int, default=20, help="repetitions for profiling")
    args = parser.parse_args()

    frame = load_bus_bgr()
    print(f"bus.jpg decoded: {frame.shape}")

    pool = NpuCorePool(MODEL)
    try:
        det = ObjectDetector(pool)
        if args.profile:
            _profile_loop(det, frame, args.reps)

        # Always run/print correctness once at the end.
        detections = det.detect(frame)
        print(f"\n  -> {len(detections)} detections:")
        for d in sorted(detections, key=lambda d: -d.confidence):
            print(
                f"     {d.class_name:<8} {d.confidence:.3f} "
                f"{tuple(round(v, 1) for v in d.bbox_xyxy)}"
            )
        score = compare(detections, label="calibrated decode (no sigmoid, wh=(2*wh)^2)")
    finally:
        pool.release()
    print(f"\nscore: {score:.3f}")


if __name__ == "__main__":
    main()

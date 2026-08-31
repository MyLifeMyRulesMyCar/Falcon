# v0.2 — NPU Detection + Motion Gate

v0.2 adds real-time object detection (YOLOv5s, COCO-80) on the RK3576 NPU,
gated by a per-camera motion detector, with live results surfaced in the
control panel (v0.2.1), dual-core NPU parallelism (v0.2.2), and an ingest-
regression investigation that ended in a measurement fix (v0.2.3). Verified
on the board end-to-end.

## Layout

```
nvr/
  motion/
    motion_gate.py          # per-camera frame gate (absdiff + skip counter)
  inference/
    npu_pool.py             # two RKNNLite handles, one per NPU core
    detector.py             # letterbox -> decode 3 heads -> NMS -> map
    detection_worker.py     # single process: feeder + 2 core threads
    inspect_model.py        # Phase A validation script
    labels_coco80.txt       # COCO-80 label list
    model/yolov5s_relu_rk3576.rknn   # model (gitignored; copy from ~/rk3576_rknn_yolov5_demo/model/)
scripts/
  verify_detector_m2.py     # calibration gate vs the C demo's ground truth
  smoke_test_m2.py          # per-camera ingest/infer fps, skip ratio, detections
tests/
  test_motion_gate.py
  test_detector_postprocess.py
  test_detection_worker.py
```

## Phase A — environment + model I/O (resolved before any pipeline code)

- NPU confirmed working: `CONFIG_ROCKCHIP_RKNPU=y`, dmesg `RKNPU 27700000.npu`,
  driver 0.9.8; the demo's `librknnrt.so` is 2.3.2; `rknn-toolkit-lite2 2.3.2`
  installed in the venv (bundles runtime 2.3.0 — works).
- **There is no `/dev/rknpu` on Radxa OS 6.1.** The kernel registers the NPU as
  a DRM device (`CONFIG_ROCKCHIP_RKNPU_DRM_GEM=y`, `[drm] Initialized rknpu
  0.9.8 ... on minor 1`), so the runtime reaches it through the render node
  `/dev/dri/renderD129` (group `render`). Verified with the pip 2.3.2 wheel and
  with the Radxa system packages `python3-rknnlite2` 2.3.0 + `rknpu2-rk3588`.
  Do not chase a missing `/dev/rknpu` — there is no udev rule to add either.
- `inspect_model.py` (zeros 640x640 frame): outputs
  `(1,255,80,80) / (1,255,40,40) / (1,255,20,20)`, **dtype=float32** — the
  runtime dequantizes; no zero-point math needed in Python.

## Calibration — the correctness gate (bus.jpg vs the C demo)

`scripts/verify_detector_m2.py` scores the detector against the demo's 5
ground-truth detections (best-IoU match, summed |confidence delta|). Three
real bugs were found and fixed by this gate:

| finding | fix | effect |
|---|---|---|
| ReLU-variant graph bakes sigmoid into xy/obj/class | `apply_sigmoid=False` (the dual-path experiment: **1.511 vs 0.366** — losing path deleted) | confidences correct |
| Model expects **RGB**; ingest delivers BGR | channel flip in `detect()` | bus conf 0.38 → 0.697 (vs GT 0.705) |
| wh decode is `(2*wh)^2 * anchor` (YOLOv8-style, from the demo binary's disassembly), not `exp(wh)` | corrected decode | IoU 0.29 → 0.74-0.95 |

**Final gate result — score 0.029:**

| GT | class | IoU | conf (GT) | conf (ours) |
|---|---|---|---|---|
| 1 | person | 0.883 | 0.880 | 0.884 |
| 2 | person | 0.847 | 0.871 | 0.867 |
| 3 | person | 0.913 | 0.832 | 0.832 |
| 4 | bus | 0.950 | 0.705 | 0.697 |
| 5 | person | 0.739 | 0.301 | 0.314 |

(Confidences within ±0.013 on all five; the two sub-0.9 IoUs are small
boxes where 4-6 px quantization differences dominate.)

## Detection pipeline

```
DetectionWorker (one process; owns both NPU cores)
  feeder thread   batch-reads each camera queue (up to 8), runs MotionGate,
                  pushes (camera, frame, skipped) to a shared queue.Queue(8)
  core-0 thread   ObjectDetector(detect on NPU core 0)
  core-1 thread   ObjectDetector(detect on NPU core 1)
                  — whichever core frees first grabs the next frame
  stats           Manager().dict(): per camera {total, skipped, infer ring,
                  inference_fps, skip_ratio, last_detections} — JSON-safe
                  (detections stored as plain dicts), updated under a
                  per-camera lock (shared queue -> both threads can touch
                  one camera's stats)
```

- The worker is **re-forked by the panel on every start/stop/rename**:
  multiprocessing.Queue objects can only be shared through fork
  inheritance (a Manager dict rejects them with a RuntimeError — tried),
  so each worker lifetime gets an exact snapshot of the running cameras.
- `get_queue()` returns `None` for stopped cameras (never KeyError) so the
  worker treats "not running" as a normal state.
- The panel prefers the worker's `total+skipped` as the frame count (the
  worker consumes every frame for the gate, starving the manager's drain
  counter), and the **true ingest** column comes from the decoder's own
  `frames_decoded` counter (see v0.2.3).

## Performance work

### Stage profile (bus.jpg, before -> after lever D)

| stage | before | after | change |
|---|---|---|---|
| letterbox | 20.5 ms | **6.4 ms** | 1-D row gather / stride-slicing fast paths (`frame[ys]` 0.56 ms vs `np.ix_` 20.2 ms) |
| decode (3 heads) | 24.2 ms | **3.9 ms** | obj-threshold mask first, then per-cell class argmax (kills the (80,80,80) product + double reduction) |
| NPU inference | 34.1 ms | 26.3 ms | fused cores measured 1.3x (later reverted for dual-core parallelism) |
| NMS | 0.4 ms | 0.4 ms | negligible |
| **total** | **79 ms** | **~37-44 ms** | ~22-27 fps single-camera |

### Dual-core parallelism (v0.2.2)

- **GIL check** (threaded vs sequential inference, N=40): clean run
  **1.91x** (2.125 s -> 1.112 s) — the GIL releases during `inference()`;
  threads confirmed over processes.
- **Step 0 baseline** (4 cameras, 60 s, fused-core worker): true combined
  throughput **8.46 detections/s**.
- **Step 4** (dual-core worker): **12.32 detections/s (1.46x)** — below the
  2x ideal; the shared-queue -> dual-core path itself proved out with a
  single camera: **cam_a-only = 17.2 detections/s (~1.8x vs single core)**.
  The 4-camera cap is detection *demand* (ingest supply + motion gate), not
  NPU capacity.

### Ingest regression investigation (v0.2.3)

- The reported 8-15x ingest collapse under detection load was **a metric
  artifact**: with the DetectionWorker consuming every queue frame, the
  ingest column (worker `total+skipped`) was bounded by NPU backpressure,
  not decoder production.
- Fix: a `frames_decoded` counter incremented in `StreamWorker`'s read loop
  (additive; the v0.0 ffmpeg invocation untouched), surfaced via
  `manager.stats()` -> API -> panel `fps` column and the smoke's
  `ingest_fps`. **True ingest with detection active: 25-30 fps on the
  local cameras — equal to or above ingest-only runs. No CPU contention,
  no affinity pinning, no transport rewrite needed.**
- Also fixed: `MODEL_PATH` was cwd-relative (worker died on any launch
  outside `nvr-rk3576/`); now file-relative.

## Test results (as of v0.2.3)

```
pytest tests/ -q
72 passed
```

- 72 passed on the fresh-board rerun (Aug 2026) — the suite has grown since
  the 57 in the original v0.2.3 write-up.

- `test_motion_gate.py` (5): identical frames skip; synthetic bright block
  triggers; below-threshold change does not; skip-counter forces a pass
  after max_skip_frames+1; odd dimensions.
- `test_detector_postprocess.py` (4): synthetic hot-anchor-cell decode
  (class/box/anchors), NMS duplicate suppression, IoU math, letterbox
  identity/scale/pad.
- `test_detection_worker.py` (4): `_update_stats` JSON-safe detections,
  skipped path, mixed skip ratio, 3-detection cap.
- v0.1/v0.1.1 suites still green (manager, control API, stream worker,
  config).

### Live panel run (4 cameras, after v0.2.3)

| camera | true ingest fps | infer fps | skip ratio | sample detections |
|---|---|---|---|---|
| cam_a | 29.9 | 13.7 | 0.57 | person x3 |
| cam_b | 25.7 | 12.2 | 0.16 | dog, cat x2 |
| cam_c | 24.5 | 13.1 | 0.35 | — |
| cam_d | 26.3 | 12.0 | 0.64 | — |

(Per-camera `infer fps` is per-inference speed (1/mean elapsed), not
per-camera cadence; cadence = combined/N. cam_a/cam_d are the local
testbed with the Big Buck Bunny clip; cam_b/cam_c are public VOD streams.)

Fresh-board reproduction (Aug 2026): the calibration table above is unchanged
(score 0.029); the live-panel run used all four *local* testbed cameras and
held ~25-30 fps true ingest, infer ~12-20 fps, combined ~16-18/s, with zero
steady-state restarts.

## Known limitations / next levers

- Per-camera detection cadence is capped near ~12/s (combined ~18-27/s)
  by motion-gated ingest demand, not NPU capacity; the NPU side measured
  1.91x parallel. **The only path to 15-20 fps/camera across 4 streams is
  a smaller model (320x320 or yolov5n), converted on a PC with
  rknn-toolkit2** — the board can't convert.
- Queued (from the reference repo, not yet needed): Numba JIT on the
  postprocess (their measured 15ms -> 5ms), FP16 quantization, batch
  inference — only if postprocess resurfaces as the limiter once ingest
  demand rises.
- No bounding-box overlays on live video (text detections in the panel
  only) — a separate feature.
- Core-1 pose reservation is gone: the whole NPU serves detection; v0.3
  (pose) must re-plan (time-slicing or a separate pool instance).

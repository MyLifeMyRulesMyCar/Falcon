# nvr-rk3576

Multi-camera NVR ingest layer for Radxa CM4 (RK3576).

- **M0** — single-stream ingest: decode one network stream with
  hardware-accelerated ffmpeg and push raw BGR frames onto a bounded queue.
- **M1** — four streams over four protocols (RTSP/RTMP/HLS/HTTP-FLV) under
  one `IngestManager`, with a local testbed, a smoke test, and a minimal
  operator control panel (M1.1). Details in `docs/m1_ingest.md`.
- **M2** — NPU object detection (YOLOv5s/COCO-80) gated by a per-camera
  motion detector, dual-core NPU parallelism, live detections in the
  control panel, and true-ingest instrumentation. Details in
  `docs/m2_detection.md`.

## Requirements

- RK3576 board with the Rockchip MPP kernel driver (`/dev/mpp_service`)
- NPU reachable through the **DRM render node**: on Radxa OS 6.1 there is no
  `/dev/rknpu` — the kernel registers the NPU as a DRM device
  (`CONFIG_ROCKCHIP_RKNPU_DRM_GEM=y`, driver 0.9.8) and librknnrt opens
  `/dev/dri/renderD129` on its own. See "Fresh-board bring-up notes".
- ffmpeg/ffprobe built with `--enable-rkmpp` and `--enable-openssl`
- Python 3.11 venv with `numpy`, `pyyaml`, `pytest`, `flask` and
  `rknn-toolkit-lite2==2.3.2` (the system `python3-rknnlite2` 2.3.0 works too)

> **Note on `rkmpp` hardware acceleration:** upstream ffmpeg exposes rkmpp as
> hardware *decoders* (`h264_rkmpp`, `hevc_rkmpp`, etc.), not as a `hwaccel`
> method. On this board the working invocation is `-hwaccel drm` together with
> `-init_hw_device drm:/dev/dri/renderD128`. `ffmpeg -hwaccels` will list
> `drm`, not `rkmpp`.

## Setup

```bash
python -m venv .venv
.venv/bin/pip install -r requirements.txt
```

The ffmpeg binary is used as a subprocess; no Python decode library is involved.

## Building ffmpeg from source

If the system ffmpeg lacks rkmpp or HTTPS support, obtain an FFmpeg source tree
separately and follow `docs/ffmpeg_rebuild_step1.md`; the short version is:

```bash
sudo apt update
sudo apt install -y libssl-dev

cd /path/to/ffmpeg-8.1.2
./configure \
    --enable-rkmpp \
    --enable-libdrm \
    --enable-openssl \
    --enable-version3 \
    --prefix=/usr/local \
    --disable-doc \
    --disable-debug

make -j$(nproc)
sudo make install
```

Verify after install:

```bash
ffmpeg -decoders | grep rkmpp      # h264_rkmpp, hevc_rkmpp, ...
ffmpeg -protocols | grep https      # https, tls
ffprobe https://test-streams.mux.dev/x36xhzz/x36xhzz.m3u8
```

## Fresh-board bring-up notes (Radxa OS 6.1.84, reproduced Aug 2026)

Things that are easy to trip over on a clean board, all verified here:

- **NPU device node.** This kernel registers the RKNPU driver as a *DRM*
  device (`[drm] Initialized rknpu 0.9.8 ... on minor 1`) because
  `CONFIG_ROCKCHIP_RKNPU_DRM_GEM=y`. There is **no `/dev/rknpu`**, no misc
  device, and no udev rule to write. librknnrt >= 2.3.0 finds the NPU through
  `/dev/dri/renderD129` (group `render`) on its own. Confirm the stack with
  the C demo before touching Python:
  ```bash
  cd ~/rk3576_rknn_yolov5_demo && ./rknn_yolov5_demo \
      ./model/yolov5s_relu_rk3576.rknn ./model/bus.jpg
  # expect the 5 ground-truth detections: person 0.880/0.871/0.832, bus 0.705, person 0.301
  ```
- **Model + calibration asset.** `nvr/inference/model/*.rknn` is gitignored.
  Get it from the Radxa demo tarball (which also provides `bus.jpg` —
  `scripts/verify_detector_m2.py` reads it from `~/rk3576_rknn_yolov5_demo/model/`):
  ```bash
  curl -sL -o /tmp/rk3576_rknn_yolov5_demo.tar.gz \
      https://dl.radxa.com/rock4/4d/images/rk3576_rknn_yolov5_demo.tar.gz
  tar -xzf /tmp/rk3576_rknn_yolov5_demo.tar.gz -C ~/
  mkdir -p nvr/inference/model && cp \
      ~/rk3576_rknn_yolov5_demo/model/yolov5s_relu_rk3576.rknn \
      nvr/inference/model/
  ```
- **mediamtx: pin v1.12.2.** The testbed binary is `testbed/mediamtx`
  (gitignored). The current release (v1.20.x) crashes its HLS muxer on the
  looped h264_rkmpp sample (`unable to extract DTS: too many reordered
  frames`), dragging cam_c down to ~0-9 fps with repeated restarts. v1.12.2
  keeps all four cameras at ~30 fps / 0 restarts.
- **Network quirks on this board.** GitHub *release-asset* downloads (Azure
  blob) and Google-hosted endpoints (proxy.golang.org, go.dev/dl) stall
  mid-transfer, while GitHub API/codeload, PyPI, ffmpeg.org, dl.radxa.com and
  the Debian mirrors are fine. If a release download hangs, route it through
  a mirror, e.g.
  `curl -L -o mediamtx.tar.gz https://ghproxy.net/https://github.com/bluenviron/mediamtx/releases/download/v1.12.2/mediamtx_v1.12.2_linux_arm64.tar.gz`
- **Paths / venv.** The docs and systemd units assume the repo lives at
  `/home/radxa/falcon/...`. On this machine it is `/home/radxa/Falcon`,
  bridged with `ln -s /home/radxa/Falcon /home/radxa/falcon`. The venv sits
  at the repo root (`/home/radxa/Falcon/.venv`, gitignored) so the systemd
  `ExecStart` paths and `scripts/run_panel.sh` work as written.

## Configuration

`config/config.yaml` lists cameras; only `name` and `url` are used. The URL
may be RTSP, RTMP, HLS, plain HTTP or HTTPS — ffmpeg handles the transport.
The default config mixes local testbed cameras (cam_a/cam_d — start them
with `./testbed/start_testbed.sh`) with public internet streams verified
reachable from the board (cam_b/cam_c):

```yaml
cameras:
  - name: cam_a
    url: rtsp://127.0.0.1:8554/cam_a
  - name: cam_b
    url: https://uploads.video-commander.com/sample/BigBuckBunny.mp4
  - name: cam_c
    url: https://test-streams.mux.dev/x36xhzz/x36xhzz.m3u8
  - name: cam_d
    url: http://127.0.0.1:8080/cam_d.flv
```

Public streams can go offline without notice; swap any URL via the panel or
this file. cam_b is a VOD MP4 (decodes as fast as the board allows — fps
spikes and it replays on EOF); cam_c is a VOD HLS (replays on EOF too).

## Usage

```bash
pytest tests/                              # unit tests (no network)

./testbed/start_testbed.sh                 # M1: 4 simulated cameras
python scripts/smoke_test_m1.py --duration 120    # per-camera fps/restarts
python scripts/smoke_test_m1.py --duration 1800   # 30-min soak

python scripts/verify_detector_m2.py       # M2: bus.jpg calibration gate (needs NPU)
python scripts/smoke_test_m2.py --duration 120    # M2: ingest|infer fps, skip%, detections

python scripts/run_control_panel.py        # M1.1/M2: http://127.0.0.1:5050
```

The M0 smoke test (`scripts/smoke_test_m0.py`) is unchanged but now uses
`cam_a` from the config, so the testbed must be running.

## Operations

### Manual start / stop / restart

```bash
# everything (after a reboot, or to re-run the whole stack)
./testbed/start_testbed.sh            # 4 simulated cameras (mediamtx + publishers)
python scripts/run_control_panel.py --host 0.0.0.0   # panel on :5050 (foreground)
# or detached:
nohup setsid scripts/run_panel.sh < /dev/null > /dev/null 2>&1 &

# start the cameras once the panel is up (or click Start in the UI)
for c in cam_a cam_b cam_c cam_d; do
  curl -s -X POST http://127.0.0.1:5050/api/cameras/$c/start > /dev/null
done

# stop
pkill -f run_control_panel          # panel + detection worker + encoders
./testbed/stop_testbed.sh all       # mediamtx + publishers
./testbed/stop_testbed.sh cam_b     # just one camera's publisher (reconnect tests)
```

A panel restart resets in-memory state: cameras come back stopped and any
URL edits are lost (`config/config.yaml` is the source of truth).

### Auto-start on boot (systemd)

Two enabled units bring the whole stack up automatically on reboot:

```bash
systemctl status nvr-testbed nvr-panel      # check
systemctl restart nvr-panel                 # restart the panel
journalctl -u nvr-panel -f                  # panel logs (Python stdout/stderr)
sudo systemctl disable --now nvr-testbed nvr-panel   # stop auto-starting
```

`nvr-testbed.service` starts mediamtx + the 4 publishers; `nvr-panel.service`
starts the control panel (ingest + detection + preview encoders) and then
auto-starts all 4 cameras via `scripts/start_cameras.sh`. The panel runs as
a `Type=simple` foreground process, so it is managed by systemd
(`Restart=on-failure`) and its logs land in the journal.

## M1 — 4-stream mixed-protocol ingest

One `IngestManager` runs one `StreamWorker` per camera, each with its own
bounded frame queue and restart counter. See `docs/m1_ingest.md` for the
full write-up (testbed, manager API, control panel, known pitfalls).

Verified on the board:

- 4 cameras over RTSP / RTMP / HLS / HTTP-FLV, each independently killable
  and recoverable without touching the others.
- Reproduced on a fresh board (Aug 2026): all four cameras hold ~30 fps with
  zero steady-state restarts — mediamtx is pinned to **v1.12.2** (v1.20.x's
  strict HLS muxer crashes on the looped h264_rkmpp sample; see bring-up notes).
- 30-min soak: 53k frames per camera at ~30 fps, `restarts: 0`, memory flat.
- CPU budget for M2: ~63% of one core for 4x 640x360 decode+colorspace
  (~2.4 cores projected for 4x 720p); Python ingest stack ~1.5 cores.

## M2 — NPU detection + motion gate

One `DetectionWorker` owns both NPU cores: a feeder thread motion-gates
frames from all cameras into a shared queue; two core threads (one per
NPU core) run the detector. The panel shows per-camera `infer fps`,
`skip%` (motion-gate savings) and live detections. Verified on the board:

- Calibration gate: all 5 bus.jpg ground-truth detections within conf
  ±0.013 (score 0.029); decode findings baked in (no sigmoid — baked into
  the ReLU-variant graph; `wh = (2*wh)^2 * anchor`; BGR->RGB flip).
- Dual-core parallelism: GIL check 1.91x; cam_a-only 17.2 detections/s
  (~1.8x); 4-camera combined ~12/s, capped by ingest demand, not NPU.
- True ingest (decoder-counted) stays ~25-30 fps with detection active —
  the earlier "ingest regression" was a metric artifact.
- pytest: 72 passed (the suite grew since the 57 in the M2 write-up).
- Fresh-board reproduction (Aug 2026): identical bus.jpg gate score 0.029;
  live panel holds all four cameras at ~30 fps true ingest with zero
  steady-state restarts and per-camera infer ~12-20 fps (combined ~16-18/s).

## Control panel (M1.1)

```bash
./testbed/start_testbed.sh                 # 4 simulated cameras
python scripts/run_control_panel.py        # http://127.0.0.1:5050
```

Start/stop individual cameras, edit their name/url (fields are locked while
a camera is running), and watch fps/frames/restarts. Each row has a
**watch** link that opens the mediamtx player for that camera
(`http://<panel-host>:8888/<name>/`). Known limitation:
camera edits are in-memory only — a panel restart resets all state and
`config/config.yaml` is never written. Config persistence is a future
increment, deliberately not part of M1.1.

The panel binds `127.0.0.1` by default; to reach it from another machine on
the LAN, run `python scripts/run_control_panel.py --host 0.0.0.0` and open
`http://<board-ip>:5050/`. There is no auth — LAN use only.

## Architecture

```
StreamWorker (multiprocessing.Process, one per camera)
  ffprobe          -> width, height, fps (retried with backoff on failure)
  ffmpeg -init_hw_device drm:/dev/dri/renderD128 -hwaccel drm \
         -i <url> -f rawvideo -pix_fmt bgr24 -vsync 0 pipe:1
  read frame_size bytes -> np (h, w, 3) -> frame_queue (drop-oldest)
  on short read: exponential backoff (min(2**attempt, 30)s), respawn ffmpeg

IngestManager (M1) — one queue + restart counter per camera, spawns/terminates
  workers individually, drains queues (consume()) to advance frame counts.
```

- Queue maxsize is chosen by the caller: 256 in the M0 smoke test, 16 in the
  M1 manager (4 cameras x 16 x 0.7 MB frames keeps the worst-case buffer
  small on this board; drop-oldest still smooths consumer hiccups).

- Decode is hw (rkmpp/MPP); the bgr24 colorspace conversion is done by
  ffmpeg on CPU for now. A zero-copy hw pipeline is a later milestone.
- The queue is non-blocking drop-oldest: a slow consumer never stalls ingest.
- ffmpeg stderr is drained by a background thread so the pipe can't fill
  and deadlock long runs.

## M0 acceptance (on the RK3576 board)

1. `pytest tests/` green
2. Smoke test FPS matches the FPS `_probe_stream` reports
3. 30-minute run: Python RSS stays flat
4. Block the stream host (e.g. `iptables -I OUTPUT -d <host> -j DROP`) for
   20s mid-run: worker logs a restart, backs off, resumes when reachable
5. Point `config/config.yaml` at the real third-party stream — unchanged code

Measured FPS and CPU per stream drive the M2 NPU/motion-gate budget
(see `docs/m1_ingest.md` for the numbers).

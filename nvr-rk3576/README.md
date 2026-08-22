# nvr-rk3576

Multi-camera NVR ingest layer for Radxa CM4 (RK3576).

- **M0** — single-stream ingest: decode one network stream with
  hardware-accelerated ffmpeg and push raw BGR frames onto a bounded queue.
- **M1** — four streams over four protocols (RTSP/RTMP/HLS/HTTP-FLV) under
  one `IngestManager`, with a local testbed, a smoke test, and a minimal
  operator control panel (M1.1). Details in `docs/m1_ingest.md`.

## Requirements

- RK3576 board with the Rockchip MPP kernel driver (`/dev/mpp_service`)
- ffmpeg/ffprobe built with `--enable-rkmpp` and `--enable-openssl`
- Python 3.11 venv with `numpy`, `pyyaml`, `pytest`, `flask`

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

python scripts/run_control_panel.py        # M1.1: http://127.0.0.1:5050
```

The M0 smoke test (`scripts/smoke_test_m0.py`) is unchanged but now uses
`cam_a` from the config, so the testbed must be running.

## M1 — 4-stream mixed-protocol ingest

One `IngestManager` runs one `StreamWorker` per camera, each with its own
bounded frame queue and restart counter. See `docs/m1_ingest.md` for the
full write-up (testbed, manager API, control panel, known pitfalls).

Verified on the board:

- 4 cameras over RTSP / RTMP / HLS / HTTP-FLV, each independently killable
  and recoverable without touching the others.
- 30-min soak: 53k frames per camera at ~30 fps, `restarts: 0`, memory flat.
- CPU budget for M2: ~63% of one core for 4x 640x360 decode+colorspace
  (~2.4 cores projected for 4x 720p); Python ingest stack ~1.5 cores.

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

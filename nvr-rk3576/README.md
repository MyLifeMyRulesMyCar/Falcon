# nvr-rk3576

Multi-camera NVR ingest layer for Radxa CM4 (RK3576). M0 = single-stream
ingest: decode one third-party network stream with hardware-accelerated
ffmpeg and push raw BGR frames onto a bounded queue.

## Requirements

- RK3576 board with the Rockchip MPP kernel driver (`/dev/mpp_service`)
- ffmpeg/ffprobe built with `--enable-rkmpp` and `--enable-openssl`
- Python 3.11 venv with `numpy`, `pyyaml`, `pytest`

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

`config/config.yaml` lists cameras; only `name` and `url` are used in M0.
The URL may be RTSP, RTMP, HLS or plain HTTP — ffmpeg handles the transport.
Swap the URL without touching code:

```yaml
cameras:
  - name: test_stream
    url: https://test-streams.mux.dev/x36xhzz/x36xhzz.m3u8
```

## Usage

```bash
pytest tests/                              # unit tests (no network)
.venv/bin/python scripts/smoke_test_m0.py --duration 30   # quick smoke
.venv/bin/python scripts/smoke_test_m0.py --duration 1800 # 30-min soak
```

The smoke test prints total frames, measured FPS and writes sample BMP
frames to `/tmp/m0_frame_<n>.bmp`.

## Architecture

```
StreamWorker (multiprocessing.Process)
  ffprobe          -> width, height, fps
  ffmpeg -init_hw_device drm:/dev/dri/renderD128 -hwaccel drm \
         -i <url> -f rawvideo -pix_fmt bgr24 -vsync 0 pipe:1
  read frame_size bytes -> np (h, w, 3) -> frame_queue (drop-oldest, maxsize 256)
  on short read: exponential backoff (min(2**attempt, 30)s), respawn ffmpeg
```

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

Reported FPS per stream drives the M1 motion-gate and M2 NPU budget.

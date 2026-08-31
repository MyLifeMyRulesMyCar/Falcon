# v0.1 — 4-Stream Mixed-Protocol Ingest

v0.1 builds on v0.0's `StreamWorker` (unchanged internals: `-hwaccel drm`
decode, drop-oldest queue, exponential backoff, stderr drain) and runs N of
them under an `IngestManager`, plus a local 4-protocol testbed and a minimal
operator control panel (v0.1.1). Verified on the RK3576 board.

## Layout

```
nvr/
  ingest/
    stream_worker.py      # v0.0: one ffmpeg decode pipeline per camera (unchanged)
    manager.py            # v0.1: IngestManager — run N workers, per-camera control
  control/
    api.py                # v0.1.1: Flask routes over IngestManager
    static/index.html     # v0.1.1: operator panel (vanilla JS, no build)
testbed/                  # local 4-camera test harness
  mediamtx.yml            # 4 independent paths (cam_a..cam_d)
  start_testbed.sh        # mediamtx + 4 publishers, one PID file per camera
  stop_testbed.sh         # kill one camera's publisher or all
  sample.mp4              # generated on first start (h264_rkmpp, 640x360, 1s GOP)
scripts/
  smoke_test_m1.py        # drain all queues, print per-camera fps/restarts
  run_control_panel.py    # v0.1.1: Flask dev server
tests/                    # unit tests (no network, no subprocesses)
```

## Testbed — 4 protocols, independently killable

> **mediamtx is pinned to v1.12.2.** Newer releases (>= 1.20) crash their HLS
> muxer on the looped h264_rkmpp sample (`unable to extract DTS: too many
> reordered frames`), driving cam_c down to ~0-9 fps with repeated restarts.
> The binary lives at `testbed/mediamtx` (gitignored). If the GitHub release
> download hangs on this board, route it through the `ghproxy.net` mirror
> (see the main README's bring-up notes).

`mediamtx` serves RTSP (:8554) / RTMP (:1935) / HLS (:8888). Four publishers
each run in their own process group (`setsid`), so any camera can be killed
without touching the others:

| camera | published as          | read back by Falcon as               |
|--------|-----------------------|--------------------------------------|
| cam_a  | RTSP                  | `rtsp://127.0.0.1:8554/cam_a`        |
| cam_b  | RTMP                  | `rtmp://127.0.0.1:1935/cam_b`        |
| cam_c  | RTSP                  | `http://127.0.0.1:8888/cam_c/index.m3u8` (HLS) |
| cam_d  | HTTP-FLV (ffmpeg `-listen 1` on :8080) + RTSP republish | `http://127.0.0.1:8080/cam_d.flv` |

> mediamtx does not serve HTTP-FLV, so cam_d's publisher is a standalone
> ffmpeg in listen mode plus an RTSP republish into mediamtx (so the web
> player at `http://host:8888/cam_d/` works). The listen server is wrapped
> in a respawn loop with a watchdog that restarts it during the publisher
> group's first 60s of life while a client is attached and the server is
> >= 20s old (a client that dies mid-header can leave the single-client
> server connected-but-broken; broken slots only ever occur at spawn/startup
> churn). The watchdog is deliberately gated to that startup window: an
> earlier version kept firing on the long-lived healthy ingest connection,
> so cam_d's `restart_count` climbed forever (~1 per 20s) instead of settling
> at the documented 1-3 startup increments. cam_d therefore shows a few
> honest `restart_count` increments at startup and is stable afterwards.

Usage:

```bash
./testbed/start_testbed.sh     # verify all 4 URLs with ffprobe
./testbed/stop_testbed.sh cam_b    # kill just cam_b's publisher
./testbed/stop_testbed.sh all      # stop everything
```

## IngestManager

One worker + one bounded frame queue (`maxsize 16`) + one lock-free
`RawValue` restart counter per camera.

- `start()` / `stop(timeout_sec)` — all cameras.
- `start_one(name)` / `stop_one(name)` — granular control; `stop_one` closes
  and drops the old queue (see the deadlock note below).
- `update_camera(name, new_config)` — `RuntimeError` while running; supports
  renames (frame counters carry over).
- `consume(timeout)` — drains every queue; this is what advances
  `frames_received` (the smoke test loop and the panel's background drain
  thread both call it).
- `is_alive(name)` / `get_queue(name)` / `stats()` — visibility; `stats()`
  returns `{alive, frames_received, restart_count, last_frame_ts}` and is
  safe for never-started cameras.

v0.1 touches to `stream_worker.py` (additive only): an optional
`restart_counter` argument incremented at each restart, and probe retry with
backoff (`_probe_with_retry`) so a worker that starts while its camera is
down survives instead of dying. The ffmpeg invocation and backoff logic are
untouched.

## Known pitfalls found on the board

- **Queue feeder data loss (CPython):** same-process `multiprocessing.Queue`
  puts can vanish (a race in the feeder's `_start_thread` buffer clear) —
  only affects tests, which now put from a real child process.
- **Drain deadlock:** SIGTERM'ing a worker mid-write leaves a partial frame
  in its queue pipe; the drain thread's `get_nowait` then blocks forever on
  the missing remainder (the next worker inherits the old pipe's open write
  end, so no EOF). Fixed by `stop_one`/`stop` closing and dropping the old
  queue, and `consume` treating `EOFError` as empty.
- **RTMP cold start is slow:** ~10-12 s from start to first frame on this
  setup (probe + keyframe sync + rkmpp init). RTSP starts in ~3 s. The panel
  and smoke outputs reflect this ramp.

## v0.1.1 — operator control panel

```bash
python scripts/run_control_panel.py          # http://127.0.0.1:5050
# --host / --port / --config are configurable; localhost by default
```

- Cameras begin stopped; the panel starts them.
- Routes: `GET /api/cameras`, `POST /api/cameras/<name>/start|stop`,
  `PUT /api/cameras/<name>` (409 while running), `GET /` (the page).
- Fields are disabled while a camera runs ("stop to edit").
- No auth — dev tool for the bench only; do not expose past localhost/LAN.
- **Known limitation:** edits are in-memory only. A panel restart resets all
  state; `config/config.yaml` is never written. Config persistence is a
  future increment.

## v0.1 acceptance — verified on the board

1. `pytest tests/` — green (38 tests).
2. All 4 cameras: climbing frame counts, non-zero fps, `restarts: 0` in
   steady state.
3. Kill one camera (`stop_testbed.sh cam_X`): its frames freeze and its
   `restart_count` climbs through the backoff sequence while the other three
   keep flowing at ~30 fps in the same status output.
4. Relaunch the publisher: the camera resumes without restarting the manager
   or touching the others. Tested individually on cam_b (RTMP) and cam_d
   (HTTP-FLV); the mechanism is identical for all four.
5. Panel: start/stop/edit/rename all behave; editing a running camera is
   refused with 409; a panel restart comes back clean with all cameras
   stopped.

### 30-minute soak (640x360@30 per stream)

| camera | frames | fps  | restarts |
|--------|--------|------|----------|
| cam_a  | 53,947 | 29.9 | 0 |
| cam_b  | 53,709 | 30.0 | 0 |
| cam_c  | 44,135 | 23.3 | 0 |
| cam_d  | 53,642 | 29.9 | 3 (startup only) |

Memory flat (~2.4 GB used / ~1.4 GB free on the 4 GB board).

### Fresh-board reproduction (Aug 2026)

All four cameras again held ~30 fps with zero steady-state restarts (90s
observation, cam_a/b/c/d `restart_count` frozen) on a clean Radxa OS 6.1.84
board, with mediamtx pinned to v1.12.2 and the cam_d watchdog gated as above.

### CPU budget for v0.2 (measured, /proc stat deltas over a stable window)

- 4 decoders + bgr24 colorspace conversion: **~63% of one core** aggregate
  at 640x360 (~16% per stream; project ~4x, ~2.4 cores, for 4x720p).
- Python ingest stack (queue pickling/transport): **~1.5 cores**.
- Testbed publishers + mediamtx: ~1-2% each.

Headroom for the v0.2 motion gate is comfortable; the Python transport
(pickle over multiprocessing queues) is the dominant cost and the first
thing to revisit if 720p ingest needs to share the board with NPU work.

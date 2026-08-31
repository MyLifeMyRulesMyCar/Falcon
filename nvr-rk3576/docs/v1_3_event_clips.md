# v1.3 — Event Clips (Post-Roll)

Scoped deliberately: "recording" was two different projects hiding under
one name (short event clips vs. continuous 24/7 recording, with its own
storage/retention/disk-full design conversation). This milestone is the
former only — post-roll clips extending v1.1's snapshot pattern. Pre-roll
was explicitly deferred: it needs a new continuously-maintained rolling
frame buffer that doesn't exist anywhere in the codebase today (would cost
~800MB across 4 cameras at 640x360/30fps for 10s of history), whereas
post-roll needs no new frame-history infrastructure at all.

## Layout
```
nvr/output/rotation.py     # rotate_by_count() — extracted from snapshot_store.py, shared
nvr/output/clip_store.py   # ClipStore / _ActiveClip — poll-driven capture + ffmpeg mux
```

## Config
```yaml
clips:
  base_dir: clips
  max_per_camera: 30
  duration_sec: 10
```

## Frame source (the real design decision here)
Neither existing frame path fit cleanly. The detection path (`work_queue`)
is motion-gated and batched — can go silent mid-clip if the subject
leaves frame. `LatestFrameStore` is unconditional but throttled to
`_PREVIEW_INTERVAL = 0.16s` (~6fps), built for browser preview bandwidth,
not archival quality. Went with `LatestFrameStore`: the only path that
keeps producing frames independent of motion state, and shared-memory
blocks are already attachable by name from any process (the panel does
exactly this for preview), so no new IPC was needed — `ClipStore` wraps a
`LatestFrameStore` reader internally, `DetectionWorker` takes only
`clip_store`, not a separate `frame_store` param.

Clips mux at their *measured* capture rate, not an assumed one, so
playback speed is correct even if actual cadence drifts under load.

## Encoder — deviation from spec
The board's ffmpeg has no `libx264` (`ffmpeg -encoders` confirmed) — clips
mux via the hardware `h264_rkmpp` encoder instead, with an explicit
`yuv420p` conversion. `encoder` is a constructor param so tests stay
hermetic.

## Wiring
`ClipStore.poll()` runs once per `feeder()` pass (already loops every
camera continuously, unlike `core_worker`, which is motion-gated).
`start_clip()` is a no-op if a clip is already active for that camera — a
second zone event mid-recording just keeps the current capture, avoiding a
second `ffmpeg` process racing the first. `api.py`'s
`GET /clips/<camera>/<filename>` shares the same guard as `/snapshots` via
one `_serve_under()` helper.

## Test results
163 passed on the board (155 + 8 hardware-only), including
`tests/test_rotation.py` and `tests/test_clip_store.py` (fake
`LatestFrameStore` stub, real `ffmpeg` subprocess). Rotation tests set
explicit `os.utime()` — tight-loop writes in a bare test share mtimes, made
deterministic.

## Live acceptance (board)
- Clips: h264 640x360, duration 10.00–10.12s (target 10), muxed at
  measured ~5.7–6.5fps — playback speed correct, not sped up/slowed.
- Route: byte-identical fetch, `..` → 403, `../../etc/passwd`-style → 404,
  missing → 404.
- Rotation: cap lowered to 3, pinned at 3 across 5 more events, oldest
  rotated by mtime.
- Disk: `clips/` at 4.0MB with cap=3; `df` on the real mount unchanged.
- CPU: hardware mux is a brief subprocess call; feeder kept producing
  events throughout, no ingest stall.
- Bug found + fixed during live testing: `create_app(...)` in
  `run_control_panel.py` was missing `clip_store`/`clip_config` — worker
  call sites were wired, the Flask app wasn't, so `/clips` 404'd until
  caught live.

## Known limitations / next levers
- ~6fps clips, not smooth video — a known, accepted trade for evidence
  over cinema. Revisit only if 6fps proves genuinely insufficient in use.
- No panel UI to browse clips — v1.4.

# v1.1 — Event Snapshots

Assembly, not a new subsystem: connects three things that already existed
(frame data, box/zone drawing, the event dispatcher). One piece of
duplication fixed along the way — the annotated-stream drawing (previously
inline in `preview_encode`, `nvr/control/preview_encoder.py`) was extracted
into a shared function, so both the browser preview and snapshot capture
draw from one implementation instead of two drifting apart over time.

## Layout
```
nvr/output/annotate.py         # shared draw_annotations(), used by preview_encode + detection_worker.py
nvr/output/snapshot_store.py   # SnapshotStore — save + max-count rotation
```

## Config
```yaml
snapshots:
  base_dir: snapshots
  max_per_camera: 200
```
`SnapshotConfig` in `nvr/config.py`; absent section → `None` → route 404s, no capture.

## Capture
On a `ZoneEvent` (in `detection_worker.py`'s zone-evaluation block, the same
site v0.4 already had), the firing zone's frame is drawn via
`draw_annotations` (fired zone highlighted red, other configured zones cyan)
and written by `SnapshotStore.save()` as
`<camera>/<zone>_<ts>_<track_id>.jpg`. The save is a blocking disk write
(PIL `Image.save`) inside the `core_worker` thread — accepted, not an
oversight: event rate is already dwell/cooldown-limited (~1/30s), so this
never became the bottleneck an async writer would exist to prevent. OpenCV
is deliberately not used (see `docs/ROADMAP.md`); drawing is PIL/numpy.

## Wiring
`event_schema.py`'s `build_zone_event_payload` carries `snapshot_path` when
present — the same payload both MQTT and HTTP publishers send, so it's not
duplicated per transport. `api.py`'s `GET /snapshots/<camera>/<filename>`
serves it, guarded against path traversal (`abspath`/`startswith` check
against `base_dir`, rejecting `..` in `camera` too, not just `filename`).

## Test results
145 passed on the board (137 + 8 hardware-only — `test_detection_worker.py`
requires `rknnlite`, so it only runs where the NPU runtime is installed),
including `tests/test_annotate.py` and `tests/test_snapshot_store.py`.

## Live acceptance (board)
- Real zone events (bird, `entry_path`) produced snapshots with the correct
  highlight coloring; `snapshot_path` present and matching an on-disk file
  in the MQTT payload.
- Traversal guard: `curl --path-as-is ".../snapshots/cam_a/.."` → 403;
  bare `curl` (no `--path-as-is`) collapses `..` client-side before
  sending, so the negative case needs the flag — worth knowing for anyone
  re-testing this by hand.
- Rotation: cap lowered to 5, count pinned at 5 across 18+ polls while
  track IDs advanced ~90 further, oldest removed by mtime.
- Disk: `df -h` on `/dev/mmcblk1p3` (not `udev` — an early verification
  mistake, corrected) unchanged across the run; capped set bounded in the
  low single-digit MB.

## Known limitations / next levers
- Single frame per event, not a clip — v1.3 adds post-roll video.
- No panel UI to browse captured snapshots — v1.4 adds an events gallery.

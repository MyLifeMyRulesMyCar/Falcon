# M4 — Zones + Tracking

M4 adds per-camera centroid tracking (stable track IDs) and polygon "zones"
that fire events when a tracked object of a trigger class dwells inside them,
all inside the two threads that already run in `DetectionWorker` — no new
process, no new IPC. Events surface in the control panel and a dedicated
`/api/zone_events` endpoint, and each camera's zones are drawn on the
annotated preview so you can see exactly where config.yaml's coordinates land.

## Layout

```
nvr/
  tracking/
    centroid_tracker.py     # per-camera nearest-centroid track ID assignment
  zones/
    zone_engine.py          # dwell/cooldown zone events from tracked objects
  inference/
    detection_worker.py     # M4 state wired into core_worker (both NPU threads)
  control/
    preview_encoder.py      # draws zone polygons + labels on the ann slot
    api.py                  # recent_zone_events + GET /api/zone_events
    static/index.html       # "zone events" column
config/config.yaml          # example zone (cam_a, native 640x360 coords)
tests/
  test_centroid_tracker.py
  test_zone_engine.py
```

## Config

`CameraConfig` gained an optional `zones` list (cameras without zones load
unchanged). Each zone:

```yaml
zones:
  - name: entry_path
    polygon: [[160, 260], [480, 260], [480, 360], [160, 360]]
    trigger_classes: [bird]
    dwell_time_sec: 2.0
    cooldown_sec: 30
```

`load_config` validates at load time (ConfigError names the offending zone):
polygon >= 3 points, `dwell_time_sec > 0`, `cooldown_sec >= 0`, non-empty
`trigger_classes` with every class in COCO-80 — a typo'd class name fails at
load, not as a silent never-firing zone.

**Coordinate space:** polygons are in the camera's native decoded resolution
(what the M1 probe reported — 640x360 for the local testbed), the same space
`detector.detect()` maps detections back to and the annotated stream renders
in. Not the model's 640x640 letterbox space. Get this wrong and zones look
right but never trigger.

## Tracking — centroid tracker

`CentroidTracker.update(detections)` greedy-matches each existing track to the
closest unmatched detection within `max_distance_px` (bbox-center distance),
updates its bbox/confidence/last_seen, and increments a per-track `missed`
counter when nothing matches; tracks drop once `missed > max_missed_frames`
(15). Unmatched detections become new tracks (`_next_id`).

`max_missed_frames` counts missed **detection passes**, not video frames —
detection only runs on motion-gated frames (~12-17/s here), so 15 tolerates a
~1.2s gap (someone turning side-on), not 15/30s.

## Zones — dwell + cooldown

`ZoneEngine.evaluate(camera, tracked_objects)` tests each object's
**bottom-center** against each zone's polygon (ray casting) and only when the
class is in `trigger_classes`. `_entry_times[(track_id, zone)]` records the
first entrance on wall-clock `now`; an event fires once
`dwell_time_sec` has elapsed, and `_last_fired[(track_id, zone)]` gates
re-fires by `cooldown_sec`.

- Dwell survives detection-cadence gaps for free: `_entry_times` is keyed on
  wall-clock time, not call count — as long as the track ID survives the gap
  (`max_missed_frames`), the next `evaluate()` includes it in `now - entered_at`.
- Cooldown gates fires **between** events, never the first: a new intrusion
  fires at dwell time even with a large `cooldown_sec`.
- `cooldown_sec: 0` means every dwell-satisfying pass fires.

## Wiring into DetectionWorker

Trackers and zone engines are built per camera in `run()` (post-fork, so each
worker lifetime — re-forked on start/stop/rename — starts with fresh state).
Both `core_worker` threads can touch one camera's tracker/engine/stats
concurrently (the shared work queue doesn't pin a camera to a core), which is
the same hazard the M2.2 lock fix addressed for stats — so M4 reuses
`stats_locks[name]`, not a second lock. A corrupted track dict would silently
wrong track IDs, worse than a crash, so this isn't optional.

Events are stored via `event_to_dict()` as plain JSON-safe dicts in a
`recent_zone_events` ring (capped at 20) inside each camera's stats — same
reason M2.1 did this for detections (Manager dict crosses a process boundary).
Each event is also logged: `ZONE EVENT: cam/zone track=... class=... dwell=...s`.

## Panel / API

- `/api/cameras` rows gain `zones` (names), `recent_zone_events`, and native
  `width`/`height`.
- `GET /api/zone_events` — flattened, most-recent-first across all cameras.
- The annotated stream (`/stream/<name>/annotated.mjpg`) now renders each
  zone's polygon outline + name (PIL ImageDraw on the ann slot) — the encoder
  annotates whenever a camera has zones OR boxes, so the outline is visible
  even before anything is detected inside it.
- The panel's per-row "zone events" column shows the last two events.

## M4.1 — UI zone editor + config persistence

Zones (and camera name/url edits) are now edited in the panel and persisted —
this lifts M1.1's "edits are in-memory only" limitation.

- **Editor** (per-camera **zones** button, Frigate-style): a modal overlays a
  live raw MJPEG frame with a canvas sized to the camera's native aspect
  (dims from `GET /api/cameras/<name>/zones`; canvas pixels map to native
  coordinates by the render scale). Click to place vertices for a new zone
  (>= 3), drag vertex handles to reshape, drag the interior to move a zone,
  delete, and edit name / trigger classes / dwell / cooldown — Save sends the
  full zone list.
- **API**: `GET /api/cameras/<name>/zones` returns `{width, height, zones}`;
  `PUT /api/cameras/<name>/zones` bulk-replaces and validates via
  `parse_zones` (400 with the offending zone name on error).
- **Persistence**: name/url/zones edits call `write_config()`
  (`nvr/config.py`): atomic temp-file + `os.replace`, round-trip validated
  through `load_config` so a bug can never leave a broken config file on
  disk. The file survives panel restarts and reboots. Hand-written comments
  in `config/config.yaml` are replaced by a generated header on the first
  save (a documented, accepted loss).
- **Wiring**: after a zones/name/url change the API re-forks the
  DetectionWorker (`restart_detection`) and the PreviewEncoders
  (`restart_encoders`) so the new config takes effect immediately; zones
  carry across a camera rename.

## Test results (as of M4.1)

```
pytest tests/ -q
101 passed
```

- `test_centroid_tracker.py` (4): drift keeps a track ID; a short gap retains
  it; a long gap drops and reassigns; far-apart detections never cross-match.
- `test_zone_engine.py` (6): under-dwell never fires; crossing dwell fires
  exactly once per intrusion; leaving resets the dwell clock; cooldown
  suppresses then re-fires; wrong class never fires; event dict is JSON-safe.
- `test_config.py` (+11): zone parse, cameras-without-zones, invalid-zone
  validation (each raising ConfigError with the zone name), and
  `write_config` round-trip / omit-empty-zones / validate-before-replace.
- `test_detection_worker.py` (+2): `recent_zone_events` ring cap + empty ring.
- `test_control_api.py` (+7): rows carry dims; zone GET shape + 404; zone PUT
  valid/invalid/unknown; zone + name/url persistence to a config file; a 409
  (running camera) never writes config.

### Live acceptance (local testbed, 640x360 BBB loop)

- One zone (`entry_path`, `[bird]`, dwell 2.0s, cooldown 30s): **5 events over
  ~2.5 min, consecutive gaps 30.0-30.4s — exactly one event per 30s loop pass,
  never one per detection tick**, and no pair closer than the cooldown.
- Cooldown: no events observed closer than the 30s window on the same track.
- Class filtering: a second zone on the *identical polygon* with
  `trigger_classes: [sheep]` fired **0 times** while `[bird]` fired — birds
  (and sheep present in the footage) never cross-fired a zone scoped to the
  other class.
- Visual: the annotated stream's frames contain the cyan polygon outline
  (pixel-scanned), sitting where the event log says it fires.

## Known limitations / next levers

- Zones are configured per camera in `config/config.yaml` — but now editable
  live in the panel's zone editor (M4.1), which persists them back to the
  file. Tuning a polygon means a few clicks, not a restart-by-hand.
- Track IDs are per camera and reset whenever the DetectionWorker re-forks
  (start/stop/rename, or a zone save). Cross-camera consistency is out of scope.
- Track stability is centroid-distance only; no IoU/Kalman tracking yet, so
  overlapping same-class objects can swap IDs (fine for zone events, not for
  "who is who" analytics).

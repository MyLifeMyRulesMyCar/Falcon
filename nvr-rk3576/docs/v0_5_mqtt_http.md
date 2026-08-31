# v0.5 — MQTT + HTTP Output

v0.5 pushes v0.4's debounced zone events and (opt-in, throttled) detection
summaries to an MQTT broker and/or an HTTP webhook, as **two separate content
types on separate topics/toggles** — a customer integrating with this wants
zone alerts always, detection summaries only sometimes, and conflating them
would remove that choice. Everything is non-blocking: `publish()` is just a
bounded-queue put with drop-oldest, so a down broker or dead endpoint can
never stall the DetectionWorker's NPU-adjacent threads.

## Layout

```
nvr/
  output/
    event_schema.py       # single source of truth for both transports' payloads
    mqtt_publisher.py     # paho-mqtt v2, bounded queue, auto-reconnect
    http_publisher.py     # requests.Session, bounded queue, log-and-drop failures
    dispatcher.py         # one call site, fans payloads out to both transports
  inference/
    detection_worker.py   # publishes zone events + throttled summaries
  control/
    api.py                # transport toggles, status, MQTT settings, per-camera publish
    static/index.html     # output bar + per-row Z/D publish toggles
config/config.yaml        # mqtt: / http_output: sections
tests/
  test_event_schema.py
  test_output_dispatcher.py
  test_mqtt_publisher.py
  test_http_publisher.py
```

## Config

Two optional top-level sections (cameras without them just don't publish):

```yaml
mqtt:
  host: 127.0.0.1
  port: 1883
  topic_prefix: nvr
  username: bob          # optional; password optional too
http_output:
  url: http://host:8080/nvr/events
  timeout_sec: 3
  enabled: false
```

`CameraConfig` gains `publish_zone_events` (default True), `publish_detections`
(default False — opt-in, high volume) and `detection_publish_interval_sec`
(default 5.0, the summary throttle). `write_config` persists the full document
(cameras + mqtt + http_output), so camera saves never drop the v0.5 sections.

## Event schema (single source of truth)

Both transports consume only `event_schema.build_*` output, so "identical
payloads on both transports" holds by construction.

- `zone_warning` — the debounced v0.4 zone event: camera/zone/track_id/
  class_name/dwell_time_sec/bbox/timestamp (UTC ISO).
- `detection_summary` — opt-in, throttled per camera: camera/detections
  (class_name/confidence/bbox)/timestamp. Summaries publish only when the
  camera actually has detections in the window (no empty-message flood) and
  at most once per `detection_publish_interval_sec`, not per inference tick.

## Publishers — non-blocking by construction

`MqttPublisher` / `HttpPublisher` both hold a bounded
`multiprocessing.Queue(maxsize=200)` drained by a daemon thread in the panel
process. `publish()` is a `put_nowait` with drop-oldest (drop the oldest item
on Full, never block or raise). A failed MQTT publish or HTTP POST is logged
and dropped — no retry storm.

- **paho-mqtt v2** (verified 2.1.0 in this venv): `mqtt.Client(
  CallbackAPIVersion.VERSION2)`; `username_pw_set()` when credentials are
  configured; `reconnect_delay_set(1, 30)` + `loop_start()` mean the client's
  background loop owns reconnection — a broker restart needs no Falcon
  restart.
- **Fork model**: the DetectionWorker is a forked process; publishers live in
  the panel and are inherited at fork. Their queues are `multiprocessing.Queue`
  (a `queue.Queue`'s lock/Condition does not survive fork — the child would
  write a private deque the panel never reads, which is exactly why v0.0's frame
  queues use multiprocessing.Queue). The `enabled` flags are backed by a
  shared `multiprocessing.Value`, so the panel's live MQTT/HTTP toggles reach
  the worker immediately instead of snapshotting at fork.

## Wiring

`OutputDispatcher` is built once in the panel and passed to the
DetectionWorker. Inside `core_worker`'s existing per-camera lock, right after
v0.4's zone evaluation:

- zone events → `publish_zone_event` for every fired event (gated by the live
  per-camera `publish_zone_events` flag);
- a detection summary when the `publish_detections` flag is on and the camera
  has detections and `detection_publish_interval_sec` has elapsed.

Both are queue puts — non-blocking, safe under the lock, so a dead broker or
endpoint can never stall the NPU threads.

## Panel

- **Output bar** above the camera table: `[MQTT: ON/OFF] [HTTP: ON/OFF]`
  (with a connected/disconnected dot for MQTT) and **MQTT settings**, which
  opens a form for host/port/topic prefix/username/password — saved via
  `PUT /api/output/mqtt`, persisted to config.yaml, and the live publisher is
  reconfigured (reconnect) immediately.
- **Per-row Z/D toggles**: `Z:ON/OFF` (zone events) and `D:ON/OFF`
  (detection summaries) per camera, in the same style as the Detect toggle.
  Together with the transport switches this gives full selection of
  "zones + detections", "zones only", or "detections only", per camera.
- API: `POST /api/output/mqtt/<on|off>`, `POST /api/output/http/<on|off>`,
  `GET /api/output/status`, `PUT /api/output/mqtt`,
  `POST /api/cameras/<name>/publish/<zone_events|detections>/<on|off>`.
  `/api/cameras` rows gain `publish_zone_events` / `publish_detections`.

## Test results (as of v0.5)

```
pytest tests/ -q
122 passed
```

- `test_event_schema.py` (3): both builders are JSON-safe; event_type
  distinguishes the shapes; UTC ISO timestamps.
- `test_output_dispatcher.py` (3): both transports receive the identical dict;
  `None` transports are skipped.
- `test_mqtt_publisher.py` (3) / `test_http_publisher.py` (2): a deliberately
  slow/blocking stand-in client/session proves `publish()` returns
  near-instantly (the actual acceptance criterion, no network/broker); the
  bounded queue never blocks on Full; disabled publishers never enqueue.
- `test_config.py` (+6): mqtt/http parse + validation; `write_config`
  round-trips the sections; camera publish fields parse.
- `test_control_api.py` (+6): transport toggles + status; MQTT settings PUT
  (validate/persist/reconfigure); per-camera publish toggles + rows; mqtt/http
  survive a camera save.

### Live acceptance (local mosquitto 2.0.11 on 127.0.0.1)

- Zone event fires → `nvr/cam_a/zone_event` within ~1s, payload matches the
  schema; a tiny local HTTP listener received the same fields/values.
- Restarting mosquitto mid-run → publisher reconnects on its own (paho's
  background loop); zone events flow again; events during the outage window
  are dropped once the bounded queue fills (expected drop-oldest).
- Pointing `http_output` at a dead port → failures are logged and dropped
  while `infer_fps` keeps climbing — the worker is unaffected.
- Toggling MQTT OFF → MQTT messages stop immediately, HTTP keeps flowing
  (and the reverse).
- Toggling a camera's `publish_detections` ON → summaries arrive ~every
  `detection_publish_interval_sec`, not once per inference tick; toggling
  `publish_zone_events` OFF for that camera stops its zone crossings.
- **Nice-to-have verified**: reconfigured live to the public
  `test.mosquitto.org` broker (via `PUT /api/output/mqtt`), received zone
  events there, then restored the local broker — public test brokers are
  flaky/rate-limited, so treat that as best-effort.

## Known limitations / next levers

- One MQTT connection and one HTTP session for the whole NVR (per-camera
  broker settings are not supported — the broker is global).
- Detection summaries are a snapshot of one inference pass, not a windowed
  aggregate; a downstream dashboard wanting counts must accumulate them.
- HTTP has no auth headers / per-endpoint credentials yet (plain POST).

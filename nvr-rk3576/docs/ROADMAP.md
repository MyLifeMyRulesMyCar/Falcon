# Falcon v1.5 Roadmap — post-M5 close-out

Status of every item the M4/M5 close-out review raised, plus what's still open
and who owns it. This is the honest gap list between "M5 merged" and "v1.0".

## Verified (evidence captured)

| Item | Status | Evidence |
|---|---|---|
| M4 unit tests green | ✅ | `pytest tests/ -q` → 122 passed (tracker + zone-engine suites included) |
| Exactly one zone event per pass | ✅ | 6 events over ~2.5 min, gaps 28–32s = the 30s loop; dwell 2.0–2.4s — never per tick |
| Cooldown suppresses close passes | ✅ | live gaps never < 30s; `test_cooldown_suppresses_then_refires` |
| Trigger-class scoping | ✅ | `[bird]` zone fired; `[sheep]` zone on the same polygon fired 0× while sheep present |
| Zone polygon on annotated stream | ✅ | cyan outline pixel-verified; events land where the bird crosses |
| MQTT delivery ~1s, schema-correct | ✅ | `nvr/cam_a/zone_event`, payload matches `event_schema` |
| MQTT vs HTTP byte-identical | ✅ | same track_ids (1241/1267/1287/1310/1336/1337) with identical dwell/bbox/timestamp on both |
| Broker restart → auto-reconnect | ✅ | `connected: True` after `systemctl restart mosquitto`; events flowed again |
| Dead HTTP port → worker unaffected | ✅ | `infer_fps` climbing on all cameras; failures logged + dropped |
| Toggle independence (both directions) | ✅ | MQTT-off: mqtt 0 / http +2; HTTP-off: mqtt +3 / http 0 |
| Detection-summary throttle | ✅ | 4 summaries in 22s (5s interval) on both transports, not ~12/s |

**Real-camera walk-through** (a phone/webcam, one minute): NOT done — needs a
physical camera in front of the board (user-side).

## M1.3 — real internet sources (this run)

| Source | Result |
|---|---|
| Public HLS (`test-streams.mux.dev/x36xhzz/x36xhzz.m3u8`) | ✅ decoded at ~31 fps (720p/60 source); genuine internet jitter observed in the preview |
| Wowza public RTSP (`entrypoint.cloud.wowza.com`) | ❌ **IP-blocked from this board** (TCP connects, RTSP SETUP → 403). URL also rotates. Retry from another network or when a real camera exists |
| RTMP | No public pull endpoint exists. Requires a self-hosted VPS relay: mediamtx on a $5/mo VPS, publish `sample.mp4` from a *different* network, Falcon pulls `rtmp://<vps-ip>:1935/...` (user-side) |

Config was reverted to all-local testbed sources so all four previews run
smooth; the internet-HLS evidence above stands as the M1.3 validation.

## In progress

- **Full-system soak** — started, running (ingest + detection + zones + MQTT +
  HTTP + panel). Monitor: `cat /tmp/soak.log` (per-camera fps/restarts, memory,
  MQTT state every 30s). Check the memory trend + restart counters over several
  hours / overnight. Look for: `mem_mb` climbing (leak), any camera `restarts`
  growing in steady state, `mqtt_connected` flapping.

### Soak finding — cam_c HLS restart churn (diagnosed)

The soak surfaced steady cam_c (testbed HLS) restart churn (~1-2/min). Diagnosis:

1. **Falcon restart reason** (`panel.log`): each churn = `Failed to reload
   playlist 0` (after a variable 33s-25min healthy run) then three `404 Not
   Found` backoffs → recovery. Restart bursts of 4 = the 1/2/4/8s backoff.
2. **Root cause** (`testbed/mediamtx.log`): mediamtx's **MPEG-TS HLS muxer
   crashes** with `unable to extract DTS: too many reordered frames (28)` on
   the looped `-c copy` RTSP testbed feed — for cam_a, cam_c AND cam_d (all
   RTSP-published cameras), every ~2-5 min. Each crash → muxer restart → a
   few seconds with no playlist → any HLS reader (Falcon cam_c or a bare
   ffmpeg) hits `Failed to reload playlist 0` → restart.
3. **Not Falcon, not contention** (isolation): a bare `ffmpeg` reading
   `http://127.0.0.1:8888/cam_c/index.m3u8` fails identically (52
   playlist-failure lines over ~25min) with no Falcon involved.
4. **Memory is independent**: soak `mem_mb` climbs/drops regardless of restart
   bursts; the big ~900MB drop happened during a restart plateau → shared root
   cause refuted. The memory sawtooth is a separate phenomenon (allocation
   churn in the panel tree, not a restart-driven leak).

**Recommended fix (tested in isolation, NOT yet applied — applying it needs a
testbed restart, which stops the running soak):** switch the testbed mediamtx
to `hlsVariant: fmp4` in `testbed/mediamtx.yml`. An isolated mediamtx on
`fmp4` showed **0 muxer crashes over 13 min / 19 consecutive clean 30s HLS
reads**, vs mpegts crashing every few minutes. This is a testbed-harness
change, not a Falcon code change — `stream_worker.py` is not involved. Apply
+ confirm cam_c churn stops after the soak completes.

## 10-hour soak — completed

1193 samples, **10.0 h**. cam_a / cam_b / cam_d: **0 restarts** throughout
(~30 fps). cam_c (testbed HLS): 863 restarts — the diagnosed mediamtx mpegts
muxer crash, not a new finding. **MQTT: 0 disconnects.** No camera ever went
not-alive. Memory: bounded sawtooth 0.95-2.3 GB (no monotonic leak; last 3h
oscillated 1.2-1.9 GB). Raw log: `docs/soak_10h.log`.

## fmp4 fix — applied + confirming

`hlsVariant: fmp4` applied to `testbed/mediamtx.yml` (top-level: v1.12.2 does
not allow `hlsVariant` per-path — the "unknown field" error). Restarted the
stack. A 30-min focused confirmation run writes `/tmp/soak_confirm.log`:
acceptance = cam_c restarts ~0 (vs ~1-2/min baseline), cam_a/b/d ~30 fps 0
restarts, MQTT connected, memory bounded. Result appended when it lands.

## Open (owned by external inputs or future work)

| Item | Blocker | Owner |
|---|---|---|
| Real camera URLs (RTSP/ONVIF) | Need actual camera feeds | user |
| Real WAN RTMP test | Need a VPS running mediamtx | user |
| Real-camera zone walk-through | Need a phone/webcam for one minute | user |
| GitHub description + topics | Settings → General (gh CLI not installed/authed here) | user |
| MQTT username/password live test | Works when a broker requires auth (config + panel support it) | user |

## Scope decisions (made, documented so future-you sees them)

- **Pose estimation: deferred / not in v1.0.** The very first plan listed pose
  as a requirement; no customer use case for posture surfaced, so v1.0 is
  **zone-presence-only** (tracking is centroid-based for zone events, not
  full pose). Revisit as M3 only if a concrete use case appears.
- **Deployment is manual by choice** (no systemd auto-start). One-time env
  install: `nvr-rk3576/scripts/install.sh`; bring the stack up after a reboot:
  `nvr-rk3576/scripts/start_all.sh`. The committed `scripts/systemd/*.service`
  units remain available if auto-start is ever wanted.
- **requirements.txt** now reflects every import (`rknn-toolkit-lite2==2.3.2`
  added; opencv is NOT used — drawing is PIL/numpy).

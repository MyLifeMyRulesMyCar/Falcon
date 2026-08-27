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

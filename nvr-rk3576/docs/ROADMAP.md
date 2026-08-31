# Falcon Roadmap — post-M5 close-out (v1.0 era)

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

## Root cause + fix — CLOSED

The crash is **load/UDP-driven, not feed-config-driven**: an isolated mediamtx
with the identical `-c copy` UDP publisher ran **31 min with 0 crashes** while
the live mediamtx crashed every ~30s-5min under the full stack load (and the
A/B test's extra load accelerated the live crashes). Under CPU/network load,
loopback **UDP RTP packets reorder/drop**, mediamtx's HLS muxer accumulates
frames past its **28-frame DTS-reorder limit** and crashes ("unable to extract
DTS: too many reordered frames"). fMP4 did NOT help (same crash live).

**Fix (applied):** `-rtsp_transport tcp` on the three RTSP publishers in
`testbed/start_testbed.sh` (cam_a/cam_c/cam_d) — TCP is ordered and loss-free
even under load. `hlsVariant` reverted to mpegts (the variant was never the
cause).

**Confirmation (30 min, live, TCP):** cam_c **0 restarts** (vs ~2/min with
UDP), cam_a/b/d 0/0/4 restarts, all ~30 fps, **0 mediamtx muxer crashes**,
MQTT connected, memory bounded (~1.1-1.4 GB). Acceptance met — the cam_c
churn item is **CLOSED**.

## Post-TCP-fix resource confirmation — completed

3h run (357 samples, ~180 min), same monitor format as `soak_10h.log`. Raw
log: `docs/soak_confirm_tcp.log`.

- **Memory range: 1.33-1.91 GB** (vs. 0.95-2.3 GB pre-fix) — **consistent**.
  Two sawtooth cycles observed (climb → drop → plateau, e.g. 1662 MB held
  flat for ~65 min, then 1844 → 1332). No monotonic drift.
- **Restarts: cam_a 0, cam_c 0, cam_b 0, cam_d 2** (startup-only) across the
  full run; no camera ever not-alive. cam_c (the former churn camera) 0.
- **MQTT: 0 disconnects.** All cameras ~30 fps.
- TCP transport is **resource-neutral**: same bounded memory band, no
  restart/MQTT/connectivity impact. Item status: **CLOSED** — every internal
  item is now closed.

**systemd auto-start — deferred past v1.0.** Manual startup
(`scripts/start_all.sh`) is sufficient for this stage; revisit when
unattended-reboot survival is actually needed.

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
- **v1.1/v1.3 event evidence: snapshots + post-roll clips, and the encoder
  reality.** Zone events produce a snapshot (`.jpg`, annotated) and a
  post-roll clip (`.mp4`, ~6fps from the shared preview broadcast — not the
  motion-gated detection path). Both are count-capped per camera via the
  shared `rotate_by_count` (oldest-by-mtime); served under `/snapshots/...`
  and `/clips/...` behind one shared traversal guard. The clip encoder is
  **`h264_rkmpp`, not `libx264`** — this board's ffmpeg build has no libx264
  (see `ffmpeg_rebuild_step1.md`); rkmpp is hardware, so mux CPU is minimal.
  Verified live: clips mux at the *measured* preview rate and playback at
  correct speed (duration ≈ configured `duration_sec`).
- **v1.2 platform abstraction (NPU core count/model path) — deliberately
  skipped.** The future RK3588 NVR will be a separate `nvr-rk3588` folder,
  so the three hardcoded NPU spots (core mask dict, model path, two
  `core_worker` threads) are a trivial fork edit done during real bring-up,
  not speculative config now; a real platform abstraction would also have to
  cover the hardcoded RK3576 CPU-core affinity (`_A72_CORES`), the install
  script, and the ffmpeg rebuild. Revisit if cross-fork diff-identity is
  ever wanted.

## Versioning note + upcoming milestones

The M-series milestones (M0–M5, and the docs that covered them) were
renamed to a v0.x scheme so the v1.x series has clean room:

| Milestone | version |
|---|---|
| M0 (hardware decode) | v0.0 |
| M1 (ingest + panel, M1.1) | v0.1 (v0.1.1) |
| M2 (NPU detection, M2.1–M2.3) | v0.2 |
| M3 (pose — deferred) | v0.3 |
| M4 (zones/tracking, M4.1) | v0.4 |
| M5 (MQTT/HTTP output) | v0.5 |
| M5 close-out / validation | **v1.0** |
| Event snapshots | **v1.1** (git `223a3d5`) |
| Event clips (post-roll) | **v1.3** (git `1b8d899`) |
| Events gallery + visual pass | **v1.4** (git `906305c`) |
| Open-source readiness (LICENSE/README/audit) | **v1.5** — next |
| Hardening | **v1.6** |
| RK3588 port | **v2.0** (major-version marker; separate `nvr-rk3588` folder) |

Git commit messages from the M-era keep their M-names (immutable history);
this doc's soak narrative above also keeps its original M-terms since they
reference those commits. v1.2 (NPU platform abstraction) was deliberately
skipped — see the scope note above.

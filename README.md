# Falcon

Multi-camera NVR with NPU-accelerated object detection on a Radxa CM4 (RK3576).

**→ Full documentation lives in [`nvr-rk3576/README.md`](nvr-rk3576/README.md)**

## Quick links
- [Setup](nvr-rk3576/README.md#setup)
- [Usage](nvr-rk3576/README.md#usage)
- [Control panel](nvr-rk3576/README.md#control-panel-m11)
- [Operations](nvr-rk3576/README.md#operations)
- [Milestone docs](nvr-rk3576/docs/)

## Milestones
- **M0** — single-stream hardware (rkmpp) decode
- **M1** — 4-stream mixed-protocol ingest (RTSP/RTMP/HLS/HTTP-FLV) + operator panel
- **M2** — NPU object detection (YOLOv5s, dual-core NPU) + motion gate
- **M4** — per-camera tracking + polygon zones, Frigate-style UI zone editor, config persistence
- **M5** — MQTT + HTTP output (zone events + detection summaries)

## Layout
```
nvr-rk3576/   # the NVR application — code, tests, docs, testbed
```

## Features
- Hardware-accelerated ingest via Rockchip MPP (`ffmpeg -hwaccel drm`, no Python decode)
- YOLOv5s / COCO-80 on both NPU cores, motion-gated, dual-core parallel
- Frigate-style zone editor; camera/zone/MQTT config persisted to `config.yaml`
- MQTT + HTTP publishing with per-camera content toggles
- Web control panel with live previews and bounding boxes (`http://<board>:5050`)

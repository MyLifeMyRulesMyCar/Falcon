"""Minimal operator control API over an IngestManager.

Dev-server only, no auth, bound to localhost by default — an operator tool
for the bench, not something to expose past localhost/LAN.
"""

import os
import threading
import time
from typing import Optional

import numpy as np
from flask import Flask, Response, jsonify, request, send_file
from PIL import Image

from nvr.config import (
    CameraConfig,
    ConfigError,
    HttpOutputConfig,
    MqttConfig,
    SnapshotConfig,
    parse_zones,
    write_config,
)
from nvr.control.preview_encoder import preview_encode, read_slot
from nvr.inference.detector import Detection
from nvr.ingest.manager import IngestManager
from nvr.output.snapshot_store import SnapshotStore

_JPEG_QUALITY = 80
_JPEG_POLL_S = 0.05
# Regular serving cadence: the generator repeats the latest slot JPEG on a
# fixed timer, so a bursty source (HLS segment decode) still yields a
# smooth frame stream — browsers render repeated frames fine.
_JPEG_SERVE_INTERVAL = 0.16

_DRAIN_INTERVAL = 0.005


def _zone_to_dict(z) -> dict:
    """JSON-safe ZoneConfig -> dict (same shape as config.yaml zones)."""
    return {
        "name": z.name,
        "polygon": [[float(x), float(y)] for x, y in z.polygon],
        "trigger_classes": list(z.trigger_classes),
        "dwell_time_sec": z.dwell_time_sec,
        "cooldown_sec": z.cooldown_sec,
    }


def _camera_dims(frame_store, name: str):
    """Native (width, height) from the frame store, or ``None`` (never
    published / absent frame_store / test doubles without ``dims``)."""
    if frame_store is None:
        return None
    try:
        return frame_store.dims(name)
    except Exception:
        return None


def create_app(
    manager: IngestManager,
    cameras: dict[str, CameraConfig],
    detection_stats: Optional[dict] = None,
    restart_detection: Optional[callable] = None,
    detection_flags: Optional[dict] = None,
    frame_store=None,
    preview_slots=None,
    restart_encoders: Optional[callable] = None,
    config_path: Optional[str] = None,
    mqtt_publisher=None,
    http_publisher=None,
    publish_zone_events_flags: Optional[dict] = None,
    publish_detections_flags: Optional[dict] = None,
    mqtt_config: Optional[MqttConfig] = None,
    http_output_config: Optional[HttpOutputConfig] = None,
    snapshot_store: Optional[SnapshotStore] = None,
    snapshot_config: Optional[SnapshotConfig] = None,
) -> Flask:
    """Build the Flask app. ``cameras`` is the live name->config dict the
    panel edits; the manager must own the same CameraConfig objects.
    ``detection_stats`` is the DetectionWorker's shared stats dict (optional;
    absent for tests, all detection fields default to zero/empty).
    ``restart_detection`` is called after any start/stop/rename so the
    DetectionWorker re-forks with the current queue snapshot (optional;
    absent for tests).
    ``detection_flags`` is the shared per-camera detection on/off dict
    (optional; absent -> all cameras detect).
    ``frame_store`` is the LatestFrameStore for the browser preview streams
    (optional; absent for tests).
    ``preview_slots`` are the PreviewEncoder's per-camera JPEG slots;
    when provided, the stream generators serve the pre-encoded bytes with
    zero in-process encode work (fallback to in-process encoding otherwise,
    which tests use).
    ``restart_encoders`` re-forks the PreviewEncoder processes (called after
    zone or name/url edits so the encoder picks up the new config).
    ``config_path`` persists name/url/zones edits to config.yaml when set
    (optional; absent for tests -> edits stay in-memory only).
    ``snapshot_store`` serves GET /snapshots/<camera>/<filename> (optional;
    absent -> that route 404s). ``snapshot_config`` is persisted to config.yaml
    alongside the camera edits so the v1.1 ``snapshots:`` section survives a
    panel save (optional; absent -> section not written)."""
    app = Flask(__name__, static_url_path="/static")
    detection_stats = detection_stats if detection_stats is not None else {}
    detection_flags = detection_flags if detection_flags is not None else {}
    publish_zone_events_flags = (
        publish_zone_events_flags if publish_zone_events_flags is not None else {}
    )
    publish_detections_flags = (
        publish_detections_flags if publish_detections_flags is not None else {}
    )

    # Frame counters only advance when someone drains the queues. The smoke
    # test does that in its loop; here a background thread does it so the
    # panel's GET /api/cameras shows climbing counts without a consumer.
    stop_drain = threading.Event()

    def drain_loop() -> None:
        while not stop_drain.is_set():
            try:
                received = manager.consume(timeout=_DRAIN_INTERVAL)
            except Exception:
                received = {}
            if not received:
                # consume() busy-waits on empty queues; don't spin hot.
                time.sleep(0.01)

    thread = threading.Thread(target=drain_loop, name="ingest-drain", daemon=True)
    thread.start()

    # fps is computed api-side from frame-count deltas between polls.
    fps_cache: dict[str, tuple[int, float]] = {}

    def fps_of(name: str, frames: int) -> float:
        now = time.monotonic()
        prev = fps_cache.get(name)
        fps_cache[name] = (frames, now)
        if prev is None or now - prev[1] <= 0:
            return 0.0
        return max(0.0, (frames - prev[0]) / (now - prev[1]))

    @app.get("/")
    def index():
        return app.send_static_file("index.html")

    # Browser preview streams. With the PreviewEncoder running, JPEG bytes
    # are served from shared slots (zero encode work in this process); the
    # in-process path below is the fallback (used by tests).
    encode_cache: dict[str, tuple[int, bytes]] = {}

    def boxes_present(name: str) -> bool:
        return bool(
            detection_flags.get(name, True)
            and detection_stats.get(name, {}).get("last_detections")
        )

    def mjpg_generator(name: str, annotated: bool):
        last_gen = -1
        last_slot_gen = {"raw": -1, "ann": -1}
        next_serve = 0.0
        while True:
            if preview_slots is not None:
                # The annotated slot is also used when the camera has zones,
                # so the zone outline is visible even without detections.
                has_zones = bool(cameras[name].zones)
                kind = "ann" if annotated and (boxes_present(name) or has_zones) else "raw"
                jpg, g = read_slot(preview_slots, name, kind)
                if not jpg:
                    time.sleep(0.1)
                    continue
                now = time.monotonic()
                if now < next_serve:
                    time.sleep(0.02)
                    continue
                next_serve = now + _JPEG_SERVE_INTERVAL
                yield b"--frame\r\nContent-Type: image/jpeg\r\n\r\n" + jpg + b"\r\n"
                continue
            result = frame_store.read_view(name) if frame_store is not None else None
            if result is None:
                time.sleep(0.5)
                continue
            frame_view, gen = result
            if gen == last_gen:
                time.sleep(_JPEG_POLL_S)
                continue
            last_gen = gen
            cached = encode_cache.get(f"{annotated}:{name}")
            if cached is not None and cached[0] == gen:
                jpg = cached[1]
            else:
                draw_ann = annotated and (boxes_present(name) or bool(cameras[name].zones))
                detections = None
                zones = None
                if draw_ann:
                    if boxes_present(name):
                        detections = [
                            Detection(
                                class_name=d.get("class_name", "?"),
                                confidence=d.get("confidence", 0.0),
                                bbox_xyxy=tuple(d.get("bbox", (0, 0, 0, 0))),
                            )
                            for d in detection_stats[name]["last_detections"]
                        ]
                    zones = cameras[name].zones
                jpg = preview_encode(frame_view, detections=detections, zones=zones)
                encode_cache[f"{annotated}:{name}"] = (gen, jpg)
            yield (
                b"--frame\r\nContent-Type: image/jpeg\r\n\r\n"
                + jpg
                + b"\r\n"
            )

    def stream_route(name: str, annotated: bool):
        if name not in cameras:
            return jsonify({"error": f"unknown camera: {name}"}), 404
        return Response(
            mjpg_generator(name, annotated),
            mimetype="multipart/x-mixed-replace; boundary=frame",
        )

    @app.get("/stream/<name>.mjpg")
    def stream_raw(name: str):
        return stream_route(name, False)

    @app.get("/stream/<name>/annotated.mjpg")
    def stream_annotated(name: str):
        return stream_route(name, True)

    @app.get("/api/cameras/<name>/snapshot.jpg")
    def snapshot(name: str):
        """One fresh JPEG (raw or annotated) for browser previews.

        The dashboard polls this instead of a persistent MJPEG stream — a
        single short GET per second is robust to browser connection limits,
        proxies, and table-rebuild aborts that make long-lived multipart
        streams render black in some browsers. Serves the encoder's shared
        slot (zero in-process encode); falls back to the raw slot when an
        annotated frame isn't available, and to in-process encoding only when
        no slots exist (tests).
        """
        if name not in cameras:
            return jsonify({"error": f"unknown camera: {name}"}), 404
        annotated = request.args.get("annotated", "0") == "1"
        if preview_slots is not None:
            kind = "ann" if annotated else "raw"
            jpg, _ = read_slot(preview_slots, name, kind)
            if not jpg and kind == "ann":
                jpg, _ = read_slot(preview_slots, name, "raw")
            if not jpg:
                return Response(status=204)
            return Response(jpg, mimetype="image/jpeg")
        result = frame_store.read(name) if frame_store is not None else None
        if result is None:
            return Response(status=204)
        frame, _ = result
        detections = None
        zones = None
        if annotated:
            if boxes_present(name):
                detections = [
                    Detection(
                        class_name=d.get("class_name", "?"),
                        confidence=d.get("confidence", 0.0),
                        bbox_xyxy=tuple(d.get("bbox", (0, 0, 0, 0))),
                    )
                    for d in detection_stats[name]["last_detections"]
                ]
            zones = cameras[name].zones
        return Response(
            preview_encode(frame, detections=detections, zones=zones),
            mimetype="image/jpeg",
        )

    @app.get("/snapshots/<camera>/<filename>")
    def get_snapshot(camera: str, filename: str):
        """Serve one event snapshot by camera + filename.

        ``camera``/``filename`` come straight from the URL. Path separators
        and ``..`` are rejected outright (routing already splits on "/", so a
        raw ``../`` traversal would otherwise 404 before reaching this guard
        — this keeps the rejection explicit and reachable), and the resolved
        path is additionally checked to stay under ``snapshot_store.base_dir``
        before the file is served. The guard matters now, not retroactively
        when the panel is exposed past localhost.
        """
        if snapshot_store is None:
            return "", 404
        if ".." in camera or ".." in filename or os.sep in filename:
            return "", 403
        base = os.path.abspath(snapshot_store.base_dir)
        path = os.path.abspath(os.path.join(base, camera, filename))
        if not (path == base or path.startswith(base + os.sep)):
            return "", 403
        if not os.path.isfile(path):
            return "", 404
        return send_file(path)

    @app.get("/api/cameras")
    def list_cameras():
        rows = []
        for name, cfg in cameras.items():
            s = manager.stats().get(name, {})
            alive = bool(s.get("alive", False))
            frames = int(s.get("frames_received", 0))
            d = detection_stats.get(name, {})
            # When the DetectionWorker is active it consumes every queue
            # frame (motion gate), starving the panel's drain counter — its
            # total+skipped IS the ingest frame count, so prefer it.
            worker_frames = int(d.get("total", 0)) + int(d.get("skipped", 0))
            detection_enabled = bool(detection_flags.get(name, True))
            # Prefer worker frame counts only while detection is active:
            # when disabled, the worker stops draining and its totals freeze
            # at stale values, so fall back to the manager's drain counter.
            if worker_frames and detection_enabled:
                frames = worker_frames
            # True ingest fps: decoder production, immune to queue consumers.
            decoded = int(s.get("frames_decoded", 0))
            infer_fps = float(d.get("inference_fps", 0.0))
            skip_ratio = float(d.get("skip_ratio", 0.0))
            if not detection_enabled:
                # Display-level decay: the stats ring holds its last values;
                # a disabled camera shows zero rather than stale numbers.
                infer_fps = 0.0
                skip_ratio = 0.0
            rows.append(
                {
                    "name": cfg.name,
                    "url": cfg.url,
                    "alive": alive,
                    "frames_received": frames,
                    "restart_count": int(s.get("restart_count", 0)),
                    "fps": round(fps_of(name, decoded) if decoded else 0.0, 1),
                    "last_error": s.get("last_error", ""),
                    "detection_enabled": detection_enabled,
                    "inference_fps": round(infer_fps, 1),
                    "skip_ratio": round(skip_ratio, 2),
                    "last_detections": d.get("last_detections", []) if detection_enabled else [],
                    "zones": [z.name for z in cfg.zones],
                    "recent_zone_events": d.get("recent_zone_events", []),
                    "width": (_camera_dims(frame_store, name) or (None, None))[0],
                    "height": (_camera_dims(frame_store, name) or (None, None))[1],
                    "publish_zone_events": bool(
                        publish_zone_events_flags.get(name, cfg.publish_zone_events)
                    ),
                    "publish_detections": bool(
                        publish_detections_flags.get(name, cfg.publish_detections)
                    ),
                }
            )
        return jsonify(rows)

    @app.get("/api/cameras/<name>/zones")
    def get_zones(name: str):
        """Full zone configs + native dimensions for the zone editor."""
        if name not in cameras:
            return jsonify({"error": f"unknown camera: {name}"}), 404
        dims = _camera_dims(frame_store, name) or (None, None)
        return jsonify(
            {
                "width": dims[0],
                "height": dims[1],
                "zones": [_zone_to_dict(z) for z in cameras[name].zones],
            }
        )

    @app.put("/api/cameras/<name>/zones")
    def set_zones(name: str):
        """Replace a camera's zones (bulk, same shape as config.yaml)."""
        if name not in cameras:
            return jsonify({"error": f"unknown camera: {name}"}), 404
        body = request.get_json(silent=True)
        if not isinstance(body, list):
            return jsonify({"error": "body must be a list of zone configs"}), 400
        try:
            parsed = parse_zones(body, name)
        except ConfigError as exc:
            return jsonify({"error": str(exc)}), 400
        old = cameras[name].zones
        cameras[name].zones = parsed
        if config_path:
            try:
                write_config(config_path, list(cameras.values()), mqtt_config, http_output_config, snapshot_config)
            except (ConfigError, OSError) as exc:
                cameras[name].zones = old  # revert
                return jsonify({"error": f"failed to persist config: {exc}"}), 500
        if restart_detection is not None:
            restart_detection()
        if restart_encoders is not None:
            restart_encoders()
        return jsonify([_zone_to_dict(z) for z in cameras[name].zones])

    @app.get("/api/zone_events")
    def zone_events():
        """Flattened zone events, most-recent-first across all cameras."""
        events = []
        for name in cameras:
            for ev in detection_stats.get(name, {}).get("recent_zone_events", []):
                events.append(ev)
        events.sort(key=lambda e: e.get("timestamp", 0), reverse=True)
        return jsonify(events[:50])

    @app.post("/api/output/mqtt/<state>")
    def toggle_mqtt(state: str):
        if mqtt_publisher is None:
            return jsonify({"error": "mqtt is not configured"}), 404
        if state not in ("on", "off"):
            return jsonify({"error": "state must be 'on' or 'off'"}), 400
        mqtt_publisher.set_enabled(state == "on")
        return "", 200

    @app.post("/api/output/http/<state>")
    def toggle_http(state: str):
        if http_publisher is None:
            return jsonify({"error": "http output is not configured"}), 404
        if state not in ("on", "off"):
            return jsonify({"error": "state must be 'on' or 'off'"}), 400
        http_publisher.set_enabled(state == "on")
        return "", 200

    @app.get("/api/output/status")
    def output_status():
        return jsonify(
            {
                "mqtt": (
                    {
                        "enabled": bool(mqtt_publisher.enabled),
                        "connected": bool(mqtt_publisher.connected()),
                        "host": mqtt_config.host if mqtt_config else None,
                        "port": mqtt_config.port if mqtt_config else None,
                        "topic_prefix": mqtt_config.topic_prefix if mqtt_config else None,
                        "username": mqtt_config.username if mqtt_config else None,
                    }
                    if mqtt_publisher is not None
                    else None
                ),
                "http": (
                    {"enabled": bool(http_publisher.enabled)}
                    if http_publisher is not None
                    else None
                ),
            }
        )

    @app.put("/api/output/mqtt")
    def set_mqtt_settings():
        """Edit MQTT host/port/topic_prefix/username/password live + persist."""
        if mqtt_config is None or mqtt_publisher is None:
            return jsonify({"error": "mqtt is not configured"}), 400
        body = request.get_json(silent=True)
        if not isinstance(body, dict):
            return jsonify({"error": "body must be an object"}), 400
        host = body.get("host")
        port = body.get("port", mqtt_config.port)
        prefix = body.get("topic_prefix", mqtt_config.topic_prefix)
        username = body.get("username", mqtt_config.username) or None
        password = body.get("password", mqtt_config.password) or None
        if not isinstance(host, str) or not host.strip():
            return jsonify({"error": "'host' must be a non-empty string"}), 400
        try:
            port = int(port)
        except (TypeError, ValueError):
            return jsonify({"error": "'port' must be an integer"}), 400
        if not 1 <= port <= 65535:
            return jsonify({"error": "'port' must be in 1..65535"}), 400
        if not isinstance(prefix, str) or not prefix.strip():
            return jsonify({"error": "'topic_prefix' must be a non-empty string"}), 400
        new_config = MqttConfig(
            host=host.strip(),
            port=port,
            topic_prefix=prefix.strip(),
            username=username,
            password=password,
            enabled=mqtt_config.enabled,
        )
        if config_path:
            try:
                write_config(
                    config_path, list(cameras.values()), new_config, http_output_config, snapshot_config
                )
            except (ConfigError, OSError) as exc:
                return jsonify({"error": f"failed to persist config: {exc}"}), 500
        # Apply live: mutate the shared config object, reconnect the publisher.
        mqtt_config.host = new_config.host
        mqtt_config.port = new_config.port
        mqtt_config.topic_prefix = new_config.topic_prefix
        mqtt_config.username = new_config.username
        mqtt_config.password = new_config.password
        mqtt_publisher.reconfigure(new_config)
        return jsonify(
            {
                "host": mqtt_config.host,
                "port": mqtt_config.port,
                "topic_prefix": mqtt_config.topic_prefix,
                "username": mqtt_config.username,
            }
        )

    @app.post("/api/cameras/<name>/publish/<content>/<state>")
    def toggle_publish(name: str, content: str, state: str):
        """Live per-camera output content switch (zone_events | detections)."""
        if name not in cameras:
            return jsonify({"error": f"unknown camera: {name}"}), 404
        if content not in ("zone_events", "detections"):
            return jsonify({"error": "content must be 'zone_events' or 'detections'"}), 400
        if state not in ("on", "off"):
            return jsonify({"error": "state must be 'on' or 'off'"}), 400
        flags = (
            publish_zone_events_flags if content == "zone_events" else publish_detections_flags
        )
        flags[name] = state == "on"
        return "", 200

    @app.post("/api/cameras/<name>/start")
    def start_camera(name: str):
        if name not in cameras:
            return jsonify({"error": f"unknown camera: {name}"}), 404
        manager.start_one(name)
        if restart_detection is not None:
            restart_detection()
        return jsonify({"name": name, "alive": True})

    @app.post("/api/cameras/<name>/stop")
    def stop_camera(name: str):
        if name not in cameras:
            return jsonify({"error": f"unknown camera: {name}"}), 404
        manager.stop_one(name)
        if restart_detection is not None:
            restart_detection()
        return jsonify({"name": name, "alive": False})

    @app.post("/api/cameras/<name>/detection/on")
    def detection_on(name: str):
        if name not in cameras:
            return jsonify({"error": f"unknown camera: {name}"}), 404
        detection_flags[name] = True
        return "", 200

    @app.post("/api/cameras/<name>/detection/off")
    def detection_off(name: str):
        if name not in cameras:
            return jsonify({"error": f"unknown camera: {name}"}), 404
        detection_flags[name] = False
        return "", 200

    @app.put("/api/cameras/<name>")
    def update_camera(name: str):
        if name not in cameras:
            return jsonify({"error": f"unknown camera: {name}"}), 404
        body = request.get_json(silent=True)
        if not isinstance(body, dict):
            return jsonify({"error": "body must be {'name': str, 'url': str}"}), 400
        new_name = body.get("name")
        new_url = body.get("url")
        if not isinstance(new_name, str) or not new_name.strip():
            return jsonify({"error": "body must be {'name': str, 'url': str}"}), 400
        if not isinstance(new_url, str) or not new_url.strip():
            return jsonify({"error": "body must be {'name': str, 'url': str}"}), 400
        new_name, new_url = new_name.strip(), new_url.strip()
        # Zones carry across a rename; only name/url are edited here.
        new_config = CameraConfig(name=new_name, url=new_url, zones=cameras[name].zones)
        # Check liveness before persisting so a write failure can't leave a
        # stale config next to a live manager (stubs without is_alive fall
        # back to manager.update_camera's RuntimeError below).
        if getattr(manager, "is_alive", lambda n: False)(name):
            return jsonify({"error": "stop the camera before editing"}), 409
        if config_path:
            prospective = [new_config if c.name == name else c for c in cameras.values()]
            try:
                write_config(config_path, prospective, mqtt_config, http_output_config, snapshot_config)
            except (ConfigError, OSError) as exc:
                return jsonify({"error": f"failed to persist config: {exc}"}), 500
        try:
            manager.update_camera(name, new_config)
        except RuntimeError:
            return jsonify({"error": "stop the camera before editing"}), 409
        cameras.pop(name, None)
        cameras[new_name] = new_config
        if restart_detection is not None:
            restart_detection()
        if restart_encoders is not None:
            restart_encoders()
        return jsonify({"name": new_name, "url": body["url"]})

    return app

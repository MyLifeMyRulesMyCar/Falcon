"""Minimal operator control API over an IngestManager.

Dev-server only, no auth, bound to localhost by default — an operator tool
for the bench, not something to expose past localhost/LAN.
"""

import threading
import time

from flask import Flask, jsonify, request

from nvr.config import CameraConfig
from nvr.ingest.manager import IngestManager

_DRAIN_INTERVAL = 0.005


def create_app(manager: IngestManager, cameras: dict[str, CameraConfig]) -> Flask:
    """Build the Flask app. ``cameras`` is the live name->config dict the
    panel edits; the manager must own the same CameraConfig objects."""
    app = Flask(__name__, static_url_path="/static")

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

    @app.get("/api/cameras")
    def list_cameras():
        rows = []
        for name, cfg in cameras.items():
            s = manager.stats().get(name, {})
            alive = bool(s.get("alive", False))
            frames = int(s.get("frames_received", 0))
            rows.append(
                {
                    "name": cfg.name,
                    "url": cfg.url,
                    "alive": alive,
                    "frames_received": frames,
                    "restart_count": int(s.get("restart_count", 0)),
                    "fps": round(fps_of(name, frames), 1),
                    "last_error": s.get("last_error", ""),
                }
            )
        return jsonify(rows)

    @app.post("/api/cameras/<name>/start")
    def start_camera(name: str):
        if name not in cameras:
            return jsonify({"error": f"unknown camera: {name}"}), 404
        manager.start_one(name)
        return jsonify({"name": name, "alive": True})

    @app.post("/api/cameras/<name>/stop")
    def stop_camera(name: str):
        if name not in cameras:
            return jsonify({"error": f"unknown camera: {name}"}), 404
        manager.stop_one(name)
        return jsonify({"name": name, "alive": False})

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
        new_config = CameraConfig(name=new_name.strip(), url=new_url.strip())
        try:
            manager.update_camera(name, new_config)
        except RuntimeError:
            return jsonify({"error": "stop the camera before editing"}), 409
        cameras.pop(name, None)
        cameras[new_name] = new_config
        return jsonify({"name": new_name, "url": body["url"]})

    return app

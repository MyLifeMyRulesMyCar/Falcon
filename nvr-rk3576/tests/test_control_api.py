"""Tests for nvr.control.api — Flask test client against a stubbed manager,
no real subprocesses or network."""

import sys
from pathlib import Path

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from nvr.config import CameraConfig, ZoneConfig
from nvr.control.api import create_app

CAMERAS = {
    "cam_a": CameraConfig(name="cam_a", url="rtsp://a"),
    "cam_b": CameraConfig(name="cam_b", url="rtsp://b"),
}


def make_cameras() -> dict[str, CameraConfig]:
    return {
        "cam_a": CameraConfig(name="cam_a", url="rtsp://a"),
        "cam_b": CameraConfig(name="cam_b", url="rtsp://b"),
    }


class StubManager:
    """Records calls; liveness is hand-controlled via ``running``."""

    def __init__(self, cameras=None):
        self.cameras = cameras if cameras is not None else make_cameras()
        self.running = set()
        self.started = []
        self.stopped = []
        self.updated = []
        self.stats_data = {c: self._idle() for c in self.cameras}

    @staticmethod
    def _idle():
        return {
            "alive": False,
            "frames_received": 0,
            "restart_count": 0,
            "last_frame_ts": None,
        }

    def start_one(self, name):
        self.started.append(name)
        self.running.add(name)
        self.stats_data[name]["alive"] = True

    def stop_one(self, name):
        self.stopped.append(name)
        self.running.discard(name)
        self.stats_data[name]["alive"] = False

    def update_camera(self, name, new_config):
        if name in self.running:
            raise RuntimeError(f"{name} is running, stop it before editing")
        self.updated.append((name, new_config))
        self.cameras.pop(name, None)
        self.cameras[new_config.name] = new_config
        self.stats_data[new_config.name] = self._idle()

    def consume(self, timeout=0.05):
        return {}

    def is_alive(self, name):
        return name in self.running

    def stats(self):
        return dict(self.stats_data)

    def get_queue(self, name):
        return object() if name in self.running else None


@pytest.fixture()
def client():
    cameras = make_cameras()
    app = create_app(StubManager(cameras), cameras)
    app.config["TESTING"] = True
    return app.test_client()


def test_list_cameras_shape_all_stopped(client):
    res = client.get("/api/cameras")
    assert res.status_code == 200
    rows = res.get_json()
    assert [r["name"] for r in rows] == ["cam_a", "cam_b"]
    for row in rows:
        assert row["alive"] is False
        assert row["frames_received"] == 0
        assert row["restart_count"] == 0
        assert row["inference_fps"] == 0.0
        assert row["skip_ratio"] == 0.0
        assert row["last_detections"] == []
        assert row["detection_enabled"] is True
        assert "fps" in row and "url" in row


def test_list_cameras_surfaces_detection_stats():
    import multiprocessing

    from nvr.control.api import create_app

    cameras = make_cameras()
    mgr = StubManager(cameras)
    detection_stats = multiprocessing.Manager().dict()
    detection_stats["cam_a"] = {
        "inference_fps": 7.5,
        "skip_ratio": 0.2,
        "last_detections": [{"class_name": "person", "confidence": 0.88}],
    }
    app = create_app(mgr, cameras, detection_stats=detection_stats)
    app.config["TESTING"] = True
    rows = app.test_client().get("/api/cameras").get_json()

    cam_a = [r for r in rows if r["name"] == "cam_a"][0]
    assert cam_a["inference_fps"] == 7.5
    assert cam_a["skip_ratio"] == 0.2
    assert cam_a["last_detections"] == [{"class_name": "person", "confidence": 0.88}]
    cam_b = [r for r in rows if r["name"] == "cam_b"][0]
    assert cam_b["inference_fps"] == 0.0
    assert cam_b["last_detections"] == []


def test_start_unknown_camera_404(client):
    res = client.post("/api/cameras/nope/start")
    assert res.status_code == 404
    assert "unknown camera" in res.get_json()["error"]


def test_stop_unknown_camera_404(client):
    res = client.post("/api/cameras/nope/stop")
    assert res.status_code == 404


def test_put_while_running_409(client):
    client.post("/api/cameras/cam_a/start")
    res = client.put("/api/cameras/cam_a", json={"name": "cam_a", "url": "rtsp://new"})
    assert res.status_code == 409
    assert "stop the camera before editing" in res.get_json()["error"]


def test_put_while_stopped_200_and_reflected(client):
    res = client.put("/api/cameras/cam_a", json={"name": "cam_a", "url": "rtsp://new"})
    assert res.status_code == 200

    rows = client.get("/api/cameras").get_json()
    assert [r for r in rows if r["name"] == "cam_a"][0]["url"] == "rtsp://new"


def test_put_rename_reflected(client):
    res = client.put("/api/cameras/cam_a", json={"name": "cam_x", "url": "rtsp://x"})
    assert res.status_code == 200

    names = [r["name"] for r in client.get("/api/cameras").get_json()]
    assert "cam_a" not in names
    assert "cam_x" in names
    assert client.post("/api/cameras/cam_a/start").status_code == 404
    assert client.post("/api/cameras/cam_x/start").status_code == 200


def test_put_malformed_body_400(client):
    res = client.put("/api/cameras/cam_a", json={"name": "cam_a"})
    assert res.status_code == 400
    res = client.put("/api/cameras/cam_a", json={"name": "", "url": "rtsp://x"})
    assert res.status_code == 400
    res = client.put("/api/cameras/cam_a", json={"name": "cam_a", "url": "   "})
    assert res.status_code == 400
    res = client.put("/api/cameras/cam_a", json={"name": "cam_a", "url": ""})
    assert res.status_code == 400


def test_index_serves_page(client):
    res = client.get("/")
    assert res.status_code == 200
    assert b"control panel" in res.data


class _FakeFrameStore:
    def __init__(self):
        self._data = {}
        self._gen = {}

    def set(self, name, frame):
        self._data[name] = frame.copy()
        self._gen[name] = self._gen.get(name, 0) + 1

    def dims(self, name):
        if name not in self._data:
            return None
        h, w = self._data[name].shape[:2]
        return (w, h)

    def read(self, name):
        if name not in self._data:
            return None
        return self._data[name].copy(), self._gen[name]

    def read_view(self, name):
        return self.read(name)


def _frame():
    return np.zeros((4, 8, 3), dtype=np.uint8)


def test_stream_raw_serves_multipart_jpeg():
    import numpy as np

    from nvr.control.api import create_app

    cameras = make_cameras()
    mgr = StubManager(cameras)
    store = _FakeFrameStore()
    store.set("cam_a", _frame())
    app = create_app(mgr, cameras, frame_store=store)
    app.config["TESTING"] = True

    # Drive the view function directly: the werkzeug test client consumes
    # infinite generators during open(), so it can't be used for MJPEG.
    with app.test_request_context():
        resp = app.view_functions["stream_raw"]("cam_a")
        assert "multipart/x-mixed-replace" in resp.content_type
        chunk = next(iter(resp.response))
        assert b"--frame" in chunk
        assert b"Content-Type: image/jpeg" in chunk
        assert b"\xff\xd8" in chunk  # JPEG SOI magic

    with app.test_request_context():
        resp = app.view_functions["stream_raw"]("nope")
        assert resp[1] == 404  # (body, status, headers) tuple
        resp = app.view_functions["stream_raw"]("cam_b")
        assert "multipart/x-mixed-replace" in resp.content_type  # empty until written


def test_stream_annotated_obeys_detection_flags():
    import multiprocessing

    import numpy as np

    from nvr.control.api import create_app

    cameras = make_cameras()
    mgr = StubManager(cameras)
    store = _FakeFrameStore()
    store.set("cam_a", _frame())
    stats = multiprocessing.Manager().dict()
    stats["cam_a"] = {
        "total": 1,
        "skipped": 0,
        "inference_fps": 9.0,
        "skip_ratio": 0.0,
        "last_detections": [{"class_name": "person", "confidence": 0.8, "bbox": [1, 1, 3, 3]}],
    }
    flags = multiprocessing.Manager().dict()
    app = create_app(mgr, cameras, detection_stats=stats, detection_flags=flags, frame_store=store)
    app.config["TESTING"] = True

    # Detection on: annotated stream encodes without error.
    with app.test_request_context():
        resp = app.view_functions["stream_annotated"]("cam_a")
        assert "multipart/x-mixed-replace" in resp.content_type
        chunk = next(iter(resp.response))
        assert b"\xff\xd8" in chunk

    # Detection off: still serves (blank overlay), just no boxes drawn.
    flags["cam_a"] = False
    with app.test_request_context():
        resp = app.view_functions["stream_annotated"]("cam_a")
        assert "multipart/x-mixed-replace" in resp.content_type
        chunk = next(iter(resp.response))
        assert b"\xff\xd8" in chunk


def test_restart_callback_fires_on_start_stop():
    from nvr.control.api import create_app

    cameras = make_cameras()
    mgr = StubManager(cameras)
    calls = []
    app = create_app(mgr, cameras, restart_detection=lambda: calls.append("restart"))
    app.config["TESTING"] = True
    client = app.test_client()

    client.post("/api/cameras/cam_a/start")
    client.post("/api/cameras/cam_a/stop")
    client.put("/api/cameras/cam_a", json={"name": "cam_a", "url": "rtsp://new"})
    assert calls == ["restart", "restart", "restart"]
    assert client.post("/api/cameras/nope/start").status_code == 404
    assert calls == ["restart", "restart", "restart"]  # unknown: no callback


def test_detection_toggle_routes_and_enabled_field():
    import multiprocessing

    from nvr.control.api import create_app

    cameras = make_cameras()
    mgr = StubManager(cameras)
    flags = multiprocessing.Manager().dict()
    app = create_app(mgr, cameras, detection_flags=flags)
    app.config["TESTING"] = True
    client = app.test_client()

    rows = client.get("/api/cameras").get_json()
    assert all(r["detection_enabled"] is True for r in rows)  # default on

    assert client.post("/api/cameras/nope/detection/off").status_code == 404
    assert client.post("/api/cameras/cam_a/detection/off").status_code == 200
    assert flags["cam_a"] is False
    assert client.post("/api/cameras/cam_a/detection/on").status_code == 200
    assert flags["cam_a"] is True

    client.post("/api/cameras/cam_a/detection/off")
    rows = client.get("/api/cameras").get_json()
    cam_a = [r for r in rows if r["name"] == "cam_a"][0]
    assert cam_a["detection_enabled"] is False
    assert cam_a["inference_fps"] == 0.0
    assert cam_a["skip_ratio"] == 0.0
    assert cam_a["last_detections"] == []


def test_detection_off_falls_back_to_manager_frame_count():
    import multiprocessing

    from nvr.control.api import create_app

    cameras = make_cameras()
    mgr = StubManager(cameras)
    flags = multiprocessing.Manager().dict()
    stats = multiprocessing.Manager().dict()
    # Stale worker counts from before the toggle; manager count is live.
    stats["cam_a"] = {
        "total": 100,
        "skipped": 50,
        "inference_fps": 9.0,
        "skip_ratio": 0.2,
        "last_detections": [{"class_name": "person", "confidence": 0.8}],
    }
    mgr.stats_data["cam_a"]["frames_received"] = 42
    app = create_app(mgr, cameras, detection_stats=stats, detection_flags=flags)
    app.config["TESTING"] = True
    client = app.test_client()

    # Detection on: worker counts preferred (150).
    cam_a = [r for r in client.get("/api/cameras").get_json() if r["name"] == "cam_a"][0]
    assert cam_a["frames_received"] == 150

    # Detection off: falls back to the manager's live count, not stale worker.
    client.post("/api/cameras/cam_a/detection/off")
    cam_a = [r for r in client.get("/api/cameras").get_json() if r["name"] == "cam_a"][0]
    assert cam_a["frames_received"] == 42
    assert cam_a["inference_fps"] == 0.0


def test_list_cameras_has_dims_when_published():
    import numpy as np

    from nvr.control.api import create_app

    cameras = make_cameras()
    mgr = StubManager(cameras)
    store = _FakeFrameStore()
    store.set("cam_a", np.zeros((360, 640, 3), dtype=np.uint8))
    app = create_app(mgr, cameras, frame_store=store)
    app.config["TESTING"] = True

    rows = app.test_client().get("/api/cameras").get_json()
    cam_a = [r for r in rows if r["name"] == "cam_a"][0]
    assert cam_a["width"] == 640 and cam_a["height"] == 360
    cam_b = [r for r in rows if r["name"] == "cam_b"][0]
    assert cam_b["width"] is None and cam_b["height"] is None


def test_get_zones_shape_and_404():
    import numpy as np

    from nvr.control.api import create_app

    cameras = make_cameras()
    cameras["cam_a"].zones = [
        ZoneConfig(
            name="entry",
            polygon=[(10, 20), (30, 40), (50, 60)],
            trigger_classes=["person"],
            dwell_time_sec=2.0,
            cooldown_sec=30,
        )
    ]
    mgr = StubManager(cameras)
    store = _FakeFrameStore()
    store.set("cam_a", np.zeros((360, 640, 3), dtype=np.uint8))
    app = create_app(mgr, cameras, frame_store=store)
    app.config["TESTING"] = True
    client = app.test_client()

    res = client.get("/api/cameras/cam_a/zones")
    assert res.status_code == 200
    body = res.get_json()
    assert body["width"] == 640 and body["height"] == 360
    assert body["zones"] == [
        {
            "name": "entry",
            "polygon": [[10.0, 20.0], [30.0, 40.0], [50.0, 60.0]],
            "trigger_classes": ["person"],
            "dwell_time_sec": 2.0,
            "cooldown_sec": 30.0,
        }
    ]
    assert client.get("/api/cameras/nope/zones").status_code == 404


def test_put_zones_valid_updates_and_calls_back():
    from nvr.control.api import create_app

    cameras = make_cameras()
    calls = []
    app = create_app(
        StubManager(cameras),
        cameras,
        restart_detection=lambda: calls.append("d"),
        restart_encoders=lambda: calls.append("e"),
    )
    app.config["TESTING"] = True
    client = app.test_client()

    payload = [
        {
            "name": "entry",
            "polygon": [[10, 20], [30, 40], [50, 60]],
            "trigger_classes": ["person"],
            "dwell_time_sec": 2.0,
            "cooldown_sec": 30,
        }
    ]
    res = client.put("/api/cameras/cam_a/zones", json=payload)
    assert res.status_code == 200
    assert cameras["cam_a"].zones[0].name == "entry"
    assert res.get_json()[0]["name"] == "entry"
    assert calls == ["d", "e"]


def test_put_zones_invalid_400_and_unknown_404():
    from nvr.control.api import create_app

    cameras = make_cameras()
    calls = []
    app = create_app(
        StubManager(cameras),
        cameras,
        restart_detection=lambda: calls.append("d"),
    )
    app.config["TESTING"] = True
    client = app.test_client()

    bad = [
        {
            "name": "z",
            "polygon": [[0, 0], [1, 1]],
            "trigger_classes": ["person"],
            "dwell_time_sec": 2.0,
            "cooldown_sec": 0,
        }
    ]
    res = client.put("/api/cameras/cam_a/zones", json=bad)
    assert res.status_code == 400
    assert "zone 'z'" in res.get_json()["error"]
    assert calls == []  # nothing applied on validation failure
    assert client.put("/api/cameras/nope/zones", json=[]).status_code == 404


def test_put_zones_persists_to_config(tmp_path):
    import numpy as np

    from nvr.config import load_config
    from nvr.control.api import create_app

    cfg_path = str(tmp_path / "config.yaml")
    cameras = make_cameras()
    mgr = StubManager(cameras)
    store = _FakeFrameStore()
    store.set("cam_a", np.zeros((360, 640, 3), dtype=np.uint8))
    app = create_app(mgr, cameras, frame_store=store, config_path=cfg_path)
    app.config["TESTING"] = True
    client = app.test_client()

    payload = [
        {
            "name": "entry",
            "polygon": [[0, 0], [640, 0], [640, 360], [0, 360]],
            "trigger_classes": ["bird"],
            "dwell_time_sec": 2.0,
            "cooldown_sec": 30,
        }
    ]
    assert client.put("/api/cameras/cam_a/zones", json=payload).status_code == 200

    loaded = load_config(cfg_path)
    assert [c.name for c in loaded.cameras] == ["cam_a", "cam_b"]
    cam_a = [c for c in loaded.cameras if c.name == "cam_a"][0]
    assert len(cam_a.zones) == 1 and cam_a.zones[0].name == "entry"


def test_put_camera_persists_rename_to_config(tmp_path):
    from nvr.config import load_config
    from nvr.control.api import create_app

    cfg_path = str(tmp_path / "config.yaml")
    cameras = make_cameras()
    app = create_app(StubManager(cameras), cameras, config_path=cfg_path)
    app.config["TESTING"] = True
    client = app.test_client()

    res = client.put("/api/cameras/cam_a", json={"name": "cam_x", "url": "rtsp://x"})
    assert res.status_code == 200

    loaded = load_config(cfg_path)
    names = [c.name for c in loaded.cameras]
    assert "cam_a" not in names and "cam_x" in names


def test_put_while_running_does_not_persist(tmp_path):
    from pathlib import Path

    from nvr.control.api import create_app

    cfg_path = str(tmp_path / "config.yaml")
    cameras = make_cameras()
    app = create_app(StubManager(cameras), cameras, config_path=cfg_path)
    app.config["TESTING"] = True
    client = app.test_client()

    client.post("/api/cameras/cam_a/start")
    res = client.put("/api/cameras/cam_a", json={"name": "cam_a", "url": "rtsp://new"})
    assert res.status_code == 409
    assert not Path(cfg_path).exists()  # config never written on 409

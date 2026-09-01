"""Tests for nvr.control.api — Flask test client against a stubbed manager,
no real subprocesses or network."""

import sys
from pathlib import Path

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from nvr.config import CameraConfig, ZoneConfig
from nvr.control.api import create_app
from nvr.output.clip_store import ClipStore
from nvr.output.snapshot_store import SnapshotStore

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


class _FakeMqttPublisher:
    def __init__(self):
        self.enabled = True
        self.connected_flag = False
        self.calls = []

    def set_enabled(self, v):
        self.enabled = v
        self.calls.append(("set_enabled", v))

    def connected(self):
        return self.connected_flag

    def reconfigure(self, cfg):
        self.calls.append(("reconfigure", cfg))


class _FakeHttpPublisher:
    def __init__(self):
        self.enabled = True
        self.calls = []

    def set_enabled(self, v):
        self.enabled = v
        self.calls.append(("set_enabled", v))


def test_output_toggles_and_status():
    from nvr.control.api import create_app

    cameras = make_cameras()
    mqtt = _FakeMqttPublisher()
    http = _FakeHttpPublisher()
    app = create_app(
        StubManager(cameras), cameras, mqtt_publisher=mqtt, http_publisher=http
    )
    app.config["TESTING"] = True
    client = app.test_client()

    st = client.get("/api/output/status").get_json()
    assert st["mqtt"]["enabled"] is True
    assert st["mqtt"]["connected"] is False
    assert st["http"]["enabled"] is True

    assert client.post("/api/output/mqtt/off").status_code == 200
    assert mqtt.enabled is False
    assert client.post("/api/output/http/off").status_code == 200
    assert http.enabled is False
    assert client.post("/api/output/mqtt/on").status_code == 200
    assert mqtt.enabled is True
    assert client.post("/api/output/mqtt/bogus").status_code == 400


def test_output_toggle_unconfigured_404():
    from nvr.control.api import create_app

    cameras = make_cameras()
    app = create_app(StubManager(cameras), cameras)  # no publishers
    app.config["TESTING"] = True
    client = app.test_client()

    assert client.post("/api/output/mqtt/on").status_code == 404
    assert client.post("/api/output/http/on").status_code == 404
    st = client.get("/api/output/status").get_json()
    assert st["mqtt"] is None and st["http"] is None


def test_put_mqtt_settings_validates_persists_reconfigures(tmp_path):
    from nvr.config import MqttConfig, load_config
    from nvr.control.api import create_app

    cfg_path = str(tmp_path / "config.yaml")
    cameras = make_cameras()
    mqtt = _FakeMqttPublisher()
    mqtt_cfg = MqttConfig(host="old", port=1883, topic_prefix="nvr")
    app = create_app(
        StubManager(cameras),
        cameras,
        mqtt_publisher=mqtt,
        mqtt_config=mqtt_cfg,
        config_path=cfg_path,
    )
    app.config["TESTING"] = True
    client = app.test_client()

    res = client.put(
        "/api/output/mqtt",
        json={
            "host": "broker.local",
            "port": 1884,
            "topic_prefix": "home",
            "username": "u",
            "password": "p",
        },
    )
    assert res.status_code == 200
    assert mqtt_cfg.host == "broker.local"
    assert mqtt_cfg.port == 1884
    assert mqtt_cfg.topic_prefix == "home"
    assert any(c[0] == "reconfigure" for c in mqtt.calls)
    loaded = load_config(cfg_path)
    assert loaded.mqtt.host == "broker.local"
    assert loaded.mqtt.username == "u"
    assert loaded.mqtt.password == "p"

    # invalid host -> 400, nothing persisted
    res = client.put("/api/output/mqtt", json={"host": "  "})
    assert res.status_code == 400
    loaded = load_config(cfg_path)
    assert loaded.mqtt.host == "broker.local"


def test_publish_toggles_and_rows():
    import multiprocessing

    from nvr.control.api import create_app

    cameras = make_cameras()
    ze = multiprocessing.Manager().dict({"cam_a": True, "cam_b": True})
    de = multiprocessing.Manager().dict({"cam_a": False, "cam_b": False})
    app = create_app(
        StubManager(cameras),
        cameras,
        publish_zone_events_flags=ze,
        publish_detections_flags=de,
    )
    app.config["TESTING"] = True
    client = app.test_client()

    rows = client.get("/api/cameras").get_json()
    assert all(r["publish_zone_events"] is True for r in rows)
    assert all(r["publish_detections"] is False for r in rows)

    assert client.post("/api/cameras/cam_a/publish/detections/on").status_code == 200
    assert de["cam_a"] is True
    assert client.post("/api/cameras/cam_a/publish/zone_events/off").status_code == 200
    assert ze["cam_a"] is False
    assert client.post("/api/cameras/nope/publish/detections/on").status_code == 404
    assert client.post("/api/cameras/cam_a/publish/bogus/on").status_code == 400
    assert client.post("/api/cameras/cam_a/publish/detections/weird").status_code == 400

    rows = client.get("/api/cameras").get_json()
    cam_a = [r for r in rows if r["name"] == "cam_a"][0]
    assert cam_a["publish_zone_events"] is False
    assert cam_a["publish_detections"] is True


def test_snapshot_serves_jpeg_and_404():
    import numpy as np

    from nvr.control.api import create_app

    cameras = make_cameras()
    mgr = StubManager(cameras)
    store = _FakeFrameStore()
    store.set("cam_a", np.zeros((360, 640, 3), dtype=np.uint8))
    app = create_app(mgr, cameras, frame_store=store)
    app.config["TESTING"] = True
    client = app.test_client()

    res = client.get("/api/cameras/cam_a/snapshot.jpg")
    assert res.status_code == 200
    assert res.mimetype == "image/jpeg"
    assert res.data[:2] == b"\xff\xd8"  # JPEG SOI

    res = client.get("/api/cameras/cam_a/snapshot.jpg?annotated=1")
    assert res.status_code == 200
    assert res.data[:2] == b"\xff\xd8"

    assert client.get("/api/cameras/nope/snapshot.jpg").status_code == 404


def test_camera_save_preserves_output_sections(tmp_path):
    from nvr.config import HttpOutputConfig, MqttConfig, load_config
    from nvr.control.api import create_app

    cfg_path = str(tmp_path / "config.yaml")
    cameras = make_cameras()
    app = create_app(
        StubManager(cameras),
        cameras,
        config_path=cfg_path,
        mqtt_config=MqttConfig(host="h", port=1883, topic_prefix="t"),
        http_output_config=HttpOutputConfig(url="http://x"),
    )
    app.config["TESTING"] = True
    client = app.test_client()

    assert client.put("/api/cameras/cam_a/zones", json=[]).status_code == 200
    loaded = load_config(cfg_path)
    assert loaded.mqtt.host == "h"
    assert loaded.http_output.url == "http://x"

    assert (
        client.put("/api/cameras/cam_a", json={"name": "cam_x", "url": "rtsp://x"}).status_code
        == 200
    )
    loaded = load_config(cfg_path)
    assert loaded.mqtt.host == "h"
    assert loaded.http_output.url == "http://x"
    assert "cam_x" in [c.name for c in loaded.cameras]


def _app_with_store(tmp_path):
    cameras = make_cameras()
    store = SnapshotStore(str(tmp_path), max_per_camera=10)
    app = create_app(StubManager(cameras), cameras, snapshot_store=store)
    app.config["TESTING"] = True
    return app.test_client(), tmp_path


def test_snapshot_route_serves_file(tmp_path):
    client, tmp_path = _app_with_store(tmp_path)
    cam_dir = tmp_path / "cam_a"
    cam_dir.mkdir()
    (cam_dir / "entry_1700000000_1.jpg").write_bytes(b"\xff\xd8fake")
    res = client.get("/snapshots/cam_a/entry_1700000000_1.jpg")
    assert res.status_code == 200
    assert res.data == b"\xff\xd8fake"


def test_snapshot_route_blocks_dotdot(tmp_path):
    client, _ = _app_with_store(tmp_path)
    # A raw ../ traversal never reaches the handler (routing splits on "/"
    # -> 404, still never serves a file); the guard itself rejects "..".
    assert client.get("/snapshots/cam_a/../../etc/passwd").status_code in (403, 404)
    assert client.get("/snapshots/cam_a/..").status_code == 403


def test_snapshot_route_404_missing_file(tmp_path):
    client, _ = _app_with_store(tmp_path)
    assert client.get("/snapshots/cam_a/nope.jpg").status_code == 404


def test_snapshot_route_404_without_store():
    cameras = make_cameras()
    app = create_app(StubManager(cameras), cameras)
    app.config["TESTING"] = True
    assert app.test_client().get("/snapshots/cam_a/x.jpg").status_code == 404


def _app_with_clip_store(tmp_path):
    cameras = make_cameras()
    store = ClipStore(str(tmp_path), object(), max_per_camera=10)
    app = create_app(StubManager(cameras), cameras, clip_store=store)
    app.config["TESTING"] = True
    return app.test_client(), tmp_path


def test_clip_route_serves_file(tmp_path):
    client, tmp_path = _app_with_clip_store(tmp_path)
    cam_dir = tmp_path / "cam_a"
    cam_dir.mkdir()
    (cam_dir / "entry_1700000000_1.mp4").write_bytes(b"\x00\x00\x00\x18ftypmp42")
    res = client.get("/clips/cam_a/entry_1700000000_1.mp4")
    assert res.status_code == 200
    assert res.data.startswith(b"\x00\x00\x00\x18ftypmp42")


def test_clip_route_blocks_dotdot(tmp_path):
    client, _ = _app_with_clip_store(tmp_path)
    assert client.get("/clips/cam_a/../../etc/passwd").status_code in (403, 404)
    assert client.get("/clips/cam_a/..").status_code == 403


def test_clip_route_404_missing_file(tmp_path):
    client, _ = _app_with_clip_store(tmp_path)
    assert client.get("/clips/cam_a/nope.mp4").status_code == 404


def test_clip_route_404_without_store():
    cameras = make_cameras()
    app = create_app(StubManager(cameras), cameras)
    app.config["TESTING"] = True
    assert app.test_client().get("/clips/cam_a/x.mp4").status_code == 404


def test_camera_save_preserves_clips_section(tmp_path):
    from nvr.config import ClipConfig, load_config

    cfg_path = str(tmp_path / "config.yaml")
    cameras = make_cameras()
    clip_cfg = ClipConfig(base_dir="clips", max_per_camera=30, duration_sec=10.0)
    app = create_app(
        StubManager(cameras),
        cameras,
        config_path=cfg_path,
        clip_config=clip_cfg,
    )
    app.config["TESTING"] = True
    client = app.test_client()

    assert client.put("/api/cameras/cam_a/zones", json=[]).status_code == 200
    loaded = load_config(cfg_path)
    assert loaded.clips == clip_cfg


def test_camera_save_preserves_snapshots_section(tmp_path):
    from nvr.config import SnapshotConfig, load_config

    cfg_path = str(tmp_path / "config.yaml")
    cameras = make_cameras()
    snap_cfg = SnapshotConfig(base_dir="snaps", max_per_camera=50)
    app = create_app(
        StubManager(cameras),
        cameras,
        config_path=cfg_path,
        snapshot_config=snap_cfg,
    )
    app.config["TESTING"] = True
    client = app.test_client()

    assert client.put("/api/cameras/cam_a/zones", json=[]).status_code == 200
    loaded = load_config(cfg_path)
    assert loaded.snapshots == snap_cfg


def _app_with_events_stores(tmp_path):
    cameras = make_cameras()
    snap_store = SnapshotStore(str(tmp_path / "snaps"), max_per_camera=10)
    clip_store = ClipStore(str(tmp_path / "clips"), object(), max_per_camera=10)
    app = create_app(
        StubManager(cameras), cameras, snapshot_store=snap_store, clip_store=clip_store
    )
    app.config["TESTING"] = True
    return app.test_client(), tmp_path


def test_camera_events_pairs_snapshot_and_clip_by_stem(tmp_path):
    client, tmp_path = _app_with_events_stores(tmp_path)
    (tmp_path / "snaps" / "cam_a").mkdir(parents=True)
    (tmp_path / "clips" / "cam_a").mkdir(parents=True)
    (tmp_path / "snaps" / "cam_a" / "entry_path_1700000000_1.jpg").write_bytes(b"j")
    (tmp_path / "clips" / "cam_a" / "entry_path_1700000000_1.mp4").write_bytes(b"m")

    evs = client.get("/api/cameras/cam_a/events").get_json()
    assert len(evs) == 1
    ev = evs[0]
    assert ev["zone"] == "entry_path"  # underscore zone survives the split
    assert ev["timestamp"] == 1700000000
    assert ev["track_id"] == 1
    assert ev["snapshot"] == "/snapshots/cam_a/entry_path_1700000000_1.jpg"
    assert ev["clip"] == "/clips/cam_a/entry_path_1700000000_1.mp4"


def test_camera_events_snapshot_only_when_clip_not_yet_finalized(tmp_path):
    client, tmp_path = _app_with_events_stores(tmp_path)
    (tmp_path / "snaps" / "cam_a").mkdir(parents=True)
    (tmp_path / "snaps" / "cam_a" / "entry_1700000000_1.jpg").write_bytes(b"j")

    evs = client.get("/api/cameras/cam_a/events").get_json()
    assert len(evs) == 1
    assert evs[0]["snapshot"] == "/snapshots/cam_a/entry_1700000000_1.jpg"
    assert evs[0]["clip"] is None  # no broken link before the clip muxes


def test_camera_events_empty_dirs_returns_empty_list(tmp_path):
    client, _ = _app_with_events_stores(tmp_path)
    assert client.get("/api/cameras/cam_a/events").get_json() == []


def test_camera_events_unknown_camera_404(tmp_path):
    client, _ = _app_with_events_stores(tmp_path)
    assert client.get("/api/cameras/nope/events").status_code == 404


def test_camera_events_orders_newest_first(tmp_path):
    client, tmp_path = _app_with_events_stores(tmp_path)
    (tmp_path / "snaps" / "cam_a").mkdir(parents=True)
    for ts in (1700000100, 1700000000):
        (tmp_path / "snaps" / "cam_a" / f"entry_{ts}_1.jpg").write_bytes(b"j")

    evs = client.get("/api/cameras/cam_a/events").get_json()
    assert [e["timestamp"] for e in evs] == [1700000100, 1700000000]


def test_camera_events_ignores_stray_files(tmp_path):
    client, tmp_path = _app_with_events_stores(tmp_path)
    (tmp_path / "snaps" / "cam_a").mkdir(parents=True)
    (tmp_path / "snaps" / "cam_a" / "notes.txt").write_text("not an event")
    (tmp_path / "snaps" / "cam_a" / "entry_1700000000_1.jpg").write_bytes(b"j")

    evs = client.get("/api/cameras/cam_a/events").get_json()
    assert len(evs) == 1
    assert evs[0]["zone"] == "entry"


import base64

import yaml
from werkzeug.security import generate_password_hash


def _app_with_auth(tmp_path, username="admin", password="secret"):
    auth_file = tmp_path / "auth.yaml"
    auth_file.write_text(
        yaml.safe_dump({"username": username, "password_hash": generate_password_hash(password)}),
        encoding="utf-8",
    )
    cameras = make_cameras()
    app = create_app(StubManager(cameras), cameras, auth_path=str(auth_file))
    app.config["TESTING"] = True
    return app.test_client()


def _basic(username, password):
    return "Basic " + base64.b64encode(f"{username}:{password}".encode()).decode()


def test_auth_missing_credentials_401(tmp_path):
    client = _app_with_auth(tmp_path)
    assert client.get("/api/cameras").status_code == 401


def test_auth_wrong_password_401(tmp_path):
    client = _app_with_auth(tmp_path)
    res = client.get("/api/cameras", headers={"Authorization": _basic("admin", "wrong")})
    assert res.status_code == 401
    assert "Basic realm" in res.headers.get("WWW-Authenticate", "")


def test_auth_correct_credentials_200(tmp_path):
    client = _app_with_auth(tmp_path)
    res = client.get("/api/cameras", headers={"Authorization": _basic("admin", "secret")})
    assert res.status_code == 200


def test_auth_absent_file_means_disabled(tmp_path):
    cameras = make_cameras()
    app = create_app(StubManager(cameras), cameras, auth_path=str(tmp_path / "nope.yaml"))
    app.config["TESTING"] = True
    assert app.test_client().get("/api/cameras").status_code == 200

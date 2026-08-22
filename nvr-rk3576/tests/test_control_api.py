"""Tests for nvr.control.api — Flask test client against a stubbed manager,
no real subprocesses or network."""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from nvr.config import CameraConfig
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

    def stats(self):
        return dict(self.stats_data)


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
        assert "fps" in row and "url" in row


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

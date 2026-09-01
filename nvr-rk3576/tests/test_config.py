"""Tests for nvr.config."""

import sys
from pathlib import Path

import pytest
import yaml

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from nvr.config import (
    CameraConfig,
    ConfigError,
    NvrConfig,
    ZoneConfig,
    load_config,
    write_config,
)

VALID_YAML = """
cameras:
  - name: test_stream
    url: https://test-streams.mux.dev/x36xhzz/x36xhzz.m3u8
"""

MULTI_YAML = """
cameras:
  - name: cam_a
    url: rtsp://a
  - name: cam_b
    url: rtsp://b
"""

ZONE_YAML = """
cameras:
  - name: cam_a
    url: rtsp://a
    zones:
      - name: entry_path
        polygon: [[100, 400], [500, 400], [500, 720], [100, 720]]
        trigger_classes: [person]
        dwell_time_sec: 2.0
        cooldown_sec: 30
"""

MQTT_HTTP_YAML = """
mqtt:
  host: 192.168.1.10
  port: 1883
  topic_prefix: nvr
  username: bob
  password: secret
http_output:
  url: http://example.com/nvr
  timeout_sec: 5
cameras:
  - name: cam_a
    url: rtsp://a
"""

SNAPSHOT_YAML = """
snapshots:
  base_dir: /var/lib/nvr/snapshots
  max_per_camera: 50
cameras:
  - name: cam_a
    url: rtsp://a
"""

CLIPS_YAML = """
clips:
  base_dir: /var/lib/nvr/clips
  max_per_camera: 50
  duration_sec: 15
cameras:
  - name: cam_a
    url: rtsp://a
"""

MQTT_TLS_YAML = """
mqtt:
  host: 127.0.0.1
  port: 8883
  topic_prefix: nvr
  username: admin
  use_tls: true
  ca_cert: config/panel.crt
cameras:
  - name: cam_a
    url: rtsp://a
"""


def _write(tmp_path: Path, content: str) -> Path:
    path = tmp_path / "config.yaml"
    path.write_text(content, encoding="utf-8")
    return path


def test_valid_yaml_loads(tmp_path):
    cfg = load_config(str(_write(tmp_path, VALID_YAML)))
    assert cfg == NvrConfig(
        cameras=[CameraConfig(name="test_stream", url="https://test-streams.mux.dev/x36xhzz/x36xhzz.m3u8")]
    )


def test_multiple_cameras_load(tmp_path):
    cfg = load_config(str(_write(tmp_path, MULTI_YAML)))
    assert [c.name for c in cfg.cameras] == ["cam_a", "cam_b"]
    assert cfg.cameras[1].url == "rtsp://b"


def test_missing_cameras_key_raises(tmp_path):
    with pytest.raises(ConfigError):
        load_config(str(_write(tmp_path, "other: 1\n")))


def test_empty_cameras_raises(tmp_path):
    with pytest.raises(ConfigError):
        load_config(str(_write(tmp_path, "cameras: []\n")))


def test_camera_missing_url_raises(tmp_path):
    with pytest.raises(ConfigError):
        load_config(str(_write(tmp_path, "cameras:\n  - name: cam_a\n")))


def test_camera_missing_name_raises(tmp_path):
    with pytest.raises(ConfigError):
        load_config(str(_write(tmp_path, "cameras:\n  - url: rtsp://a\n")))


def test_missing_file_raises(tmp_path):
    with pytest.raises(ConfigError):
        load_config(str(tmp_path / "nope.yaml"))


def test_zones_parse(tmp_path):
    cfg = load_config(str(_write(tmp_path, ZONE_YAML)))
    assert len(cfg.cameras[0].zones) == 1
    z = cfg.cameras[0].zones[0]
    assert z.name == "entry_path"
    assert z.polygon == [(100.0, 400.0), (500.0, 400.0), (500.0, 720.0), (100.0, 720.0)]
    assert z.trigger_classes == ["person"]
    assert z.dwell_time_sec == 2.0
    assert z.cooldown_sec == 30.0


def test_camera_without_zones_still_loads(tmp_path):
    cfg = load_config(str(_write(tmp_path, VALID_YAML)))
    assert cfg.cameras[0].zones == []


def _zone_yaml(zone: dict) -> str:
    doc = {"cameras": [{"name": "cam_a", "url": "rtsp://a", "zones": [zone]}]}
    return yaml.safe_dump(doc)


@pytest.mark.parametrize(
    "zone, needle",
    [
        (
            {"name": "z", "polygon": [[0, 0], [1, 1]], "trigger_classes": ["person"],
             "dwell_time_sec": 2.0, "cooldown_sec": 0},
            "polygon must have >= 3 points",
        ),
        (
            {"name": "z", "polygon": [[0, 0], [1, 1], [2, 2]], "trigger_classes": ["persoon"],
             "dwell_time_sec": 2.0, "cooldown_sec": 0},
            "unknown class",
        ),
        (
            {"name": "z", "polygon": [[0, 0], [1, 1], [2, 2]], "trigger_classes": [],
             "dwell_time_sec": 2.0, "cooldown_sec": 0},
            "non-empty list",
        ),
        (
            {"name": "z", "polygon": [[0, 0], [1, 1], [2, 2]], "trigger_classes": ["person"],
             "dwell_time_sec": 0, "cooldown_sec": 0},
            "dwell_time_sec must be > 0",
        ),
        (
            {"name": "z", "polygon": [[0, 0], [1, 1], [2, 2]], "trigger_classes": ["person"],
             "dwell_time_sec": 2.0, "cooldown_sec": -1},
            "cooldown_sec must be >= 0",
        ),
    ],
)
def test_invalid_zone_raises(tmp_path, zone, needle):
    with pytest.raises(ConfigError) as ei:
        load_config(str(_write(tmp_path, _zone_yaml(zone))))
    assert "zone 'z'" in str(ei.value)
    assert needle in str(ei.value)


def test_write_config_round_trip(tmp_path):
    cfg = load_config(str(_write(tmp_path, ZONE_YAML)))
    out = tmp_path / "out.yaml"
    write_config(str(out), cfg.cameras)
    assert load_config(str(out)).cameras == cfg.cameras


def test_write_config_omits_empty_zones(tmp_path):
    cfg = load_config(str(_write(tmp_path, MULTI_YAML)))
    out = tmp_path / "out.yaml"
    write_config(str(out), cfg.cameras)
    # no camera entry carries a "zones" key (the generated header comment
    # mentions the word, so check the parsed structure, not raw text)
    parsed = yaml.safe_load(out.read_text())
    assert all("zones" not in c for c in parsed["cameras"])
    assert load_config(str(out)).cameras == cfg.cameras


def test_write_config_validates_before_replacing(tmp_path):
    cfg = load_config(str(_write(tmp_path, VALID_YAML)))
    out = tmp_path / "out.yaml"
    write_config(str(out), cfg.cameras)
    before = out.read_text()
    # A camera list that would fail load_config (unknown class) must not
    # clobber the existing valid file.
    bad = CameraConfig(
        name="cam_a",
        url="rtsp://a",
        zones=[
            ZoneConfig(
                name="z",
                polygon=[(0, 0), (1, 1), (2, 2)],
                trigger_classes=["persoon"],
                dwell_time_sec=2.0,
                cooldown_sec=0,
            )
        ],
    )
    with pytest.raises(ConfigError):
        write_config(str(out), [bad])
    assert out.read_text() == before


def test_mqtt_and_http_parse(tmp_path):
    cfg = load_config(str(_write(tmp_path, MQTT_HTTP_YAML)))
    assert cfg.mqtt is not None
    assert cfg.mqtt.host == "192.168.1.10"
    assert cfg.mqtt.port == 1883
    assert cfg.mqtt.topic_prefix == "nvr"
    assert cfg.mqtt.username == "bob"
    assert cfg.mqtt.password == "secret"
    assert cfg.http_output is not None
    assert cfg.http_output.url == "http://example.com/nvr"
    assert cfg.http_output.timeout_sec == 5.0


def test_mqtt_http_absent_by_default(tmp_path):
    cfg = load_config(str(_write(tmp_path, VALID_YAML)))
    assert cfg.mqtt is None
    assert cfg.http_output is None


def test_invalid_mqtt_raises(tmp_path):
    with pytest.raises(ConfigError) as ei:
        load_config(
            str(_write(tmp_path, "mqtt:\n  host: ''\ncameras:\n  - name: a\n    url: rtsp://a\n"))
        )
    assert "mqtt" in str(ei.value)


def test_write_config_round_trips_mqtt_http(tmp_path):
    cfg = load_config(str(_write(tmp_path, MQTT_HTTP_YAML)))
    out = tmp_path / "out.yaml"
    write_config(str(out), cfg.cameras, cfg.mqtt, cfg.http_output)
    loaded = load_config(str(out))
    assert loaded.cameras == cfg.cameras
    assert loaded.mqtt == cfg.mqtt
    assert loaded.http_output == cfg.http_output


def test_camera_publish_fields_parse(tmp_path):
    y = (
        "cameras:\n  - name: cam_a\n    url: rtsp://a\n"
        "    publish_zone_events: false\n    publish_detections: true\n"
        "    detection_publish_interval_sec: 2.5\n"
    )
    cfg = load_config(str(_write(tmp_path, y)))
    c = cfg.cameras[0]
    assert c.publish_zone_events is False
    assert c.publish_detections is True
    assert c.detection_publish_interval_sec == 2.5


def test_snapshots_parse(tmp_path):
    cfg = load_config(str(_write(tmp_path, SNAPSHOT_YAML)))
    assert cfg.snapshots is not None
    assert cfg.snapshots.base_dir == "/var/lib/nvr/snapshots"
    assert cfg.snapshots.max_per_camera == 50


def test_snapshots_absent_by_default(tmp_path):
    cfg = load_config(str(_write(tmp_path, VALID_YAML)))
    assert cfg.snapshots is None


def test_snapshots_defaults(tmp_path):
    y = "snapshots:\n  base_dir: snaps\ncameras:\n  - name: a\n    url: rtsp://a\n"
    cfg = load_config(str(_write(tmp_path, y)))
    assert cfg.snapshots.base_dir == "snaps"
    assert cfg.snapshots.max_per_camera == 200


def test_invalid_snapshots_raises(tmp_path):
    with pytest.raises(ConfigError) as ei:
        load_config(
            str(_write(
                tmp_path,
                "snapshots:\n  base_dir: ''\ncameras:\n  - name: a\n    url: rtsp://a\n",
            ))
        )
    assert "snapshots" in str(ei.value)


def test_write_config_round_trips_snapshots(tmp_path):
    cfg = load_config(str(_write(tmp_path, SNAPSHOT_YAML)))
    out = tmp_path / "out.yaml"
    write_config(str(out), cfg.cameras, cfg.mqtt, cfg.http_output, cfg.snapshots)
    loaded = load_config(str(out))
    assert loaded.snapshots == cfg.snapshots
    assert loaded.cameras == cfg.cameras


def test_clips_parse(tmp_path):
    cfg = load_config(str(_write(tmp_path, CLIPS_YAML)))
    assert cfg.clips is not None
    assert cfg.clips.base_dir == "/var/lib/nvr/clips"
    assert cfg.clips.max_per_camera == 50
    assert cfg.clips.duration_sec == 15.0


def test_clips_absent_by_default(tmp_path):
    cfg = load_config(str(_write(tmp_path, VALID_YAML)))
    assert cfg.clips is None


def test_clips_defaults(tmp_path):
    y = "clips:\n  base_dir: c\ncameras:\n  - name: a\n    url: rtsp://a\n"
    cfg = load_config(str(_write(tmp_path, y)))
    assert cfg.clips.base_dir == "c"
    assert cfg.clips.max_per_camera == 30
    assert cfg.clips.duration_sec == 10.0


def test_invalid_clips_raises(tmp_path):
    with pytest.raises(ConfigError) as ei:
        load_config(
            str(_write(
                tmp_path,
                "clips:\n  duration_sec: -1\ncameras:\n  - name: a\n    url: rtsp://a\n",
            ))
        )
    assert "clips" in str(ei.value)


def test_write_config_round_trips_clips(tmp_path):
    cfg = load_config(str(_write(tmp_path, CLIPS_YAML)))
    out = tmp_path / "out.yaml"
    write_config(str(out), cfg.cameras, cfg.mqtt, cfg.http_output, cfg.snapshots, cfg.clips)
    loaded = load_config(str(out))
    assert loaded.clips == cfg.clips
    assert loaded.cameras == cfg.cameras


def test_mqtt_tls_parse(tmp_path):
    cfg = load_config(str(_write(tmp_path, MQTT_TLS_YAML)))
    assert cfg.mqtt is not None
    assert cfg.mqtt.port == 8883
    assert cfg.mqtt.use_tls is True
    assert cfg.mqtt.ca_cert == "config/panel.crt"


def test_mqtt_tls_defaults_when_absent(tmp_path):
    cfg = load_config(str(_write(tmp_path, MQTT_HTTP_YAML)))
    assert cfg.mqtt.use_tls is False
    assert cfg.mqtt.ca_cert is None


def test_write_config_round_trips_mqtt_tls(tmp_path):
    cfg = load_config(str(_write(tmp_path, MQTT_TLS_YAML)))
    out = tmp_path / "out.yaml"
    write_config(str(out), cfg.cameras, cfg.mqtt, cfg.http_output, cfg.snapshots, cfg.clips)
    loaded = load_config(str(out))
    assert loaded.mqtt == cfg.mqtt
    assert loaded.mqtt.use_tls is True
    assert loaded.mqtt.ca_cert == "config/panel.crt"

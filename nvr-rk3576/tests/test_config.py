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

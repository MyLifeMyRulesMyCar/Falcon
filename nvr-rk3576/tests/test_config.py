"""Tests for nvr.config."""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from nvr.config import CameraConfig, ConfigError, NvrConfig, load_config

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

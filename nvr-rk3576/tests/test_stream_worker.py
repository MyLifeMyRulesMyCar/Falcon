"""Tests for nvr.ingest.stream_worker — pure-function only, no network, no subprocess."""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from nvr.ingest.stream_worker import (
    _build_ffmpeg_cmd,
    _parse_fraction,
    _restart_on_failure,
)


def test_build_ffmpeg_cmd_exact_argv():
    expected = [
        "ffmpeg",
        "-init_hw_device", "drm:/dev/dri/renderD128",
        "-hwaccel", "drm",
        "-i", "rtsp://x",
        "-f", "rawvideo",
        "-pix_fmt", "bgr24",
        "-vsync", "0",
        "pipe:1",
    ]
    assert _build_ffmpeg_cmd("rtsp://x", 640, 480) == expected


def test_build_ffmpeg_cmd_uses_given_dimensions_only_in_comment_free_exact_form():
    cmd = _build_ffmpeg_cmd("https://hls.example/playlist.m3u8", 1920, 1080)
    assert cmd[6] == "https://hls.example/playlist.m3u8"
    assert cmd == [
        "ffmpeg",
        "-init_hw_device", "drm:/dev/dri/renderD128",
        "-hwaccel", "drm",
        "-i", "https://hls.example/playlist.m3u8",
        "-f", "rawvideo", "-pix_fmt", "bgr24",
        "-vsync", "0", "pipe:1",
    ]


def test_restart_on_failure_backoff_sequence():
    attempts = list(range(7))
    expected = [1, 2, 4, 8, 16, 30, 30]
    assert [_restart_on_failure(a) for a in attempts] == expected


def test_restart_on_failure_caps_at_30():
    for attempt in (6, 7, 8, 20):
        assert _restart_on_failure(attempt) == 30


def test_parse_fraction():
    assert _parse_fraction("30/1") == 30.0
    assert _parse_fraction("30000/1001") == pytest.approx(30000 / 1001, abs=0.001)
    assert _parse_fraction("0/0") == 0.0
    assert _parse_fraction("garbage") == 0.0
    assert _parse_fraction("") == 0.0

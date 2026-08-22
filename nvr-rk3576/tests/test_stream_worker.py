"""Tests for nvr.ingest.stream_worker — pure-function only, no network, no subprocess."""

import multiprocessing
import subprocess
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import nvr.ingest.stream_worker as sw
from nvr.config import CameraConfig
from nvr.ingest.stream_worker import (
    StreamProbeError,
    StreamWorker,
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


class FakeTime:
    """Stand-in for the ``time`` module inside stream_worker only.

    Replacing ``sw.time`` (the module binding) keeps other threads' real
    ``time.sleep`` calls untouched — patching ``time.sleep`` globally would
    hijack the control-panel drain threads still alive in the pytest
    process and break their sleeping.
    """

    sleeps: list[float] = []

    @staticmethod
    def sleep(seconds: float) -> None:
        FakeTime.sleeps.append(seconds)

    @staticmethod
    def monotonic() -> float:
        return time.monotonic()


def test_probe_with_retry_survives_failures_and_counts_restarts(monkeypatch):
    counter = multiprocessing.Value("i", 0)
    worker = StreamWorker(
        CameraConfig(name="cam_a", url="rtsp://a"),
        multiprocessing.Queue(),
        restart_counter=counter,
    )
    calls = {"n": 0}
    FakeTime.sleeps.clear()

    def flaky_probe(url):
        calls["n"] += 1
        if calls["n"] < 3:
            raise StreamProbeError(url, "boom")
        return 1280, 720, 30.0

    monkeypatch.setattr(sw, "_probe_stream", flaky_probe)
    monkeypatch.setattr(sw, "time", FakeTime)

    fps = worker._probe_with_retry()

    assert fps == 30.0
    assert worker.width == 1280
    assert worker.height == 720
    assert counter.value == 2
    assert calls["n"] == 3
    assert FakeTime.sleeps == [1, 2]


def test_probe_with_retry_survives_timeout_and_sets_error(monkeypatch):
    worker = StreamWorker(
        CameraConfig(name="cam_a", url="rtsp://a"),
        multiprocessing.Queue(),
        last_error=multiprocessing.RawArray("c", 512),
    )
    recorded = []
    monkeypatch.setattr(worker, "_set_error", recorded.append)
    calls = {"n": 0}

    def slow_probe(url):
        calls["n"] += 1
        if calls["n"] == 1:
            raise subprocess.TimeoutExpired("ffprobe", 60)
        return 640, 360, 30.0

    monkeypatch.setattr(sw, "_probe_stream", slow_probe)
    monkeypatch.setattr(sw, "time", FakeTime)

    fps = worker._probe_with_retry()

    assert fps == 30.0
    assert calls["n"] == 2
    assert recorded[0].startswith("probe failed")
    assert recorded[1] == ""  # cleared once the probe succeeds

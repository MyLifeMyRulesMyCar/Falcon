"""Single-stream ingest worker: decode one network stream via hardware-accelerated ffmpeg."""

import json
import logging
import multiprocessing
import shutil
import subprocess
import threading
import time
from typing import Optional

import numpy as np

from nvr.config import CameraConfig

log = logging.getLogger(__name__)

_FRAME_RATE_FALLBACK = 0.0
_RESTART_CAP_SECONDS = 30


class StreamProbeError(Exception):
    """Raised when ffprobe cannot produce video stream metadata for a URL."""

    def __init__(self, url: str, stderr: str):
        super().__init__(f"cannot probe video stream at {url}: {stderr}")
        self.url = url
        self.stderr = stderr


def _parse_fraction(value: str) -> float:
    """Parse an ffprobe ``r_frame_rate`` value like ``"30/1"`` into a float."""
    if not isinstance(value, str) or "/" not in value:
        return _FRAME_RATE_FALLBACK
    num, _, den = value.partition("/")
    try:
        num_f, den_f = float(num), float(den)
    except ValueError:
        return _FRAME_RATE_FALLBACK
    if den_f <= 0.0:
        return _FRAME_RATE_FALLBACK
    return num_f / den_f


def _probe_stream(url: str) -> tuple[int, int, float]:
    """Return ``(width, height, fps)`` for the video stream at ``url``.

    Raises:
        StreamProbeError: if ffprobe fails or reports no video stream.
    """
    ffprobe = shutil.which("ffprobe")
    if ffprobe is None:
        raise StreamProbeError(url, "ffprobe not found on PATH")

    cmd = [
        ffprobe,
        "-v", "error",
        "-select_streams", "v:0",
        "-show_entries", "stream=width,height,r_frame_rate",
        "-of", "json",
        url,
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
    if proc.returncode != 0:
        raise StreamProbeError(url, proc.stderr.strip())

    try:
        data = json.loads(proc.stdout)
    except json.JSONDecodeError as exc:
        raise StreamProbeError(url, f"invalid ffprobe JSON output: {exc}") from exc

    streams = data.get("streams") or []
    if not streams:
        raise StreamProbeError(url, "no video stream found")

    stream = streams[0]
    width = stream.get("width")
    height = stream.get("height")
    if not width or not height:
        raise StreamProbeError(url, "video stream has no width/height")

    return int(width), int(height), _parse_fraction(stream.get("r_frame_rate", "0/1"))


_DRM_DEVICE = "/dev/dri/renderD128"


def _build_ffmpeg_cmd(url: str, width: int, height: int) -> list[str]:
    """Return the exact argv used to decode ``url`` to raw bgr24 frames."""
    return [
        "ffmpeg",
        "-init_hw_device", f"drm:{_DRM_DEVICE}",
        "-hwaccel", "drm",
        "-i", url,
        "-f", "rawvideo",
        "-pix_fmt", "bgr24",
        "-vsync", "0",
        "pipe:1",
    ]


def _queue_put_dropold(q: multiprocessing.Queue, item: object) -> None:
    """Push ``item`` to ``q`` non-blocking, dropping the oldest element if full."""
    while q.full():
        try:
            q.get_nowait()
        except (multiprocessing.queues.Empty, OSError):
            break
    try:
        q.put_nowait(item)
    except (multiprocessing.queues.Full, OSError):
        pass


def _restart_on_failure(attempt: int) -> int:
    """Backoff seconds before restart attempt ``attempt``: ``min(2**attempt, 30)``."""
    return min(2 ** attempt, _RESTART_CAP_SECONDS)


class StreamWorker(multiprocessing.Process):
    """Decode one camera stream and push raw BGR frames onto ``frame_queue``.

    On stream failure the ffmpeg subprocess is restarted with exponential
    backoff; the backoff resets once at least one full frame has been read
    after a restart.
    """

    def __init__(self, camera: CameraConfig, frame_queue: multiprocessing.Queue):
        super().__init__(name=f"stream-{camera.name}")
        self.camera = camera
        self.frame_queue = frame_queue
        self.width = 0
        self.height = 0

    def run(self) -> None:
        self.width, self.height, fps = _probe_stream(self.camera.url)
        log.info(
            "camera %s: probed %dx%d @ ~%.2ffps, starting hw decode",
            self.camera.name, self.width, self.height, fps,
        )

        attempt = 0
        while True:
            proc = self._spawn_decoder()
            if proc is None:
                sleep_s = _restart_on_failure(attempt)
                log.error(
                    "camera %s: spawn failed, retrying in %.0fs (attempt %d)",
                    self.camera.name, sleep_s, attempt,
                )
                time.sleep(sleep_s)
                attempt += 1
                continue

            frame_size = self.width * self.height * 3
            stderr_lines: list[str] = []
            drainer = threading.Thread(
                target=_drain_stderr, args=(proc, stderr_lines), daemon=True
            )
            drainer.start()

            frames_after_restart = 0
            try:
                while True:
                    raw = proc.stdout.read(frame_size)
                    if len(raw) < frame_size:
                        break
                    frame = np.frombuffer(raw, dtype=np.uint8).reshape(
                        (self.height, self.width, 3)
                    )
                    _queue_put_dropold(self.frame_queue, frame)
                    frames_after_restart += 1
            finally:
                proc.stdout.close()
                proc.stderr.close()
                _terminate(proc)

            if frames_after_restart > 0:
                attempt = 0
            sleep_s = _restart_on_failure(attempt)
            stderr_tail = " | ".join(stderr_lines[-3:])
            log.warning(
                "camera %s: stream ended after %d frames (attempt %d), restart in %.0fs %s",
                self.camera.name, frames_after_restart, attempt, sleep_s, stderr_tail,
            )
            time.sleep(sleep_s)
            attempt += 1

    def _spawn_decoder(self) -> Optional[subprocess.Popen]:
        cmd = _build_ffmpeg_cmd(self.camera.url, self.width, self.height)
        try:
            return subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        except OSError as exc:
            log.error("camera %s: cannot launch ffmpeg: %s", self.camera.name, exc)
            return None


def _drain_stderr(proc: subprocess.Popen, lines: list[str]) -> None:
    """Read ffmpeg stderr to a bounded line buffer so the pipe never fills."""
    for line in proc.stderr:
        decoded = line.decode(errors="replace").strip()
        lines.append(decoded)
        if len(lines) > 20:
            lines.pop(0)
    proc.stderr.close()


def _terminate(proc: subprocess.Popen) -> None:
    if proc.poll() is None:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=5)

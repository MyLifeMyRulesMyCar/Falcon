"""Post-roll event clips (v1.3): on a zone event, poll LatestFrameStore for
``duration_sec`` and mux the captured frames into an mp4 via ffmpeg.

Frame rate is whatever LatestFrameStore actually delivers (~6fps, its preview
cadence) — muxed at the *measured* rate, not a hardcoded one, so playback
speed is correct even if the actual capture cadence drifts under load. Frame
capture happens in the DetectionWorker process, which attaches to the shared
preview blocks as a second reader (the panel already does this) — no new IPC.

Encoder: ``h264_rkmpp`` (hardware, via the DRM/Mali driver) because this
board's ffmpeg build has no libx264 (see docs/ffmpeg_rebuild_step1.md); it is
already proven for mp4 output (testbed/sample.mp4 is encoded with it). The
encoder name is a parameter so tests can override it.
"""

import logging
import os
import subprocess
import time

import numpy as np

from nvr.ingest.frame_broadcast import LatestFrameStore
from nvr.output.rotation import rotate_by_count

log = logging.getLogger(__name__)

_DEFAULT_ENCODER = "h264_rkmpp"  # no libx264 on this board's ffmpeg
_ENCODE_TIMEOUT_S = 30


class _ActiveClip:
    def __init__(self, camera: str, zone: str, track_id: int, start_ts: float):
        self.camera = camera
        self.zone = zone
        self.track_id = track_id
        self.start_ts = start_ts
        self.frames: list[np.ndarray] = []
        self.last_gen = -1


class ClipStore:
    def __init__(
        self,
        base_dir: str,
        frame_store: LatestFrameStore,
        max_per_camera: int = 30,
        duration_sec: float = 10.0,
        encoder: str = _DEFAULT_ENCODER,
    ):
        self.base_dir = base_dir
        self.frame_store = frame_store
        self.max_per_camera = max_per_camera
        self.duration_sec = duration_sec
        self.encoder = encoder
        self._active: dict[str, _ActiveClip] = {}  # one in-flight clip per camera

    def start_clip(self, camera: str, zone: str, track_id: int, ts: float) -> None:
        """Begin (or extend) a post-roll capture for this camera.

        A second event on the same camera while one is already recording does
        NOT start a second clip — it's still evidence of the same visit, and
        two ffmpeg processes racing to write frames for one camera is a bug,
        not a feature. Just keep recording the current one.
        """
        if camera in self._active:
            return
        self._active[camera] = _ActiveClip(camera, zone, track_id, ts)

    def poll(self) -> None:
        """Call every feeder pass. Pulls any new frame per active camera,
        finalizes clips whose duration has elapsed. No-op when idle."""
        now = time.time()
        for camera, clip in list(self._active.items()):
            got = self.frame_store.read(camera)
            if got is not None:
                frame, gen = got
                if gen != clip.last_gen:
                    clip.last_gen = gen
                    clip.frames.append(frame)
            if now - clip.start_ts >= self.duration_sec:
                self._finalize(clip)
                del self._active[camera]

    def _finalize(self, clip: "_ActiveClip") -> None:
        if len(clip.frames) < 2:
            log.warning(
                "clip %s/%s: too few frames (%d), skipping",
                clip.camera, clip.zone, len(clip.frames),
            )
            return
        cam_dir = os.path.join(self.base_dir, clip.camera)
        os.makedirs(cam_dir, exist_ok=True)
        rel_path = f"{clip.camera}/{clip.zone}_{int(clip.start_ts)}_{clip.track_id}.mp4"
        out_path = os.path.join(self.base_dir, rel_path)
        h, w = clip.frames[0].shape[:2]
        # Measured rate, not assumed: actual capture cadence over the clip's
        # real elapsed wall time.
        elapsed = time.time() - clip.start_ts
        fps = max(len(clip.frames) / elapsed, 1.0) if elapsed > 0 else 6.0
        raw = b"".join(f.tobytes() for f in clip.frames)
        try:
            proc = subprocess.run(
                ["ffmpeg", "-y", "-f", "rawvideo", "-pix_fmt", "bgr24",
                 "-s", f"{w}x{h}", "-r", f"{fps:.2f}", "-i", "-",
                 "-vf", "format=yuv420p",
                 "-c:v", self.encoder, "-b:v", "800k", "-pix_fmt", "yuv420p",
                 out_path],
                input=raw, capture_output=True, timeout=_ENCODE_TIMEOUT_S,
            )
        except subprocess.TimeoutExpired:
            log.error("clip %s: ffmpeg mux timed out, aborting", rel_path)
            return
        if proc.returncode != 0:
            log.error(
                "ffmpeg mux failed for %s: %s",
                rel_path, proc.stderr.decode(errors="replace")[-500:],
            )
            return
        rotate_by_count(cam_dir, self.max_per_camera, "*.mp4")

"""Motion gate: decide whether a frame needs NPU inference.

Compares a grayscale downscale of the current frame against the previous
one; if the changed-pixel fraction exceeds a threshold, inference runs.
A periodic forced pass guarantees stationary objects still get detected.
"""

import numpy as np

_GRAYSCALE_WIDTH = 320


class MotionGate:
    """Per-camera frame gate; one instance per camera."""

    def __init__(self, motion_threshold_pct: float = 1.5, max_skip_frames: int = 90):
        self.motion_threshold = motion_threshold_pct / 100.0
        self.max_skip_frames = max_skip_frames
        self._previous: np.ndarray | None = None
        self._frames_since_last = 0

    def should_run_inference(self, frame: np.ndarray) -> bool:
        """True if the frame differs enough from the previous one, or if too
        many frames have passed since the last inference.

        Always updates internal state (previous frame, skip counter).
        """
        small = self._downscale(frame)
        if self._previous is None:
            self._previous = small
            self._frames_since_last = 0
            return True  # first frame always runs inference

        motion = False
        if self._previous.shape == small.shape:
            diff = np.abs(small.astype(np.int16) - self._previous.astype(np.int16))
            changed = float(np.count_nonzero(diff > 12)) / diff.size
            motion = changed >= self.motion_threshold
        self._previous = small

        self._frames_since_last += 1
        if motion or self._frames_since_last > self.max_skip_frames:
            self._frames_since_last = 0
            return True
        return False

    def _downscale(self, frame: np.ndarray) -> np.ndarray:
        h, w = frame.shape[:2]
        scale = _GRAYSCALE_WIDTH / float(w)
        new_h = max(1, int(round(h * scale)))
        gray = frame.mean(axis=2) if frame.ndim == 3 else frame
        # Simple area-ish downsample via slicing; cheap and deterministic.
        ys = (np.linspace(0, h - 1, new_h)).astype(int)
        xs = (np.linspace(0, w - 1, _GRAYSCALE_WIDTH)).astype(int)
        return gray[np.ix_(ys, xs)]

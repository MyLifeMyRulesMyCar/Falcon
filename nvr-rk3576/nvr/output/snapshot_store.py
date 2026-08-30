"""Max-count-capped on-disk store for event snapshots (v1.1).

Rotation is by file count per camera (oldest mtime removed past the cap), not
disk usage: zone events are already dwell/cooldown-rate-limited, so count is
a real bound. OpenCV is deliberately not used (see ROADMAP); annotated frames
arrive BGR and are flipped + saved via PIL.
"""

import glob
import os

import numpy as np
from PIL import Image

_JPEG_QUALITY = 85


class SnapshotStore:
    def __init__(self, base_dir: str, max_per_camera: int = 200):
        self.base_dir = base_dir
        self.max_per_camera = max_per_camera

    def save(
        self,
        camera: str,
        zone: str,
        track_id: int,
        annotated_frame: np.ndarray,
        ts: float,
    ) -> str:
        """Write one annotated frame and rotate this camera's directory.

        Returns the path relative to ``base_dir`` (``camera/zone_ts_track.jpg``).
        """
        cam_dir = os.path.join(self.base_dir, camera)
        os.makedirs(cam_dir, exist_ok=True)
        rel_path = f"{camera}/{zone}_{int(ts)}_{track_id}.jpg"
        # BGR (ingest convention) -> RGB for PIL save.
        img = Image.fromarray(np.ascontiguousarray(annotated_frame[:, :, ::-1]))
        img.save(os.path.join(self.base_dir, rel_path), quality=_JPEG_QUALITY)
        self._rotate(cam_dir)
        return rel_path

    def _rotate(self, cam_dir: str):
        """Remove the oldest files in ``cam_dir`` until at most
        ``max_per_camera`` remain."""
        files = sorted(glob.glob(os.path.join(cam_dir, "*.jpg")), key=os.path.getmtime)
        while len(files) > self.max_per_camera:
            os.remove(files.pop(0))

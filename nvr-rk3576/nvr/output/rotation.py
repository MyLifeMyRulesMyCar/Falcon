"""Max-count file rotation shared by the snapshot and clip stores (v1.3).

Same DRY rationale as ``annotate.py`` in v1.1: both stores cap a camera's
output directory at ``max_count`` files, oldest mtime first, so the rotation
logic lives in exactly one place instead of drifting apart.
"""

import glob
import os


def rotate_by_count(dir_path: str, max_count: int, pattern: str = "*") -> None:
    """Remove the oldest files (by mtime) in ``dir_path`` until at most
    ``max_count`` matching ``pattern`` remain."""
    files = sorted(glob.glob(os.path.join(dir_path, pattern)), key=os.path.getmtime)
    while len(files) > max_count:
        os.remove(files.pop(0))

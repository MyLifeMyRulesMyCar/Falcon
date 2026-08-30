"""Shared annotation renderer — the single source of truth for "what does
annotated look like" (v1.1).

Used by both processes: the preview path (PreviewEncoder / preview_encode,
downscaled) and event snapshots (DetectionWorker, full resolution). Pure
function with no process-specific state: copies the input frame, never
mutates the caller's. OpenCV is deliberately not used (see ROADMAP); drawing
is PIL/numpy.
"""

from typing import Optional

import numpy as np
from PIL import Image, ImageDraw

from nvr.config import ZoneConfig
from nvr.inference.detector import Detection

# PIL draws on RGB (the ingest/BGR convention is flipped before drawing), so
# colors are specified in RGB: cyan, red, green.
_ZONE_COLOR = (0, 255, 255)
_HIGHLIGHT_COLOR = (255, 0, 0)
_BOX_COLOR = (0, 255, 0)


def draw_annotations(
    frame: np.ndarray,
    detections: list[Detection],
    zones: list[ZoneConfig],
    highlight_zone: Optional[str] = None,
) -> np.ndarray:
    """Return ``frame`` with detection boxes and zone polygons drawn on a copy.

    ``frame`` is BGR (the ingest/detection convention) and the returned array
    is BGR as well. Zones render cyan, except the one named by
    ``highlight_zone`` which renders red (snapshots mark the zone that just
    fired). Detection boxes are green with a "class conf" label. Coordinates
    are in ``frame``'s own resolution.
    """
    out = frame.copy()
    if not detections and not zones:
        return out
    rgb = np.ascontiguousarray(out[:, :, ::-1])
    # PIL may copy on fromarray; always read back through np.asarray(img),
    # never the input array, so the drawn pixels are actually captured.
    img = Image.fromarray(rgb)
    draw = ImageDraw.Draw(img)
    for zone in zones:
        color = _HIGHLIGHT_COLOR if zone.name == highlight_zone else _ZONE_COLOR
        pts = [(float(x), float(y)) for x, y in zone.polygon]
        draw.polygon(pts, outline=color)
        draw.text(tuple(zone.polygon[0]), zone.name, fill=color)
    for d in detections:
        x1, y1, x2, y2 = map(int, d.bbox_xyxy)
        draw.rectangle([x1, y1, x2, y2], outline=_BOX_COLOR)
        draw.text(
            (x1, max(y1 - 5, 0)), f"{d.class_name} {d.confidence:.2f}", fill=_BOX_COLOR
        )
    return np.ascontiguousarray(np.asarray(img)[:, :, ::-1])

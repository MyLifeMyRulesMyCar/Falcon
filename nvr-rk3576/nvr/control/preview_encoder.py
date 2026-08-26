"""Dedicated preview encoder process (Phase 2 of the jitter fix).

All JPEG encoding for the browser previews happens in ONE process with its
own GIL, pinned to the A72 cluster, instead of inside the panel's Flask
threads. The Flask generators then become pure servlets of per-camera JPEG
slots — zero encode work in the panel, so the API and every stream stay
responsive regardless of how many previews are open.

The annotated slot is only encoded when the camera currently has
detections; otherwise the generator serves the raw slot, halving the
encode load for quiet cameras.
"""

import multiprocessing
import multiprocessing.shared_memory
import os
import struct
import time
from typing import Optional

import numpy as np
from PIL import Image, ImageDraw

from nvr.ingest.frame_broadcast import LatestFrameStore

_JPEG_QUALITY = 80
_MAX_JPEG = 128 * 1024  # 640-wide JPEGs are ~25-40KB; headroom for 960px
_HEADER = 4  # bytes: LE uint32 jpeg length
_A72_CORES = {4, 5, 6, 7}
_POLL_S = 0.02


def make_slots(camera_names: list[str]) -> dict:
    """Per-camera JPEG slots: {name: {"raw": (array, gen), "ann": (array, gen)}}.

    Slot layout: [4-byte LE length][jpeg bytes] in a RawArray of _MAX_JPEG.
    """
    slots = {}
    for name in camera_names:
        slots[name] = {
            kind: (
                multiprocessing.RawArray("c", _MAX_JPEG + _HEADER),
                multiprocessing.RawValue("L", 0),
            )
            for kind in ("raw", "ann")
        }
    return slots


def read_slot(slots: dict, name: str, kind: str) -> tuple[bytes, int]:
    """Return (jpeg bytes, slot generation) for a camera's slot."""
    arr, gen = slots[name][kind]
    g = gen.value
    try:
        n = struct.unpack("<I", bytes(arr[0:_HEADER]))[0]
    except Exception:
        return b"", g
    if n <= 0 or n > _MAX_JPEG:
        return b"", g
    return bytes(arr[_HEADER : _HEADER + n]), g


def _put_slot(slots: dict, name: str, kind: str, jpg: bytes) -> None:
    arr, gen = slots[name][kind]
    arr[0:_HEADER] = struct.pack("<I", len(jpg))
    arr[_HEADER : _HEADER + len(jpg)] = jpg
    gen.value += 1


def preview_encode(frame_bgr: np.ndarray, boxes=None, zones=None) -> bytes:
    """Stride-downscale to 320-wide with numpy, draw scaled boxes/zones, encode.

    The preview <img> renders at ~160px, so 320-wide is visually identical
    while costing ~4x less encode CPU — the difference between the encoder
    keeping up with the 8fps write cadence under full-system load and
    falling behind into jitter. No Pillow resize and no full-frame copies:
    a wide frame is downscaled by integer strides to a view (free), copied
    only at the small size, and the boxes (in native coords) are scaled by
    the same factor.

    ``zones`` is a list of ``(polygon, label)`` where polygon is a list of
    (x, y) in the camera's native decoded resolution (same space as boxes);
    each zone is outlined and labeled via ImageDraw.
    """
    factor = 1
    if frame_bgr.shape[1] > 320:
        factor = frame_bgr.shape[1] // 320
        small = frame_bgr[::factor, ::factor]
        if boxes:
            boxes = [(x1 / factor, y1 / factor, x2 / factor, y2 / factor)
                     for x1, y1, x2, y2 in boxes]
        if zones:
            zones = [
                ([(px / factor, py / factor) for px, py in poly], label)
                for poly, label in zones
            ]
    else:
        small = frame_bgr

    work = np.ascontiguousarray(small)  # the only copy; <=640 wide
    if boxes:
        for x1, y1, x2, y2 in boxes:
            x1, y1 = max(0, int(x1)), max(0, int(y1))
            x2 = min(work.shape[1], int(x2))
            y2 = min(work.shape[0], int(y2))
            if x2 <= x1 or y2 <= y1:
                continue
            # 2px green border strips (BGR); never fill interior.
            work[y1:y2, x1 : x1 + 2, :] = (0, 255, 0)
            work[y1:y2, x2 - 2 : x2, :] = (0, 255, 0)
            work[y1 : y1 + 2, x1:x2, :] = (0, 255, 0)
            work[y2 - 2 : y2, x1:x2, :] = (0, 255, 0)

    rgb = np.ascontiguousarray(work[:, :, ::-1])
    img = Image.fromarray(rgb)
    if zones:
        draw = ImageDraw.Draw(img)
        for poly, label in zones:
            draw.polygon([(px, py) for px, py in poly], outline="cyan")
            draw.text((poly[0][0], poly[0][1]), label, fill="cyan")
    buf = __import__("io").BytesIO()
    img.save(buf, format="JPEG", quality=_JPEG_QUALITY)
    return buf.getvalue()


class PreviewEncoder(multiprocessing.Process):
    """One process encoding all cameras' preview slots from the frame store."""

    def __init__(
        self,
        camera_names: list[str],
        frame_store: LatestFrameStore,
        slots: dict,
        detection_stats: dict,
        detection_flags: dict,
        a72_cores=None,
        zones=None,
    ):
        super().__init__(name="preview-encoder")
        self.camera_names = camera_names
        self.frame_store = frame_store
        self.slots = slots
        self.detection_stats = detection_stats
        self.detection_flags = detection_flags
        self.a72_cores = a72_cores if a72_cores is not None else _A72_CORES
        # Per-camera M4 zone configs: {name: [ZoneConfig, ...]}. Absent -> none.
        self.zones = zones if zones is not None else {}
        self._boxes_cache: dict[str, tuple] = {}

    def _boxes(self, name: str) -> Optional[list]:
        if not self.detection_flags.get(name, True):
            return None
        dets = self.detection_stats.get(name, {}).get("last_detections", [])
        if not dets:
            return None
        return [d.get("bbox", (0, 0, 0, 0)) for d in dets]

    def _boxes_cached(self, name: str) -> Optional[list]:
        """Manager-proxy reads are the hot-loop bottleneck (they serialize
        through the Manager server's unix socket); cache them."""
        now = time.monotonic()
        cached, ts = self._boxes_cache.get(name, (None, 0.0))
        if now - ts < 0.5:
            return cached
        boxes = self._boxes(name)
        self._boxes_cache[name] = (boxes, now)
        return boxes

    def run(self) -> None:
        try:
            os.sched_setaffinity(0, self.a72_cores)
        except Exception:
            pass
        last = {name: -1 for name in self.camera_names}
        last_boxes = {}
        while True:
            for name in self.camera_names:
                result = self.frame_store.read_view(name)
                if result is None:
                    continue
                view, gen = result
                if gen == last[name]:
                    continue
                last[name] = gen
                _put_slot(self.slots, name, "raw", preview_encode(view))
                boxes = self._boxes_cached(name)
                # Annotate when there are detections to draw, or when the
                # camera has zones (so the zone outline renders even before
                # anything is detected inside it).
                zone_list = self.zones.get(name, [])
                if boxes or zone_list:
                    key = repr((boxes, [(z.name, z.polygon) for z in zone_list]))
                    if last_boxes.get(name) != key:
                        last_boxes[name] = key
                        _put_slot(
                            self.slots,
                            name,
                            "ann",
                            preview_encode(
                                view,
                                boxes=boxes,
                                zones=[(z.polygon, z.name) for z in zone_list],
                            ),
                        )
            time.sleep(_POLL_S)

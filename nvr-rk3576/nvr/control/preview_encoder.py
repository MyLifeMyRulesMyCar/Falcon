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
from PIL import Image

from nvr.config import ZoneConfig
from nvr.ingest.frame_broadcast import LatestFrameStore
from nvr.inference.detector import Detection
from nvr.output.annotate import draw_annotations

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


def preview_encode(
    frame_bgr: np.ndarray,
    detections: Optional[list[Detection]] = None,
    zones: Optional[list[ZoneConfig]] = None,
    highlight_zone: Optional[str] = None,
) -> bytes:
    """Stride-downscale to 320-wide with numpy, draw, encode.

    The preview <img> renders at ~160px, so 320-wide is visually identical
    while costing ~4x less encode CPU — the difference between the encoder
    keeping up with the 8fps write cadence under full-system load and
    falling behind into jitter. No Pillow resize and no full-frame copies:
    a wide frame is downscaled by integer strides to a view (free), copied
    only at the small size, and the detections/zones (in native coords) are
    scaled by the same factor.

    Drawing is delegated to the shared
    :func:`nvr.output.annotate.draw_annotations` renderer so the preview and
    event snapshots share one implementation of "what does annotated look
    like". ``highlight_zone`` renders that zone red (previews never set it).
    """
    factor = 1
    if frame_bgr.shape[1] > 320:
        factor = frame_bgr.shape[1] // 320
        small = frame_bgr[::factor, ::factor]
        if detections:
            detections = [
                Detection(
                    d.class_name,
                    d.confidence,
                    tuple(v / factor for v in d.bbox_xyxy),
                )
                for d in detections
            ]
        if zones:
            zones = [
                ZoneConfig(
                    z.name,
                    [(px / factor, py / factor) for px, py in z.polygon],
                    z.trigger_classes,
                    z.dwell_time_sec,
                    z.cooldown_sec,
                )
                for z in zones
            ]
    else:
        small = frame_bgr

    if detections or zones:
        work = draw_annotations(small, detections or [], zones or [], highlight_zone)
    else:
        work = np.ascontiguousarray(small)  # the only copy; <=640 wide

    rgb = np.ascontiguousarray(work[:, :, ::-1])
    img = Image.fromarray(rgb)
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
        return [
            Detection(
                class_name=d.get("class_name", "?"),
                confidence=d.get("confidence", 0.0),
                bbox_xyxy=tuple(d.get("bbox", (0, 0, 0, 0))),
            )
            for d in dets
        ]

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
                dets = self._boxes_cached(name)
                # Annotate when there are detections to draw, or when the
                # camera has zones (so the zone outline renders even before
                # anything is detected inside it).
                zone_list = self.zones.get(name, [])
                if dets or zone_list:
                    key = repr((dets, [(z.name, z.polygon) for z in zone_list]))
                    if last_boxes.get(name) != key:
                        last_boxes[name] = key
                        _put_slot(
                            self.slots,
                            name,
                            "ann",
                            preview_encode(view, detections=dets, zones=zone_list),
                        )
            time.sleep(_POLL_S)

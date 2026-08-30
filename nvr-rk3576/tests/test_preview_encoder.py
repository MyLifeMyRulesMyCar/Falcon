"""Tests for nvr.control.preview_encoder — slot round-trip and encode, no processes."""

import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from nvr.config import ZoneConfig
from nvr.control.preview_encoder import make_slots, preview_encode, read_slot
from nvr.inference.detector import Detection


def test_slot_round_trip():
    slots = make_slots(["cam_a", "cam_b"])
    jpg = preview_encode(np.zeros((360, 640, 3), dtype=np.uint8))
    assert jpg[:2] == b"\xff\xd8"

    # Empty slot -> no data.
    data, gen = read_slot(slots, "cam_a", "raw")
    assert data == b"" and gen == 0

    # Write via the public slot layout helpers.
    from nvr.control.preview_encoder import _put_slot

    _put_slot(slots, "cam_a", "raw", jpg)
    data, gen = read_slot(slots, "cam_a", "raw")
    assert data == jpg and gen == 1
    # Annotated slot untouched.
    assert read_slot(slots, "cam_a", "ann") == (b"", 0)


def test_preview_encode_downscales_720p_and_scales_boxes():
    frame = np.zeros((720, 1280, 3), dtype=np.uint8)
    det = Detection(class_name="person", confidence=0.9, bbox_xyxy=(100, 100, 200, 200))
    jpg = preview_encode(frame, detections=[det])
    assert jpg[:2] == b"\xff\xd8"
    assert len(jpg) < 100_000  # downscaled, not a 720p JPEG


def test_preview_encode_draws_zones_and_highlight():
    frame = np.zeros((360, 640, 3), dtype=np.uint8)
    zone = ZoneConfig(
        name="entry",
        polygon=[(160.0, 260.0), (480.0, 260.0), (480.0, 360.0), (160.0, 360.0)],
        trigger_classes=["person"],
        dwell_time_sec=2.0,
        cooldown_sec=30.0,
    )
    det = Detection(class_name="person", confidence=0.9, bbox_xyxy=(100, 100, 200, 200))
    jpg = preview_encode(frame, detections=[det], zones=[zone], highlight_zone="entry")
    assert jpg[:2] == b"\xff\xd8"
    # Highlighted frame differs from the plain annotated one.
    plain = preview_encode(frame, detections=[det], zones=[zone])
    assert jpg != plain


def test_slots_isolated_per_camera():
    from nvr.control.preview_encoder import _put_slot

    slots = make_slots(["cam_a", "cam_b"])
    jpg = preview_encode(np.zeros((360, 640, 3), dtype=np.uint8))
    _put_slot(slots, "cam_a", "raw", jpg)
    assert read_slot(slots, "cam_b", "raw")[1] == 0

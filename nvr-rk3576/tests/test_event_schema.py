"""Tests for nvr.output.event_schema — payload builders, no network."""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from nvr.inference.detector import Detection
from nvr.output.event_schema import (
    build_detection_summary_payload,
    build_zone_event_payload,
)
from nvr.zones.zone_engine import ZoneEvent


def _zone_event():
    return ZoneEvent(
        camera="cam_a",
        zone="entry",
        track_id=3,
        class_name="person",
        dwell_time_sec=2.5,
        bbox_xyxy=(10.0, 20.0, 30.0, 40.0),
        timestamp=1700000000.0,
    )


def test_zone_event_payload_is_json_safe_and_shaped():
    payload = build_zone_event_payload("cam_a", _zone_event())
    assert json.dumps(payload)  # serializable
    assert payload["event_type"] == "zone_warning"
    assert payload["camera"] == "cam_a"
    assert payload["zone"] == "entry"
    assert payload["track_id"] == 3
    assert payload["class_name"] == "person"
    assert payload["dwell_time_sec"] == 2.5
    assert payload["bbox"] == [10.0, 20.0, 30.0, 40.0]
    assert "T" in payload["timestamp"] and payload["timestamp"].endswith("+00:00")


def test_zone_event_payload_omits_snapshot_path_when_absent():
    assert "snapshot_path" not in build_zone_event_payload("cam_a", _zone_event())


def test_zone_event_payload_includes_snapshot_path_when_given():
    payload = build_zone_event_payload(
        "cam_a", _zone_event(), snapshot_path="cam_a/entry_1700000000_3.jpg"
    )
    assert payload["snapshot_path"] == "cam_a/entry_1700000000_3.jpg"


def test_detection_summary_payload_is_json_safe_and_shaped():
    dets = [
        Detection(class_name="person", confidence=0.88, bbox_xyxy=(1.0, 2.0, 3.0, 4.0)),
        Detection(class_name="bird", confidence=0.64, bbox_xyxy=(5.0, 6.0, 7.0, 8.0)),
    ]
    payload = build_detection_summary_payload("cam_a", dets, 1700000000.0)
    assert json.dumps(payload)
    assert payload["event_type"] == "detection_summary"
    assert payload["camera"] == "cam_a"
    assert payload["detections"] == [
        {"class_name": "person", "confidence": 0.88, "bbox": [1.0, 2.0, 3.0, 4.0]},
        {"class_name": "bird", "confidence": 0.64, "bbox": [5.0, 6.0, 7.0, 8.0]},
    ]
    assert "T" in payload["timestamp"]


def test_event_types_distinct():
    assert build_zone_event_payload("c", _zone_event())["event_type"] == "zone_warning"
    assert build_detection_summary_payload("c", [], 0.0)["event_type"] == "detection_summary"

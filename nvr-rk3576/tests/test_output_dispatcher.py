"""Tests for nvr.output.dispatcher — payload fan-out with recorded fakes."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from nvr.inference.detector import Detection
from nvr.output.dispatcher import OutputDispatcher
from nvr.zones.zone_engine import ZoneEvent


class _RecorderMqtt:
    def __init__(self):
        self.items = []

    def publish(self, topic, payload):
        self.items.append((topic, payload))


class _RecorderHttp:
    def __init__(self):
        self.items = []

    def publish(self, payload):
        self.items.append(payload)


def _event():
    return ZoneEvent("cam_a", "entry", 3, "person", 2.5, (1, 2, 3, 4), 1700000000.0)


def test_zone_event_fans_out_identical_payload():
    m = _RecorderMqtt()
    h = _RecorderHttp()
    d = OutputDispatcher(m, h, "nvr")
    d.publish_zone_event("cam_a", _event())
    assert len(m.items) == 1 and len(h.items) == 1
    topic, payload = m.items[0]
    assert topic == "nvr/cam_a/zone_event"
    assert payload == h.items[0]  # identical payload on both transports
    assert payload["event_type"] == "zone_warning"


def test_zone_event_payload_carries_snapshot_path():
    m = _RecorderMqtt()
    h = _RecorderHttp()
    d = OutputDispatcher(m, h, "nvr")
    d.publish_zone_event("cam_a", _event(), snapshot_path="cam_a/entry_1700000000_3.jpg")
    _, mqtt_payload = m.items[0]
    assert mqtt_payload["snapshot_path"] == "cam_a/entry_1700000000_3.jpg"
    assert h.items[0]["snapshot_path"] == "cam_a/entry_1700000000_3.jpg"


def test_zone_event_payload_without_snapshot_path():
    m = _RecorderMqtt()
    d = OutputDispatcher(m, None, "nvr")
    d.publish_zone_event("cam_a", _event())
    assert "snapshot_path" not in m.items[0][1]


def test_detection_summary_fans_out():
    m = _RecorderMqtt()
    h = _RecorderHttp()
    d = OutputDispatcher(m, h, "nvr")
    dets = [Detection(class_name="person", confidence=0.88, bbox_xyxy=(1, 2, 3, 4))]
    d.publish_detection_summary("cam_a", dets, 1700000000.0)
    topic, payload = m.items[0]
    assert topic == "nvr/cam_a/detections"
    assert payload == h.items[0]
    assert payload["event_type"] == "detection_summary"


def test_none_transport_skipped():
    d = OutputDispatcher(None, None, "nvr")
    d.publish_zone_event("cam_a", _event())  # no transports, no error
    d.publish_detection_summary("cam_a", [], 0.0)

    d2 = OutputDispatcher(_RecorderMqtt(), None, "nvr")
    d2.publish_zone_event("cam_a", _event())
    assert len(d2.mqtt.items) == 1

    d3 = OutputDispatcher(None, _RecorderHttp(), "nvr")
    d3.publish_zone_event("cam_a", _event())
    assert len(d3.http.items) == 1

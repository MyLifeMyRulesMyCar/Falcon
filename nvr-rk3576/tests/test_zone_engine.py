"""Tests for nvr.zones.zone_engine — pure logic, explicit ``now``, no board."""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from nvr.config import ZoneConfig
from nvr.tracking.centroid_tracker import TrackedObject
from nvr.zones.zone_engine import ZoneEngine, ZoneEvent, event_to_dict


def _zone(name="z", cls=("person",), dwell=2.0, cooldown=30.0):
    return ZoneConfig(
        name=name,
        polygon=[(0, 0), (100, 0), (100, 100), (0, 100)],
        trigger_classes=list(cls),
        dwell_time_sec=dwell,
        cooldown_sec=cooldown,
    )


def _obj(tid=1, cls="person", bbox=(20, 20, 40, 40)):
    # bottom-center of (20,20,40,40) is (30, 40) — inside the 0..100 zone
    return TrackedObject(
        track_id=tid,
        class_name=cls,
        confidence=0.8,
        bbox_xyxy=bbox,
        first_seen_ts=0.0,
        last_seen_ts=0.0,
    )


def test_under_dwell_no_event():
    eng = ZoneEngine([_zone(dwell=2.0)])
    obj = _obj()
    assert eng.evaluate("cam", [obj], now=0.0) == []
    assert eng.evaluate("cam", [obj], now=1.0) == []
    assert eng.evaluate("cam", [obj], now=1.5) == []


def test_fires_once_per_intrusion():
    eng = ZoneEngine([_zone(dwell=2.0, cooldown=30.0)])
    obj = _obj()
    assert eng.evaluate("cam", [obj], now=0.0) == []
    evs = eng.evaluate("cam", [obj], now=2.5)
    assert len(evs) == 1
    assert evs[0].dwell_time_sec == pytest.approx(2.5)
    # still inside: the cooldown window suppresses per-tick re-fires
    assert eng.evaluate("cam", [obj], now=3.0) == []
    assert eng.evaluate("cam", [obj], now=4.0) == []


def test_leave_resets_dwell():
    eng = ZoneEngine([_zone(dwell=2.0, cooldown=30.0)])
    obj = _obj()
    eng.evaluate("cam", [obj], now=0.0)
    # leaves the zone (bottom-center outside) before the threshold
    outside = _obj(bbox=(200, 200, 220, 220))
    eng.evaluate("cam", [outside], now=1.0)
    # re-enters: the dwell clock restarts, so still under 2.0s at now=2.5
    assert eng.evaluate("cam", [obj], now=2.5) == []
    evs = eng.evaluate("cam", [obj], now=5.0)
    assert len(evs) == 1
    assert evs[0].dwell_time_sec == pytest.approx(2.5)


def test_cooldown_suppresses_then_refires():
    eng = ZoneEngine([_zone(dwell=2.0, cooldown=10.0)])
    obj = _obj()
    eng.evaluate("cam", [obj], now=0.0)
    first = eng.evaluate("cam", [obj], now=2.5)
    assert len(first) == 1
    # within the 10s cooldown -> suppressed (11.0 - 2.5 = 8.5 < 10)
    assert eng.evaluate("cam", [obj], now=5.0) == []
    assert eng.evaluate("cam", [obj], now=11.0) == []
    # cooldown elapsed -> fires again
    second = eng.evaluate("cam", [obj], now=13.0)
    assert len(second) == 1


def test_wrong_class_never_fires():
    eng = ZoneEngine([_zone(cls=("person",), dwell=1.0, cooldown=0.0)])
    obj = _obj(cls="bird", bbox=(20, 20, 40, 40))
    for n in range(5):
        assert eng.evaluate("cam", [obj], now=float(n)) == []


def test_event_to_dict_is_json_safe():
    ev = ZoneEvent("cam", "z", 3, "person", 2.5, (1.0, 2.0, 3.0, 4.0), 1000.5)
    assert event_to_dict(ev) == {
        "camera": "cam",
        "zone": "z",
        "track_id": 3,
        "class_name": "person",
        "dwell_time_sec": 2.5,
        "bbox_xyxy": [1.0, 2.0, 3.0, 4.0],
        "timestamp": 1000.5,
    }

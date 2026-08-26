"""Zone occupancy events driven by tracked objects (M4).

Zones are polygons in the camera's native decoded resolution (the space the
annotated preview renders in and the space detector.detect() maps detections
back to). An event fires once a tracked object of a trigger class has its
bottom-center inside the polygon for ``dwell_time_sec``, gated by
``cooldown_sec`` between consecutive fires for the same (track, zone).

Why dwell survives detection-cadence gaps for free: ``_entry_times`` is keyed
by track_id on wall-clock ``now``, not on call count. As long as
CentroidTracker keeps the track_id alive through a gap (max_missed_frames),
ZoneEngine never even sees the gap — the next evaluate() just computes
now - entered_at including it.
"""

import time
from dataclasses import dataclass

from nvr.config import ZoneConfig
from nvr.tracking.centroid_tracker import TrackedObject


@dataclass
class ZoneEvent:
    camera: str
    zone: str
    track_id: int
    class_name: str
    dwell_time_sec: float
    bbox_xyxy: tuple[float, float, float, float]
    timestamp: float


def event_to_dict(ev: ZoneEvent) -> dict:
    """JSON-safe dict for the Manager dict / panel API. Dataclass instances
    don't serialize cleanly across the process boundary, so detections and
    events are stored as plain dicts (same reason M2.1 did this)."""
    return {
        "camera": ev.camera,
        "zone": ev.zone,
        "track_id": ev.track_id,
        "class_name": ev.class_name,
        "dwell_time_sec": round(ev.dwell_time_sec, 2),
        "bbox_xyxy": [round(v, 1) for v in ev.bbox_xyxy],
        "timestamp": round(ev.timestamp, 3),
    }


class ZoneEngine:
    def __init__(self, zones: list[ZoneConfig]):
        self.zones = zones
        self._entry_times: dict[tuple[int, str], float] = {}
        self._last_fired: dict[tuple[int, str], float] = {}

    def evaluate(
        self, camera: str, tracked_objects: list[TrackedObject], now: float | None = None
    ) -> list[ZoneEvent]:
        now = time.time() if now is None else now
        events: list[ZoneEvent] = []
        for obj in tracked_objects:
            point = self._bottom_center(obj.bbox_xyxy)
            for zone in self.zones:
                key = (obj.track_id, zone.name)
                inside = (
                    obj.class_name in zone.trigger_classes
                    and self._point_in_polygon(point, zone.polygon)
                )
                if not inside:
                    self._entry_times.pop(key, None)  # left the zone (or wrong class)
                    continue
                entered_at = self._entry_times.setdefault(key, now)
                dwell = now - entered_at
                if dwell >= zone.dwell_time_sec:
                    # Cooldown gates fires *between* events for the same
                    # (track, zone), never the first one: a brand-new
                    # intrusion must fire at dwell time even when cooldown_sec
                    # is large (the config example uses 30s).
                    last_fired = self._last_fired.get(key)
                    if last_fired is None or now - last_fired >= zone.cooldown_sec:
                        events.append(
                            ZoneEvent(
                                camera,
                                zone.name,
                                obj.track_id,
                                obj.class_name,
                                dwell,
                                obj.bbox_xyxy,
                                now,
                            )
                        )
                        self._last_fired[key] = now
        return events

    @staticmethod
    def _bottom_center(bbox_xyxy):
        x1, y1, x2, y2 = bbox_xyxy
        return ((x1 + x2) / 2.0, y2)  # where the object touches the ground

    @staticmethod
    def _point_in_polygon(point, polygon) -> bool:
        """Ray casting: is ``point`` inside the closed ``polygon``?"""
        x, y = point
        inside = False
        n = len(polygon)
        for i in range(n):
            x1, y1 = polygon[i]
            x2, y2 = polygon[(i + 1) % n]
            if (y1 > y) != (y2 > y):
                x_intersect = x1 + (y - y1) * (x2 - x1) / (y2 - y1)
                if x < x_intersect:
                    inside = not inside
        return inside

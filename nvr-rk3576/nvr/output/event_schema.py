"""Single source of truth for the JSON payloads both output transports send.

Both publishers consume only what these two builders return, so "both
transports receive identical payloads" holds by construction rather than by
separate verification in each publisher.
"""

from datetime import datetime, timezone

from nvr.inference.detector import Detection
from nvr.zones.zone_engine import ZoneEvent


def _utc_iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat()


def build_zone_event_payload(camera: str, event: ZoneEvent) -> dict:
    return {
        "camera": camera,
        "event_type": "zone_warning",
        "zone": event.zone,
        "track_id": event.track_id,
        "class_name": event.class_name,
        "dwell_time_sec": round(event.dwell_time_sec, 2),
        "bbox": list(event.bbox_xyxy),
        "timestamp": _utc_iso(event.timestamp),
    }


def build_detection_summary_payload(
    camera: str, detections: list[Detection], ts: float
) -> dict:
    return {
        "camera": camera,
        "event_type": "detection_summary",
        "detections": [
            {
                "class_name": d.class_name,
                "confidence": round(d.confidence, 2),
                "bbox": list(d.bbox_xyxy),
            }
            for d in detections
        ],
        "timestamp": _utc_iso(ts),
    }

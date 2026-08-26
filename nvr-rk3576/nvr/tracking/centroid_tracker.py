"""Per-camera object tracking by centroid matching (M4).

Assigns stable track IDs to detections across motion-gated detection passes
using a nearest-centroid greedy match, and drops tracks after a configurable
number of missed detection passes.

Calibration note: ``max_missed_frames`` counts missed *detection passes*, not
missed ingest frames — detection only runs on motion-gated frames (measured
~10-17/s per camera on this board), so it must be calibrated against that
cadence, not the ingest fps. At ~12/s, ``max_missed_frames=15`` tolerates a
~1.2s gap before dropping a track (e.g. someone briefly turning side-on to
the camera). Do not copy a value tuned for 30fps video.
"""

import time
from dataclasses import dataclass

from nvr.inference.detector import Detection


@dataclass
class TrackedObject:
    track_id: int
    class_name: str
    confidence: float
    bbox_xyxy: tuple[float, float, float, float]
    first_seen_ts: float
    last_seen_ts: float


class CentroidTracker:
    def __init__(self, max_distance_px: float = 80, max_missed_frames: int = 15):
        self.max_distance_px = max_distance_px
        self.max_missed_frames = max_missed_frames
        self._next_id = 0
        self._tracked: dict[int, TrackedObject] = {}
        self._missed: dict[int, int] = {}

    @staticmethod
    def _center(bbox_xyxy):
        x1, y1, x2, y2 = bbox_xyxy
        return ((x1 + x2) / 2.0, (y1 + y2) / 2.0)

    def update(
        self, detections: list[Detection], now: float | None = None
    ) -> list[TrackedObject]:
        """Match detections to existing tracks and update state.

        For each tracked object, claim the closest unmatched detection within
        ``max_distance_px`` (bbox-center distance). Matched tracks update
        bbox/confidence/last_seen and reset their missed counter; unmatched
        tracks increment missed and are dropped when it exceeds
        ``max_missed_frames``; unmatched detections become new tracks.

        Returns the current tracked objects (matched + newly created), NOT
        objects dropped on this call.
        """
        now = time.time() if now is None else now
        matched: set[int] = set()
        result: list[TrackedObject] = []

        for tid, obj in list(self._tracked.items()):
            cx, cy = self._center(obj.bbox_xyxy)
            best_i, best_d2 = None, None
            for i, det in enumerate(detections):
                if i in matched:
                    continue
                dx, dy = self._center(det.bbox_xyxy)
                d2 = (dx - cx) ** 2 + (dy - cy) ** 2
                if d2 <= self.max_distance_px**2 and (best_d2 is None or d2 < best_d2):
                    best_d2, best_i = d2, i
            if best_i is None:
                self._missed[tid] = self._missed.get(tid, 0) + 1
                if self._missed[tid] > self.max_missed_frames:
                    del self._tracked[tid]
                    del self._missed[tid]
                continue
            matched.add(best_i)
            det = detections[best_i]
            prev = self._tracked[tid]
            self._tracked[tid] = TrackedObject(
                track_id=tid,
                class_name=det.class_name,
                confidence=det.confidence,
                bbox_xyxy=det.bbox_xyxy,
                first_seen_ts=prev.first_seen_ts,
                last_seen_ts=now,
            )
            self._missed[tid] = 0
            result.append(self._tracked[tid])

        for i, det in enumerate(detections):
            if i in matched:
                continue
            tid = self._next_id
            self._next_id += 1
            obj = TrackedObject(
                track_id=tid,
                class_name=det.class_name,
                confidence=det.confidence,
                bbox_xyxy=det.bbox_xyxy,
                first_seen_ts=now,
                last_seen_ts=now,
            )
            self._tracked[tid] = obj
            self._missed[tid] = 0
            result.append(obj)

        return result

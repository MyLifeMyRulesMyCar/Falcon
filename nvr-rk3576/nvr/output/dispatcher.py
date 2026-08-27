"""Fans zone events / detection summaries out to the configured transports.

Each payload is built once here and handed to both publishers, so the two
transports never diverge.
"""

from typing import Optional

from nvr.inference.detector import Detection
from nvr.output.event_schema import (
    build_detection_summary_payload,
    build_zone_event_payload,
)
from nvr.output.http_publisher import HttpPublisher
from nvr.output.mqtt_publisher import MqttPublisher
from nvr.zones.zone_engine import ZoneEvent


class OutputDispatcher:
    def __init__(
        self,
        mqtt: Optional[MqttPublisher],
        http: Optional[HttpPublisher],
        topic_prefix: str,
    ):
        self.mqtt = mqtt
        self.http = http
        self.topic_prefix = topic_prefix

    def publish_zone_event(self, camera: str, event: ZoneEvent) -> None:
        payload = build_zone_event_payload(camera, event)
        if self.mqtt:
            self.mqtt.publish(f"{self.topic_prefix}/{camera}/zone_event", payload)
        if self.http:
            self.http.publish(payload)

    def publish_detection_summary(
        self, camera: str, detections: list[Detection], ts: float
    ) -> None:
        payload = build_detection_summary_payload(camera, detections, ts)
        if self.mqtt:
            self.mqtt.publish(f"{self.topic_prefix}/{camera}/detections", payload)
        if self.http:
            self.http.publish(payload)

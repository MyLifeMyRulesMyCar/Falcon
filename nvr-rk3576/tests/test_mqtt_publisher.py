"""MqttPublisher: publish() must return near-instantly even with a slow/blocking
underlying client — the real acceptance criterion, tested without a broker."""

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from nvr.config import MqttConfig
from nvr.output.mqtt_publisher import MqttPublisher


class _SlowClient:
    """publish() blocks 5s per call; the drain thread can only consume slowly."""

    def __init__(self):
        self.sent = []

    def publish(self, topic, payload, qos=0):
        time.sleep(5)
        self.sent.append((topic, payload))

    def is_connected(self):
        return True

    def loop_stop(self):
        pass

    def disconnect(self):
        pass


def test_publish_returns_quickly_with_slow_client():
    pub = MqttPublisher(MqttConfig(host="localhost", port=1883), client=_SlowClient())
    pub.set_enabled(True)
    t0 = time.monotonic()
    for i in range(10):
        pub.publish("nvr/cam/zone_event", {"i": i})
    assert time.monotonic() - t0 < 0.5  # publish() is a queue put, not network
    pub.set_enabled(False)


def test_publish_when_disabled_never_enqueues():
    pub = MqttPublisher(
        MqttConfig(host="localhost", port=1883, enabled=False), client=_SlowClient()
    )
    pub.publish("t", {})
    assert pub._queue.empty()
    pub.set_enabled(True)


def test_full_queue_never_blocks_publish():
    pub = MqttPublisher(MqttConfig(host="localhost", port=1883), client=_SlowClient())
    pub.set_enabled(True)
    # Fill the bounded queue; the slow client keeps the drain thread busy.
    for i in range(210):
        pub.publish("t", {"i": i})
    t0 = time.monotonic()
    pub.publish("t", {"i": "last"})
    assert time.monotonic() - t0 < 0.2  # drop-oldest path, never blocks/raises
    assert pub._queue.qsize() <= 200
    pub.set_enabled(False)

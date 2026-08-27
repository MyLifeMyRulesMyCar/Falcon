"""HttpPublisher: publish() must return near-instantly even with a slow/blocking
session — the real acceptance criterion, tested without real HTTP."""

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from nvr.config import HttpOutputConfig
from nvr.output.http_publisher import HttpPublisher


class _SlowSession:
    def post(self, url, json=None, timeout=None):
        time.sleep(5)


def test_publish_returns_quickly_with_slow_session():
    pub = HttpPublisher(
        HttpOutputConfig(url="http://localhost:9/x"), session=_SlowSession()
    )
    pub.set_enabled(True)
    t0 = time.monotonic()
    for i in range(210):
        pub.publish({"i": i})
    assert time.monotonic() - t0 < 0.5
    assert pub._queue.qsize() <= 200
    pub.set_enabled(False)


def test_publish_when_disabled_never_enqueues():
    pub = HttpPublisher(
        HttpOutputConfig(url="http://localhost:9/x", enabled=False), session=_SlowSession()
    )
    pub.publish({"i": 1})
    assert pub._queue.empty()
    pub.set_enabled(True)

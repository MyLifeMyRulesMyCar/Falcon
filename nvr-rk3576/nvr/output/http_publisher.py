"""HTTP output publisher (M5). Same non-blocking shape as MqttPublisher: a
bounded multiprocessing.Queue drained by a daemon thread, so a dead endpoint
never blocks the detection worker. A failed POST is logged and dropped (no
retry storm).
"""

import json
import logging
import multiprocessing
import queue
import threading

import requests

from nvr.config import HttpOutputConfig

log = logging.getLogger(__name__)

_QUEUE_MAXSIZE = 200


class HttpPublisher:
    def __init__(self, config: HttpOutputConfig, session=None):
        self._enabled = multiprocessing.Value("i", 1 if config.enabled else 0)
        self._url = config.url
        self._timeout = config.timeout_sec
        self._session = session if session is not None else requests.Session()
        self._queue: multiprocessing.Queue = multiprocessing.Queue(maxsize=_QUEUE_MAXSIZE)
        self._lock = threading.Lock()
        threading.Thread(target=self._drain_loop, daemon=True).start()

    @property
    def enabled(self) -> bool:
        return bool(self._enabled.value)

    def set_enabled(self, enabled: bool) -> None:
        self._enabled.value = int(bool(enabled))

    def publish(self, payload: dict) -> None:
        if not self.enabled:
            return
        with self._lock:
            try:
                self._queue.put_nowait(payload)
            except (queue.Full, multiprocessing.queues.Full):
                try:
                    self._queue.get_nowait()  # drop-oldest
                except (queue.Empty, multiprocessing.queues.Empty):
                    pass
                try:
                    self._queue.put_nowait(payload)
                except (queue.Full, multiprocessing.queues.Full):
                    pass  # still full — drop the new item, never block

    def _drain_loop(self) -> None:
        while True:
            try:
                payload = self._queue.get()
            except (EOFError, OSError):
                break  # queue owner exited (process teardown); nothing to send
            if not self.enabled:
                continue
            try:
                self._session.post(self._url, json=payload, timeout=self._timeout)
            except requests.RequestException as exc:
                log.warning("HTTP publish failed: %s", exc)

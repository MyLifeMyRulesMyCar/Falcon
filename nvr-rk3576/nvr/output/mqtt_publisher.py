"""MQTT publisher (M5). Non-blocking: a bounded queue + drop-oldest + a daemon
drain thread — the same pattern the camera frame queues have used since M0 —
so a down broker can never block the detection worker's NPU-adjacent threads.

The queue is a ``multiprocessing.Queue``: the DetectionWorker is a forked
process that calls ``publish()``, while the drain thread lives in the panel.
A plain ``queue.Queue``'s lock/Condition does not survive fork (the child
would push into a private deque the panel never reads) — which is exactly why
M0's frame queues use multiprocessing.Queue.

paho-mqtt v2 (verified 2.1.0 in this venv): ``mqtt.Client`` requires an
explicit ``CallbackAPIVersion`` argument. The client's background loop
(``loop_start``) owns reconnection, so the broker restarting mid-run is
handled without any Falcon process restart.
"""

import json
import logging
import multiprocessing
import queue
import threading

import paho.mqtt.client as mqtt

from nvr.config import MqttConfig

log = logging.getLogger(__name__)

_QUEUE_MAXSIZE = 200


class MqttPublisher:
    def __init__(self, config: MqttConfig, client=None):
        # Fork-shared: the DetectionWorker is forked and calls publish() while
        # the panel toggles via set_enabled(). A plain bool would snapshot at
        # fork and the live toggle would silently not reach the worker.
        self._enabled = multiprocessing.Value("i", 1 if config.enabled else 0)
        self._topic_prefix = config.topic_prefix
        self._queue: multiprocessing.Queue = multiprocessing.Queue(maxsize=_QUEUE_MAXSIZE)
        self._client = client if client is not None else self._build_client(config)
        self._lock = threading.Lock()
        threading.Thread(target=self._drain_loop, daemon=True).start()

    @property
    def enabled(self) -> bool:
        return bool(self._enabled.value)

    def set_enabled(self, enabled: bool) -> None:
        self._enabled.value = int(bool(enabled))

    def connected(self) -> bool:
        with self._lock:
            try:
                return bool(self._client.is_connected())
            except Exception:
                return False

    @staticmethod
    def _build_client(config: MqttConfig) -> mqtt.Client:
        client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2)
        if config.username is not None:
            client.username_pw_set(config.username, config.password)
        client.reconnect_delay_set(min_delay=1, max_delay=30)
        client.connect(config.host, config.port)
        client.loop_start()
        return client

    def reconfigure(self, config: MqttConfig) -> None:
        """Swap in a new broker config (host/port/credentials/topic prefix)."""
        with self._lock:
            old = self._client
            self._client = self._build_client(config)
            self._topic_prefix = config.topic_prefix
        try:
            old.loop_stop()
            old.disconnect()
        except Exception:
            pass

    def publish(self, topic: str, payload: dict) -> None:
        if not self.enabled:
            return
        # Both DetectionWorker core threads can call this on the same shared
        # queue object; serialize the drop-oldest + put (the multiprocessing
        # queue handles cross-process concurrency itself).
        with self._lock:
            try:
                self._queue.put_nowait((topic, payload))
            except (queue.Full, multiprocessing.queues.Full):
                try:
                    self._queue.get_nowait()  # drop-oldest
                except (queue.Empty, multiprocessing.queues.Empty):
                    pass
                try:
                    self._queue.put_nowait((topic, payload))
                except (queue.Full, multiprocessing.queues.Full):
                    pass  # still full — drop the new item, never block

    def _drain_loop(self) -> None:
        while True:
            try:
                topic, payload = self._queue.get()
            except (EOFError, OSError):
                break  # queue owner exited (process teardown); nothing to send
            if not self.enabled:
                continue
            with self._lock:
                client = self._client
            try:
                client.publish(topic, json.dumps(payload), qos=1)
            except Exception as exc:
                log.warning("MQTT publish failed: %s", exc)

"""MQTT connections: liveness check, SUBACK/PUBACK-confirmed subscribe and publish."""
from __future__ import annotations

import threading
import uuid
from typing import Callable
from urllib.parse import urlparse

import certifi
import paho.mqtt.client as mqtt

TLS_SCHEMES = {"mqtts", "ssl", "tls"}
PLAIN_SCHEMES = {"mqtt", "tcp"}

# (topic, payload bytes, retain flag)
MessageHandler = Callable[[str, bytes, bool], None]


class BrokerError(Exception):
    """A broker could not be reached, refused us, or did not acknowledge in time."""


def parse_url(url: str) -> tuple[str, int, bool]:
    u = urlparse(url)
    if u.scheme not in TLS_SCHEMES | PLAIN_SCHEMES or not u.hostname:
        raise BrokerError(f"unsupported broker URL {url!r} (expected mqtts://host:port)")
    tls = u.scheme in TLS_SCHEMES
    return u.hostname, u.port or (8883 if tls else 1883), tls


class BrokerClient:
    """One MQTT v5 connection with blocking, acknowledged operations.

    Callbacks run on paho's network thread; `on_message` (if given) is called
    there for every message received and must not block.
    """

    def __init__(self, name: str, url: str, username: str, password: str,
                 client_id_prefix: str, on_message: MessageHandler | None = None):
        self.name = name
        self.url = url
        self.host, self.port, tls = parse_url(url)
        self._connected = threading.Event()
        self._connack = None
        self._lock = threading.Lock()
        self._pending_sub: dict[int, tuple[threading.Event, list]] = {}
        self._pending_pub: dict[int, tuple[threading.Event, list]] = {}
        self._handler = on_message

        self._client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2,
                                   client_id=f"{client_id_prefix}-{name}-{uuid.uuid4().hex[:8]}",
                                   protocol=mqtt.MQTTv5)
        self._client.username_pw_set(username, password)
        if tls:
            self._client.tls_set(ca_certs=certifi.where())
        self._client.on_connect = self._on_connect
        self._client.on_disconnect = self._on_disconnect
        self._client.on_subscribe = self._on_subscribe
        self._client.on_publish = self._on_publish
        self._client.on_message = self._on_message

    # --- paho callbacks (network thread) ---
    def _on_connect(self, client, userdata, flags, reason_code, properties):
        self._connack = reason_code
        self._connected.set()

    def _on_disconnect(self, client, userdata, flags, reason_code, properties):
        self._connected.clear()

    def _on_subscribe(self, client, userdata, mid, reason_codes, properties):
        self._resolve(self._pending_sub, mid, list(reason_codes))

    def _on_publish(self, client, userdata, mid, reason_code, properties):
        self._resolve(self._pending_pub, mid, [reason_code])

    def _on_message(self, client, userdata, msg):
        if self._handler:
            self._handler(msg.topic, msg.payload, bool(msg.retain))

    def _resolve(self, pending, mid, codes):
        with self._lock:
            entry = pending.get(mid)
            if entry is None:  # ack arrived before we registered it
                pending[mid] = (threading.Event(), codes)
                pending[mid][0].set()
                return
        entry[1].extend(codes)
        entry[0].set()

    def _await(self, pending, mid, timeout: float, what: str) -> list:
        with self._lock:
            entry = pending.setdefault(mid, (threading.Event(), []))
        if not entry[0].wait(timeout):
            raise BrokerError(f"[{self.name}] no {what} within {timeout:g}s")
        with self._lock:
            pending.pop(mid, None)
        return entry[1]

    # --- operations ---
    def connect(self, timeout: float) -> None:
        """Connect and wait for a successful CONNACK; raises BrokerError otherwise."""
        try:
            self._client.connect(self.host, self.port, keepalive=60)
        except Exception as exc:  # DNS, TCP or TLS failure
            raise BrokerError(f"[{self.name}] cannot connect to {self.host}:{self.port}: {exc}") from None
        self._client.loop_start()
        if not self._connected.wait(timeout):
            self.close()
            raise BrokerError(f"[{self.name}] no CONNACK from {self.host}:{self.port} within {timeout:g}s")
        if self._connack.is_failure:
            self.close()
            raise BrokerError(f"[{self.name}] connection refused by {self.host}: {self._connack}")

    def subscribe(self, topics: list[str], timeout: float, qos: int = 1) -> None:
        result, mid = self._client.subscribe([(t, qos) for t in topics])
        if result != mqtt.MQTT_ERR_SUCCESS:
            raise BrokerError(f"[{self.name}] subscribe failed: {mqtt.error_string(result)}")
        codes = self._await(self._pending_sub, mid, timeout, "SUBACK")
        refused = [f"{t}: {c}" for t, c in zip(topics, codes) if c.is_failure]
        if refused:
            raise BrokerError(f"[{self.name}] subscription refused: {'; '.join(refused)}")

    def publish(self, topic: str, payload: bytes, timeout: float, qos: int = 1) -> None:
        """Publish (not retained) and wait for PUBACK; raises if refused (e.g. not authorised)."""
        info = self._client.publish(topic, payload, qos=qos, retain=False)
        if info.rc != mqtt.MQTT_ERR_SUCCESS:
            raise BrokerError(f"[{self.name}] publish failed: {mqtt.error_string(info.rc)}")
        if qos == 0:
            return
        (code,) = self._await(self._pending_pub, info.mid, timeout, "PUBACK") or [None]
        if code is not None and getattr(code, "is_failure", False):
            raise BrokerError(f"[{self.name}] publish to {topic} refused: {code}")

    def close(self) -> None:
        try:
            self._client.disconnect()
        finally:
            self._client.loop_stop()

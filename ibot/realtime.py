"""Realtime messages — the push channel.

This is the piece that makes the bot feel like the WhatsApp bot: Instagram
pushes new messages over a persistent WebSocket (MQTToT, the protocol the
phone app uses), so the bot hears about a message the moment it arrives
instead of waiting for the next poll.

The push payload is complete. A new message arrives with its thread, its
sender, its text and its timestamp, so the bot can answer **without reading
the inbox at all** — no polling delay, no ten-second inbox request.

Three things make this reliable in the wild:

* **Subscribe properly.** The socket connects, but nothing arrives until the
  bot subscribes to the message stream with the sequence id the inbox hands
  out. Skip that and the connection is silent forever — which is exactly
  what happens if you only call ``connect()``.
* **Read the socket.** The events are dispatched while reading. A client that
  connects and then never reads receives nothing.
* **Reconnect.** Instagram closes idle push connections. The bridge
  reconnects with a back-off and re-subscribes, forever.

If the push channel cannot be established the bot falls back to polling. It
is an accelerator, never a dependency.
"""

from __future__ import annotations

import json
import socket
import threading
import time
from dataclasses import dataclass
from typing import Any, Callable

from . import log

log = log.get(__name__)


@dataclass
class RealtimeMessage:
    """A message that arrived over the push channel."""

    thread_id: str
    message_id: str
    user_id: str
    text: str = ""
    timestamp: float = 0.0
    item_type: str = "text"
    is_self: bool = False

    @property
    def is_command(self) -> bool:
        return self.text.strip().startswith("/")


def _dig(payload: Any, *keys, default=None):
    """Read a nested value without raising."""
    node = payload
    for key in keys:
        if isinstance(node, dict) and key in node:
            node = node[key]
        else:
            return default
    return node


class RealtimeBridge:
    """Keeps a push connection open and hands messages to a callback."""

    def __init__(
        self,
        client,
        cfg,
        on_message: Callable[[RealtimeMessage], None] | None = None,
        on_status: Callable[[dict], None] | None = None,
    ):
        self.client = client
        self.cfg = cfg
        self.on_message = on_message
        self.on_status = on_status

        self._rt = None
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._lock = threading.Lock()

        self.connected = False
        self.connect_attempts = 0
        self.messages_received = 0
        self.last_event_at = 0.0
        self.last_error = ""
        self.subscribed_at = 0.0
        self.seq_id = 0
        self.pings = 0

    # ------------------------------------------------------------ lifecycle
    def start(self) -> bool:
        """Open the connection in the background. Returns straight away."""
        if self._thread is not None:
            return self.connected
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="realtime", daemon=True)
        self._thread.start()
        return True

    def stop(self) -> None:
        self._stop.set()
        rt = self._rt
        if rt is not None:
            try:
                rt.disconnect()
            except Exception:  # noqa: BLE001
                pass
        if self._thread is not None:
            self._thread.join(timeout=5)
            self._thread = None
        self.connected = False

    def _run(self) -> None:
        """Connect, subscribe, read — and do it again whenever it drops."""
        backoff = 5.0
        while not self._stop.is_set():
            try:
                self._connect_once()
                backoff = 5.0
                self._pump()
            except Exception as exc:  # noqa: BLE001
                self.last_error = f"{type(exc).__name__}: {exc}"
                log.warning("the push connection dropped: %s", exc)
            finally:
                self.connected = False
                self._rt = None
            if self._stop.is_set():
                break
            self.connect_attempts += 1
            wait = min(120.0, backoff)
            log.info("reconnecting the push channel in %.0f seconds", wait)
            if self._stop.wait(wait):
                break
            backoff = min(120.0, backoff * 1.8)

    def _connect_once(self) -> None:
        """Open the socket and subscribe to the message stream."""
        from instagrapi.realtime.client import RealtimeClient

        rt = RealtimeClient(self.client)
        rt.on("message", self._on_message_event)
        rt.connect()
        # This is the part that is easy to miss: without the subscription
        # the connection stays silent forever.
        state = rt.direct_subscribe(amount=1)
        with self._lock:
            self._rt = rt
            self.connected = True
            self.subscribed_at = time.time()
            self.seq_id = int(state.get("seq_id") or 0)
        log.info(
            "push channel connected and subscribed (seq %s)", self.seq_id
        )
        self._emit_status()

    def _pump(self) -> None:
        """Read the socket until it closes or we are asked to stop."""
        rt = self._rt
        if rt is None:
            return
        while not self._stop.is_set():
            try:
                rt.read_once()
            except (TimeoutError, socket.timeout):
                # The socket has a 30 second read timeout, so silence is the
                # normal case on an idle connection — it is not a dropped
                # connection. Say hello and keep waiting.
                self._keepalive(rt)
                continue
            except Exception:
                # A closed socket is a real drop: reconnect.
                raise

    def _keepalive(self, rt) -> None:
        """Send a ping so Instagram keeps the connection open."""
        try:
            from instagrapi.realtime.mqttot import write_pingreq_packet

            rt.transport.send(write_pingreq_packet())
            self.pings += 1
        except Exception as exc:  # noqa: BLE001
            log.debug("the keepalive ping failed: %s", exc)

    # --------------------------------------------------------------- events
    def _on_message_event(self, payload: Any) -> None:
        """A message arrived."""
        self.messages_received += 1
        self.last_event_at = time.time()

        message = self._parse(payload)
        if message is None:
            return
        log.info(
            "push: %s in thread %s", (message.text or message.item_type)[:60], message.thread_id
        )
        if self.on_message is not None:
            try:
                self.on_message(message)
            except Exception as exc:  # noqa: BLE001
                log.exception("the push handler failed: %s", exc)

    def _parse(self, payload: Any) -> RealtimeMessage | None:
        """Pull the useful fields out of the push payload."""
        if not isinstance(payload, dict):
            return None
        message = payload.get("message")
        if not isinstance(message, dict):
            return None

        path = str(message.get("path") or "")
        thread_id = str(message.get("thread_id") or "")
        if not thread_id and "/direct_v2/threads/" in path:
            thread_id = path.split("/direct_v2/threads/", 1)[1].split("/", 1)[0]

        user_id = message.get("user_id")
        own = str(getattr(self.client, "user_id", "") or "")
        is_self = bool(user_id) and str(user_id) == own

        timestamp = message.get("timestamp")
        try:
            # Instagram sends microseconds.
            seconds = float(timestamp) / 1_000_000 if timestamp else time.time()
        except (TypeError, ValueError):
            seconds = time.time()

        return RealtimeMessage(
            thread_id=thread_id,
            message_id=str(message.get("message_id") or message.get("item_id") or ""),
            user_id=str(user_id or ""),
            text=str(message.get("text") or ""),
            timestamp=seconds,
            item_type=str(message.get("item_type") or "text"),
            is_self=is_self,
        )

    def _emit_status(self) -> None:
        if self.on_status is not None:
            try:
                self.on_status(self.health())
            except Exception:  # noqa: BLE001
                pass

    # --------------------------------------------------------------- status
    def health(self) -> dict[str, Any]:
        return {
            "realtime": "connected" if self.connected else "off",
            "connected": self.connected,
            "messages": self.messages_received,
            "reconnects": self.connect_attempts,
            "seq_id": self.seq_id,
            "last_event_at": self.last_event_at,
            "pings": self.pings,
            "error": self.last_error,
        }

    def dump_last(self) -> str:
        """A readable snapshot of the last payload, for the CLI."""
        return json.dumps(self.health(), indent=2, default=str)

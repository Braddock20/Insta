"""Keep the Render free tier awake.

A free Render instance is put to sleep after 15 minutes without incoming
traffic, and a sleeping instance takes ~30 seconds to wake up — which means a
customer who writes at 3am gets no answer until their message times out the
window.

The fix is a self-ping: the bot asks its own health endpoint every few
minutes. Render counts that as traffic, so the instance stays up, and the
health endpoint doubles as the proof that the service is alive.

Set ``keepalive.enabled: false`` to turn it off — for example when you run
the bot somewhere that never sleeps.
"""

from __future__ import annotations

import threading
import time
import urllib.error
import urllib.request

from . import log

log = log.get(__name__)


class KeepAlive:
    """Pings the bot's own health endpoint on a timer."""

    def __init__(self, url: str, interval: int = 240, timeout: int = 10):
        self.url = url
        self.interval = max(60, int(interval))
        self.timeout = timeout
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self.last_ping = 0.0
        self.failures = 0

    def start(self) -> None:
        if self._thread is not None:
            return
        self._thread = threading.Thread(target=self._run, name="keepalive", daemon=True)
        self._thread.start()
        log.info("keepalive started (%ss)", self.interval)

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=5)
            self._thread = None

    def ping_once(self) -> bool:
        try:
            with urllib.request.urlopen(self.url, timeout=self.timeout) as resp:
                ok = 200 <= resp.status < 400
        except (urllib.error.URLError, OSError, ValueError) as exc:
            log.debug("keepalive ping failed: %s", exc)
            self.failures += 1
            return False
        self.last_ping = time.time()
        if ok:
            self.failures = 0
        return ok

    def _run(self) -> None:
        while not self._stop.is_set():
            # The first ping waits one interval: the service has just booted
            # and Render does not need a ping yet.
            if self._stop.wait(self.interval):
                return
            try:
                self.ping_once()
            except Exception as exc:  # noqa: BLE001
                log.debug("keepalive error: %s", exc)

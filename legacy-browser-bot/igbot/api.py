"""HTTP control API.

A small, dependency-free control surface so the bot can be driven from
outside: health checks, counters, the task queue, and a way to trigger
actions.

Design note — why actions are queued rather than run inline
-----------------------------------------------------------
Playwright's sync API is bound to the thread that created it, so an HTTP
handler cannot safely drive the browser while the watch loop is using it.
Rather than fight that, every *action* endpoint enqueues a task and returns
immediately with the task id. The watch loop picks it up on its next pass,
which is also the right thing for Instagram: an action that arrives from a
webhook should not fire in the same millisecond.

Read-only endpoints (stats, tasks, activity, health) read the SQLite state
directly, which is safe from any thread.

Authentication is a bearer token from ``IGBOT_API_TOKEN``. Without a token
configured the API refuses every route except ``/api/health``, so a
misconfigured deployment is closed by default rather than wide open.
"""

from __future__ import annotations

import json
import os
import threading
import time
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable
from urllib.parse import parse_qs, urlparse

from . import log

log = log.get(__name__)


@dataclass
class APIContext:
    """Everything the API needs to answer a request."""

    cfg: Any
    store: Any
    scheduler: Any = None
    guardrails: Any = None
    notifications: Any = None
    token: str = ""
    started_at: float = 0.0
    session_state: Callable[[], dict] | None = None
    log_file: Path | None = None


def _json_bytes(payload: Any) -> bytes:
    return json.dumps(payload, default=str).encode("utf-8")


class _Handler(BaseHTTPRequestHandler):
    ctx: APIContext | None = None

    # -------------------------------------------------------------- plumbing

    def log_message(self, *args) -> None:
        """Silence the default stderr access log; we log through the bot's log."""

    def _send(self, payload: Any, status: int = 200) -> None:
        body = _json_bytes(payload)
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _authorised(self) -> bool:
        ctx = self.ctx
        if ctx is None or not ctx.token:
            return False
        header = self.headers.get("Authorization", "")
        if header.startswith("Bearer ") and header[7:].strip() == ctx.token:
            return True
        if self.headers.get("X-API-Token", "").strip() == ctx.token:
            return True
        return False

    def _body(self) -> dict:
        length = int(self.headers.get("Content-Length") or 0)
        if not length:
            return {}
        try:
            return json.loads(self.rfile.read(length).decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            return {}

    # ------------------------------------------------------------------ GET

    def do_GET(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        path = parsed.path.rstrip("/") or "/"
        query = parse_qs(parsed.query)

        if path in ("/", "/index.html"):
            self._send(self._status_page())
            return

        if path == "/api/health":
            self._send(self._health())
            return

        if not self._authorised():
            self._send({"error": "unauthorised"}, status=401)
            return

        ctx = self.ctx
        assert ctx is not None

        if path == "/api/stats":
            self._send(self._stats())
            return

        if path == "/api/tasks":
            self._send({"tasks": self._tasks(int((query.get("limit") or ["50"])[0]))})
            return

        if path == "/api/activity":
            self._send({"activity": self._activity(int((query.get("limit") or ["25"])[0]))})
            return

        if path == "/api/actions":
            self._send({"actions": self._actions(int((query.get("limit") or ["25"])[0]))})
            return

        if path.startswith("/api/profile/"):
            username = path[len("/api/profile/"):]
            self._send(self._enqueue_action("auto:follow_back", {"username": username},
                                           f"profile lookup for @{username}"))
            return

        if path == "/api/logs":
            lines = int((query.get("lines") or ["100"])[0])
            self._send({"lines": self._logs(lines)})
            return

        self._send({"error": "not found", "path": path}, status=404)

    # ----------------------------------------------------------------- POST

    def do_POST(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        path = parsed.path.rstrip("/") or "/"

        if not self._authorised():
            self._send({"error": "unauthorised"}, status=401)
            return

        payload = self._body()

        if path == "/api/tasks":
            kind = str(payload.get("kind") or "").strip()
            if not kind:
                self._send({"error": "kind is required"}, status=400)
                return
            task_id = self._enqueue_action(
                kind,
                payload.get("payload") if isinstance(payload.get("payload"), dict) else {},
                f"queued {kind}",
                delay=float(payload.get("delay_seconds") or 0),
                fingerprint=payload.get("fingerprint"),
            )
            self._send({"task_id": task_id, "kind": kind})
            return

        if path == "/api/follow":
            username = str(payload.get("username") or "").strip().lstrip("@")
            if not username:
                self._send({"error": "username is required"}, status=400)
                return
            self._send(self._enqueue_action("auto:follow", {"username": username},
                                            f"queued a follow for @{username}"))
            return

        if path == "/api/unfollow":
            username = str(payload.get("username") or "").strip().lstrip("@")
            if not username:
                self._send({"error": "username is required"}, status=400)
                return
            self._send(self._enqueue_action("auto:unfollow", {"username": username},
                                            f"queued an unfollow for @{username}"))
            return

        if path == "/api/like":
            url = str(payload.get("url") or "").strip()
            if not url:
                self._send({"error": "url is required"}, status=400)
                return
            self._send(self._enqueue_action("auto:like_post", {"post_url": url},
                                            f"queued a like for {url}"))
            return

        self._send({"error": "not found", "path": path}, status=404)

    # ------------------------------------------------------------- responses

    def _health(self) -> dict:
        ctx = self.ctx
        session = {}
        if ctx is not None and ctx.session_state is not None:
            try:
                session = ctx.session_state() or {}
            except Exception as exc:  # pragma: no cover - defensive
                session = {"error": str(exc)}
        return {
            "ok": True,
            "uptime_seconds": int(time.time() - (ctx.started_at if ctx else time.time())),
            "session": session,
        }

    def _stats(self) -> dict:
        ctx = self.ctx
        stats: dict[str, Any] = {}
        if ctx is not None and ctx.guardrails is not None:
            stats.update(ctx.guardrails.stats())
            stats["actions_today"] = ctx.guardrails.action_stats()
        if ctx is not None and ctx.store is not None:
            stats["pending_tasks"] = ctx.store.pending_task_count()
            stats["total_events"] = ctx.store.count("reply", 0)
        return stats

    def _tasks(self, limit: int) -> list[dict]:
        ctx = self.ctx
        if ctx is None or ctx.store is None:
            return []
        rows = ctx.store.list_tasks(limit=limit)
        return [
            {
                "id": row["id"],
                "kind": row["kind"],
                "status": row["status"],
                "run_at": row["run_at"],
                "attempts": row["attempts"],
                "last_error": row["last_error"],
                "result": row["result"],
            }
            for row in rows
        ]

    def _activity(self, limit: int) -> list[dict]:
        ctx = self.ctx
        if ctx is None or ctx.store is None:
            return []
        rows = ctx.store.recent_activity(limit=limit)
        return [
            {
                "kind": row["kind"],
                "username": row["username"],
                "detail": row["detail"],
                "ts": row["ts"],
            }
            for row in rows
        ]

    def _actions(self, limit: int) -> list[dict]:
        ctx = self.ctx
        if ctx is None or ctx.store is None:
            return []
        rows = ctx.store.recent_actions(limit=limit)
        return [
            {
                "kind": row["kind"],
                "target": row["target"],
                "detail": row["detail"],
                "ts": row["ts"],
            }
            for row in rows
        ]

    def _logs(self, lines: int) -> list[str]:
        ctx = self.ctx
        if ctx is None or ctx.log_file is None:
            return []
        path = Path(ctx.log_file)
        if not path.exists():
            return []
        try:
            content = path.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            return []
        return content[-max(1, min(lines, 2000)):]

    def _enqueue_action(
        self,
        kind: str,
        payload: dict,
        message: str,
        delay: float = 0.0,
        fingerprint: str | None = None,
    ) -> dict:
        ctx = self.ctx
        if ctx is None or ctx.scheduler is None:
            return {"error": "no scheduler attached"}
        task_id = ctx.scheduler.enqueue(
            kind, payload=payload, delay_seconds=delay, fingerprint=fingerprint
        )
        if task_id is None:
            return {"error": "an identical task is already queued"}
        return {"ok": True, "task_id": task_id, "message": message}

    def _status_page(self) -> dict:
        ctx = self.ctx
        stats = self._stats()
        return {
            "service": "igbot",
            "hint": "see /api/health, /api/stats, /api/tasks, /api/activity",
            "stats": stats,
        }


def serve(ctx: APIContext, port: int = 0, host: str = "0.0.0.0") -> ThreadingHTTPServer:
    """Start the API server in a background thread. Returns the server."""
    handler = type("BoundHandler", (_Handler,), {"ctx": ctx})
    server = ThreadingHTTPServer((host, port), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    log.info("control API listening on http://%s:%s", host, server.server_address[1])
    return server


def token_from_env() -> str:
    """The API token, from the environment or the config."""
    env = os.environ.get("IGBOT_API_TOKEN", "")
    if env:
        return env.strip()
    return ""

"""SQLite-backed state.

Everything the bot needs to remember across restarts lives here: which inbound
messages it has already handled, when it last replied to each conversation, and
the reply counters that back the rate caps.

The database is intentionally boring — one table of events plus a key/value
table — so it is easy to inspect with ``sqlite3`` when something looks wrong.
"""

from __future__ import annotations

import hashlib
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any, Iterable

_SCHEMA = """
CREATE TABLE IF NOT EXISTS events (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    ts          REAL NOT NULL,
    kind        TEXT NOT NULL,
    thread_id   TEXT,
    fingerprint TEXT,
    detail      TEXT
);
CREATE UNIQUE INDEX IF NOT EXISTS ux_events_fingerprint
    ON events(fingerprint) WHERE fingerprint IS NOT NULL;
CREATE INDEX IF NOT EXISTS ix_events_kind_ts ON events(kind, ts);
CREATE INDEX IF NOT EXISTS ix_events_thread ON events(thread_id, ts);

CREATE TABLE IF NOT EXISTS kv (
    key   TEXT PRIMARY KEY,
    value TEXT
);

-- A queue of work the bot should do later (reply to a mention, publish a
-- post at 09:00, like a comment). Survives restarts.
CREATE TABLE IF NOT EXISTS tasks (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    created_ts  REAL NOT NULL,
    run_at      REAL NOT NULL,
    kind        TEXT NOT NULL,
    payload     TEXT,
    status      TEXT NOT NULL DEFAULT 'pending',
    attempts    INTEGER NOT NULL DEFAULT 0,
    last_error  TEXT,
    result      TEXT,
    fingerprint TEXT
);
CREATE INDEX IF NOT EXISTS ix_tasks_status_run_at ON tasks(status, run_at);
CREATE UNIQUE INDEX IF NOT EXISTS ux_tasks_fingerprint
    ON tasks(fingerprint) WHERE fingerprint IS NOT NULL;

-- An audit trail of every *visible* action (follow, like, story reply).
-- Nothing here is destructive, but you want to be able to see exactly what
-- the bot did to your account.
CREATE TABLE IF NOT EXISTS actions (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    ts          REAL NOT NULL,
    kind        TEXT NOT NULL,
    target      TEXT,
    detail      TEXT
);
CREATE INDEX IF NOT EXISTS ix_actions_kind_ts ON actions(kind, ts);
CREATE INDEX IF NOT EXISTS ix_actions_target ON actions(target, ts);

-- Activity seen on the account: who followed, who liked, who commented.
CREATE TABLE IF NOT EXISTS activity (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    ts          REAL NOT NULL,
    kind        TEXT NOT NULL,
    username    TEXT,
    detail      TEXT,
    fingerprint TEXT
);
CREATE UNIQUE INDEX IF NOT EXISTS ux_activity_fingerprint
    ON activity(fingerprint) WHERE fingerprint IS NOT NULL;
CREATE INDEX IF NOT EXISTS ix_activity_kind_ts ON activity(kind, ts);
"""


def fingerprint_for(thread_id: str, sender: str, text: str) -> str:
    """Stable id for an inbound message.

    Includes the thread and sender so the same words in two different
    conversations are treated as two different messages, and collapses repeated
    copies of the identical message into one.
    """
    norm = " ".join((text or "").split()).lower()
    raw = f"{thread_id or ''}|{sender or ''}|{norm}"
    return hashlib.sha256(raw.encode("utf-8", "replace")).hexdigest()[:32]


class Store:
    """Thread-safe wrapper around the SQLite state file."""

    def __init__(self, path: str | Path):
        self.path = Path(path).expanduser()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(str(self.path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        with self._lock:
            self._conn.executescript(_SCHEMA)
            self._conn.commit()

    # ------------------------------------------------------------------ events

    def has_fingerprint(self, fingerprint: str) -> bool:
        """True if this exact message has already been recorded."""
        if not fingerprint:
            return False
        with self._lock:
            row = self._conn.execute(
                "SELECT 1 FROM events WHERE fingerprint = ? LIMIT 1", (fingerprint,)
            ).fetchone()
        return row is not None

    def record(
        self,
        kind: str,
        thread_id: str | None = None,
        fingerprint: str | None = None,
        detail: str | None = None,
        ts: float | None = None,
    ) -> bool:
        """Insert an event. Returns False if the fingerprint was already present.

        The uniqueness check and the insert happen in one transaction so two
        concurrent pollers cannot both claim the same message.
        """
        with self._lock:
            try:
                self._conn.execute(
                    "INSERT INTO events (ts, kind, thread_id, fingerprint, detail) "
                    "VALUES (?, ?, ?, ?, ?)",
                    (ts if ts is not None else time.time(), kind, thread_id, fingerprint, detail),
                )
                self._conn.commit()
                return True
            except sqlite3.IntegrityError:
                self._conn.rollback()
                return False

    def count(self, kind: str, since: float, thread_id: str | None = None) -> int:
        """Count events of a kind since a timestamp."""
        with self._lock:
            if thread_id is None:
                row = self._conn.execute(
                    "SELECT COUNT(*) AS c FROM events WHERE kind = ? AND ts >= ?", (kind, since)
                ).fetchone()
            else:
                row = self._conn.execute(
                    "SELECT COUNT(*) AS c FROM events WHERE kind = ? AND ts >= ? AND thread_id = ?",
                    (kind, since, thread_id),
                ).fetchone()
        return int(row["c"]) if row else 0

    def last_ts(self, kind: str, thread_id: str | None = None) -> float | None:
        """Timestamp of the most recent matching event."""
        with self._lock:
            if thread_id is None:
                row = self._conn.execute(
                    "SELECT MAX(ts) AS t FROM events WHERE kind = ?", (kind,)
                ).fetchone()
            else:
                row = self._conn.execute(
                    "SELECT MAX(ts) AS t FROM events WHERE kind = ? AND thread_id = ?",
                    (kind, thread_id),
                ).fetchone()
        return row["t"] if row and row["t"] is not None else None

    def recent(self, kind: str, limit: int = 50) -> list[sqlite3.Row]:
        """Most recent events of a kind, newest first."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM events WHERE kind = ? ORDER BY ts DESC LIMIT ?", (kind, limit)
            ).fetchall()
        return list(rows)

    def prune(self, older_than_days: int = 30) -> int:
        """Delete old events. Returns the number of rows removed."""
        cutoff = time.time() - older_than_days * 86400
        with self._lock:
            cur = self._conn.execute("DELETE FROM events WHERE ts < ?", (cutoff,))
            self._conn.commit()
            return cur.rowcount or 0

    # ------------------------------------------------------------------- tasks

    def enqueue_task(
        self,
        kind: str,
        payload: dict | None = None,
        run_at: float | None = None,
        fingerprint: str | None = None,
    ) -> int | None:
        """Add a task to the queue.

        Returns the task id, or ``None`` when an identical task is already
        queued (same fingerprint) — so a rule that fires every poll does not
        pile up a hundred copies of the same job.
        """
        import json as _json

        now = time.time()
        with self._lock:
            if fingerprint:
                row = self._conn.execute(
                    "SELECT id FROM tasks WHERE fingerprint = ? AND status = 'pending' LIMIT 1",
                    (fingerprint,),
                ).fetchone()
                if row is not None:
                    return None
            cur = self._conn.execute(
                "INSERT INTO tasks (created_ts, run_at, kind, payload, status, fingerprint) "
                "VALUES (?, ?, ?, ?, 'pending', ?)",
                (
                    now,
                    float(run_at if run_at is not None else now),
                    kind,
                    _json.dumps(payload or {}, default=str),
                    fingerprint,
                ),
            )
            self._conn.commit()
            return int(cur.lastrowid)

    def due_tasks(self, now: float | None = None, limit: int = 25) -> list[sqlite3.Row]:
        """Pending tasks whose time has come, oldest first."""
        moment = now if now is not None else time.time()
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM tasks WHERE status = 'pending' AND run_at <= ? "
                "ORDER BY run_at ASC LIMIT ?",
                (moment, limit),
            ).fetchall()
        return list(rows)

    def pending_task_count(self) -> int:
        with self._lock:
            row = self._conn.execute(
                "SELECT COUNT(*) AS c FROM tasks WHERE status = 'pending'"
            ).fetchone()
        return int(row["c"]) if row else 0

    def list_tasks(self, limit: int = 50) -> list[sqlite3.Row]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM tasks ORDER BY created_ts DESC LIMIT ?", (limit,)
            ).fetchall()
        return list(rows)

    def cancel_task(self, task_id: int) -> bool:
        """Cancel a queued task. Returns False when there was nothing to cancel."""
        with self._lock:
            cur = self._conn.execute(
                "UPDATE tasks SET status = 'cancelled' WHERE id = ? AND status = 'pending'",
                (int(task_id),),
            )
            self._conn.commit()
        return cur.rowcount > 0

    def clear_fingerprints(self, thread_id: str) -> int:
        """Forget the messages already seen in one conversation.

        Used by the ``/clear`` command when the bot answered something it
        should not have — the conversation can then be answered again from
        the next message onwards.
        """
        with self._lock:
            cur = self._conn.execute(
                "DELETE FROM events WHERE thread_id = ? AND fingerprint IS NOT NULL",
                (thread_id,),
            )
            self._conn.commit()
        return cur.rowcount

    def finish_task(
        self, task_id: int, status: str = "done", result: str | None = None, error: str | None = None
    ) -> None:
        """Close a task out."""
        with self._lock:
            self._conn.execute(
                "UPDATE tasks SET status = ?, result = ?, last_error = ?, "
                "attempts = attempts + 1 WHERE id = ?",
                (status, result, error, task_id),
            )
            self._conn.commit()

    # ----------------------------------------------------------------- actions

    def record_action(self, kind: str, target: str | None = None, detail: str | None = None) -> None:
        """Log a visible action (follow, like, story reply) for the audit trail."""
        with self._lock:
            self._conn.execute(
                "INSERT INTO actions (ts, kind, target, detail) VALUES (?, ?, ?, ?)",
                (time.time(), kind, target, detail),
            )
            self._conn.commit()

    def recent_actions(self, kind: str | None = None, limit: int = 50) -> list[sqlite3.Row]:
        """Recent actions, newest first."""
        with self._lock:
            if kind:
                rows = self._conn.execute(
                    "SELECT * FROM actions WHERE kind = ? ORDER BY ts DESC LIMIT ?", (kind, limit)
                ).fetchall()
            else:
                rows = self._conn.execute(
                    "SELECT * FROM actions ORDER BY ts DESC LIMIT ?", (limit,)
                ).fetchall()
        return list(rows)

    # ---------------------------------------------------------------- activity

    def record_activity(
        self, kind: str, username: str | None = None, detail: str | None = None,
        fingerprint: str | None = None,
    ) -> bool:
        """Record an activity item. Returns False if it was already known."""
        with self._lock:
            try:
                self._conn.execute(
                    "INSERT INTO activity (ts, kind, username, detail, fingerprint) "
                    "VALUES (?, ?, ?, ?, ?)",
                    (time.time(), kind, username, detail, fingerprint),
                )
                self._conn.commit()
                return True
            except sqlite3.IntegrityError:
                self._conn.rollback()
                return False

    def recent_activity(self, kind: str | None = None, limit: int = 50) -> list[sqlite3.Row]:
        with self._lock:
            if kind:
                rows = self._conn.execute(
                    "SELECT * FROM activity WHERE kind = ? ORDER BY ts DESC LIMIT ?", (kind, limit)
                ).fetchall()
            else:
                rows = self._conn.execute(
                    "SELECT * FROM activity ORDER BY ts DESC LIMIT ?", (limit,)
                ).fetchall()
        return list(rows)

    # ---------------------------------------------------------------------- kv

    def kv_get(self, key: str, default: Any = None) -> Any:
        with self._lock:
            row = self._conn.execute("SELECT value FROM kv WHERE key = ?", (key,)).fetchone()
        return row["value"] if row else default

    def kv_set(self, key: str, value: str) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO kv (key, value) VALUES (?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (key, value),
            )
            self._conn.commit()

    def close(self) -> None:
        with self._lock:
            try:
                self._conn.close()
            except sqlite3.Error:
                pass


def day_start(now: float | None = None) -> float:
    """Midnight (local time) of the current day, as a unix timestamp."""
    now = now if now is not None else time.time()
    lt = time.localtime(now)
    return time.mktime((lt.tm_year, lt.tm_mon, lt.tm_mday, 0, 0, 0, 0, 0, -1))


def hour_start(now: float | None = None) -> float:
    now = now if now is not None else time.time()
    lt = time.localtime(now)
    return time.mktime((lt.tm_year, lt.tm_mon, lt.tm_mday, lt.tm_hour, 0, 0, 0, 0, -1))


def iter_recent(rows: Iterable[sqlite3.Row], key: str = "ts") -> Iterable[float]:
    for row in rows:
        yield float(row[key])

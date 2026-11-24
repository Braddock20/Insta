"""Task queue and scheduling.

Two things live here:

* a **queue** of work, persisted in SQLite, so a task survives a restart;
* **schedules** — rules such as "publish at 09:00 every day" or "check the
  activity feed every 30 minutes" that are evaluated on every pass of the
  watch loop.

Why a queue at all
------------------
A reaction to a follow is not something you want to do *during* the poll that
discovered it. The poll also has a cooldown and a cap to respect, and a slow
action (uploading a photo) must not hold up the inbox. So a rule *enqueues* a
task, and the task runs on a later pass when its turn comes.

Every task carries a fingerprint. Re-queuing the same task is a no-op, which
means a rule that fires on every poll cannot pile up a hundred copies of the
same job.
"""

from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass, field
from typing import Any, Callable

from . import log
from .store import Store

log = log.get(__name__)

# "09:00"        -> every day at 09:00
# "every 30m"    -> every 30 minutes
# "every 2h"     -> every two hours
# "every 1d"     -> every day (from now)
_DAILY = re.compile(r"^(\d{1,2}):(\d{2})$")
_EVERY = re.compile(r"^every\s+(\d+)\s*([mhd])$", re.I)


@dataclass
class Task:
    """One queued job."""

    id: int
    kind: str
    payload: dict = field(default_factory=dict)
    run_at: float = 0.0
    attempts: int = 0

    @classmethod
    def from_row(cls, row) -> "Task":
        try:
            payload = json.loads(row["payload"] or "{}")
        except (ValueError, TypeError):
            payload = {}
        return cls(
            id=int(row["id"]),
            kind=str(row["kind"]),
            payload=payload if isinstance(payload, dict) else {},
            run_at=float(row["run_at"] or 0.0),
            attempts=int(row["attempts"] or 0),
        )


@dataclass
class Schedule:
    """A recurring rule."""

    name: str
    kind: str
    spec: str
    payload: dict = field(default_factory=dict)
    enabled: bool = True

    def next_run(self, after: float | None = None) -> float | None:
        """The next timestamp this schedule fires, or None if it is malformed."""
        moment = after if after is not None else time.time()
        daily = _DAILY.match((self.spec or "").strip())
        if daily:
            hour, minute = int(daily.group(1)), int(daily.group(2))
            if hour > 23 or minute > 59:
                return None
            local = time.localtime(moment)
            candidate = time.mktime(
                (local.tm_year, local.tm_mon, local.tm_mday, hour, minute, 0, 0, 0, -1)
            )
            if candidate <= moment:
                candidate += 86400
            return candidate

        every = _EVERY.match((self.spec or "").strip())
        if every:
            amount = int(every.group(1))
            unit = every.group(2).lower()
            seconds = {"m": 60, "h": 3600, "d": 86400}[unit] * max(1, amount)
            return moment + seconds

        return None


class Scheduler:
    """Owns the task queue and the recurring schedules."""

    def __init__(self, store: Store, cfg=None):
        self.store = store
        self.cfg = cfg

    # ------------------------------------------------------------------ queue

    def enqueue(
        self,
        kind: str,
        payload: dict | None = None,
        delay_seconds: float = 0.0,
        fingerprint: str | None = None,
    ) -> int | None:
        """Add a task to the queue. Returns None if an identical one is queued."""
        run_at = time.time() + max(0.0, float(delay_seconds or 0.0))
        task_id = self.store.enqueue_task(
            kind, payload=payload or {}, run_at=run_at, fingerprint=fingerprint
        )
        if task_id is not None:
            log.info("queued task %s (id %s)", kind, task_id)
        return task_id

    def pending(self) -> int:
        return self.store.pending_task_count()

    def list_tasks(self, limit: int = 50) -> list[Task]:
        return [Task.from_row(row) for row in self.store.list_tasks(limit)]

    def due(self, now: float | None = None) -> list[Task]:
        return [Task.from_row(row) for row in self.store.due_tasks(now)]

    def cancel(self, task_id: int) -> bool:
        self.store.finish_task(task_id, status="cancelled")
        return True

    # --------------------------------------------------------------- running

    def run_due(
        self,
        handlers: dict[str, Callable[[Task], Any]],
        now: float | None = None,
        limit: int = 10,
    ) -> list[dict]:
        """Run every task that is due.

        A handler returns ``(ok, message)`` or just ``ok``. A task that raises
        is marked failed and retried up to ``max_attempts`` times; after that
        it is abandoned rather than retried forever.
        """
        results: list[dict] = []
        max_attempts = 3

        for task in self.due(now)[:limit]:
            handler = handlers.get(task.kind)
            if handler is None:
                self.store.finish_task(
                    task.id, status="failed", error=f"no handler for {task.kind!r}"
                )
                results.append({"id": task.id, "kind": task.kind, "ok": False,
                                "message": f"no handler for {task.kind!r}"})
                continue

            try:
                outcome = handler(task)
            except Exception as exc:  # pragma: no cover - handler dependent
                log.exception("task %s (%s) failed: %s", task.id, task.kind, exc)
                attempts = task.attempts + 1
                status = "failed" if attempts >= max_attempts else "pending"
                # Retry in five minutes.
                self.store.finish_task(task.id, status=status, error=str(exc)[:300])
                if status == "pending":
                    self.store.enqueue_task(
                        task.kind, payload=task.payload, run_at=time.time() + 300,
                        fingerprint=None,
                    )
                results.append({"id": task.id, "kind": task.kind, "ok": False, "message": str(exc)})
                continue

            ok, message = _split(outcome)
            self.store.finish_task(
                task.id, status="done" if ok else "failed", result=str(message)[:300]
            )
            results.append({"id": task.id, "kind": task.kind, "ok": ok, "message": message})
            log.info("task %s (%s) finished: %s", task.id, task.kind, message)

        return results

    # ------------------------------------------------------------- schedules

    def schedules_from_config(self) -> list[Schedule]:
        """Read the schedules out of the config."""
        raw = self.cfg.get("automations.schedules", []) if self.cfg is not None else []
        out: list[Schedule] = []
        for entry in raw or []:
            if not isinstance(entry, dict):
                continue
            name = str(entry.get("name") or entry.get("kind") or "")
            kind = str(entry.get("kind") or entry.get("action") or "")
            spec = str(entry.get("schedule") or entry.get("spec") or entry.get("at") or "")
            if not name or not kind or not spec:
                continue
            payload = entry.get("params") or entry.get("payload") or {}
            out.append(
                Schedule(
                    name=name,
                    kind=kind,
                    spec=spec,
                    payload=payload if isinstance(payload, dict) else {},
                    enabled=bool(entry.get("enabled", True)),
                )
            )
        return out

    def tick_schedules(self, now: float | None = None) -> list[int]:
        """Enqueue the tasks that the schedules are due for.

        The fingerprint includes the schedule name and the time slot it
        belongs to, so a daily schedule fires once a day and not once a
        minute.
        """
        moment = now if now is not None else time.time()
        created: list[int] = []
        for schedule in self.schedules_from_config():
            if not schedule.enabled:
                continue
            next_at = schedule.next_run(moment)
            if next_at is None:
                log.warning("schedule %r has an invalid spec %r", schedule.name, schedule.spec)
                continue
            slot = int(next_at // 60)
            task_id = self.enqueue(
                schedule.kind,
                payload=schedule.payload,
                delay_seconds=max(0.0, next_at - moment),
                fingerprint=f"schedule:{schedule.name}:{slot}",
            )
            if task_id is not None:
                created.append(task_id)
        return created


def _split(outcome: Any) -> tuple[bool, str]:
    """Normalise a handler's return value into ``(ok, message)``."""
    if isinstance(outcome, tuple) and len(outcome) == 2:
        return bool(outcome[0]), str(outcome[1])
    if isinstance(outcome, bool):
        return outcome, "ok" if outcome else "failed"
    if outcome is None:
        return True, "done"
    return True, str(outcome)

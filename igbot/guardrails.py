"""Guardrails: the rules that decide whether a reply may be sent right now.

Two jobs:

1. **Protection** — cooldowns and caps stop the bot from replying to every
   message the moment it arrives, which is exactly the behaviour that makes an
   account look automated.
2. **Honesty** — when a reply is blocked the reason is recorded, so the log
   says *why* nothing was sent instead of leaving you guessing.

The reply is only counted once it has been *confirmed* sent. A failed send does
not burn the user's quota and does not start the cooldown.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field

from .store import Store, day_start, hour_start

KIND_REPLY = "reply"
KIND_INBOUND = "inbound"
KIND_PUBLISH = "publish"
KIND_SEEN = "seen"


@dataclass
class Decision:
    """Outcome of a guardrail check."""

    allowed: bool
    reason: str = ""
    wait_seconds: int = 0
    details: dict = field(default_factory=dict)
    # True when the action is blocked because it was already done today —
    # which is a *no-op*, not a failure.
    already_satisfied: bool = False

    def __bool__(self) -> bool:
        return self.allowed

    def __str__(self) -> str:  # pragma: no cover - display helper
        if self.allowed:
            return "allowed"
        return f"blocked: {self.reason}"


class Guardrails:
    """Applies cooldown and rate-cap policy to outgoing replies."""

    def __init__(self, store: Store, cfg) -> None:
        self.store = store
        self.cfg = cfg

    # ------------------------------------------------------------------ policy

    def _cooldown(self) -> int:
        return int(self.cfg.get("behaviour.cooldown_seconds", 900) or 0)

    def _hourly_cap(self) -> int:
        return int(self.cfg.get("behaviour.hourly_cap", 8) or 0)

    def _daily_cap(self) -> int:
        return int(self.cfg.get("behaviour.daily_cap", 25) or 0)

    def _global_daily_cap(self) -> int:
        return int(self.cfg.get("behaviour.global_daily_cap", 120) or 0)

    # ------------------------------------------------------------------ checks

    def check(self, thread_id: str) -> Decision:
        """Decide whether we may reply to ``thread_id`` right now."""
        now = time.time()

        if bool(self.cfg.get("behaviour.baseline_only", False)):
            return Decision(False, "baseline mode is on (no replies are sent)")

        cooldown = self._cooldown()
        last = self.store.last_ts(KIND_REPLY, thread_id)
        if last is not None and cooldown > 0:
            elapsed = now - last
            if elapsed < cooldown:
                wait = int(cooldown - elapsed)
                return Decision(
                    False,
                    f"conversation is in cooldown for another {wait}s",
                    wait_seconds=wait,
                    details={"last_reply_at": last},
                )

        hour_cap = self._hourly_cap()
        if hour_cap > 0:
            used = self.store.count(KIND_REPLY, hour_start(now), thread_id)
            if used >= hour_cap:
                return Decision(
                    False,
                    f"hourly cap reached for this conversation ({used}/{hour_cap})",
                    wait_seconds=int(3600 - (now - hour_start(now))),
                    details={"used": used, "cap": hour_cap},
                )

        day_cap = self._daily_cap()
        if day_cap > 0:
            used = self.store.count(KIND_REPLY, day_start(now), thread_id)
            if used >= day_cap:
                return Decision(
                    False,
                    f"daily cap reached for this conversation ({used}/{day_cap})",
                    wait_seconds=int(86400 - (now - day_start(now))),
                    details={"used": used, "cap": day_cap},
                )

        global_cap = self._global_daily_cap()
        if global_cap > 0:
            used = self.store.count(KIND_REPLY, day_start(now), None)
            if used >= global_cap:
                return Decision(
                    False,
                    f"global daily cap reached ({used}/{global_cap})",
                    wait_seconds=int(86400 - (now - day_start(now))),
                    details={"used": used, "cap": global_cap},
                )

        return Decision(True)

    def record_reply(self, thread_id: str, detail: str | None = None) -> None:
        """Record a *confirmed* outgoing reply."""
        self.store.record(KIND_REPLY, thread_id=thread_id, detail=detail)

    def record_inbound(self, thread_id: str, fingerprint: str, detail: str | None = None) -> bool:
        """Record that an inbound message was seen.

        Returns False when the fingerprint was already known, which is how
        duplicate suppression works across restarts.
        """
        return self.store.record(
            KIND_INBOUND, thread_id=thread_id, fingerprint=fingerprint, detail=detail
        )

    # ------------------------------------------------------------------ actions

    def _action_caps(self, kind: str) -> tuple[int, int]:
        """(hourly cap, daily cap) for a non-reply action such as ``like``."""
        caps = self.cfg.get("behaviour.action_caps", {}) or {}
        entry = caps.get(kind) or {}
        hourly = int(entry.get("hourly", 0) or 0)
        daily = int(entry.get("daily", 0) or 0)
        return hourly, daily

    def check_action(self, kind: str, target: str | None = None) -> Decision:
        """Decide whether a visible action (like, follow, story reply) is allowed.

        Same idea as :meth:`check`, but for actions other than DM replies. They
        are public, so the caps are usually tighter than the reply caps.
        """
        now = time.time()

        if bool(self.cfg.get("behaviour.baseline_only", False)):
            return Decision(False, "baseline mode is on (no actions are performed)")

        hourly_cap, daily_cap = self._action_caps(kind)
        if hourly_cap > 0:
            used = self.store.count(f"action:{kind}", hour_start(now))
            if used >= hourly_cap:
                return Decision(
                    False,
                    f"hourly cap reached for {kind} ({used}/{hourly_cap})",
                    wait_seconds=int(3600 - (now - hour_start(now))),
                    details={"used": used, "cap": hourly_cap},
                )

        if daily_cap > 0:
            used = self.store.count(f"action:{kind}", day_start(now))
            if used >= daily_cap:
                return Decision(
                    False,
                    f"daily cap reached for {kind} ({used}/{daily_cap})",
                    wait_seconds=int(86400 - (now - day_start(now))),
                    details={"used": used, "cap": daily_cap},
                )

        # Never repeat the same action against the same target within a day.
        if target:
            recent = self.store.count(f"action:{kind}", day_start(now), target)
            if recent > 0:
                return Decision(
                    False,
                    f"{kind} on {target} was already done today",
                    details={"target": target},
                    already_satisfied=True,
                )

        return Decision(True)

    def record_action(self, kind: str, target: str | None = None, detail: str | None = None) -> None:
        """Record a performed action against the caps and the audit trail."""
        self.store.record(f"action:{kind}", thread_id=target, detail=detail)
        self.store.record_action(kind, target=target, detail=detail)

    # ------------------------------------------------------------------- stats

    def action_stats(self) -> dict:
        """Counts of the visible actions performed, today."""
        now = time.time()
        stats: dict[str, int] = {}
        for kind in ("like", "unlike", "follow", "unfollow", "comment_reply", "story_reply", "story_post"):
            stats[kind] = self.store.count(f"action:{kind}", day_start(now))
        return stats

    def stats(self) -> dict:
        """Snapshot of current usage, for the CLI and the dashboard."""
        now = time.time()
        return {
            "replies_today": self.store.count(KIND_REPLY, day_start(now)),
            "replies_this_hour": self.store.count(KIND_REPLY, hour_start(now)),
            "inbound_today": self.store.count(KIND_INBOUND, day_start(now)),
            "global_daily_cap": self._global_daily_cap(),
            "daily_cap": self._daily_cap(),
            "hourly_cap": self._hourly_cap(),
            "cooldown_seconds": self._cooldown(),
            "baseline_only": bool(self.cfg.get("behaviour.baseline_only", False)),
        }

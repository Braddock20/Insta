"""Risk control.

The honest truth about Instagram automation: you cannot make it zero risk.
What you *can* do is make the account behave so much like a person that the
systems that flag automation never see anything to flag. That is what this
module is for.

Six mechanisms, in order of how much they matter:

1. **A global gap between visible actions.** Every like, follow, story view
   and comment is separated from the previous one by a random pause of
   minutes, not seconds. A human account does not like forty posts in a row.
   This one rule removes more risk than every other rule combined.
2. **A burst limit.** At most a handful of visible actions in any short
   window, no matter what the per-kind caps allow.
3. **A daily ceiling on everything.** Per-kind caps let an account do 100
   likes plus 20 follows plus 80 story views — 200 visible actions, which is
   a lot. The ceiling cuts the total.
4. **A warm-up ramp.** A brand new account is allowed very little, and the
   budget grows over two weeks. An account that suddenly likes things at
   full speed on day one is exactly the profile the systems are looking for.
5. **Safe mode.** A rate-limit response, a forbidden response or a challenge
   switches the bot to read-only for an hour, with a longer cooldown if it
   happens again. The bot stops before it is stopped.
6. **Content de-duplication.** The same reply is never sent twice in a
   window, because a human does not paste the same sentence twelve times.

None of this makes the account safe in the abstract. It makes the *pattern*
unremarkable, which is the thing that actually gets accounts restricted.
"""

from __future__ import annotations

import hashlib
import random
import time
from dataclasses import dataclass
from typing import Any

from . import log
from .guardrails import Decision
from .store import Store, day_start, hour_start

log = log.get(__name__)

KIND_REQUEST = "risk:request"
KIND_REPLY = "risk:reply"
KIND_VISIBLE = "risk:visible"
KIND_DEDUPE = "risk:dedupe"


@dataclass
class RiskState:
    """Everything the guard knows about the account's current risk."""

    safe_mode: bool = False
    safe_reason: str = ""
    safe_until: float = 0.0
    breaches: int = 0
    last_visible_at: float = 0.0

    def as_dict(self) -> dict[str, Any]:
        return {
            "safe_mode": self.safe_mode,
            "safe_reason": self.safe_reason,
            "safe_until": self.safe_until,
            "breaches": self.breaches,
            "last_visible_at": self.last_visible_at,
        }


class RiskGuard:
    """Keeps the bot's behaviour unremarkable."""

    def __init__(self, cfg, store: Store):
        self.cfg = cfg
        self.store = store
        self.state = RiskState()

    # ------------------------------------------------------------- settings
    def _num(self, path: str, default: float) -> float:
        try:
            return float(self.cfg.get(path, default) or default)
        except (TypeError, ValueError):
            return default

    @property
    def request_budget_hour(self) -> int:
        return int(self._num("risk.requests_per_hour", 600))

    @property
    def daily_visible_ceiling(self) -> int:
        return int(self._num("risk.daily_visible_ceiling", 60))

    @property
    def burst_limit(self) -> int:
        return int(self._num("risk.burst_limit", 3))

    @property
    def burst_window(self) -> int:
        return int(self._num("risk.burst_window_seconds", 600))

    @property
    def min_gap(self) -> float:
        return self._num("risk.min_visible_gap_seconds", 20)

    @property
    def max_gap(self) -> float:
        return self._num("risk.max_visible_gap_seconds", 75)

    @property
    def dedupe_hours(self) -> int:
        return int(self._num("risk.dedupe_hours", 12))

    # -------------------------------------------------------------- warm-up
    def _account_age_days(self) -> int:
        """How long this bot has been running against this account."""
        first = self.store.kv_get("risk:first_seen")
        now = time.time()
        if not first:
            self.store.kv_set("risk:first_seen", str(now))
            return 0
        try:
            return int((now - float(first)) // 86400)
        except (TypeError, ValueError):
            return 0

    def warmup_factor(self) -> float:
        """How much of the budget the account is allowed today."""
        ramp = self.cfg.get("risk.warmup_days", [2, 6, 13]) or [2, 6, 13]
        age = self._account_age_days()
        try:
            first, second, third = (int(x) for x in ramp)
        except (TypeError, ValueError):
            first, second, third = 2, 6, 13
        if age < first:
            return 0.15
        if age < second:
            return 0.40
        if age < third:
            return 0.70
        return 1.0

    # ------------------------------------------------------------ safe mode
    def in_safe_mode(self) -> bool:
        if self.state.safe_mode and time.time() > self.state.safe_until:
            self.state.safe_mode = False
            self.state.safe_reason = ""
            log.info("safe mode over — the bot is active again")
        return self.state.safe_mode

    def enter_safe_mode(self, reason: str, minutes: float | None = None) -> None:
        """Stop everything except reading, for a while."""
        self.state.breaches += 1
        base = float(minutes if minutes is not None else self._num("risk.safe_mode_minutes", 60))
        # Repeat offences cost more time.
        multiplier = min(8, 2 ** (self.state.breaches - 1))
        wait = base * multiplier
        self.state.safe_mode = True
        self.state.safe_reason = reason
        self.state.safe_until = time.time() + wait * 60
        log.error(
            "safe mode for %.0f minutes: %s (breach %d)", wait, reason, self.state.breaches
        )
        self.store.record_activity("safe_mode", None, reason)

    # ------------------------------------------------------------- requests
    def before_request(self) -> None:
        """Called before every API call.

        Raises :class:`RiskStop` when the bot must not make the call.
        """
        if self.in_safe_mode():
            raise RiskStop(
                f"the bot is in safe mode for another "
                f"{int((self.state.safe_until - time.time()) / 60)} minutes "
                f"({self.state.safe_reason})"
            )
        used = self.store.count(KIND_REQUEST, hour_start())
        if self.request_budget_hour and used >= self.request_budget_hour:
            raise RiskStop(
                f"the hourly request budget is spent ({used}/{self.request_budget_hour}). "
                "The bot is resting until the top of the hour."
            )

    def note_request(self) -> None:
        self.store.record(KIND_REQUEST)

    def note_failure(self, exc: Exception) -> None:
        """Look at a failed call and decide whether it is a warning or a stop."""
        text = f"{type(exc).__name__}: {exc}".lower()
        if "please wait" in text or "429" in text or "rate limit" in text:
            self.enter_safe_mode("Instagram asked the bot to slow down")
        elif "challenge" in text or "checkpoint" in text or "suspicious" in text:
            self.enter_safe_mode(
                "Instagram asked for a verification (challenge or checkpoint)", minutes=240
            )
        elif "forbidden" in text or "403" in text:
            self.enter_safe_mode("Instagram refused the request")

    # ------------------------------------------------------- visible actions
    def _visible_today(self) -> int:
        return self.store.count(KIND_VISIBLE, day_start())

    def _visible_in_window(self) -> int:
        return self.store.count(KIND_VISIBLE, time.time() - self.burst_window)

    def check_reply(self, text: str | None = None) -> Decision:
        """May this DM reply go out right now?

        A reply is a conversation, not a public action, so the gap, the
        burst limit and the daily visible ceiling do not apply to it — a
        customer who wrote should not wait a minute for an answer. What
        still applies is safe mode, and never sending the same text twice
        in a row.
        """
        if self.in_safe_mode():
            return Decision(
                False,
                f"safe mode is on ({self.state.safe_reason})",
                wait_seconds=int(max(0, self.state.safe_until - time.time())),
            )

        if bool(self.cfg.get("behaviour.baseline_only", False)):
            return Decision(False, "baseline mode is on (no replies are sent)")

        if text:
            decision = self._check_duplicate(text)
            if not decision.allowed:
                return decision

        return Decision(True)

    def _check_duplicate(self, text: str) -> Decision:
        """True when this exact text went out recently."""
        digest = hashlib.sha256(text.strip().lower().encode("utf-8")).hexdigest()[:32]
        window = max(1, self.dedupe_hours) * 3600
        bucket = int(time.time() // window)
        for offset in (0, -1):
            if self.store.has_fingerprint(f"{digest}:{bucket + offset}"):
                return Decision(
                    False,
                    "the same text was already sent recently — sending it twice "
                    "looks automated",
                    already_satisfied=True,
                )
        return Decision(True)

    def record_reply(self, text: str | None = None, target: str | None = None) -> None:
        """Record a reply that went out, and remember its text."""
        self.store.record(KIND_REPLY, thread_id=target, detail="reply")
        if text:
            digest = hashlib.sha256(text.strip().lower().encode("utf-8")).hexdigest()[:32]
            window = max(1, self.dedupe_hours) * 3600
            bucket = int(time.time() // window)
            self.store.record(
                KIND_DEDUPE,
                fingerprint=f"{digest}:{bucket}",
                detail=f"reply:{target or ''}",
            )

    def check_visible(self, kind: str, target: str | None = None, text: str | None = None) -> Decision:
        """May this visible action happen right now?"""
        if self.in_safe_mode():
            return Decision(
                False,
                f"safe mode is on ({self.state.safe_reason})",
                wait_seconds=int(max(0, self.state.safe_until - time.time())),
            )

        if bool(self.cfg.get("behaviour.baseline_only", False)):
            return Decision(False, "baseline mode is on (no actions are performed)")

        # 1. The gap since the last visible action.
        gap_min = self.min_gap
        gap_max = max(self.max_gap, gap_min)
        if self.state.last_visible_at:
            elapsed = time.time() - self.state.last_visible_at
            if elapsed < gap_min:
                wait = int(gap_min - elapsed)
                return Decision(
                    False,
                    f"waiting {wait}s before the next visible action",
                    wait_seconds=wait,
                )

        # 2. The burst limit.
        if self.burst_limit and self._visible_in_window() >= self.burst_limit:
            return Decision(
                False,
                f"the burst limit is reached "
                f"({self._visible_in_window()}/{self.burst_limit} in {self.burst_window // 60} min)",
                wait_seconds=self.burst_window,
            )

        # 3. The daily ceiling, with the warm-up ramp applied.
        ceiling = max(1, int(self.daily_visible_ceiling * self.warmup_factor()))
        if self._visible_today() >= ceiling:
            return Decision(
                False,
                f"today's visible-action budget is spent "
                f"({self._visible_today()}/{ceiling})",
                wait_seconds=int(86400 - (time.time() - day_start())),
            )

        # 4. Content de-duplication.
        if text:
            decision = self._check_duplicate(text)
            if not decision.allowed:
                return decision
            digest = hashlib.sha256(text.strip().lower().encode("utf-8")).hexdigest()[:32]
            window = max(1, self.dedupe_hours) * 3600
            bucket = int(time.time() // window)
            self.store.record(
                KIND_DEDUPE,
                fingerprint=f"{digest}:{bucket}",
                detail=f"{kind}:{target or ''}",
            )

        return Decision(True)

    def record_visible(self, kind: str, target: str | None = None, detail: str | None = None) -> None:
        """Record a visible action that actually happened."""
        self.store.record(KIND_VISIBLE, thread_id=target, detail=f"{kind}:{detail or ''}")
        self.state.last_visible_at = time.time()

    def wait_for_gap(self) -> None:
        """Sleep the remaining part of the global gap. Called before acting."""
        if not self.state.last_visible_at:
            return
        elapsed = time.time() - self.state.last_visible_at
        if elapsed < self.min_gap:
            time.sleep(self.min_gap - elapsed)

    # --------------------------------------------------------------- status
    def score(self) -> dict[str, Any]:
        """A snapshot for the ``/risk`` command."""
        age = self._account_age_days()
        return {
            "safe_mode": self.in_safe_mode(),
            "safe_reason": self.state.safe_reason,
            "breaches": self.state.breaches,
            "visible_today": self._visible_today(),
            "replies_today": self.store.count(KIND_REPLY, day_start()),
            "daily_ceiling": max(1, int(self.daily_visible_ceiling * self.warmup_factor())),
            "full_ceiling": self.daily_visible_ceiling,
            "warmup_factor": self.warmup_factor(),
            "account_age_days": age,
            "requests_this_hour": self.store.count(KIND_REQUEST, hour_start()),
            "request_budget": self.request_budget_hour,
            "burst_used": self._visible_in_window(),
            "burst_limit": self.burst_limit,
            "gap_seconds": [self.min_gap, self.max_gap],
            "dedupe_hours": self.dedupe_hours,
        }

    def reset(self) -> None:
        """Leave safe mode by hand."""
        self.state.safe_mode = False
        self.state.safe_reason = ""
        self.state.safe_until = 0.0
        self.state.breaches = 0


class RiskStop(RuntimeError):
    """Raised when the bot must not make an API call right now."""

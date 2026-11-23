"""Automation rules — what the bot does when something happens.

A rule has three parts:

``trigger``
    The kind of event that starts it. Right now that is an **activity**
    (``follow``, ``like``, ``comment``, ``mention``) or a **message**.

``action``
    What to do about it: ``follow_back``, ``follow``, ``unfollow``,
    ``like_post``, ``like_comment``, ``reply_dm``, ``reply_story``.

``params``
    The details — which post to like, what to say.

The rules live in ``config.yaml``:

.. code-block:: yaml

    automations:
      enabled: true
      rules:
        - name: follow_back
          trigger: follow
          action: follow_back
          delay_seconds: 120
        - name: like_commenters
          trigger: comment
          action: like_comment
          params:
            post_url: "https://www.instagram.com/p/ABC123/"

Nothing here acts immediately. A rule *queues* a task, and the task runs on a
later pass — after a human-ish delay, and after the guardrails have agreed.
That is what keeps the account from looking like a machine: the reply is not
instantaneous, and the number of visible actions per hour is capped.

Every rule can be switched off individually, and ``automations.enabled: false``
switches off the lot — which is the same "baseline first" stance the rest of
the bot takes.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Callable

from . import log
from .notifications import ActivityItem
from .scheduler import Task

log = log.get(__name__)


@dataclass
class Rule:
    """One automation rule."""

    name: str
    trigger: str
    action: str
    params: dict = field(default_factory=dict)
    delay_seconds: float = 120.0
    enabled: bool = True
    max_per_day: int = 0

    @classmethod
    def from_config(cls, entry: dict) -> "Rule | None":
        if not isinstance(entry, dict):
            return None
        name = str(entry.get("name") or "")
        trigger = str(entry.get("trigger") or entry.get("when") or "").strip().lower()
        action = str(entry.get("action") or entry.get("do") or "").strip().lower()
        if not trigger or not action:
            return None
        params = entry.get("params") or entry.get("payload") or {}
        return cls(
            name=name or f"{trigger}->{action}",
            trigger=trigger,
            action=action,
            params=params if isinstance(params, dict) else {},
            delay_seconds=float(entry.get("delay_seconds", 120) or 0),
            enabled=bool(entry.get("enabled", True)),
            max_per_day=int(entry.get("max_per_day", 0) or 0),
        )


class AutomationEngine:
    """Turns events into queued tasks, and queued tasks into actions."""

    def __init__(
        self,
        cfg,
        scheduler,
        dom=None,
        profile_bot=None,
        like_bot=None,
        story_bot=None,
        notifications=None,
        rulebook=None,
        guardrails=None,
        ai=None,
    ):
        self.cfg = cfg
        self.scheduler = scheduler
        self.dom = dom
        self.profile_bot = profile_bot
        self.like_bot = like_bot
        self.story_bot = story_bot
        self.notifications = notifications
        self.rulebook = rulebook
        self.guardrails = guardrails
        self.ai = ai

    # ------------------------------------------------------------------ rules

    @property
    def enabled(self) -> bool:
        return bool(self.cfg.get("automations.enabled", False))

    def rules(self) -> list[Rule]:
        """Every configured rule, valid ones only."""
        raw = self.cfg.get("automations.rules", []) or []
        out: list[Rule] = []
        for entry in raw:
            rule = Rule.from_config(entry)
            if rule is not None:
                out.append(rule)
        return out

    # -------------------------------------------------------------- reacting

    def react_to_activity(self, items: list[ActivityItem]) -> list[int]:
        """Queue a task for every rule that matches one of the activity items."""
        if not self.enabled:
            return []
        queued: list[int] = []
        for item in items:
            for rule in self.rules():
                if not rule.enabled or rule.trigger != item.kind:
                    continue
                task_id = self.scheduler.enqueue(
                    f"auto:{rule.action}",
                    payload={
                        "rule": rule.name,
                        "username": item.username,
                        "activity_text": item.text,
                        **rule.params,
                    },
                    delay_seconds=rule.delay_seconds,
                    fingerprint=f"rule:{rule.name}:{item.fingerprint}",
                )
                if task_id is not None:
                    queued.append(task_id)
                    log.info("rule %r queued for @%s", rule.name, item.username)
        return queued

    def react_to_message(self, username: str, text: str) -> list[int]:
        """Queue a task for every rule that matches an inbound message."""
        if not self.enabled:
            return []
        queued: list[int] = []
        for rule in self.rules():
            if not rule.enabled or rule.trigger not in ("message", "dm"):
                continue
            if not self._message_matches(rule, text):
                continue
            task_id = self.scheduler.enqueue(
                f"auto:{rule.action}",
                payload={"rule": rule.name, "username": username, "message": text, **rule.params},
                delay_seconds=rule.delay_seconds,
                fingerprint=f"rule:{rule.name}:msg:{username}:{text[:60]}",
            )
            if task_id is not None:
                queued.append(task_id)
        return queued

    def _message_matches(self, rule: Rule, text: str) -> bool:
        """Does a message rule's keyword list match the message?"""
        keywords = rule.params.get("match") or rule.params.get("keywords") or []
        if isinstance(keywords, str):
            keywords = [keywords]
        if not keywords:
            return False
        haystack = (text or "").lower()
        return any(str(k).strip().lower() in haystack for k in keywords)

    # --------------------------------------------------------------- running

    def handlers(self) -> dict[str, Callable[[Task], Any]]:
        """The action implementations, keyed by task kind."""
        return {
            "auto:follow_back": self._do_follow_back,
            "auto:follow": self._do_follow,
            "auto:unfollow": self._do_unfollow,
            "auto:like_post": self._do_like_post,
            "auto:like_comment": self._do_like_comment,
            "auto:reply_dm": self._do_reply_dm,
            "auto:reply_story": self._do_reply_story,
        }

    def _do_follow_back(self, task: Task):
        if self.profile_bot is None:
            return False, "no profile bot available"
        username = str(task.payload.get("username") or "")
        result = self.profile_bot.follow(username)
        return result.ok, result.message

    def _do_follow(self, task: Task):
        if self.profile_bot is None:
            return False, "no profile bot available"
        username = str(task.payload.get("username") or "")
        result = self.profile_bot.follow(username)
        return result.ok, result.message

    def _do_unfollow(self, task: Task):
        if self.profile_bot is None:
            return False, "no profile bot available"
        username = str(task.payload.get("username") or "")
        result = self.profile_bot.unfollow(username)
        return result.ok, result.message

    def _do_like_post(self, task: Task):
        if self.like_bot is None:
            return False, "no like bot available"
        url = str(task.payload.get("post_url") or task.payload.get("url") or "")
        if not url:
            return False, "no post url in the task"
        result = self.like_bot.like_post(url)
        return result.ok, result.message

    def _do_like_comment(self, task: Task):
        if self.like_bot is None:
            return False, "no like bot available"
        url = str(task.payload.get("post_url") or task.payload.get("url") or "")
        comment = str(task.payload.get("comment_text") or task.payload.get("activity_text") or "")
        result = self.like_bot.like_comment(url, comment)
        return result.ok, result.message

    def _do_reply_dm(self, task: Task):
        if self.dom is None:
            return False, "no DOM helper available"
        username = str(task.payload.get("username") or "")
        text = str(task.payload.get("reply") or "")
        if not text:
            text = self._reply_text_for(task)
        if not text:
            return False, "no reply text for the task"
        if not username:
            return False, "no username for the task"

        info = self.dom.open_thread_with(username)
        if not info.thread_key:
            return False, f"could not open a DM thread with @{username}"

        before = len(self.dom.read_messages())
        if not self.dom.type_message(text):
            return False, "could not type the reply"
        if not self.dom.click_send():
            return False, "no send button appeared"
        confirmed, reason = self.dom.wait_for_send_confirmation(text, before)
        if confirmed:
            if self.guardrails is not None:
                self.guardrails.record_action("comment_reply", username, text[:80])
            return True, f"replied ({reason})"
        return False, f"could not be confirmed: {reason}"

    def _do_reply_story(self, task: Task):
        if self.story_bot is None:
            return False, "no story bot available"
        username = str(task.payload.get("username") or "")
        text = str(task.payload.get("reply") or "")
        if not text:
            text = self._reply_text_for(task)
        result = self.story_bot.reply_to_story(username, text)
        return result.ok, result.message

    def _reply_text_for(self, task: Task) -> str:
        """Work out what to say: a fixed reply, else the rulebook, else the AI."""
        fixed = str(task.payload.get("reply") or "")
        if fixed:
            return fixed
        message = str(task.payload.get("message") or task.payload.get("activity_text") or "")
        if self.rulebook is not None:
            default = str(self.cfg.get("reply.default_reply", "") or "")
            text, _ = self.rulebook.reply_for(message, default)
            if text:
                return text
        return default_text(self.cfg)

    # ------------------------------------------------------------------- tick

    def tick(self) -> dict[str, Any]:
        """One pass: look for new activity, queue the matching tasks."""
        summary = {"activity": 0, "queued": 0, "rules": len(self.rules())}
        if not self.enabled or self.notifications is None:
            return summary

        try:
            fresh = self.notifications.new_items()
        except Exception as exc:  # pragma: no cover - network dependent
            log.warning("could not read the activity feed: %s", exc)
            return summary

        summary["activity"] = len(fresh)
        summary["queued"] = len(self.react_to_activity(fresh))
        return summary


def default_text(cfg) -> str:
    return str(cfg.get("reply.default_reply", "") or "")


def _now() -> float:  # pragma: no cover - test seam
    return time.time()

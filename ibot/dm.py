"""The inbox loop.

This is the heart of the bot: it reads the direct inbox over the API, works
out which messages are new, and answers them.

Three things make it safe to leave running unattended:

* **Fingerprints.** Every inbound message is hashed and stored, so a restart
  never answers the same message twice — the reply is not lost when the
  process dies mid-conversation, and a message is not answered twice when
  Instagram returns the same thread on the next poll.

* **Guardrails.** Cooldowns, hourly and daily caps, and a randomised reply
  delay. A person does not reply to five messages in four seconds, and a
  bot that does looks like a bot.

* **Confirm before counting.** A reply only counts once Instagram confirms
  it. A failed send neither burns the quota nor starts the cooldown, so the
  next poll retries it.
"""

from __future__ import annotations

import hashlib
import queue
import random
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field

from . import log
from .client.base import Message, Thread
from .guardrails import Guardrails
from .rules import Rulebook

log = log.get(__name__)


@dataclass
class PollResult:
    """What one pass over the inbox produced."""

    threads_seen: int = 0
    inbound: int = 0
    replied: int = 0
    blocked: int = 0
    failed: int = 0
    skipped: int = 0
    commands: int = 0
    errors: list[str] = field(default_factory=list)
    lock: threading.Lock = field(default_factory=threading.Lock, repr=False, compare=False)

    def summary(self) -> str:
        return (
            f"threads={self.threads_seen} inbound={self.inbound} replied={self.replied} "
            f"blocked={self.blocked} failed={self.failed} skipped={self.skipped} "
            f"commands={self.commands}"
        )


def fingerprint_for(thread_id: str, sender: str, message_id: str, text: str) -> str:
    """Stable identity of an inbound message.

    The message id is part of the hash, so the same text sent twice is two
    different messages — but the same message seen twice is one message.
    """
    raw = f"{thread_id}|{sender}|{message_id}|{text}".encode("utf-8")
    return hashlib.sha256(raw).hexdigest()[:32]


class DMBot:
    """Polls the inbox and answers messages."""

    def __init__(
        self,
        client,
        cfg,
        store,
        guardrails: Guardrails,
        rulebook: Rulebook,
        ai=None,
        command_router=None,
        risk=None,
    ):
        self.client = client
        self.cfg = cfg
        self.store = store
        self.guardrails = guardrails
        self.rulebook = rulebook
        self.ai = ai
        self.commands = command_router
        self.risk = risk
        self._marker = str(self.cfg.get("reply.marker", "") or "")
        # Set when a message arrives over the push channel, so the poll loop
        # can wake up immediately instead of waiting for the next tick.
        self.wake = threading.Event()
        self.pushed = 0
        # Pushed messages are handled by a worker so that the reply delay
        # never blocks the push channel from reading.
        self._push_queue: queue.Queue = queue.Queue()
        self._worker: threading.Thread | None = None
        self._push_lock = threading.Lock()
        self._push_done = 0

    # -------------------------------------------------------------- helpers
    def _quiet_until(self) -> float:
        """A quiet period set by ``/quiet``."""
        if self.store is None:
            return 0.0
        try:
            return float(self.store.kv_get("quiet_until", 0) or 0)
        except (TypeError, ValueError):
            return 0.0

    def _delay(self) -> int:
        """How long to wait before answering. Humans are not instant."""
        low = int(self.cfg.get("behaviour.reply_delay_min", 12) or 0)
        high = int(self.cfg.get("behaviour.reply_delay_max", 55) or 0)
        if high < low:
            high = low
        if high == 0:
            return 0
        return random.randint(low, high)

    def _decorate(self, text: str) -> str:
        return f"{self._marker} {text}".strip() if self._marker else text

    def _own_id(self) -> str:
        try:
            return self.client.own_user_id
        except Exception:  # noqa: BLE001
            return ""

    # ---------------------------------------------------------------- prime
    def prime(self) -> int:
        """Record everything in the inbox as seen without answering anything.

        Run this on the first start so the bot does not reply to a backlog
        of messages from before it existed.
        """
        count = 0
        for thread in self._threads():
            for msg in thread.messages:
                if msg.is_self:
                    continue
                fp = fingerprint_for(thread.id, msg.user_id, msg.id, msg.text)
                if self.guardrails.record_inbound(thread.id, fp, detail="primed"):
                    count += 1
        log.info("primed %d messages", count)
        return count

    def _threads(self) -> list[Thread]:
        """The conversations to read.

        Message requests are only read when ``accept_requests`` is on. A
        request has no composer until it is accepted, so there is nowhere to
        send the answer — and skipping them entirely means the message is
        still waiting, unanswered, on the day you decide to accept.
        """
        amount = int(self.cfg.get("behaviour.thread_count", 25) or 25)
        threads = self.client.threads(amount=amount)
        if bool(self.cfg.get("behaviour.read_requests", True)):
            try:
                pending = self.client.pending_threads(amount=10)
            except Exception as exc:  # noqa: BLE001
                log.warning("could not read the request inbox: %s", exc)
                pending = []
            if bool(self.cfg.get("behaviour.accept_requests", False)):
                threads += pending
            else:
                for t in pending:
                    log.info(
                        "message request from %s is waiting (accept_requests is off)",
                        t.peer,
                    )
        return threads

    # ----------------------------------------------------------------- poll
    def poll_once(self) -> PollResult:
        """One pass over the inbox."""
        result = PollResult()
        own_id = self._own_id()

        try:
            threads = self._threads()
        except Exception as exc:  # noqa: BLE001
            result.errors.append(f"inbox: {exc}")
            log.error("could not read the inbox: %s", exc)
            return result

        result.threads_seen = len(threads)

        # Commands go to you, customers go to customers. The commands are
        # independent of each other, so they run at the same time — that is
        # the difference between a command landing in a second and a
        # command landing after the whole inbox has been read.
        commands: list[tuple[Thread, Message]] = []
        for thread in threads:
            for msg in sorted(thread.messages, key=lambda m: m.timestamp or 0):
                if msg.is_self or (own_id and msg.user_id == own_id):
                    commands.append((thread, msg))
                else:
                    self._handle_message(thread, msg, result)

        if commands:
            def run_command(item: tuple[Thread, Message]) -> None:
                thread, msg = item
                try:
                    self._handle_command(thread, msg, result)
                except Exception as exc:  # noqa: BLE001
                    log.error("command %r failed: %s", msg.text, exc)

            workers = int(self.cfg.get("speed.commands_parallel", 8) or 8)
            if workers > 1 and len(commands) > 1:
                with ThreadPoolExecutor(max_workers=min(workers, len(commands))) as pool:
                    list(pool.map(run_command, commands))
            else:
                for item in commands:
                    run_command(item)

        return result

    # ------------------------------------------------------------- handlers
    def handle_realtime(self, push) -> None:
        """Queue a message that arrived over the push channel.

        The push payload already carries the text, the thread and the
        sender, so the bot can answer without reading the inbox at all.

        The work is handed to a worker thread rather than done here: the
        reply delay means this call can take a minute, and the push
        channel must keep reading while it waits.
        """
        self.pushed += 1
        self.wake.set()
        self._ensure_worker()
        self._push_queue.put(push)

    def drain(self, timeout: float = 2.0) -> None:
        """Wait until everything that arrived over the push channel is done.

        The push channel answers in a worker thread, so anything that wants
        to see the answer has to wait for it.
        """
        deadline = time.time() + timeout
        while time.time() < deadline:
            with self._push_lock:
                if self._push_done >= self.pushed:
                    return
            time.sleep(0.01)

    def _ensure_worker(self) -> None:
        if self._worker is not None and self._worker.is_alive():
            return
        self._worker = threading.Thread(target=self._push_worker, name="push-worker", daemon=True)
        self._worker.start()

    def _push_worker(self) -> None:
        while True:
            push = self._push_queue.get()
            try:
                self._process_push(push)
            except Exception as exc:  # noqa: BLE001
                log.exception("the push handler failed: %s", exc)

    def _process_push(self, push) -> None:
        try:
            self._handle_push(push)
        finally:
            with self._push_lock:
                self._push_done += 1

    def _handle_push(self, push) -> None:
        # The push may or may not have flagged the message as ours, so the
        # account id is checked as well.
        own_id = self._own_id()
        is_self = bool(push.is_self) or bool(own_id and push.user_id == own_id)

        if is_self:
            # A command sent to yourself.
            thread = Thread(id=push.thread_id, title="", usernames=[], user_ids=[])
            msg = Message(
                id=push.message_id,
                thread_id=push.thread_id,
                text=push.text,
                user_id=push.user_id,
                username="",
                timestamp=push.timestamp,
                is_self=True,
            )
            result = PollResult()
            self._handle_command(thread, msg, result)
            return

        thread = Thread(id=push.thread_id, title="", usernames=[], user_ids=[])
        msg = Message(
            id=push.message_id,
            thread_id=push.thread_id,
            text=push.text,
            user_id=push.user_id,
            username="",
            timestamp=push.timestamp,
            is_self=False,
        )
        result = PollResult()
        self._handle_message(thread, msg, result)

    def _handle_message(self, thread: Thread, msg: Message, result: PollResult) -> None:
        fp = fingerprint_for(thread.id, msg.user_id, msg.id, msg.text)
        if not self.guardrails.record_inbound(thread.id, fp, detail=msg.text[:120]):
            result.skipped += 1
            return
        result.inbound += 1

        if bool(self.cfg.get("behaviour.baseline_only", False)):
            log.info("baseline: %s said %r", thread.peer, msg.text[:120])
            return

        if self._quiet_until() > time.time():
            log.info("quiet period: not answering %r", msg.text[:60])
            return

        decision = self.guardrails.check(thread.id)
        if not decision.allowed:
            result.blocked += 1
            log.info("not answering %s: %s", thread.peer, decision.reason)
            return

        reply, source = self._choose_reply(msg.text)
        if not reply:
            result.skipped += 1
            return

        # The risk guard has the last word: it knows about the global gap
        # between visible actions, the daily ceiling, the warm-up ramp and
        # whether the same text went out recently.
        if self.risk is not None:
            risk_decision = self.risk.check_reply(text=reply)
            if not risk_decision.allowed:
                result.blocked += 1
                log.info("not answering %s: %s", thread.peer, risk_decision.reason)
                return

        delay = self._delay()
        if delay:
            time.sleep(delay)

        sent = self.client.send_text(thread.id, self._decorate(reply))
        if not sent.ok:
            result.failed += 1
            result.errors.append(f"send failed: {sent.detail}")
            log.error("could not answer %s: %s", thread.peer, sent.detail)
            return

        # Only now does the reply count.
        self.guardrails.record_reply(thread.id, detail=source)
        if self.risk is not None:
            self.risk.record_reply(text=reply, target=thread.id)
        result.replied += 1
        log.info("answered %s via %s", thread.peer, source)

        try:
            self.client.mark_seen(thread.id)
        except Exception:  # noqa: BLE001
            pass

    def _handle_command(self, thread: Thread, msg: Message, result: PollResult) -> None:
        """A message from yourself is a command, not a customer message."""
        if self.commands is None:
            return
        if not msg.text.strip().startswith("/"):
            return
        fp = fingerprint_for(thread.id, msg.user_id, msg.id, msg.text)
        if not self.guardrails.record_inbound(thread.id, fp, detail="command"):
            result.skipped += 1
            return
        try:
            answer = self.commands.run(msg.text, thread)
        except Exception as exc:  # noqa: BLE001
            log.error("command %r failed: %s", msg.text, exc)
            answer = f"❌ that command failed: {exc}"
        with result.lock:
            result.commands += 1
        if answer:
            self.client.send_text(thread.id, self._decorate(answer))

    # --------------------------------------------------------------- replies
    def _choose_reply(self, text: str) -> tuple[str, str]:
        """Pick the answer: the rulebook first, then the AI, then the default."""
        reply, source = self.rulebook.reply_for(
            text, str(self.cfg.get("reply.default_reply", "") or "")
        )
        if reply:
            return reply, source

        if self.ai is not None:
            try:
                ai_result = self.ai.generate(text)
                ai_reply = getattr(ai_result, "text", "") or ""
            except Exception as exc:  # noqa: BLE001
                log.warning("the AI backend failed: %s", exc)
                ai_reply = ""
            if ai_reply:
                return ai_reply, "ai"

        return "", "none"

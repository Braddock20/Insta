"""The new commands and the push-answer path."""

from __future__ import annotations

from ibot.commands import CommandRouter
from ibot.dm import DMBot
from ibot.protocol import REGISTRY
from ibot.realtime import RealtimeMessage
from ibot.risk import RiskGuard


def _router(client, cfg, store=None, guardrails=None, risk=None, protocol=None, realtime=None):
    return CommandRouter(
        client,
        cfg,
        store=store,
        guardrails=guardrails,
        session=None,
        risk=risk,
        protocol=protocol,
        realtime=realtime,
    )


# ------------------------------------------------------------------ commands
def test_the_risk_command_shows_the_budget(client, cfg, store):
    risk = RiskGuard(cfg, store)
    answer = _router(client, cfg, store=store, risk=risk).run("/risk")
    assert "visible today" in answer
    assert "warm-up" in answer


def test_the_protocol_command_shows_the_identity(client, cfg):
    class FakeProtocol:
        state = type("S", (), {"identity": REGISTRY[0], "working": True, "detail": "ok"})()

    answer = _router(client, cfg, protocol=FakeProtocol()).run("/protocol")
    assert REGISTRY[0].version in answer


def test_block_and_unblock(client, cfg):
    router = _router(client, cfg)
    assert "blocked" in router.run("/block badactor")
    assert "badactor" in client.blocked
    assert "unblocked" in router.run("/unblock badactor")
    assert "badactor" not in client.blocked


def test_the_mute_commands_work(client, cfg):
    router = _router(client, cfg)
    assert "muted" in router.run("/muteposts noisy")
    assert "back" in router.run("/unmuteposts noisy")


def test_search_finds_accounts(client, cfg):
    answer = _router(client, cfg).run("/search alice")
    assert "alice" in answer


def test_quiet_silences_the_bot(client, cfg, store):
    router = _router(client, cfg, store=store)
    assert "no visible actions" in router.run("/quiet 30")
    assert float(store.kv_get("quiet_until")) > 0


def test_resume_clears_the_quiet_period(client, cfg, store):
    router = _router(client, cfg, store=store)
    router.run("/quiet 30")
    router.run("/resume")
    assert float(store.kv_get("quiet_until")) == 0


def test_the_realtime_command_reports_the_channel(client, cfg):
    class FakeBridge:
        def health(self):
            return {"realtime": "connected", "messages": 3}

    answer = _router(client, cfg, realtime=FakeBridge()).run("/realtime")
    assert "connected" in answer


def test_the_new_commands_are_all_registered(client, cfg):
    router = _router(client, cfg)
    for name in ("risk", "protocol", "realtime", "block", "search", "quiet", "resume", "backup"):
        assert name in router.names()


def test_help_lists_the_new_groups(client, cfg):
    answer = _router(client, cfg).run("/help")
    assert "/risk" in answer
    assert "/block" in answer


# --------------------------------------------------------------- push answer
def test_a_pushed_message_is_answered_without_reading_the_inbox(
    client, cfg, store, guardrails, rulebook
):
    bot = DMBot(client, cfg, store, guardrails, rulebook)
    push = RealtimeMessage(
        thread_id="t1",
        message_id="m1",
        user_id="2",
        text="hello",
        timestamp=0.0,
    )
    bot.handle_realtime(push)
    bot.drain()
    assert client.sent == [("t1", "Hi there!")]


def test_a_pushed_message_is_only_answered_once(client, cfg, store, guardrails, rulebook):
    bot = DMBot(client, cfg, store, guardrails, rulebook)
    push = RealtimeMessage(thread_id="t1", message_id="m1", user_id="2", text="hello")
    bot.handle_realtime(push)
    bot.handle_realtime(push)
    bot.drain()
    assert len(client.sent) == 1


def test_the_poll_loop_wakes_up_on_a_push(client, cfg, store, guardrails, rulebook):
    bot = DMBot(client, cfg, store, guardrails, rulebook)
    assert not bot.wake.is_set()
    bot.handle_realtime(RealtimeMessage(thread_id="t1", message_id="m1", user_id="2", text="hello"))
    assert bot.wake.is_set()
    bot.drain()


def test_a_pushed_command_is_answered(client, cfg, store, guardrails, rulebook):
    router = _router(client, cfg, store=store)
    bot = DMBot(client, cfg, store, guardrails, rulebook, command_router=router)
    push = RealtimeMessage(thread_id="t3", message_id="m9", user_id="1", text="/whoami")
    bot.handle_realtime(push)
    # The push is answered by a worker thread, so wait for it.
    bot.drain()
    assert client.sent
    assert "@me" in client.sent[-1][1]


def test_the_risk_guard_can_stop_a_push_answer(client, cfg, store, guardrails, rulebook):
    risk = RiskGuard(cfg, store)
    risk.enter_safe_mode("test", minutes=10)
    bot = DMBot(client, cfg, store, guardrails, rulebook, risk=risk)
    bot.handle_realtime(RealtimeMessage(thread_id="t1", message_id="m1", user_id="2", text="hello"))
    bot.drain()
    assert client.sent == []

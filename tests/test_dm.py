"""The inbox loop."""

from __future__ import annotations

from ibot.dm import DMBot, fingerprint_for


def _bot(client, cfg, store, guardrails, rulebook, router=None):
    return DMBot(client, cfg, store, guardrails, rulebook, None, command_router=router)


def test_new_message_is_answered(client, cfg, store, guardrails, rulebook):
    bot = _bot(client, cfg, store, guardrails, rulebook)
    result = bot.poll_once()
    assert result.inbound == 1
    assert result.replied == 1
    assert client.sent == [("t1", "Hi there!")]


def test_the_same_message_is_never_answered_twice(client, cfg, store, guardrails, rulebook):
    bot = _bot(client, cfg, store, guardrails, rulebook)
    bot.poll_once()
    result = bot.poll_once()
    assert result.inbound == 0
    assert result.replied == 0
    assert len(client.sent) == 1


def test_a_new_message_in_the_same_chat_is_answered(client, cfg, store, guardrails, rulebook):
    bot = _bot(client, cfg, store, guardrails, rulebook)
    bot.poll_once()
    client.add_message("t1", "hello again")
    result = bot.poll_once()
    assert result.replied == 1
    assert len(client.sent) == 2


def test_baseline_mode_answers_nothing(client, cfg, store, guardrails, rulebook):
    cfg.set("behaviour.baseline_only", True)
    bot = _bot(client, cfg, store, guardrails, rulebook)
    result = bot.poll_once()
    assert result.inbound == 1
    assert result.replied == 0
    assert client.sent == []


def test_the_default_reply_is_used_when_no_rule_matches(client, cfg, store, guardrails, rulebook):
    cfg.set("reply.default_reply", "We got your message.")
    client.add_message("t1", "something entirely different")
    bot = _bot(client, cfg, store, guardrails, rulebook)
    bot.poll_once()
    assert ("t1", "We got your message.") in client.sent


def test_priming_records_without_answering(client, cfg, store, guardrails, rulebook):
    bot = _bot(client, cfg, store, guardrails, rulebook)
    # Only the conversation that can be answered is primed; the request is
    # left alone so it can still be answered once it is accepted.
    assert bot.prime() == 1
    result = bot.poll_once()
    assert result.replied == 0
    assert client.sent == []


def test_a_blocked_reply_is_not_sent(client, cfg, store, guardrails, rulebook):
    cfg.set("behaviour.cooldown_seconds", 900)
    cfg.set("behaviour.hourly_cap", 1)
    bot = _bot(client, cfg, store, guardrails, rulebook)
    client.add_message("t1", "hello")
    result = bot.poll_once()
    # The first answer goes out, the second is inside the hourly cap.
    assert result.replied == 1
    assert result.blocked == 1


def test_a_message_request_is_not_answered(client, cfg, store, guardrails, rulebook):
    # Bob wrote first, so his message is a request. There is no composer
    # until it is accepted, so the bot leaves it alone.
    bot = _bot(client, cfg, store, guardrails, rulebook)
    result = bot.poll_once()
    assert result.inbound == 1
    assert all(thread_id != "t2" for thread_id, _ in client.sent)


def test_a_message_request_is_answered_when_requests_are_accepted(
    client, cfg, store, guardrails, rulebook
):
    cfg.set("behaviour.accept_requests", True)
    bot = _bot(client, cfg, store, guardrails, rulebook)
    result = bot.poll_once()
    assert result.inbound == 2
    assert ("t2", "Here are our prices.") in client.sent


def test_a_failed_send_does_not_burn_the_quota(client, cfg, store, guardrails, rulebook):
    cfg.set("behaviour.hourly_cap", 1)
    client.fail_next = RuntimeError("network down")
    bot = _bot(client, cfg, store, guardrails, rulebook)
    result = bot.poll_once()
    assert result.failed == 1
    assert result.replied == 0
    assert guardrails.stats()["replies_today"] == 0


def test_the_marker_is_added_to_answers(client, cfg, store, guardrails, rulebook):
    cfg.set("reply.marker", "🤖")
    bot = _bot(client, cfg, store, guardrails, rulebook)
    bot.poll_once()
    assert client.sent[0][1] == "🤖 Hi there!"


def test_fingerprints_differ_per_message_but_are_stable():
    a = fingerprint_for("t1", "u1", "m1", "hello")
    b = fingerprint_for("t1", "u1", "m2", "hello")
    assert a != b
    assert a == fingerprint_for("t1", "u1", "m1", "hello")

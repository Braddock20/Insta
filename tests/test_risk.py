"""The risk controls."""

from __future__ import annotations

import time

from ibot.risk import RiskGuard


def _guard(cfg, store):
    return RiskGuard(cfg, store)


def test_the_first_visible_action_is_allowed(cfg, store):
    g = _guard(cfg, store)
    assert g.check_visible("like", target="alice").allowed


def test_a_second_action_inside_the_gap_is_blocked(cfg, store):
    cfg.set("risk.min_visible_gap_seconds", 60)
    g = _guard(cfg, store)
    g.record_visible("like", target="alice")
    decision = g.check_visible("like", target="bob")
    assert not decision.allowed
    assert "waiting" in decision.reason


def test_the_gap_expires(cfg, store):
    cfg.set("risk.min_visible_gap_seconds", 1)
    g = _guard(cfg, store)
    g.record_visible("like", target="alice")
    g.state.last_visible_at = time.time() - 5
    assert g.check_visible("like", target="bob").allowed


def test_the_burst_limit_stops_a_burst(cfg, store):
    cfg.set("risk.burst_limit", 2)
    cfg.set("risk.min_visible_gap_seconds", 0)
    g = _guard(cfg, store)
    g.record_visible("like", target="a")
    g.record_visible("like", target="b")
    g.state.last_visible_at = time.time() - 100
    decision = g.check_visible("like", target="c")
    assert not decision.allowed
    assert "burst" in decision.reason


def test_the_daily_ceiling_stops_the_day(cfg, store):
    cfg.set("risk.daily_visible_ceiling", 2)
    cfg.set("risk.min_visible_gap_seconds", 0)
    g = _guard(cfg, store)
    g.record_visible("like", target="a")
    g.record_visible("like", target="b")
    g.state.last_visible_at = time.time() - 100
    assert not g.check_visible("like", target="c").allowed


def test_the_warm_up_ramp_limits_a_new_account(cfg, store):
    cfg.set("risk.warmup_days", [2, 6, 13])
    g = _guard(cfg, store)
    # A brand new account is allowed very little.
    assert g.warmup_factor() == 0.15
    g.store.kv_set("risk:first_seen", str(time.time() - 10 * 86400))
    assert g.warmup_factor() == 0.70
    g.store.kv_set("risk:first_seen", str(time.time() - 20 * 86400))
    assert g.warmup_factor() == 1.0


def test_the_same_text_is_not_sent_twice(cfg, store):
    g = _guard(cfg, store)
    assert g.check_visible("reply", target="t1", text="Thanks for reaching out!").allowed
    decision = g.check_visible("reply", target="t2", text="thanks for reaching out!")
    assert not decision.allowed
    assert decision.already_satisfied


def test_different_text_is_allowed(cfg, store):
    g = _guard(cfg, store)
    assert g.check_visible("reply", target="t1", text="one").allowed
    assert g.check_visible("reply", target="t2", text="two").allowed


def test_safe_mode_stops_everything(cfg, store):
    g = _guard(cfg, store)
    g.enter_safe_mode("Instagram asked the bot to slow down", minutes=30)
    assert g.in_safe_mode()
    assert not g.check_visible("like", target="alice").allowed


def test_safe_mode_expires(cfg, store):
    g = _guard(cfg, store)
    g.enter_safe_mode("test", minutes=1)
    g.state.safe_until = time.time() - 1
    assert not g.in_safe_mode()


def test_a_repeat_breach_costs_more_time(cfg, store):
    g = _guard(cfg, store)
    g.enter_safe_mode("first", minutes=10)
    first = g.state.safe_until
    g.state.safe_until = 0
    g.enter_safe_mode("second", minutes=10)
    assert g.state.safe_until > first


def test_a_rate_limit_enters_safe_mode(cfg, store):
    g = _guard(cfg, store)
    g.note_failure(RuntimeError("429 too many requests, please wait"))
    assert g.in_safe_mode()
    assert "slow down" in g.state.safe_reason


def test_a_challenge_enters_safe_mode(cfg, store):
    g = _guard(cfg, store)
    g.note_failure(RuntimeError("challenge_required"))
    assert g.in_safe_mode()


def test_an_ordinary_failure_does_not_stop_the_bot(cfg, store):
    g = _guard(cfg, store)
    g.note_failure(RuntimeError("connection reset"))
    assert not g.in_safe_mode()


def test_the_request_budget_is_enforced(cfg, store):
    cfg.set("risk.requests_per_hour", 2)
    g = _guard(cfg, store)
    g.note_request()
    g.note_request()
    from ibot.risk import RiskStop

    try:
        g.before_request()
    except RiskStop as exc:
        assert "budget" in str(exc)
    else:
        raise AssertionError("the budget should have stopped the request")


def test_reset_leaves_safe_mode(cfg, store):
    g = _guard(cfg, store)
    g.enter_safe_mode("test")
    g.reset()
    assert not g.in_safe_mode()
    assert g.state.breaches == 0


def test_a_reply_is_not_blocked_by_the_visible_action_gap(cfg, store):
    """A customer who wrote should not wait a minute for an answer."""
    cfg.set("risk.min_visible_gap_seconds", 60)
    g = _guard(cfg, store)
    g.record_visible("like", target="alice")
    assert g.check_reply(text="hello, how can I help?").allowed


def test_a_reply_is_still_blocked_in_safe_mode(cfg, store):
    g = _guard(cfg, store)
    g.enter_safe_mode("test", minutes=10)
    assert not g.check_reply(text="hello").allowed


def test_a_reply_is_never_sent_twice_in_the_same_words(cfg, store):
    g = _guard(cfg, store)
    assert g.check_reply(text="Thanks for reaching out!").allowed
    g.record_reply(text="Thanks for reaching out!", target="t1")
    assert not g.check_reply(text="thanks for reaching out!").allowed


def test_replies_do_not_use_up_the_visible_action_budget(cfg, store):
    cfg.set("risk.daily_visible_ceiling", 2)
    g = _guard(cfg, store)
    for _ in range(5):
        g.record_reply(text="a different answer", target="t1")
    assert g.score()["visible_today"] == 0
    assert g.score()["replies_today"] == 5

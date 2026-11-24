"""Guardrails, the rulebook, the store and the cookie parser."""

from __future__ import annotations

import time

from ibot.cookies import parse_cookie_header, parse_cookie_source, sessionid_from_cookies
from ibot.guardrails import Guardrails
from ibot.rules import Rulebook, load_rules


# ------------------------------------------------------------------ guardrails
def test_the_cooldown_blocks_a_second_reply(store, cfg):
    cfg.set("behaviour.cooldown_seconds", 900)
    g = Guardrails(store, cfg)
    assert g.check("t1").allowed
    g.record_reply("t1")
    decision = g.check("t1")
    assert not decision.allowed
    assert "cooldown" in decision.reason


def test_the_hourly_cap_blocks(store, cfg):
    cfg.set("behaviour.hourly_cap", 2)
    g = Guardrails(store, cfg)
    g.record_reply("t1")
    g.record_reply("t1")
    assert not g.check("t1").allowed


def test_the_global_cap_is_shared_between_conversations(store, cfg):
    cfg.set("behaviour.global_daily_cap", 1)
    g = Guardrails(store, cfg)
    g.record_reply("t1")
    assert not g.check("t2").allowed


def test_baseline_mode_blocks_everything(store, cfg):
    cfg.set("behaviour.baseline_only", True)
    g = Guardrails(store, cfg)
    assert not g.check("t1").allowed


def test_an_action_is_not_repeated_on_the_same_target(store, cfg):
    g = Guardrails(store, cfg)
    assert g.check_action("like", target="alice").allowed
    g.record_action("like", target="alice")
    decision = g.check_action("like", target="alice")
    assert not decision.allowed
    assert decision.already_satisfied


def test_inbound_fingerprints_are_deduplicated(store, cfg):
    g = Guardrails(store, cfg)
    assert g.record_inbound("t1", "fp1") is True
    assert g.record_inbound("t1", "fp1") is False


# --------------------------------------------------------------------- rules
def test_short_keywords_do_not_fire_on_substrings():
    rules = load_rules([{"name": "greeting", "match": ["hi"], "reply": "Hello"}])
    book = Rulebook(rules)
    assert book.match("hi there") is not None
    assert book.match("which one is shipping") is None
    assert book.match("this is high quality") is None


def test_the_most_specific_rule_wins():
    rules = load_rules(
        [
            {"name": "shipping_and_returns", "match": ["shipping and returns"], "reply": "A"},
            {"name": "returns", "match": ["returns"], "reply": "B"},
        ]
    )
    book = Rulebook(rules)
    assert book.match("shipping and returns policy") is not None
    assert book.reply_for("shipping and returns policy", "")[0] == "A"


def test_the_default_reply_is_used_when_nothing_matches():
    book = Rulebook(load_rules([{"match": ["zzz"], "reply": "x"}]))
    assert book.reply_for("hello", "fallback")[0] == "fallback"


# --------------------------------------------------------------------- store
def test_the_store_remembers_and_clears_fingerprints(store):
    store.record("inbound", thread_id="t1", fingerprint="fp")
    assert store.has_fingerprint("fp")
    assert store.clear_fingerprints("t1") == 1
    assert not store.has_fingerprint("fp")


def test_a_task_can_be_cancelled(store):
    task_id = store.enqueue_task("auto:like", {"x": 1})
    assert task_id is not None
    assert store.cancel_task(task_id)
    assert not store.cancel_task(task_id)


def test_the_store_survives_a_restart(tmp_path):
    from ibot.store import Store

    path = tmp_path / "s.db"
    first = Store(path)
    first.record("inbound", thread_id="t1", fingerprint="fp")
    first.close()
    second = Store(path)
    assert second.has_fingerprint("fp")
    second.close()


# ------------------------------------------------------------------- cookies
def test_a_cookie_header_is_parsed_and_decoded():
    cookies = parse_cookie_header("sessionid=631%3Aabc; csrftoken=xyz")
    assert cookies[0] == {"name": "sessionid", "value": "631:abc"}


def test_the_sessionid_is_found_in_any_format():
    text = "csrftoken=x; sessionid=12345%3Aabc"
    assert sessionid_from_cookies(parse_cookie_source(text)) == "12345:abc"


def test_a_cookie_value_may_contain_equals_signs():
    cookies = parse_cookie_header("rur=RVA%2C1%2C2%3A01abc==")
    assert cookies[0]["value"] == "RVA,1,2:01abc=="


def test_an_empty_source_is_not_a_crash():
    assert parse_cookie_source("") == []

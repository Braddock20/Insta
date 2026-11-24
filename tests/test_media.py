"""The automatic visible actions."""

from __future__ import annotations

from ibot.media import MediaBot


def _bot(client, cfg, store, guardrails):
    return MediaBot(client, cfg, store, guardrails)


def _with_following(client):
    client.add_profile = None  # not used; the fake returns everyone
    return client


def test_nothing_happens_when_the_toggles_are_off(client, cfg, store, guardrails):
    cfg.set("automations.default_view", False)
    cfg.set("automations.default_like", False)
    cfg.set("automations.default_story", False)
    cfg.set("automations.default_follow", False)
    bot = _bot(client, cfg, store, guardrails)
    summary = bot.run_once()
    assert summary["stories_viewed"] == 0
    assert summary["posts_liked"] == 0


def test_a_story_is_viewed_when_the_toggle_is_on(client, cfg, store, guardrails):
    cfg.set("automations.default_view", True)
    client.add_story("alice", 2)
    bot = _bot(client, cfg, store, guardrails)
    summary = bot.run_once()
    assert summary["stories_viewed"] == 2
    assert client._seen


def test_a_story_is_liked_when_the_toggle_is_on(client, cfg, store, guardrails):
    cfg.set("automations.default_story", True)
    client.add_story("alice", 1)
    bot = _bot(client, cfg, store, guardrails)
    summary = bot.run_once()
    assert summary["stories_liked"] == 1
    assert client.liked


def test_the_daily_cap_stops_a_repeat(client, cfg, store, guardrails):
    cfg.set("automations.default_view", True)
    cfg.set("behaviour.action_caps.story_view", {"hourly": 20, "daily": 1})
    client.add_story("alice", 1)
    bot = _bot(client, cfg, store, guardrails)
    bot.run_once()
    client.add_story("alice", 1)
    summary = bot.run_once()
    # The same account is not viewed twice in one day.
    assert summary["stories_viewed"] == 0


def test_notes_are_read(client, cfg, store, guardrails):
    cfg.set("automations.default_note", True)
    client.add_note("alice", "hello from a note")
    bot = _bot(client, cfg, store, guardrails)
    notes = bot.auto_read_notes()
    assert len(notes) == 1
    assert store.recent_activity("note")


def test_the_toggle_can_be_changed_at_runtime(client, cfg, store, guardrails):
    bot = _bot(client, cfg, store, guardrails)
    store.kv_set("auto:view", "on")
    client.add_story("alice", 1)
    assert bot.auto_view_stories() == 1

"""Shared fixtures.

Every test runs against the in-memory fake client, so the suite needs no
account, no network and no browser. That is what makes it possible to run
the tests on a free Render instance or in CI in a couple of seconds.
"""

from __future__ import annotations

import pytest

from ibot.client.fake import FakeClient
from ibot.config import load
from ibot.guardrails import Guardrails
from ibot.rules import Rulebook
from ibot.store import Store


@pytest.fixture()
def cfg(tmp_path):
    """A config rooted in a temp folder, with instant replies."""
    config = load(tmp_path)
    config.set("behaviour.reply_delay_min", 0)
    config.set("behaviour.reply_delay_max", 0)
    config.set("behaviour.baseline_only", False)
    config.set("behaviour.cooldown_seconds", 0)
    config.set("behaviour.hourly_cap", 0)
    config.set("behaviour.daily_cap", 0)
    config.set("behaviour.global_daily_cap", 0)
    return config


@pytest.fixture()
def store(tmp_path):
    s = Store(tmp_path / "state" / "igbot.db")
    yield s
    s.close()


@pytest.fixture()
def guardrails(store, cfg):
    return Guardrails(store, cfg)


@pytest.fixture()
def rulebook(cfg):
    cfg.set(
        "reply.rules",
        [
            {"name": "pricing", "match": ["price", "how much"], "reply": "Here are our prices."},
            {"name": "greeting", "match": ["hi", "hello"], "reply": "Hi there!"},
        ],
    )
    return Rulebook.from_config(cfg)


@pytest.fixture()
def client():
    c = FakeClient(own_user_id="1", own_username="me")
    c.add_thread("t1", "alice", ["hello there"])
    c.add_thread("t2", "bob", ["what is the price?"], pending=True)
    c.add_thread("t3", "me", ["/help"], self_thread=True)
    return c

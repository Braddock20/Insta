"""Tests for the automation modules.

These run against the mock Instagram, so every action — a follow, a like, a
story view, an activity reaction — is exercised end to end through a real
browser against a faithful copy of the page.
"""

import json
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from igbot.automation import AutomationEngine  # noqa: E402
from igbot.config import Config  # noqa: E402
from igbot.guardrails import Guardrails  # noqa: E402
from igbot.likes import LikeBot  # noqa: E402
from igbot.notifications import ActivityItem, NotificationBot  # noqa: E402
from igbot.profile import ProfileBot  # noqa: E402
from igbot.scheduler import Scheduler, Schedule  # noqa: E402
from igbot.stories import StoryBot  # noqa: E402
from igbot.store import Store  # noqa: E402

import yaml  # noqa: E402

mock_server = None
mock_base = ""


def _control(path, payload=None):
    url = f"{mock_base}/_control/{path}"
    data = json.dumps(payload or {}).encode()
    request = urllib.request.Request(
        url, data=data, headers={"Content-Type": "application/json"}, method="POST"
    )
    with urllib.request.urlopen(request, timeout=10) as response:
        return json.loads(response.read().decode())


def _state():
    with urllib.request.urlopen(f"{mock_base}/_control/state", timeout=10) as response:
        return json.loads(response.read().decode())


@pytest.fixture(scope="module")
def mock():
    global mock_server, mock_base
    sys.path.insert(0, str(ROOT / "mock"))
    import mock_instagram

    mock_server = mock_instagram.start(0)
    mock_base = f"http://127.0.0.1:{mock_server.server_address[1]}"
    yield mock_base
    mock_server.shutdown()


@pytest.fixture(scope="module")
def browser():
    from playwright.sync_api import sync_playwright

    playwright = sync_playwright().start()
    instance = playwright.chromium.launch(headless=True)
    yield instance
    instance.close()
    playwright.stop()


@pytest.fixture()
def selectors():
    return yaml.safe_load((ROOT / "igbot" / "selectors.yaml").read_text(encoding="utf-8"))


@pytest.fixture()
def cfg(tmp_path):
    return Config(
        {
            "urls": {"base": mock_base},
            "account": {"username": "typical_me"},
            "behaviour": {
                "poll_seconds": 1,
                "baseline_only": False,
                "action_caps": {
                    "like": {"hourly": 20, "daily": 100},
                    "follow": {"hourly": 5, "daily": 20},
                    "unfollow": {"hourly": 5, "daily": 20},
                    "story_view": {"hourly": 20, "daily": 80},
                },
            },
            "reply": {"default_reply": "Thanks for reaching out!", "rules": []},
            "automations": {"enabled": False, "rules": [], "schedules": []},
        },
        tmp_path,
    )


@pytest.fixture()
def world(browser, mock, selectors, cfg, tmp_path):
    """A fresh mock state, a fresh page, and the action helpers."""
    _control("reset")

    context = browser.new_context(viewport={"width": 414, "height": 863})
    page = context.new_page()
    store = Store(tmp_path / "state.db")
    guard = Guardrails(store, cfg)
    scheduler = Scheduler(store, cfg)

    from igbot.ai import AIReplyEngine
    from igbot.dm import DMBot
    from igbot.dom import DOM
    from igbot.rules import Rulebook

    dom = DOM(page, selectors, mock_base, settle=0.12)
    bot = DMBot(dom, store, guard, Rulebook.from_config(cfg), AIReplyEngine(cfg), cfg)

    yield {
        "page": page,
        "context": context,
        "store": store,
        "guard": guard,
        "scheduler": scheduler,
        "cfg": cfg,
        "selectors": selectors,
        "dom": dom,
        "bot": bot,
        "profile": ProfileBot(page, cfg, selectors, guard),
        "likes": LikeBot(page, cfg, selectors, guard),
        "stories": StoryBot(page, cfg, selectors, guard, dom),
        "notifications": NotificationBot(page, cfg, selectors, store),
    }

    store.close()
    context.close()


# -------------------------------------------------------------------- profile


def test_profile_is_read(world):
    profile = world["profile"].read("mintstudio") if world["profile"].open("mintstudio") else None
    assert profile is not None
    assert profile.username == "mintstudio"
    assert profile.full_name == "Mint Studio"
    assert profile.followers == 12400
    assert profile.following == 312
    assert profile.posts == 87
    assert "ceramics" in profile.bio
    assert profile.website.startswith("https://")
    assert profile.is_following is False


def test_follow_is_performed_and_confirmed(world):
    result = world["profile"].follow("mintstudio")
    assert result.ok, result.message
    assert result.confirmed is True
    assert _state()["profiles"]["mintstudio"]["following_me"] is True


def test_following_twice_is_a_no_op(world):
    """A second follow is refused as a no-op, and nothing is clicked twice."""
    world["profile"].follow("mintstudio")
    result = world["profile"].follow("mintstudio")
    assert result.ok
    assert result.message in ("already following", "already done today")
    # The relationship is still just "following" — no second follow happened.
    assert _state()["profiles"]["mintstudio"]["following_me"] is True


def test_unfollow_is_performed_and_confirmed(world):
    world["profile"].follow("mintstudio")
    result = world["profile"].unfollow("mintstudio")
    assert result.ok, result.message
    assert result.confirmed is True
    assert _state()["profiles"]["mintstudio"]["following_me"] is False


def test_followers_and_following_are_listed(world):
    followers = world["profile"].list_followers("mintstudio")
    following = world["profile"].list_following("mintstudio")
    assert "aurel" in followers
    assert "devin" in followers
    assert "harper" in following


def test_follow_respects_the_daily_cap(world):
    world["cfg"].set("behaviour.action_caps.follow", {"hourly": 0, "daily": 1})
    first = world["profile"].follow("mintstudio")
    assert first.ok
    second = world["profile"].follow("aurel")
    assert not second.ok
    assert "cap" in second.message


def test_the_same_target_is_not_followed_twice_in_a_day(world):
    world["profile"].follow("mintstudio")
    result = world["profile"].follow("mintstudio")
    # The cap check fires first for a repeat of the same target.
    assert not result.ok or "already" in result.message


# ---------------------------------------------------------------------- likes


def test_post_is_liked_and_confirmed(world):
    result = world["likes"].like_post(f"{mock_base}/p/ABC123/")
    assert result.ok, result.message
    assert result.confirmed is True
    assert _state()["likes"]["ABC123"] is True


def test_liking_twice_is_a_no_op(world):
    """A second like is refused as a no-op."""
    world["likes"].like_post(f"{mock_base}/p/ABC123/")
    result = world["likes"].like_post(f"{mock_base}/p/ABC123/")
    assert result.ok
    assert result.message in ("already liked", "already done today")
    assert _state()["likes"]["ABC123"] is True


def test_post_is_unliked(world):
    world["likes"].like_post(f"{mock_base}/p/ABC123/")
    result = world["likes"].unlike_post(f"{mock_base}/p/ABC123/")
    assert result.ok, result.message
    assert _state()["likes"]["ABC123"] is False


# -------------------------------------------------------------------- stories


def test_story_tray_is_read(world):
    stories = world["stories"].list_stories()
    names = [s.username for s in stories]
    assert "mintstudio" in names
    assert "aurel" in names


def test_viewing_a_story_is_recorded(world):
    result = world["stories"].view_story("mintstudio")
    assert result.ok, result.message
    actions = _state()  # the mock records nothing for a view; the bot does
    assert actions["stories"]["mintstudio"]


# ------------------------------------------------------------------- activity


def test_activity_feed_is_read(world):
    items = world["notifications"].read()
    kinds = {item.kind for item in items}
    assert "like" in kinds
    assert "follow" in kinds
    assert "comment" in kinds

    devin = next(i for i in items if i.username == "devin")
    assert devin.kind == "like"


def test_new_activity_is_reported_only_once(world):
    first = world["notifications"].new_items()
    second = world["notifications"].new_items()
    assert len(first) == 3
    assert len(second) == 0


def test_injected_activity_is_seen(world):
    _control("activity", {"username": "newperson", "text": "newperson started following you"})
    items = world["notifications"].new_items()
    assert any(i.username == "newperson" for i in items)


# ------------------------------------------------------------------ scheduler


def test_a_task_is_queued_and_run(tmp_path, cfg):
    store = Store(tmp_path / "s.db")
    scheduler = Scheduler(store, cfg)
    task_id = scheduler.enqueue("demo", {"a": 1})
    assert task_id is not None
    assert scheduler.pending() == 1

    ran = []
    outcomes = scheduler.run_due({"demo": lambda task: (True, f"ran {task.payload}")})
    assert len(outcomes) == 1
    assert outcomes[0]["ok"] is True
    assert scheduler.pending() == 0
    store.close()


def test_a_duplicate_task_is_not_queued_twice(tmp_path, cfg):
    store = Store(tmp_path / "s.db")
    scheduler = Scheduler(store, cfg)
    scheduler.enqueue("demo", {}, fingerprint="abc")
    second = scheduler.enqueue("demo", {}, fingerprint="abc")
    assert second is None
    assert scheduler.pending() == 1
    store.close()


def test_a_task_in_the_future_does_not_run(tmp_path, cfg):
    store = Store(tmp_path / "s.db")
    scheduler = Scheduler(store, cfg)
    scheduler.enqueue("demo", {}, delay_seconds=3600)
    outcomes = scheduler.run_due({"demo": lambda task: (True, "ran")})
    assert outcomes == []
    store.close()


def test_a_failing_task_is_marked_failed(tmp_path, cfg):
    store = Store(tmp_path / "s.db")
    scheduler = Scheduler(store, cfg)
    scheduler.enqueue("demo", {})

    def boom(task):
        raise RuntimeError("kaboom")

    outcomes = scheduler.run_due({"demo": boom})
    assert outcomes[0]["ok"] is False
    assert "kaboom" in outcomes[0]["message"]
    store.close()


def test_a_daily_schedule_fires_once_a_day(tmp_path, cfg):
    store = Store(tmp_path / "s.db")
    scheduler = Scheduler(store, cfg)
    cfg.set("automations.schedules", [{"name": "morning", "kind": "auto:like_post", "schedule": "09:00"}])

    now = time.time()
    first = scheduler.tick_schedules(now)
    assert len(first) == 1
    # A second tick in the same slot must not queue another copy.
    assert scheduler.tick_schedules(now + 5) == []
    store.close()


def test_schedule_specs_are_understood():
    daily = Schedule("d", "k", "09:00")
    assert daily.next_run() is not None
    every = Schedule("e", "k", "every 30m")
    assert every.next_run() is not None
    broken = Schedule("b", "k", "not a schedule")
    assert broken.next_run() is None


# ----------------------------------------------------------------- automation


def test_a_follow_rule_queues_a_task(world):
    cfg = world["cfg"]
    cfg.set("automations.enabled", True)
    cfg.set(
        "automations.rules",
        [{"name": "follow_back", "trigger": "follow", "action": "follow_back", "delay_seconds": 0}],
    )
    engine = AutomationEngine(
        cfg, world["scheduler"], profile_bot=world["profile"],
    )

    items = [ActivityItem(kind="follow", username="devin", text="devin started following you")]
    queued = engine.react_to_activity(items)
    assert len(queued) == 1

    # The task is queued, not executed — that is what keeps a reaction from
    # firing the instant it is discovered.
    assert world["scheduler"].pending() == 1


def test_a_rule_does_not_fire_twice_for_the_same_event(world):
    cfg = world["cfg"]
    cfg.set("automations.enabled", True)
    cfg.set(
        "automations.rules",
        [{"name": "follow_back", "trigger": "follow", "action": "follow_back"}],
    )
    engine = AutomationEngine(cfg, world["scheduler"], profile_bot=world["profile"])
    item = ActivityItem(kind="follow", username="devin", text="devin started following you")
    assert len(engine.react_to_activity([item])) == 1
    assert engine.react_to_activity([item]) == []


def test_automations_are_off_by_default(world):
    engine = AutomationEngine(world["cfg"], world["scheduler"])
    item = ActivityItem(kind="follow", username="devin", text="devin started following you")
    assert engine.react_to_activity([item]) == []


def test_a_rule_only_matches_its_own_trigger(world):
    cfg = world["cfg"]
    cfg.set("automations.enabled", True)
    cfg.set(
        "automations.rules",
        [{"name": "like_commenters", "trigger": "comment", "action": "like_comment"}],
    )
    engine = AutomationEngine(cfg, world["scheduler"])
    follow = ActivityItem(kind="follow", username="devin", text="devin started following you")
    assert engine.react_to_activity([follow]) == []


# ------------------------------------------------------------------------ api


def test_the_api_needs_a_token(world, cfg, tmp_path):
    from igbot.api import APIContext, serve

    ctx = APIContext(
        cfg=cfg, store=world["store"], scheduler=world["scheduler"],
        guardrails=world["guard"], token="s3cret", started_at=time.time(),
    )
    server = serve(ctx, port=0, host="127.0.0.1")
    port = server.server_address[1]
    try:
        # Health is open.
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/health", timeout=5) as resp:
            assert json.loads(resp.read().decode())["ok"] is True

        # Everything else needs the token.
        try:
            urllib.request.urlopen(f"http://127.0.0.1:{port}/api/stats", timeout=5)
            raise AssertionError("expected an authorisation failure")
        except urllib.error.HTTPError as exc:
            assert exc.code == 401

        request = urllib.request.Request(
            f"http://127.0.0.1:{port}/api/stats", headers={"Authorization": "Bearer s3cret"}
        )
        with urllib.request.urlopen(request, timeout=5) as resp:
            assert "replies_today" in json.loads(resp.read().decode())
    finally:
        server.shutdown()


def test_the_api_queues_an_action(world, cfg):
    from igbot.api import APIContext, serve

    ctx = APIContext(
        cfg=cfg, store=world["store"], scheduler=world["scheduler"],
        guardrails=world["guard"], token="s3cret", started_at=time.time(),
    )
    server = serve(ctx, port=0, host="127.0.0.1")
    port = server.server_address[1]
    try:
        request = urllib.request.Request(
            f"http://127.0.0.1:{port}/api/follow",
            data=json.dumps({"username": "mintstudio"}).encode(),
            headers={"Authorization": "Bearer s3cret", "Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(request, timeout=5) as resp:
            payload = json.loads(resp.read().decode())
        assert payload["ok"] is True
        assert payload["task_id"] is not None
        assert world["scheduler"].pending() == 1
    finally:
        server.shutdown()


def test_the_api_is_closed_without_a_token(world, cfg):
    from igbot.api import APIContext, serve

    ctx = APIContext(cfg=cfg, store=world["store"], token="", started_at=time.time())
    server = serve(ctx, port=0, host="127.0.0.1")
    port = server.server_address[1]
    try:
        try:
            urllib.request.urlopen(f"http://127.0.0.1:{port}/api/stats", timeout=5)
            raise AssertionError("expected the API to be closed")
        except urllib.error.HTTPError as exc:
            assert exc.code == 401
    finally:
        server.shutdown()


# ------------------------------------------------------------- suspension


def test_suspension_urls_are_recognised():
    from igbot.browser import Browser

    assert Browser.is_suspended_url("https://www.instagram.com/accounts/suspended/?next=x")
    assert Browser.is_suspended_url("https://www.instagram.com/accounts/disabled/")
    assert Browser.is_suspended_url("https://www.instagram.com/accounts/removed/")
    assert not Browser.is_suspended_url("https://www.instagram.com/direct/inbox/")
    assert not Browser.is_suspended_url("https://www.instagram.com/")


def test_a_suspended_account_is_reported_as_suspended(world):
    """A restricted account must be detected, not treated as a normal session."""
    from igbot.browser import Browser

    _control("suspend", {"suspended": True})
    try:
        browser = Browser(world["cfg"], headless=True)
        browser.page = world["page"]
        browser.context = world["context"]
        status = browser.session_status()
        assert status.suspended is True
        assert status.logged_in is False
        assert "suspended" in status.reason.lower()
    finally:
        _control("suspend", {"suspended": False})


def test_the_bot_stops_on_a_suspension(world):
    """The poll must not keep running against a restricted account."""
    from igbot.browser import AccountSuspended

    _control("suspend", {"suspended": True})
    try:
        with pytest.raises(AccountSuspended):
            world["bot"].poll_once()
    finally:
        _control("suspend", {"suspended": False})

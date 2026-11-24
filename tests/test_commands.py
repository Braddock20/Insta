"""The command router."""

from __future__ import annotations

from ibot.commands import CommandRouter


def _router(client, cfg, store=None, guardrails=None, session=None):
    return CommandRouter(client, cfg, store=store, guardrails=guardrails, session=session)


def test_help_lists_the_commands(client, cfg):
    answer = _router(client, cfg).run("/help")
    assert "/inbox" in answer
    assert "commands" in answer


def test_an_unknown_command_suggests_alternatives(client, cfg):
    answer = _router(client, cfg).run("/inboxx")
    assert "Unknown command" in answer


def test_a_command_that_is_not_a_command_returns_nothing(client, cfg):
    assert _router(client, cfg).run("hello") == ""


def test_inbox_lists_the_conversations(client, cfg):
    answer = _router(client, cfg).run("/inbox")
    assert "alice" in answer
    # Bob wrote first, so he lives in the request folder, not the inbox.
    assert "bob" not in answer
    assert "bob" in _router(client, cfg).run("/requests")


def test_whoami_reports_the_account(client, cfg):
    answer = _router(client, cfg).run("/whoami")
    assert "@me" in answer


def test_a_toggle_can_be_switched_on_and_off(client, cfg, store):
    router = _router(client, cfg, store=store)
    assert "ON" in router.run("/autolike on")
    assert "ON" in router.run("/auto")
    assert "off" in router.run("/autolike off")


def test_reply_sends_into_the_current_thread(client, cfg):
    router = _router(client, cfg)
    thread = client.threads()[0]
    answer = router.run("/reply hello you", thread)
    assert "sent" in answer
    assert client.sent[-1] == ("t1", "hello you")


def test_a_command_that_needs_an_argument_says_so(client, cfg):
    answer = _router(client, cfg).run("/follow")
    assert "which account" in answer


def test_health_reports_the_transport(client, cfg):
    answer = _router(client, cfg).run("/health")
    assert "fake" in answer


def test_notes_are_listed(client, cfg):
    client.add_note("alice", "having a good day")
    answer = _router(client, cfg).run("/notes")
    assert "having a good day" in answer


def test_a_story_is_liked(client, cfg):
    client.add_story("alice", 1)
    answer = _router(client, cfg).run("/storylike alice")
    assert "liked" in answer
    assert client.liked


def test_a_post_is_liked(client, cfg):
    post = client.add_media("alice", "nice picture")
    answer = _router(client, cfg).run(f"/like {post.pk}")
    assert "liked" in answer


def test_every_registered_command_has_a_help_line(client, cfg):
    router = _router(client, cfg)
    for name in router.names():
        entry = router._commands[name]
        assert entry[0], f"/{name} has no help text"

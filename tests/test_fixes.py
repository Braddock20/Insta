"""Regression tests for the fixes of 2026-11-24.

Every test here stands for a bug that was visible in the Telegram-style
command log:

* ``/scan`` answered "0 followers, 0 following" — the Rust engine walked
  into Instagram without a session, got HTTP 401, and reported the empty
  answer as a success.
* ``/following all`` answered "@all was not found" — the word "all" was
  read as the name of an account.
* ``/realtime`` answered "the push channel is not attached" on a channel
  that was perfectly healthy.
* every command was answered twice, and ``/autoview`` switched itself
  back off again.
"""

from __future__ import annotations

import time
from pathlib import Path

from ibot.bulk import BulkEngine, Scanner
from ibot.client.base import Message
from ibot.commands import CommandRouter
from ibot.dm import DMBot, fingerprint_for
from ibot.realtime import RealtimeMessage


# --------------------------------------------------------------- the engine
class _FakeIG:
    """The little bit of instagrapi the Python engine talks to."""

    def __init__(self, users: list[dict]):
        self._users = users

    def private_request(self, endpoint, params=None):  # noqa: ARG002
        return {"users": self._users}


class _StubClient:
    def __init__(self, users: list[dict] | None = None):
        self.cl = _FakeIG(users or [])


class _SessionClient:
    """A client that has a session — the engine may talk to Instagram."""

    class cl:
        cookie_dict = {"sessionid": "abc", "csrftoken": "def"}
        ig_www_claim = ""
        app_id = "567067343352427"
        settings = {"user_agent": "Instagram 1.2.3 Android"}


def _users(count: int) -> list[dict]:
    return [
        {"pk": str(1000 + n), "username": f"user{n}", "full_name": f"User {n}"}
        for n in range(count)
    ]


class _BrokenEngine(BulkEngine):
    """An engine whose walk failed — the way it did when Instagram said no."""

    def binary(self):
        return Path("/bin/true")

    def _call(self, binary, request):  # noqa: ARG002
        return {
            "ok": True,
            "engine": "rust",
            "count": 0,
            "pages": 0,
            "errors": ["page 0: HTTP 401: login required"],
        }


class _SessionlessEngine(BulkEngine):
    """An engine with no credentials at all."""

    def binary(self):
        return Path("/bin/true")

    def session(self):
        return {"sessionid": "", "csrftoken": "", "cookie": "", "app_id": "1"}

    def _call(self, binary, request):  # noqa: ARG002
        raise AssertionError("the engine must not be called without a session")


def test_the_list_comes_back_even_without_a_file(tmp_path):
    """The engine hands the list back through a file. When the caller did
    not ask for one, the list still has to arrive — otherwise every command
    that walks the connections answers "not following anyone"."""
    import json

    class _Engine(BulkEngine):
        def binary(self):
            return Path("/bin/true")

        def _call(self, binary, request):  # noqa: ARG002
            out = request.get("out")
            Path(out).parent.mkdir(parents=True, exist_ok=True)
            with open(out, "w", encoding="utf-8") as handle:
                for n in (1, 2):
                    handle.write(
                        json.dumps({"pk": str(n), "username": f"u{n}", "full_name": f"U{n}"}) + "\n"
                    )
            return {"ok": True, "engine": "rust", "count": 2, "pages": 1}

    engine = _Engine(None, _SessionClient(), tmp_path)
    result = engine.list_connections("following", "1")
    assert result.ok
    assert result.count == 2
    assert [u["username"] for u in result.users] == ["u1", "u2"]


def test_a_failed_walk_falls_back_to_python(tmp_path):
    """A refused walk must never be reported as an empty follower list."""
    engine = _BrokenEngine(None, _StubClient(_users(2)), tmp_path)
    engine.client = _StubClient(_users(2))
    result = engine.list_connections("followers", "1", out=tmp_path / "f.jsonl")
    assert result.ok
    assert result.count == 2
    assert result.engine == "python"


def test_an_engine_without_a_session_never_answers(tmp_path):
    """No sessionid means no walk — the Python engine takes the job."""
    engine = _SessionlessEngine(None, _StubClient(_users(3)), tmp_path)
    engine.client = _StubClient(_users(3))
    result = engine.list_connections("followers", "1", out=tmp_path / "f.jsonl")
    assert result.ok
    assert result.count == 3
    assert result.engine == "python"


def test_the_engine_gets_the_cookies_out_of_the_cookie_jar(tmp_path):
    """The cookies come from the jar the client really uses."""
    engine = BulkEngine(None, None, tmp_path)

    class _Client:
        class cl:
            cookie_dict = {"sessionid": "abc", "csrftoken": "def", "ds_user_id": "9"}
            ig_www_claim = "hmac.claim"
            app_id = "567067343352427"
            settings = {"user_agent": "Instagram 1.2.3 Android"}

    engine.client = _Client()
    session = engine.session()
    assert session["sessionid"] == "abc"
    assert session["csrftoken"] == "def"
    assert session["ig_www_claim"] == "hmac.claim"
    # ``ds_user_id`` makes Instagram answer 403 on the friendships
    # endpoints, so it is never handed over.
    assert "ds_user_id" not in session["cookie"]
    assert session["cookie"] == "sessionid=abc; csrftoken=def"


def test_an_empty_list_on_an_account_with_followers_is_a_failure(client, cfg, tmp_path):
    """The profile says 496 followers and the list says none — that is a
    broken read, not an empty account."""
    client.whoami().follower_count = 496

    class _EmptyEngine(BulkEngine):
        def binary(self):
            return Path("/bin/true")

        def _call(self, binary, request):  # noqa: ARG002
            return {"ok": True, "engine": "rust", "count": 0, "pages": 0, "errors": []}

    scanner = Scanner(client, None, cfg)
    scanner.engine = _EmptyEngine(None, _StubClient(_users(7)), tmp_path)
    summary = scanner.scan(kinds=("followers",))
    assert summary["lists"]["followers"]["count"] == 7
    assert summary["lists"]["followers"]["engine"] == "python"


# -------------------------------------------------------------- the commands
def test_following_all_is_not_an_account_called_all(client, cfg):
    """``/following all`` means everybody, not @all."""
    router = CommandRouter(client, cfg)
    answer = router.run("/following all")
    assert "accounts @me follows" in answer
    assert "not found" not in answer


def test_followers_all_is_not_an_account_called_all(client, cfg):
    router = CommandRouter(client, cfg)
    answer = router.run("/followers all")
    assert "followers of @me" in answer
    assert "not found" not in answer


def test_following_still_takes_a_username(client, cfg):
    router = CommandRouter(client, cfg)
    assert "@alice" in router.run("/following alice")


def test_a_count_still_works(client, cfg):
    router = CommandRouter(client, cfg)
    assert "accounts @me follows" in router.run("/following 2")


# ------------------------------------------------------- the double execution
def test_the_same_command_is_not_run_twice(client, cfg, store, guardrails, rulebook):
    """The push channel and the inbox disagree about the id of the
    note-to-self conversation. The command must still run exactly once."""
    router = CommandRouter(client, cfg, store=store)
    bot = DMBot(client, cfg, store, guardrails, rulebook, command_router=router)

    # The inbox sees the message under one thread id ...
    client.add_thread("t9", "me", [], self_thread=True)
    client._threads["t9"].messages.append(
        Message(
            id="m42",
            thread_id="t9",
            text="/whoami",
            user_id="1",
            username="me",
            timestamp=time.time(),
            is_self=True,
        )
    )

    # ... and the push channel sees the very same message under another one.
    push = RealtimeMessage(thread_id="t3", message_id="m42", user_id="1", text="/whoami")
    bot.handle_realtime(push)
    bot.drain()
    bot.poll_once()

    answers = [text for _thread, text in client.sent if "@me" in text]
    assert len(answers) == 1, f"the command ran {len(answers)} times"


def test_a_push_without_an_id_is_left_to_the_inbox(client, cfg, store, guardrails, rulebook):
    """A push with no message id cannot be told apart from the copy the
    inbox hands over, so it is not answered at all."""
    router = CommandRouter(client, cfg, store=store)
    bot = DMBot(client, cfg, store, guardrails, rulebook, command_router=router)
    bot.handle_realtime(RealtimeMessage(thread_id="t9", message_id="", user_id="1", text="/whoami"))
    bot.drain()
    assert client.sent == []


def test_the_fingerprint_follows_the_message_id():
    """Two different threads carrying the same message are one message."""
    a = fingerprint_for("t1", "1", "m9", "/scan")
    b = fingerprint_for("340282366841710300949128268756767920000", "1", "m9", "/scan")
    assert a == b
    # ... and two different messages are two different messages.
    assert fingerprint_for("t1", "1", "m9", "/scan") != fingerprint_for("t1", "1", "m10", "/scan")


# ------------------------------------------------------------- the protocol
def test_the_protocol_state_is_saved_under_the_project_root(cfg, tmp_path):
    """The identity has to survive a restart, or the account looks like a
    new app every time."""
    from ibot.protocol import ProtocolManager

    cfg.set("protocol.protocol_file", "state/protocol.json")
    manager = ProtocolManager(cfg)
    manager._save()
    path = tmp_path / "state" / "protocol.json"
    assert path.is_file()
    assert json_has_content(path)


def json_has_content(path: Path) -> bool:
    import json

    return bool(json.loads(path.read_text(encoding="utf-8")))

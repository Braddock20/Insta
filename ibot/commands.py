"""The command router.

Every command is sent to yourself in a DM. That is deliberate: Instagram has
no chat-command mechanism, and a DM to yourself is the one channel that is
guaranteed to exist on every account, needs no extra app, and works from the
phone.

Commands are plain text starting with a slash::

    /help
    /inbox
    /stories
    /like https://www.instagram.com/p/XXXX/

The router is deliberately thin: it parses, calls the client, and formats.
All the real work lives in the client and in the media helpers.
"""

from __future__ import annotations

import json
import shlex
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, Callable

from . import log
from .client.base import Thread

log = log.get(__name__)


class CommandResult:
    """A command answer that may also carry file attachments (e.g. JSON lists)."""

    def __init__(self, text: str, files: list[str] | None = None):
        self.text = text or ""
        self.files = list(files or [])

    def __str__(self) -> str:
        return self.text


class CommandError(RuntimeError):
    """A command could not be completed — the message is shown to the user."""


class CommandRouter:
    """Parses and runs commands sent to the bot."""

    def __init__(
        self,
        client,
        cfg,
        store=None,
        guardrails=None,
        session=None,
        media=None,
        risk=None,
        protocol=None,
        realtime=None,
    ):
        self.client = client
        self.cfg = cfg
        self.store = store
        self.guardrails = guardrails
        self.session = session
        self.media = media
        self.risk = risk
        self.protocol = protocol
        self.realtime = realtime
        self.started_at = time.time()
        self._commands: dict[str, tuple[str, Callable[[list[str], Thread], str]]] = {}
        self._register_all()

    # ------------------------------------------------------------- plumbing
    def register(self, name: str, help_text: str):
        """Decorator that adds a command to the table."""

        def wrap(fn: Callable[[list[str], Thread], str]):
            self._commands[name] = (help_text, fn)
            return fn

        return wrap

    def names(self) -> list[str]:
        return sorted(self._commands)

    def run(self, text: str, thread: Thread | None = None) -> str:
        """Run a command and return the answer."""
        raw = (text or "").strip()
        if not raw.startswith("/"):
            return ""
        try:
            parts = shlex.split(raw[1:])
        except ValueError:
            parts = raw[1:].split()
        if not parts:
            return "Send a command, or /help to see the list."
        name = parts[0].lower().lstrip("/")
        args = parts[1:]
        entry = self._commands.get(name)
        if entry is None:
            close = [n for n in self._commands if n.startswith(name[:3])]
            hint = f" Did you mean: {', '.join(close[:3])}?" if close else ""
            return f"Unknown command /{name}.{hint} Try /help."
        _help, fn = entry
        try:
            return fn(args, thread)
        except CommandError as exc:
            return f"⚠️ {exc}"
        except Exception as exc:  # noqa: BLE001
            log.exception("command /%s failed", name)
            return f"❌ /{name} failed: {type(exc).__name__}: {exc}"

    # ------------------------------------------------------------ utilities
    @property
    def scanner(self):
        """The scanner — the thing that reads the whole follower list."""
        scanner = getattr(self, "_scanner", None)
        if scanner is None:
            from .bulk import Scanner

            scanner = Scanner(self.client, self.store, self.cfg)
            self._scanner = scanner
        return scanner

    def _parallel(self, items: list, fn: Callable[[Any], Any], workers: int | None = None) -> list:
        """Run ``fn`` over ``items`` at the same time. The order is kept.

        This is what makes the bot feel quick: reading a hundred accounts'
        stories one after the other takes a minute; reading them at the same
        time takes a second.
        """
        if workers is None:
            workers = int(self.cfg.get("speed.commands_parallel", 32) or 32)
        # No artificial ceiling — use what the machine and config allow
        workers = max(1, int(workers))
        if workers == 1 or len(items) <= 1:
            return [fn(item) for item in items]

        def guarded(item):
            try:
                return fn(item)
            except Exception as exc:  # noqa: BLE001
                log.debug("parallel task failed: %s", exc)
                return None

        with ThreadPoolExecutor(max_workers=workers) as pool:
            return list(pool.map(guarded, items))

    def _with_json(self, text: str, data: Any, stem: str = "list") -> CommandResult:
        """Attach a JSON file with the full list so the chat answer stays short."""
        try:
            payload = data
            if isinstance(data, list) and data and not isinstance(data[0], (str, int, float, bool, type(None))):
                # already dict-like rows
                payload = data
            path = Path(tempfile.gettempdir()) / f"igbot_{stem}_{int(time.time())}.json"
            path.write_text(json.dumps(payload, indent=2, default=str, ensure_ascii=False), encoding="utf-8")
            return CommandResult(text, files=[str(path)])
        except Exception as exc:  # noqa: BLE001
            log.debug("could not attach json: %s", exc)
            return CommandResult(text)

    def _limit(self, args: list[str], default: int = 0) -> int:
        """How many of something. 0 means *all of them*."""
        for arg in args:
            text = arg.lower()
            if text in ("all", "every", "*"):
                return 0
            if text.isdigit():
                return int(text)
        return default

    @staticmethod
    def _is_count(text: str) -> bool:
        """Is this argument a number of accounts rather than a name?"""
        text = (text or "").strip().lower()
        return text in ("all", "every", "*") or text.isdigit()

    def _user_and_limit(self, args: list[str]) -> tuple[str, int]:
        """Split the arguments of a list command into (user, limit).

        ``/following``          — everybody you follow
        ``/following all``      — everybody you follow, every page
        ``/following 50``       — the first fifty
        ``/following @user 50`` — that user's first fifty

        The count may come first, so ``/following all`` is not read as "the
        account called @all" — which is what it used to do.
        """
        args = list(args or [])
        user = ""
        if args and not self._is_count(args[0]):
            user = args[0].lstrip("@")
            args = args[1:]
        return user, self._limit(args)

    def _dump(self, kind: str, user: str, people: list) -> str:
        """Keep the whole list on disk — a DM cannot hold 50,000 names.

        (This is a method of the router, not a helper inside the command
        block: it used to be defined in the wrong place and every call to
        ``/followers`` and ``/following`` died with an AttributeError.)
        """
        try:
            from .bulk import write_jsonl

            folder = self.scanner.folder()
            path = folder / f"{kind}-{user}.jsonl"
            write_jsonl(
                path,
                [
                    {
                        "pk": p.user_id,
                        "username": p.username,
                        "full_name": p.full_name,
                        "is_private": p.is_private,
                        "is_verified": p.is_verified,
                    }
                    for p in people
                ],
            )
            return str(path)
        except Exception:  # noqa: BLE001
            return "the state folder"

    def _graph(self, flag: str) -> list[dict]:
        """The connections graph from the database.

        A scan runs first when the graph is empty, so the very first question
        already has an answer.
        """
        if self.store is None:
            raise CommandError("no store attached")
        rows = self.store.connections(flag)
        if rows:
            return rows
        try:
            self.scanner.scan()
        except Exception as exc:  # noqa: BLE001
            raise CommandError(f"the scan failed: {exc}") from exc
        return self.store.connections(flag)

    def _allowed(self, kind: str, target: str) -> bool:
        """Is this visible action allowed right now?

        The guardrails hold the caps — how many likes a day, how many
        follows, how many story views. Every visible action goes through
        them, so no command can push the account past the budget.
        """
        if self.guardrails is None:
            return True
        decision = self.guardrails.check_action(kind, target=target)
        if not decision.allowed:
            log.info("skipped %s on %s: %s", kind, target, decision.reason)
            return False
        return True

    def _autoflag(self, name: str, default: bool = False) -> bool:
        if self.store is None:
            return default
        raw = self.store.kv_get(f"auto:{name}")
        if raw is None:
            return default
        return str(raw).lower() in ("1", "true", "on", "yes")

    def _set_autoflag(self, name: str, value: bool) -> None:
        if self.store is not None:
            self.store.kv_set(f"auto:{name}", "on" if value else "off")

    def _target_user(self, args: list[str], index: int = 0) -> str:
        if len(args) <= index:
            raise CommandError("which account? Try /help for the list of commands.")
        return args[index].lstrip("@")

    @staticmethod
    def _short(text: str, size: int = 60) -> str:
        text = " ".join((text or "").split())
        return text if len(text) <= size else text[: size - 1] + "…"

    # ----------------------------------------------------------- registration
    def _register_all(self) -> None:
        self._register_meta()
        self._register_inbox()
        self._register_content()
        self._register_people()
        self._register_graph()
        self._register_notes()
        self._register_automation()
        self._register_ops()
        self._register_moderation()
        self._register_discovery()
        self._register_publishing()

    # ----------------------------------------------------------------- meta
    def _register_meta(self) -> None:
        @self.register("help", "show this list")
        def _help(args, thread):
            groups = [
                ("⚡ Basics", ["help", "whoami", "status", "health", "relogin", "ping", "uptime", "about", "version", "engine"]),
                ("💬 Inbox", ["inbox", "threads", "unread", "last", "requests", "accept", "reply", "send", "react", "clear", "dms", "dmsearch"]),
                ("📸 Stories", ["stories", "storyview", "storylike", "storiesall", "viewall", "storyreply", "viewers", "highlights"]),
                ("🖼 Posts", ["post", "like", "unlike", "comments", "comment", "feed", "reels"]),
                ("👥 People", ["profile", "follow", "unfollow", "followers", "following", "followback", "mutuals", "search"]),
                ("🕸 The graph", ["scan", "nonfollowers", "fans", "unfollowers"]),
                ("📝 Notes", ["notes", "note", "notelike"]),
                ("🛡 Moderation", ["block", "unblock", "muteposts", "unmuteposts", "mutestories", "unmutestories", "archive", "mute", "unmute", "followrequests", "approve", "decline"]),
                ("🚀 Publishing", ["postphoto", "postvideo", "storypost"]),
                ("🤖 Automation", ["auto", "autolike", "autoview", "autostory", "autofollow", "autocomment", "autonote", "autowelcome", "autostorypost"]),
                ("🔒 Safety", ["risk", "quiet", "pause", "resume", "protocol", "protocolprobe", "realtime", "backup"]),
                ("📊 Limits", ["stats", "caps", "tasks", "cancel"]),
            ]
            lines = [
                "╔══════════════════════════════════════╗",
                "║     ⚡  InstaBaileys Command Center  ║",
                "╚══════════════════════════════════════╝",
                "",
                "Send any of these to yourself. Lists also come with a JSON file.",
                "",
            ]
            for title, names in groups:
                lines.append(f"▸ {title}")
                lines.append("─" * 36)
                for n in names:
                    help_text = self._commands.get(n, ("", None))[0]
                    lines.append(f"  /{n}")
                    if help_text:
                        lines.append(f"      {help_text}")
                lines.append("")
            lines.append(f"✦ {len(self._commands)} commands ready · turbo mode on")
            return "\n".join(lines)

        @self.register("whoami", "the account this bot is running as")
        def _whoami(args, thread):
            me = self.client.whoami()
            return (
                f"@{me.username} ({me.full_name})\n"
                f"id: {me.user_id}\n"
                f"{me.follower_count} followers · {me.following_count} following · {me.media_count} posts"
            )

        @self.register("status", "session, counters and what the bot is doing")
        def _status(args, thread):
            lines = []
            if self.session is not None:
                st = self.session.status()
                lines.append(f"session: {'ok' if st.ok else 'down'} ({st.mode}) as @{st.username}")
            if self.guardrails is not None:
                s = self.guardrails.stats()
                lines.append(
                    f"replies today: {s['replies_today']} (hour {s['replies_this_hour']}) · "
                    f"inbound today: {s['inbound_today']}"
                )
                lines.append(
                    f"caps: {s['daily_cap']}/day per chat, {s['global_daily_cap']}/day global, "
                    f"cooldown {s['cooldown_seconds']}s"
                )
                if s.get("baseline_only"):
                    lines.append("baseline mode is ON — nothing is sent")
            return "\n".join(lines) or "no status available"

        @self.register("health", "check the session and the API round trip")
        def _health(args, thread):
            start = time.time()
            data = self.client.health()
            ms = int((time.time() - start) * 1000)
            data["latency_ms"] = ms
            return "✅ " + " · ".join(f"{k}: {v}" for k, v in data.items() if v)

        @self.register("relogin", "rebuild the session from the saved cookie")
        def _relogin(args, thread):
            if self.session is None:
                raise CommandError("no session manager is attached")
            self.session.rebuild()
            st = self.session.status()
            return f"✅ session rebuilt as @{st.username} ({st.mode})"

        @self.register("ping", "how long Instagram takes to answer")
        def _ping(args, thread):
            start = time.time()
            self.client.whoami()
            ms = int((time.time() - start) * 1000)
            return f"🏓 {ms} ms"

        @self.register("uptime", "how long the bot has been running")
        def _uptime(args, thread):
            secs = int(time.time() - self.started_at)
            return f"⏱ {secs // 3600}h {(secs % 3600) // 60}m {secs % 60}s"

        @self.register("about", "what this is")
        def _about(args, thread):
            return (
                "InstaBaileys — the Baileys for Instagram.\n"
                "Talks to Instagram's own API over plain HTTP: no browser, "
                "~60 MB of RAM, and it stays logged in."
            )

        @self.register("version", "the version")
        def _version(args, thread):
            from . import __version__

            return f"InstaBaileys {__version__}"

    # ---------------------------------------------------------------- inbox
    def _register_inbox(self) -> None:
        @self.register("inbox", "the conversations in the inbox")
        def _inbox(args, thread):
            threads = self.client.threads(amount=20)
            if not threads:
                return "the inbox is empty"
            lines = []
            for t in threads:
                last = t.messages[0].text if t.messages else ""
                flag = "📥" if t.unread else "  "
                lines.append(f"{flag} {t.peer}: {self._short(last)}")
            return "\n".join(lines)

        @self.register("threads", "the same inbox, with ids")
        def _threads(args, thread):
            threads = self.client.threads(amount=20)
            lines = []
            for t in threads:
                last = t.messages[0].text if t.messages else ""
                lines.append(f"{t.id} · {t.peer} · {self._short(last, 50)}")
            return "\n".join(lines) or "the inbox is empty"

        @self.register("unread", "only the conversations with unread messages")
        def _unread(args, thread):
            threads = [t for t in self.client.threads(amount=40) if t.unread]
            if not threads:
                return "nothing unread"
            return "\n".join(
                f"{t.peer}: {self._short(t.messages[0].text if t.messages else '')}" for t in threads
            )

        @self.register("last", "the last messages of this conversation")
        def _last(args, thread):
            count = int(args[0]) if args and args[0].isdigit() else 5
            if thread is None:
                raise CommandError("this command only works inside a conversation")
            lines = [f"last {count} messages:"]
            for m in thread.messages[:count]:
                who = "me" if m.is_self else (m.username or "them")
                lines.append(f"  {who}: {self._short(m.text, 120)}")
            return "\n".join(lines)

        @self.register("requests", "message requests waiting for approval")
        def _requests(args, thread):
            pending = self.client.pending_threads(amount=20)
            if not pending:
                return "no message requests"
            return "\n".join(
                f"{t.id} · {t.peer}: {self._short(t.messages[0].text if t.messages else '')}"
                for t in pending
            )

        @self.register("accept", "accept a message request: /accept <id>")
        def _accept(args, thread):
            if not args:
                raise CommandError("which request? /requests lists them")
            ok = self.client.approve_request(args[0])
            return "✅ accepted" if ok else "❌ could not accept that request"

        @self.register("reply", "reply to this conversation: /reply <text>")
        def _reply(args, thread):
            if not args:
                raise CommandError("what should I say?")
            if thread is None:
                raise CommandError("this command only works inside a conversation")
            sent = self.client.send_text(thread.id, " ".join(args))
            return "✅ sent" if sent.ok else f"❌ {sent.detail}"

        @self.register("send", "message someone: /send @user <text>")
        def _send(args, thread):
            if len(args) < 2:
                raise CommandError("usage: /send @user <message>")
            user, text = args[0].lstrip("@"), " ".join(args[1:])
            sent = self.client.send_to_user(user, text)
            return f"✅ sent to @{user}" if sent.ok else f"❌ {sent.detail}"

        @self.register("react", "react to the last message: /react ❤️")
        def _react(args, thread):
            if thread is None or not thread.messages:
                raise CommandError("no message to react to")
            emoji = args[0] if args else "❤️"
            msg = thread.messages[0]
            ok = self.client.react(thread.id, msg.id, emoji)
            return f"✅ reacted {emoji}" if ok else "❌ the reaction was refused"

        @self.register("clear", "forget what has been answered in this chat")
        def _clear(args, thread):
            if self.store is None or thread is None:
                raise CommandError("nothing to clear")
            n = self.store.clear_fingerprints(thread.id)
            return f"✅ cleared {n} remembered messages from this chat"

    # -------------------------------------------------------------- content
    def _register_content(self) -> None:
        @self.register("stories", "who has stories: /stories [user]")
        def _stories(args, thread):
            if args:
                user = args[0].lstrip("@")
                stories = self.client.stories(user)
                if not stories:
                    return f"@{user} has no stories right now"
                return "\n".join(f"story {s.pk} ({s.item_type})" for s in stories)
            me = self.client.whoami()
            following = self.client.connections("following", me.username, 0)
            found = self._parallel(following, lambda p: (p, self.client.stories(p.username)))
            lines = []
            for item in found:
                if not item:
                    continue
                person, stories = item
                if stories:
                    lines.append(f"@{person.username}: {len(stories)} story")
            return "\n".join(lines) or "nobody you follow has a story right now"

        @self.register("storyview", "view (and mark seen) a story: /storyview [user]")
        def _storyview(args, thread):
            user = self._target_user(args) if args else self.client.whoami().username
            stories = self.client.stories(user)
            if not stories:
                return f"@{user} has no stories"
            ok = self.client.view_stories([s.pk for s in stories])
            return f"✅ viewed {len(stories)} stories from @{user}" if ok else "❌ could not mark them seen"

        @self.register("storylike", "like every story of an account: /storylike [user]")
        def _storylike(args, thread):
            user = self._target_user(args) if args else self.client.whoami().username
            stories = self.client.stories(user)
            if not stories:
                return f"@{user} has no stories"
            liked = 0
            for story in stories:
                if self._allowed("story_like", user):
                    if self.client.like_story(story.pk):
                        liked += 1
                        if self.guardrails is not None:
                            self.guardrails.record_action("story_like", target=user, detail=story.pk)
                else:
                    break
            return f"✅ liked {liked} of {len(stories)} stories from @{user}"

        @self.register("storiesall", "pull story tray and like every active story")
        def _storiesall(args, thread):
            # Baileys-style: one tray pull → like everything active. No following walk.
            if hasattr(self.client, "like_tray_stories"):
                liked, accounts = self.client.like_tray_stories()
                return f"✅ liked {liked} stories from {accounts} accounts (tray)"
            # Fallback
            tray = self.client.stories_tray(fill_items=True) if hasattr(self.client, "stories_tray") else {}
            total = 0
            for uname, stories in tray.items():
                for story in stories:
                    if story.pk and self.client.like_story(story.pk):
                        total += 1
            return f"✅ liked {total} stories from {len(tray)} accounts (tray)"

        @self.register("viewall", "pull story tray and mark every active story seen")
        def _viewall(args, thread):
            # Baileys-style: one tray pull → mark all seen.
            if hasattr(self.client, "view_tray_stories"):
                viewed, accounts = self.client.view_tray_stories()
                return f"✅ viewed {viewed} stories from {accounts} accounts (tray)"
            tray = self.client.stories_tray(fill_items=True) if hasattr(self.client, "stories_tray") else {}
            all_pks = [s.pk for stories in tray.values() for s in stories if s.pk]
            if all_pks:
                self.client.view_stories(all_pks)
            return (
                f"✅ viewed {len(all_pks)} stories from {len(tray)} accounts (tray)"
                if all_pks
                else "no active stories right now"
            )

        @self.register("storyreply", "reply to a story as a DM: /storyreply <user> <text>")
        def _storyreply(args, thread):
            if len(args) < 2:
                raise CommandError("usage: /storyreply @user <text>")
            user, text = args[0].lstrip("@"), " ".join(args[1:])
            sent = self.client.send_to_user(user, text)
            return f"✅ sent to @{user}" if sent.ok else f"❌ {sent.detail}"

        @self.register("post", "read a post: /post <url>")
        def _post(args, thread):
            if not args:
                raise CommandError("usage: /post <url>")
            m = self.client.media(args[0])
            return (
                f"@{m.username}\n{self._short(m.caption, 300)}\n"
                f"❤️ {m.like_count} · 💬 {m.comment_count}\n{m.url}"
            )

        @self.register("like", "like a post: /like <url>")
        def _like(args, thread):
            if not args:
                raise CommandError("usage: /like <url>")
            m = self.client.media(args[0])
            ok = self.client.like_media(m.pk)
            return f"✅ liked @{m.username}'s post" if ok else "❌ the like was refused"

        @self.register("unlike", "unlike a post: /unlike <url>")
        def _unlike(args, thread):
            if not args:
                raise CommandError("usage: /unlike <url>")
            m = self.client.media(args[0])
            ok = self.client.unlike_media(m.pk)
            return "✅ unliked" if ok else "❌ could not unlike"

        @self.register("comments", "read the comments: /comments <url>")
        def _comments(args, thread):
            if not args:
                raise CommandError("usage: /comments <url>")
            m = self.client.media(args[0])
            comments = self.client.comments(m.pk, amount=10)
            if not comments:
                return "no comments"
            return "\n".join(f"@{c.username}: {self._short(c.text, 80)}" for c in comments)

        @self.register("comment", "comment on a post: /comment <url> <text>")
        def _comment(args, thread):
            if len(args) < 2:
                raise CommandError("usage: /comment <url> <text>")
            m = self.client.media(args[0])
            text = " ".join(args[1:])
            done = self.client.comment(m.pk, text)
            return "✅ commented" if done else "❌ the comment was refused"

        @self.register("feed", "recent posts: /feed [user]")
        def _feed(args, thread):
            user = args[0].lstrip("@") if args else self.client.whoami().username
            posts = self.client.user_posts(user, amount=8)
            if not posts:
                return f"@{user} has no posts"
            return "\n".join(
                f"{p.pk} · ❤️ {p.like_count} · 💬 {p.comment_count} · {self._short(p.caption, 60)}"
                for p in posts
            )

    # --------------------------------------------------------------- people
    def _register_people(self) -> None:
        @self.register("profile", "read a profile: /profile @user")
        def _profile(args, thread):
            user = self._target_user(args)
            p = self.client.profile(user)
            return (
                f"@{p.username} ({p.full_name})\n"
                f"{p.follower_count} followers · {p.following_count} following · {p.media_count} posts\n"
                f"{self._short(p.biography, 200)}"
            )

        @self.register("follow", "follow someone: /follow @user")
        def _follow(args, thread):
            user = self._target_user(args)
            ok = self.client.follow(user)
            if ok and self.guardrails is not None:
                self.guardrails.record_action("follow", target=user)
            return f"✅ following @{user}" if ok else f"❌ could not follow @{user}"

        @self.register("unfollow", "unfollow someone: /unfollow @user")
        def _unfollow(args, thread):
            user = self._target_user(args)
            ok = self.client.unfollow(user)
            return f"✅ unfollowed @{user}" if ok else f"❌ could not unfollow @{user}"

        @self.register("followers", "every follower: /followers [user] [n|all]")
        def _followers(args, thread):
            user, limit = self._user_and_limit(args)
            if not user:
                user = self.client.whoami().username
            people = self.client.connections("followers", user, limit)
            if not people:
                return "no followers to show"
            path = self._dump("followers", user, people)
            names = [p.username for p in people]
            lines = [f"{len(people)} followers of @{user}:"]
            lines += [f"@{n}" for n in names[:60]]
            if len(people) > 60:
                lines.append(f"… and {len(people) - 60} more")
            return self._with_json("\n".join(lines), names, f"followers_{user}")

        @self.register("following", "everyone you follow: /following [user] [n|all]")
        def _following(args, thread):
            user, limit = self._user_and_limit(args)
            if not user:
                user = self.client.whoami().username
            people = self.client.connections("following", user, limit)
            if not people:
                return "not following anyone"
            path = self._dump("following", user, people)
            names = [p.username for p in people]
            lines = [f"{len(people)} accounts @{user} follows:"]
            lines += [f"@{n}" for n in names[:60]]
            if len(people) > 60:
                lines.append(f"… and {len(people) - 60} more")
            return self._with_json("\n".join(lines), names, f"following_{user}")

        @self.register("followback", "follow the people who followed you")
        def _followback(args, thread):
            me = self.client.whoami()
            followers = {p.username for p in self.client.connections("followers", me.username, 0)}
            following = {p.username for p in self.client.connections("following", me.username, 0)}
            targets = sorted(followers - following)
            if not targets:
                return "nobody new to follow back"
            done = []
            for name in targets:
                if self.guardrails is not None:
                    decision = self.guardrails.check_action("follow", target=name)
                    if not decision.allowed:
                        continue
                if self.client.follow(name):
                    if self.guardrails is not None:
                        self.guardrails.record_action("follow", target=name)
                    done.append(name)
            if not done:
                return "nothing new to follow back (the daily cap may be reached)"
            return "✅ followed back: " + ", ".join(f"@{n}" for n in done)

    # ----------------------------------------------------------- the graph
    def _register_graph(self) -> None:
        """The commands that read the whole account, not just the first page."""

        @self.register("scan", "read every follower and following: /scan")
        def _scan(args, thread):
            summary = self.scanner.scan(kinds=("followers", "following"))
            lists = summary.get("lists", {})
            parts = []
            for kind, result in lists.items():
                mark = "✅" if result.get("ok") else "❌"
                parts.append(
                    f"{mark} {kind}: {result.get('count', 0)} in {result.get('elapsed_ms', 0)} ms"
                )
            lines = ["the picture is taken:", *parts]
            if not summary.get("ok", True):
                errors = [
                    f"{kind}: {result.get('error')}"
                    for kind, result in lists.items()
                    if not result.get("ok") and result.get("error")
                ]
                if errors:
                    lines.append("⚠️ " + "; ".join(errors))
            for warning in summary.get("warnings", []) or []:
                lines.append(f"⚠️ {warning}")
            if "diff" in summary:
                lines.append(
                    f"non-followers: {summary.get('nonfollowers', 0)} · "
                    f"mutuals: {summary.get('mutuals', 0)} · "
                    f"fans: {summary.get('fans', 0)}"
                )
            lines.append(f"engine: {summary.get('engine')} · {summary.get('elapsed_ms', 0)} ms")
            lines.append(f"kept in: {summary.get('folder')}")
            return "\n".join(lines)

        @self.register("nonfollowers", "the people you follow that do not follow back")
        def _nonfollowers(args, thread):
            rows = self._graph("nonfollowers")
            if not rows:
                return "everyone you follow follows you back"
            limit = self._limit(args[1:]) or 40
            names = [str(row.get("username") or row.get("pk")) for row in rows]
            lines = [f"{len(rows)} people you follow do not follow you back:"]
            for name in names[:limit]:
                lines.append(f"@{name}")
            if len(rows) > limit:
                lines.append(f"… and {len(rows) - limit} more")
            lines.append("/unfollowers removes them")
            return self._with_json("\n".join(lines), names, "nonfollowers")

        @self.register("fans", "the people who follow you that you do not follow back")
        def _fans(args, thread):
            rows = self._graph("fans")
            if not rows:
                return "you follow everybody who follows you"
            limit = self._limit(args[1:]) or 40
            names = [str(row.get("username") or row.get("pk")) for row in rows]
            lines = [f"{len(rows)} people follow you but you do not follow back:"]
            for name in names[:limit]:
                lines.append(f"@{name}")
            if len(rows) > limit:
                lines.append(f"… and {len(rows) - limit} more")
            return self._with_json("\n".join(lines), names, "fans")

        @self.register("unfollowers", "unfollow every non-follower now. Add 'dry' to only preview.")
        def _unfollowers(args, thread):
            """Remove every non-follower.

            Forces a fresh scan so the list is accurate, then unfollows each
            one. No daily "already done" skips and no hourly/daily caps —
            this is an explicit cleanup order.
            """
            # 1. Always refresh the graph so the list is current
            try:
                self.scanner.scan(kinds=("followers", "following"))
            except Exception as exc:  # noqa: BLE001
                log.warning("pre-unfollow scan failed (using last graph): %s", exc)

            rows = self._graph("nonfollowers")
            if not rows:
                return "there is nobody to remove — graph is clean"

            limit = self._limit(args[:1], default=int(self.cfg.get("unfollow.limit", 0) or 0))
            dry = any(a.lower() in ("dry", "rehearse", "preview", "plan", "list") for a in args)
            whitelist = {
                name.strip().lstrip("@").lower()
                for name in str(self.cfg.get("unfollow.whitelist", "") or "").split(",")
                if name.strip()
            }
            skip_verified = bool(self.cfg.get("unfollow.skip_verified", False))
            skip_private = bool(self.cfg.get("unfollow.skip_private", False))

            targets = []
            for row in rows:
                name = str(row.get("username") or "")
                if name and name.lower() in whitelist:
                    continue
                if skip_verified and row.get("is_verified"):
                    continue
                if skip_private and row.get("is_private"):
                    continue
                targets.append(row)
                if limit and len(targets) >= limit:
                    break

            if not targets:
                return "everybody on the list is on the whitelist"

            if dry:
                lines = [
                    f"🧪 dry-run — {len(targets)} account(s) would be unfollowed:",
                    *[f"@{row.get('username') or row.get('pk')}" for row in targets[:40]],
                ]
                if len(targets) > 40:
                    lines.append(f"… and {len(targets) - 40} more")
                lines.append("send /unfollowers  (no args) to do it for real")
                return self._with_json("\n".join(lines), [r.get("username") for r in targets], "unfollowers_dry")

            # Gaps only — no guardrail caps / no "already done today"
            turbo = bool(self.cfg.get("speed.turbo", True))
            gap_min = float(self.cfg.get("unfollow.gap_min", 2) or 0)
            gap_max = float(self.cfg.get("unfollow.gap_max", 5) or 0)
            if turbo:
                gap_min = min(gap_min, 1.5)
                gap_max = min(gap_max, 3.0)
            if gap_max < gap_min:
                gap_max = gap_min

            done: list[str] = []
            failed: list[str] = []
            for row in targets:
                name = str(row.get("username") or "")
                pk = str(row.get("pk") or row.get("user_id") or "")
                try:
                    # Prefer numeric pk. Never blocked by "already done" or hourly caps.
                    ok = self.client.unfollow_by_id(pk, name)
                except Exception as exc:  # noqa: BLE001
                    failed.append(f"@{name or pk} ({type(exc).__name__})")
                    log.warning("unfollow failed for @%s (pk=%s): %s", name, pk, exc)
                    ok = False
                if ok:
                    done.append(name or pk)
                    # Record for history only — never used to block this command
                    try:
                        if self.store is not None:
                            self.store.record_action("unfollow", target=name or pk, detail="non-follower")
                    except Exception:  # noqa: BLE001
                        pass
                else:
                    if not any((name and name in f) or (pk and pk in f) for f in failed):
                        failed.append(f"@{name or pk} (refused by Instagram)")
                if gap_max > 0:
                    time.sleep(gap_min + (time.time() % max(0.001, gap_max - gap_min)))

            lines = [
                f"✅ unfollowed {len(done)} / {len(targets)} account(s)",
            ]
            if failed:
                lines.append(f"⚠️  {len(failed)} refused by Instagram: " + ", ".join(failed[:15]))
                if len(failed) > 15:
                    lines.append(f"… and {len(failed) - 15} more")
            return self._with_json("\n".join(lines), {"done": done, "failed": failed}, "unfollowers_done")

        @self.register("engine", "which engine is working and how fast it is")
        def _engine(args, thread):
            engine = self.scanner.engine
            binary = engine.binary()
            lines = [
                f"engine    : {engine.engine()}",
                f"binary    : {binary or 'not built — Python is doing the work'}",
                f"page size : {self.cfg.get('speed.page_size', 200)}",
                f"parallel  : {self.cfg.get('speed.commands_parallel', 8)} at a time",
                f"turbo     : {'on' if self.cfg.get('speed.turbo', True) else 'off'}",
            ]
            if self.store is not None:
                last = self.store.last_scan()
                if last:
                    lines.append(
                        f"last scan : {last.get('kind')} · {last.get('count')} · "
                        f"{time.strftime('%Y-%m-%d %H:%M', time.localtime(last.get('ts') or 0))}"
                    )
                counts = self.store.connection_counts()
                lines.append(
                    f"graph     : {counts['followers']} followers · "
                    f"{counts['following']} following"
                )
            return "\n".join(lines)

    # ---------------------------------------------------------------- notes
    def _register_notes(self) -> None:
        @self.register("notes", "every note in your inbox")
        def _notes(args, thread):
            notes = self.client.notes()
            if not notes:
                return "no notes right now"
            with_text = [n for n in notes if n.text]
            lines = [f"{len(notes)} notes ({len(with_text)} with text):"]
            lines += [f"@{n.username}: {self._short(n.text, 90)}" for n in with_text[:40]]
            if len(with_text) > 40:
                lines.append(f"… and {len(with_text) - 40} more")
            return "\n".join(lines)

        @self.register("note", "one person's note: /note @user")
        def _note(args, thread):
            user = self._target_user(args)
            for n in self.client.notes():
                if n.username.lower() == user.lower():
                    return f"@{n.username}: {n.text}"
            return f"@{user} has no note right now"

        @self.register("notelike", "like every note in the inbox")
        def _notelike(args, thread):
            """Put a heart on every note that is there.

            Instagram never built a like button for notes. What it did build
            is a reaction on the message itself, and that is what the bot
            uses — a heart on the note, sent the same way you would send it
            by hand.
            """
            notes = self.client.notes()
            if not notes:
                return "no notes right now"
            liked = 0
            missed: list[str] = []
            for note in notes:
                if not note.text:
                    continue
                if self._allowed("like", note.username):
                    if self._like_note(note):
                        liked += 1
                        if self.guardrails is not None:
                            self.guardrails.record_action("like", target=note.username, detail="note")
                    else:
                        missed.append(f"@{note.username}")
                else:
                    break
            lines = [f"✅ liked {liked} of {len(notes)} notes"]
            if missed:
                lines.append("Instagram refused: " + ", ".join(missed[:10]))
            return "\n".join(lines)

        def _like_note(self, note) -> bool:
            """Like one note — a reaction on the message, or the story endpoint."""
            thread_id = str(getattr(note, "thread_id", "") or "")
            message_id = str(getattr(note, "id", "") or "")
            if thread_id and message_id:
                try:
                    if self.client.react(thread_id, message_id, "❤️"):
                        return True
                except Exception:  # noqa: BLE001
                    pass
            media_id = str(getattr(note, "media_id", "") or "")
            if media_id:
                try:
                    if self.client.like_story(media_id):
                        return True
                except Exception:  # noqa: BLE001
                    pass
            return False

    # ------------------------------------------------------------ automation
    def _register_automation(self) -> None:
        toggles = [
            ("like", "autolike", "like posts from the people you follow"),
            ("view", "autoview", "view the stories of the people you follow"),
            ("story", "autostory", "like the stories of the people you follow"),
            ("follow", "autofollow", "follow back new followers"),
            ("comment", "autocomment", "answer comments on your posts"),
            ("note", "autonote", "read the notes that arrive"),
            ("welcome", "autowelcome", "say hello to new followers"),
            ("storypost", "autostorypost", "publish a story from the story folder"),
        ]

        @self.register("auto", "show or set the automatic actions: /auto <name> on|off")
        def _auto(args, thread):
            if not args:
                lines = ["automatic actions:"]
                for key, command, help_text in toggles:
                    lines.append(f"  /{command} — {help_text}: {'ON' if self._autoflag(key) else 'off'}")
                return "\n".join(lines)
            name = args[0].lower().lstrip("/")
            match = next((t for t in toggles if t[0] == name or t[1] == name), None)
            if match is None:
                raise CommandError("unknown automatic action. /auto lists them.")
            key, command, _ = match
            if len(args) < 2:
                return f"/{command} is {'ON' if self._autoflag(key) else 'off'}"
            value = args[1].lower() in ("on", "1", "true", "yes", "enable", "enabled")
            self._set_autoflag(key, value)
            return f"✅ /{command} is now {'ON' if value else 'off'}"

        def make_toggle(key: str, command: str, help_text: str):
            @self.register(command, help_text)
            def _toggle(args, thread):
                value = not self._autoflag(key) if not args else args[0].lower() in (
                    "on", "1", "true", "yes", "enable", "enabled"
                )
                self._set_autoflag(key, value)
                return f"✅ /{command} is now {'ON' if value else 'off'}"

        for key, command, help_text in toggles:
            make_toggle(key, command, help_text)

        @self.register("stats", "the counters")
        def _stats(args, thread):
            if self.store is None:
                return "no store attached"
            out = ["recent actions:"]
            for row in self.store.recent_actions(limit=10):
                out.append(f"  {row['kind']} {row['target'] or ''} {row['detail'] or ''}".rstrip())
            return "\n".join(out)

        @self.register("caps", "how much of today's budget is used")
        def _caps(args, thread):
            if self.guardrails is None:
                return "no guardrails attached"
            stats = self.guardrails.action_stats()
            lines = ["visible actions today:"]
            for k, v in stats.items():
                lines.append(f"  {k}: {v}")
            return "\n".join(lines)

        @self.register("tasks", "the task queue")
        def _tasks(args, thread):
            if self.store is None:
                return "no store attached"
            rows = self.store.list_tasks(limit=15)
            if not rows:
                return "the queue is empty"
            return "\n".join(f"#{r['id']} {r['kind']} · {r['detail'] or ''}" for r in rows)

        @self.register("cancel", "cancel a queued task: /cancel <id>")
        def _cancel(args, thread):
            if not args:
                raise CommandError("which task? /tasks lists them")
            if self.store is None:
                return "no store attached"
            ok = self.store.cancel_task(int(args[0]))
            return "✅ cancelled" if ok else "❌ no such task"

    # ------------------------------------------------------------------ ops
    def _register_ops(self) -> None:
        @self.register("risk", "the risk dashboard: budget, safe mode, warm-up")
        def _risk(args, thread):
            if self.risk is None:
                return "the risk guard is not attached"
            s = self.risk.score()
            lines = [
                f"safe mode     : {'ON — ' + s['safe_reason'] if s['safe_mode'] else 'off'}",
                f"visible today : {s['visible_today']}/{s['daily_ceiling']} "
                f"(full ceiling {s['full_ceiling']})",
                f"warm-up       : ×{s['warmup_factor']} (day {s['account_age_days']})",
                f"requests      : {s['requests_this_hour']}/{s['request_budget']} per hour",
                f"burst         : {s['burst_used']}/{s['burst_limit']} "
                f"in {self.risk.burst_window // 60} min",
                f"gap           : {int(s['gap_seconds'][0])}–{int(s['gap_seconds'][1])}s between actions",
                f"de-duplicate  : {s['dedupe_hours']}h",
                f"breaches      : {s['breaches']}",
            ]
            return "\n".join(lines)

        @self.register("protocol", "which app identity the bot is wearing")
        def _protocol(args, thread):
            if self.protocol is None:
                return "the protocol manager is not attached"
            state = self.protocol.state
            return (
                f"app id        : {state.identity.app_id}\n"
                f"version       : {state.identity.version} ({state.identity.version_code})\n"
                f"device        : {state.identity.manufacturer} {state.identity.model}\n"
                f"working       : {state.working} — {state.detail}"
            )

        @self.register("protocolprobe", "test every known app identity")
        def _protocol_probe(args, thread):
            if self.protocol is None:
                return "the protocol manager is not attached"
            results = self.protocol.probe(self.client)
            lines = []
            for r in results:
                mark = "✅" if r["ok"] else "❌"
                lines.append(f"{mark} {r['version']} ({r['app_id']}) — {r['detail']} [{r['ms']} ms]")
            return "\n".join(lines)

        @self.register("realtime", "the push channel: connected, messages, reconnects")
        def _realtime(args, thread):
            if self.realtime is None:
                return "the push channel is not attached"
            h = self.realtime.health()
            return "\n".join(f"{k}: {v}" for k, v in h.items())

        @self.register("quiet", "no visible actions for a while: /quiet <minutes>")
        def _quiet(args, thread):
            if self.store is None:
                return "no store attached"
            minutes = int(args[0]) if args and args[0].isdigit() else 60
            until = time.time() + minutes * 60
            self.store.kv_set("quiet_until", str(until))
            return f"🤫 no visible actions for {minutes} minutes"

        @self.register("backup", "copy the state and the session into backups/")
        def _backup(args, thread):
            if self.store is None:
                return "no store attached"
            import shutil
            from pathlib import Path as _Path

            root = _Path(str(self.cfg.root))
            target = root / "backups"
            target.mkdir(parents=True, exist_ok=True)
            stamp = time.strftime("%Y%m%d-%H%M%S")
            out = []
            for name in ("state/igbot.db", "state/session.json", "state/protocol.json"):
                src = root / name
                if src.exists():
                    dest = target / f"{stamp}-{_Path(name).name}"
                    shutil.copy2(src, dest)
                    out.append(dest.name)
            if not out:
                return "nothing to back up yet"
            return "✅ backed up: " + ", ".join(out)

        @self.register("pause", "stop answering for an hour")
        def _pause(args, thread):
            return self.run("/quiet 60", thread)

        @self.register("resume", "start answering again")
        def _resume(args, thread):
            if self.store is None:
                return "no store attached"
            self.store.kv_set("quiet_until", "0")
            if self.risk is not None:
                self.risk.reset()
            return "✅ the bot is active again"

    # ---------------------------------------------------------- moderation
    def _register_moderation(self) -> None:
        @self.register("block", "block an account")
        def _block(args, thread):
            user = self._target_user(args)
            ok = self.client.block(user)
            return f"🚫 blocked @{user}" if ok else f"❌ could not block @{user}"

        @self.register("unblock", "unblock an account")
        def _unblock(args, thread):
            user = self._target_user(args)
            ok = self.client.unblock(user)
            return f"✅ unblocked @{user}" if ok else f"❌ could not unblock @{user}"

        @self.register("muteposts", "stop seeing an account's posts")
        def _muteposts(args, thread):
            user = self._target_user(args)
            ok = self.client.mute_posts(user)
            return f"🔇 @{user}'s posts are muted" if ok else "❌ could not mute"

        @self.register("unmuteposts", "see an account's posts again")
        def _unmuteposts(args, thread):
            user = self._target_user(args)
            ok = self.client.unmute_posts(user)
            return f"🔊 @{user}'s posts are back" if ok else "❌ could not unmute"

        @self.register("mutestories", "stop seeing an account's stories")
        def _mutestories(args, thread):
            user = self._target_user(args)
            ok = self.client.mute_stories(user)
            return f"🔇 @{user}'s stories are muted" if ok else "❌ could not mute"

        @self.register("unmutestories", "see an account's stories again")
        def _unmutestories(args, thread):
            user = self._target_user(args)
            ok = self.client.unmute_stories(user)
            return f"🔊 @{user}'s stories are back" if ok else "❌ could not unmute"

        @self.register("archive", "hide a conversation from the inbox")
        def _archive(args, thread):
            if not args:
                raise CommandError("which conversation? /threads lists the ids")
            ok = self.client.archive_thread(args[0])
            return "📦 archived" if ok else "❌ could not archive it"

        @self.register("mute", "mute a conversation")
        def _mute(args, thread):
            if not args:
                raise CommandError("which conversation? /threads lists the ids")
            ok = self.client.mute_thread(args[0])
            return "🔇 muted" if ok else "❌ could not mute it"

        @self.register("unmute", "unmute a conversation")
        def _unmute(args, thread):
            if not args:
                raise CommandError("which conversation? /threads lists the ids")
            ok = self.client.unmute_thread(args[0])
            return "🔊 unmuted" if ok else "❌ could not unmute it"

        @self.register("followrequests", "the accounts waiting to follow you")
        def _followrequests(args, thread):
            people = self.client.follow_requests(amount=20)
            if not people:
                return "nobody is waiting"
            return "\n".join(f"@{p.username}" for p in people)

        @self.register("approve", "accept a follow request: /approve @user")
        def _approve(args, thread):
            user = self._target_user(args)
            ok = self.client.approve_follow_request(user)
            return f"✅ @{user} can follow you now" if ok else f"❌ could not approve @{user}"

        @self.register("decline", "refuse a follow request: /decline @user")
        def _decline(args, thread):
            user = self._target_user(args)
            ok = self.client.decline_follow_request(user)
            return f"✅ refused @{user}" if ok else f"❌ could not decline @{user}"

    # ----------------------------------------------------------- discovery
    def _register_discovery(self) -> None:
        @self.register("search", "find an account by name")
        def _search(args, thread):
            if not args:
                raise CommandError("what should I look for?")
            query = " ".join(args)
            people = self.client.search_users(query, amount=10)
            if not people:
                return f"nobody found for {query!r}"
            return "\n".join(f"@{p.username} — {p.full_name}" for p in people)

        @self.register("dmsearch", "search inside the conversations")
        def _dmsearch(args, thread):
            if not args:
                raise CommandError("what should I look for?")
            query = " ".join(args).lower()
            found = []
            for t in self.client.threads(amount=30):
                for m in t.messages:
                    if query in (m.text or "").lower():
                        found.append(f"{t.peer}: {self._short(m.text, 70)}")
                        break
            return "\n".join(found[:15]) or "nothing found"

        @self.register("dms", "read a conversation: /dms <thread id>")
        def _dms(args, thread):
            if not args:
                raise CommandError("which conversation? /threads lists the ids")
            messages = self.client.thread_messages(args[0], amount=20)
            if not messages:
                return "no messages"
            return "\n".join(
                f"  {'me' if m.is_self else (m.username or 'them')}: {self._short(m.text, 100)}"
                for m in messages
            )

        @self.register("mutuals", "who follows you and you follow back")
        def _mutuals(args, thread):
            rows = self._graph("mutuals")
            if not rows:
                return "no mutual followers"
            limit = self._limit(args[1:]) or 40
            lines = [f"{len(rows)} people follow you and you follow back:"]
            for row in rows[:limit]:
                lines.append(f"@{row.get('username') or row.get('pk')}")
            if len(rows) > limit:
                lines.append(f"… and {len(rows) - limit} more")
            return "\n".join(lines)

        @self.register("reels", "the reels of an account: /reels [user]")
        def _reels(args, thread):
            user = args[0].lstrip("@") if args else self.client.whoami().username
            reels = self.client.reels(user, amount=10)
            if not reels:
                return f"@{user} has no reels"
            return "\n".join(f"{r.pk} · ❤️ {r.like_count} · {self._short(r.caption, 50)}" for r in reels)

        @self.register("highlights", "the highlights of an account")
        def _highlights(args, thread):
            user = args[0].lstrip("@") if args else self.client.whoami().username
            items = self.client.highlights(user)
            if not items:
                return f"@{user} has no highlights"
            return "\n".join(f"{h['id']} · {h['title']} ({h['items']} items)" for h in items)

        @self.register("viewers", "who saw a story: /viewers <story id>")
        def _viewers(args, thread):
            if not args:
                raise CommandError("which story? /stories gives the ids")
            people = self.client.story_viewers(args[0])
            if not people:
                return "no viewers, or the list is not visible"
            return "\n".join(f"@{p.username}" for p in people)

        @self.register("insights", "the account's numbers (business accounts)")
        def _insights(args, thread):
            data = self.client.insights()
            if not data:
                return "no insights for this account"
            return "\n".join(f"{k}: {v}" for k, v in list(data.items())[:15])

    # ---------------------------------------------------------- publishing
    def _register_publishing(self) -> None:
        @self.register("postphoto", "publish a photo: /postphoto <file> [caption]")
        def _postphoto(args, thread):
            if not args:
                raise CommandError("usage: /postphoto <file> [caption]")
            caption = " ".join(args[1:])
            media = self.client.publish_photo(args[0], caption)
            return f"✅ posted {media.url}" if media else "❌ the upload failed"

        @self.register("postvideo", "publish a video: /postvideo <file> [caption]")
        def _postvideo(args, thread):
            if not args:
                raise CommandError("usage: /postvideo <file> [caption]")
            caption = " ".join(args[1:])
            media = self.client.publish_video(args[0], caption)
            return f"✅ posted {media.url}" if media else "❌ the upload failed"

        @self.register("storypost", "publish a story: /storypost <file> [caption]")
        def _storypost(args, thread):
            if not args:
                raise CommandError("usage: /storypost <file> [caption]")
            caption = " ".join(args[1:])
            pk = self.client.publish_story(args[0], caption)
            return f"✅ story {pk} published" if pk else "❌ the upload failed"

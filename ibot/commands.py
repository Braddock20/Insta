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

import shlex
import time
from typing import Callable

from . import log
from .client.base import Thread

log = log.get(__name__)


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
                ("Basics", ["help", "whoami", "status", "health", "relogin", "ping", "uptime", "about", "version"]),
                ("Inbox", ["inbox", "threads", "unread", "last", "requests", "accept", "reply", "send", "react", "clear", "dms", "dmsearch"]),
                ("Stories", ["stories", "storyview", "storylike", "storyreply", "viewers", "highlights"]),
                ("Posts", ["post", "like", "unlike", "comments", "comment", "feed", "reels"]),
                ("People", ["profile", "follow", "unfollow", "followers", "following", "followback", "mutuals", "search"]),
                ("Notes", ["notes", "note"]),
                ("Moderation", ["block", "unblock", "muteposts", "unmuteposts", "mutestories", "unmutestories", "archive", "mute", "unmute", "followrequests", "approve", "decline"]),
                ("Publishing", ["postphoto", "postvideo", "storypost"]),
                ("Automation", ["auto", "autolike", "autoview", "autostory", "autofollow", "autocomment", "autonote", "autowelcome", "autostorypost"]),
                ("Safety", ["risk", "quiet", "pause", "resume", "protocol", "protocolprobe", "realtime", "backup"]),
                ("Limits", ["stats", "caps", "tasks", "cancel"]),
            ]
            lines = ["InstaBaileys — commands you can send to yourself:", ""]
            for title, names in groups:
                lines.append(f"*{title}*")
                for n in names:
                    help_text = self._commands.get(n, ("", None))[0]
                    lines.append(f"/{n} — {help_text}")
                lines.append("")
            lines.append(f"{len(self._commands)} commands.")
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
            following = self.client.following(me.username, amount=25)
            lines = []
            for person in following:
                try:
                    stories = self.client.stories(person.username)
                except Exception:  # noqa: BLE001
                    continue
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

        @self.register("storylike", "like a story: /storylike [user]")
        def _storylike(args, thread):
            user = self._target_user(args) if args else self.client.whoami().username
            stories = self.client.stories(user)
            if not stories:
                return f"@{user} has no stories"
            ok = self.client.like_story(stories[0].pk)
            return f"✅ liked @{user}'s story" if ok else "❌ the like was refused"

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

        @self.register("followers", "list followers: /followers [user]")
        def _followers(args, thread):
            user = args[0].lstrip("@") if args else self.client.whoami().username
            people = self.client.followers(user, amount=30)
            if not people:
                return "no followers to show"
            return "\n".join(f"@{p.username}" for p in people)

        @self.register("following", "list who you follow: /following [user]")
        def _following(args, thread):
            user = args[0].lstrip("@") if args else self.client.whoami().username
            people = self.client.following(user, amount=30)
            if not people:
                return "not following anyone"
            return "\n".join(f"@{p.username}" for p in people)

        @self.register("followback", "follow the people who followed you")
        def _followback(args, thread):
            me = self.client.whoami()
            followers = {p.username for p in self.client.followers(me.username, amount=50)}
            following = {p.username for p in self.client.following(me.username, amount=50)}
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

    # ---------------------------------------------------------------- notes
    def _register_notes(self) -> None:
        @self.register("notes", "the notes in your inbox")
        def _notes(args, thread):
            notes = self.client.notes()
            if not notes:
                return "no notes right now"
            return "\n".join(
                f"@{n.username}: {self._short(n.text, 90)}" for n in notes[:15] if n.text
            ) or "no notes with text right now"

        @self.register("note", "one person's note: /note @user")
        def _note(args, thread):
            user = self._target_user(args)
            for n in self.client.notes():
                if n.username.lower() == user.lower():
                    return f"@{n.username}: {n.text}"
            return f"@{user} has no note right now"

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
            me = self.client.whoami()
            followers = {p.username for p in self.client.followers(me.username, amount=100)}
            following = {p.username for p in self.client.following(me.username, amount=100)}
            both = sorted(followers & following)
            return "\n".join(f"@{n}" for n in both) or "no mutual followers"

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

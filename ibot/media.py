"""The automatic actions.

These are the visible things the bot does without being asked: viewing
stories, liking them, liking posts, following people back.

Every one of them goes through the guardrails first, because a visible
action is what makes an account look automated. The caps in
``behaviour.action_caps`` are deliberately much tighter than the DM reply
caps, and the same target is never touched twice in a day.

What Instagram does *not* allow is also recorded here, rather than
pretended: notes have no like control at all — not on the web, not in the
API. The app never shipped one. So the bot reads notes, and answers them
with a DM if you want it to, but there is nothing to like.
"""

from __future__ import annotations

import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

from . import log
from .client.base import Story

log = log.get(__name__)

AUTO_ACTIONS = (
    ("view", "story_view"),
    ("like", "like"),
    ("story", "story_like"),
    ("follow", "follow"),
)


class MediaBot:
    """Runs the automatic visible actions."""

    def __init__(self, client, cfg, store, guardrails, risk=None):
        self.client = client
        self.cfg = cfg
        self.store = store
        self.guardrails = guardrails
        self.risk = risk

    # ------------------------------------------------------------- helpers
    def _enabled(self, name: str, default: bool = False) -> bool:
        raw = self.store.kv_get(f"auto:{name}")
        if raw is None:
            return bool(self.cfg.get(f"automations.default_{name}", default))
        return str(raw).lower() in ("1", "true", "on", "yes")

    def _allowed(self, kind: str, target: str) -> bool:
        decision = self.guardrails.check_action(kind, target=target)
        if not decision.allowed:
            log.debug("skipped %s on %s: %s", kind, target, decision.reason)
            return False
        return True

    def _people_to_watch(self, amount: int = 0) -> list[str]:
        """The accounts the bot watches — the ones you follow."""
        me = self.client.whoami()
        people = self.client.connections("following", me.username, amount)
        return [p.username for p in people]

    def _graph(self, flag: str) -> list[dict]:
        """The connections graph, from the database.

        A scan runs when the graph is empty, so the automations always have
        the full picture and never the first thirty names.
        """
        if self.store is None:
            return []
        rows = self.store.connections(flag)
        if rows:
            return rows
        try:
            from .bulk import Scanner

            Scanner(self.client, self.store, self.cfg).scan()
        except Exception as exc:  # noqa: BLE001
            log.warning("the scan failed: %s", exc)
            return []
        return self.store.connections(flag)

    # ------------------------------------------------------------- stories
    def stories_of(self, usernames: list[str]) -> dict[str, list[Story]]:
        """Stories per account, skipping the accounts that have none.

        The accounts are read at the same time — reading two hundred
        accounts one after the other is what made the bot feel slow.
        """
        found: dict[str, list[Story]] = {}

        def fetch(name: str):
            try:
                return name, self.client.stories(name)
            except Exception as exc:  # noqa: BLE001
                log.debug("no stories for @%s: %s", name, exc)
                return name, None

        workers = int(self.cfg.get("speed.commands_parallel", 32) or 32)
        if workers > 1 and len(usernames) > 1:
            with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
                results = list(pool.map(fetch, usernames))
        else:
            results = [fetch(name) for name in usernames]
        for name, stories in results:
            if stories:
                found[name] = stories
        return found

    def auto_view_stories(self, usernames: list[str] | None = None) -> int:
        """View the stories of the accounts you follow — fast path."""
        if not self._enabled("view"):
            return 0
        names = usernames if usernames is not None else self._people_to_watch()
        count = 0
        found = self.stories_of(names)

        def _one(item):
            name, stories = item
            if not stories or not self._allowed("story_view", name):
                return 0
            pks = [s.pk for s in stories]
            if self.client.view_stories(pks):
                self.guardrails.record_action("story_view", target=name, detail=f"{len(pks)} stories")
                log.info("viewed %d stories from @%s", len(pks), name)
                return len(pks)
            return 0

        from concurrent.futures import ThreadPoolExecutor
        workers = int(self.cfg.get("speed.commands_parallel", 32) or 32)
        with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
            for n in pool.map(_one, found.items()):
                count += n
        return count

    def auto_like_stories(self, usernames: list[str] | None = None) -> int:
        """Like every story of the accounts you follow — fast path."""
        if not self._enabled("story"):
            return 0
        names = usernames if usernames is not None else self._people_to_watch()
        count = 0
        found = self.stories_of(names)

        def _one(item):
            name, stories = item
            if not stories or not self._allowed("story_like", name):
                return 0
            n = 0
            for story in stories:
                if self.client.like_story(story.pk):
                    self.guardrails.record_action("story_like", target=name, detail=story.pk)
                    n += 1
                    log.info("liked @%s's story %s", name, story.pk)
                else:
                    break
            return n

        from concurrent.futures import ThreadPoolExecutor
        workers = int(self.cfg.get("speed.commands_parallel", 32) or 32)
        with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
            for n in pool.map(_one, found.items()):
                count += n
        return count

    # --------------------------------------------------------------- posts
    def auto_like_posts(self, usernames: list[str] | None = None) -> int:
        """Like the newest post of the accounts you follow — fast path."""
        if not self._enabled("like"):
            return 0
        names = usernames if usernames is not None else self._people_to_watch(40)
        count = 0

        def _one(name: str) -> int:
            if not self._allowed("like", name):
                return 0
            try:
                posts = self.client.user_posts(name, amount=1)
            except Exception as exc:  # noqa: BLE001
                log.debug("no posts for @%s: %s", name, exc)
                return 0
            if not posts:
                return 0
            post = posts[0]
            if self.client.like_media(post.pk):
                self.guardrails.record_action("like", target=name, detail=post.pk)
                log.info("liked @%s's post %s", name, post.pk)
                return 1
            return 0

        from concurrent.futures import ThreadPoolExecutor
        workers = int(self.cfg.get("speed.commands_parallel", 32) or 32)
        with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
            for n in pool.map(_one, names):
                count += n
        return count

    # ------------------------------------------------------------- follows
    def auto_follow_back(self) -> int:
        """Follow the accounts that followed you and that you do not follow."""
        if not self._enabled("follow"):
            return 0
        rows = self._graph("fans")
        count = 0
        for row in rows:
            name = str(row.get("username") or "")
            if not name:
                continue
            if not self._allowed("follow", name):
                continue
            if self.client.follow(name):
                self.guardrails.record_action("follow", target=name, detail="follow back")
                count += 1
                log.info("followed back @%s", name)
            time.sleep(0.5)
        return count

    # --------------------------------------------------------------- notes
    def auto_read_notes(self) -> list:
        """Read the notes in the inbox.

        Notes cannot be *liked* with a like button — Instagram never built
        one — but a note is a message, and messages can carry a reaction.
        When ``automations.like_notes`` is on, the bot puts a heart on every
        note that is there.
        """
        if not self._enabled("note"):
            return []
        try:
            notes = self.client.notes()
        except Exception as exc:  # noqa: BLE001
            log.warning("could not read the notes: %s", exc)
            return []
        for note in notes:
            if not note.text:
                continue
            self.store.record_activity("note", note.username, note.text[:120], fingerprint=note.id)
        return notes

    def auto_like_notes(self, notes: list | None = None) -> int:
        """Put a heart on every note that is there."""
        if notes is None:
            try:
                notes = self.client.notes()
            except Exception as exc:  # noqa: BLE001
                log.warning("could not read the notes: %s", exc)
                return 0
        count = 0
        for note in notes or []:
            if not note.text:
                continue
            if not self._allowed("like", note.username):
                break
            thread_id = str(getattr(note, "thread_id", "") or "")
            message_id = str(getattr(note, "id", "") or "")
            liked = False
            if thread_id and message_id:
                liked = bool(self.client.react(thread_id, message_id, "❤️"))
            if not liked:
                media_id = str(getattr(note, "media_id", "") or "")
                if media_id:
                    liked = bool(self.client.like_story(media_id))
            if liked:
                self.guardrails.record_action("like", target=note.username, detail="note")
                count += 1
                log.info("liked @%s's note", note.username)
        return count

    # ------------------------------------------------------------ unfollows
    def auto_unfollow_nonfollowers(self) -> int:
        """Remove the accounts that do not follow you back.

        Off by default, and a rehearsal until ``unfollow.dry_run`` is
        switched off. The gap between two removals is the one thing that
        keeps this from being a red flag.
        """
        if not bool(self.cfg.get("unfollow.enabled", False)):
            return 0
        if not self._enabled("unfollow"):
            return 0
        try:
            from .bulk import Scanner

            scanner = Scanner(self.client, self.store, self.cfg)
            summary = scanner.scan()
        except Exception as exc:  # noqa: BLE001
            log.warning("the scan failed: %s", exc)
            return 0

        rows = self.store.connections("nonfollowers") if self.store is not None else []
        whitelist = {
            name.strip().lstrip("@").lower()
            for name in str(self.cfg.get("unfollow.whitelist", "") or "").split(",")
            if name.strip()
        }
        targets = [
            row
            for row in rows
            if str(row.get("username") or "").lower() not in whitelist
        ]
        limit = int(self.cfg.get("unfollow.limit", 0) or 0)
        if limit:
            targets = targets[:limit]
        if not targets:
            return 0

        if bool(self.cfg.get("unfollow.dry_run", True)):
            log.info("rehearsal: %d non-followers would be unfollowed", len(targets))
            return 0

        gap_min = float(self.cfg.get("unfollow.gap_min", 20) or 0)
        gap_max = float(self.cfg.get("unfollow.gap_max", 60) or 0)
        if gap_max < gap_min:
            gap_max = gap_min
        count = 0
        for row in targets:
            name = str(row.get("username") or "")
            if not self._allowed("unfollow", name):
                break
            if self.client.unfollow_by_id(str(row.get("pk") or ""), name):
                self.guardrails.record_action("unfollow", target=name, detail="non-follower")
                count += 1
                log.info("unfollowed @%s (does not follow back)", name)
            if gap_max:
                time.sleep(gap_min + (time.time() % max(0.001, gap_max - gap_min)))
        return count

    # ------------------------------------------------------------- welcome
    def auto_welcome_new_followers(self) -> int:
        """Say hello to the accounts that started following you.

        A DM to a new follower is the oldest trick in the book, and it is
        also the fastest way to look automated — everybody who follows gets
        the same message. So the message is templated, the delay is long
        and random, the budget is tiny, and the same text is never sent
        twice.
        """
        if not self._enabled("welcome"):
            return 0
        template = str(self.cfg.get("automations.welcome_message", "") or "").strip()
        if not template:
            return 0

        me = self.client.whoami()
        rows = self._graph("followers")
        followers = {str(row.get("username") or "") for row in rows}
        followers.discard("")
        if not followers:
            log.warning("could not read the followers")
            return 0

        known_raw = self.store.kv_get("auto:followers")
        known = set((known_raw or "").split(",")) if known_raw else set()
        if not known:
            # The first run only records who is already there.
            self.store.kv_set("auto:followers", ",".join(sorted(followers)))
            log.info("recorded %d followers for the welcome check", len(followers))
            return 0

        fresh = sorted(followers - known)
        self.store.kv_set("auto:followers", ",".join(sorted(followers)))
        if not fresh:
            return 0

        count = 0
        for name in fresh:
            if self.risk is not None:
                decision = self.risk.check_visible("welcome", target=name, text=template)
                if not decision.allowed:
                    log.info("no welcome for @%s: %s", name, decision.reason)
                    continue
            text = template.replace("{username}", name).replace("{name}", name)
            sent = self.client.send_to_user(name, text)
            if sent.ok:
                count += 1
                if self.risk is not None:
                    self.risk.record_visible("welcome", target=name, detail="new follower")
                log.info("welcomed @%s", name)
            time.sleep(2.0)
        return count

    # ------------------------------------------------------------ comments
    def auto_answer_comments(self) -> int:
        """Answer new comments on your own posts.

        Only comments that have not been answered before are touched, and
        the answer comes from a template with a rotating opening so the
        replies do not read like a form letter.
        """
        if not self._enabled("comment"):
            return 0
        template = str(self.cfg.get("automations.comment_message", "") or "").strip()
        if not template:
            return 0

        me = self.client.whoami()
        posts = self.client.user_posts(me.username, amount=int(self.cfg.get("automations.comment_posts", 3) or 3))
        count = 0
        for post in posts:
            try:
                comments = self.client.comments(post.pk, amount=10)
            except Exception as exc:  # noqa: BLE001
                log.debug("could not read the comments of %s: %s", post.pk, exc)
                continue
            for comment in comments:
                if comment.user_id == self.client.own_user_id:
                    continue
                fingerprint = f"comment:{comment.pk}"
                if not self.store.record_activity("comment", comment.username, fingerprint=fingerprint):
                    continue
                if self.risk is not None:
                    decision = self.risk.check_visible("comment", target=comment.username)
                    if not decision.allowed:
                        continue
                text = template.replace("{username}", comment.username).replace("{name}", comment.username)
                done = self.client.comment(post.pk, text)
                if done:
                    count += 1
                    if self.risk is not None:
                        self.risk.record_visible("comment", target=comment.username, detail=post.pk)
                    log.info("answered a comment from @%s", comment.username)
                time.sleep(3.0)
        return count

    # -------------------------------------------------------------- stories
    def auto_publish_story(self) -> int:
        """Publish the next file from the story folder as a story.

        Files are used in order and the name is remembered, so a restart
        does not publish the same photo twice.
        """
        if not self._enabled("storypost"):
            return 0
        folder = str(self.cfg.get("automations.story_folder", "") or "")
        if not folder:
            return 0
        root = Path(folder).expanduser()
        if not root.is_absolute():
            root = Path(str(self.cfg.root)) / root
        if not root.is_dir():
            log.warning("the story folder %s does not exist", root)
            return 0

        files = sorted(
            p for p in root.iterdir() if p.suffix.lower() in (".jpg", ".jpeg", ".png", ".webp")
        )
        if not files:
            return 0

        last = self.store.kv_get("auto:story_last", "")
        index = 0
        if last in [f.name for f in files]:
            index = [f.name for f in files].index(last) + 1
            if index >= len(files):
                index = 0
        target = files[index]

        if self.risk is not None:
            decision = self.risk.check_visible("story_post", target=target.name)
            if not decision.allowed:
                log.info("no story published: %s", decision.reason)
                return 0

        caption = str(self.cfg.get("automations.story_caption", "") or "")
        pk = self.client.publish_story(str(target), caption)
        if not pk:
            return 0
        self.store.kv_set("auto:story_last", target.name)
        if self.risk is not None:
            self.risk.record_visible("story_post", target=target.name, detail=pk)
        log.info("published the story %s (%s)", target.name, pk)
        return 1

    # ----------------------------------------------------------------- run
    def run_once(self) -> dict:
        """Run every enabled automatic action once — at the same time.

        The jobs do not depend on each other, so there is no reason to run
        them one after the other. That was the second reason the bot felt
        slow: every pass did its work in a queue.
        """
        summary = {
            "stories_viewed": 0,
            "stories_liked": 0,
            "posts_liked": 0,
            "followed_back": 0,
            "welcomed": 0,
            "comments_answered": 0,
            "stories_published": 0,
            "notes_read": 0,
            "notes_liked": 0,
            "unfollowed": 0,
        }

        def notes_job() -> tuple[int, int]:
            notes = self.auto_read_notes() or []
            liked = 0
            if bool(self.cfg.get("automations.like_notes", False)):
                liked = self.auto_like_notes(notes)
            return len(notes), liked

        jobs: list[tuple[str, Any]] = [
            ("stories_viewed", self.auto_view_stories),
            ("stories_liked", self.auto_like_stories),
            ("posts_liked", self.auto_like_posts),
            ("followed_back", self.auto_follow_back),
            ("notes", notes_job),
            ("welcomed", self.auto_welcome_new_followers),
            ("comments_answered", self.auto_answer_comments),
            ("stories_published", self.auto_publish_story),
            ("unfollowed", self.auto_unfollow_nonfollowers),
        ]

        def run(job: tuple[str, Any]):
            key, fn = job
            try:
                return key, fn()
            except Exception as exc:  # noqa: BLE001
                log.warning("the automatic action %s failed: %s", key, exc)
                return key, 0

        workers = int(self.cfg.get("speed.commands_parallel", 8) or 8)
        if workers > 1 and len(jobs) > 1:
            with ThreadPoolExecutor(max_workers=min(workers, len(jobs))) as pool:
                results = list(pool.map(run, jobs))
        else:
            results = [run(job) for job in jobs]

        for key, value in results:
            if key == "notes":
                read, liked = value if isinstance(value, tuple) else (0, 0)
                summary["notes_read"] = read
                summary["notes_liked"] = liked
            else:
                summary[key] = value
        return summary

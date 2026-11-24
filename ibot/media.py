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

    def __init__(self, client, cfg, store, guardrails):
        self.client = client
        self.cfg = cfg
        self.store = store
        self.guardrails = guardrails

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

    def _people_to_watch(self, amount: int = 20) -> list[str]:
        """The accounts the bot watches — the ones you follow."""
        me = self.client.whoami()
        people = self.client.following(me.username, amount=amount)
        return [p.username for p in people]

    # ------------------------------------------------------------- stories
    def stories_of(self, usernames: list[str]) -> dict[str, list[Story]]:
        """Stories per account, skipping the accounts that have none."""
        found: dict[str, list[Story]] = {}
        for name in usernames:
            try:
                stories = self.client.stories(name)
            except Exception as exc:  # noqa: BLE001
                log.debug("no stories for @%s: %s", name, exc)
                continue
            if stories:
                found[name] = stories
        return found

    def auto_view_stories(self, usernames: list[str] | None = None) -> int:
        """View the stories of the accounts you follow."""
        if not self._enabled("view"):
            return 0
        names = usernames if usernames is not None else self._people_to_watch()
        count = 0
        for name, stories in self.stories_of(names).items():
            if not self._allowed("story_view", name):
                continue
            pks = [s.pk for s in stories]
            if self.client.view_stories(pks):
                self.guardrails.record_action("story_view", target=name, detail=f"{len(pks)} stories")
                count += len(pks)
                log.info("viewed %d stories from @%s", len(pks), name)
            time.sleep(0.4)
        return count

    def auto_like_stories(self, usernames: list[str] | None = None) -> int:
        """Like the newest story of the accounts you follow."""
        if not self._enabled("story"):
            return 0
        names = usernames if usernames is not None else self._people_to_watch()
        count = 0
        for name, stories in self.stories_of(names).items():
            if not self._allowed("story_like", name):
                continue
            newest = stories[0]
            if self.client.like_story(newest.pk):
                self.guardrails.record_action("story_like", target=name, detail=newest.pk)
                count += 1
                log.info("liked @%s's story", name)
            time.sleep(0.4)
        return count

    # --------------------------------------------------------------- posts
    def auto_like_posts(self, usernames: list[str] | None = None) -> int:
        """Like the newest post of the accounts you follow."""
        if not self._enabled("like"):
            return 0
        names = usernames if usernames is not None else self._people_to_watch(10)
        count = 0
        for name in names:
            if not self._allowed("like", name):
                continue
            try:
                posts = self.client.user_posts(name, amount=2)
            except Exception as exc:  # noqa: BLE001
                log.debug("no posts for @%s: %s", name, exc)
                continue
            if not posts:
                continue
            post = posts[0]
            if self.client.like_media(post.pk):
                self.guardrails.record_action("like", target=name, detail=post.pk)
                count += 1
                log.info("liked @%s's post %s", name, post.pk)
            time.sleep(0.5)
        return count

    # ------------------------------------------------------------- follows
    def auto_follow_back(self) -> int:
        """Follow the accounts that followed you and that you do not follow."""
        if not self._enabled("follow"):
            return 0
        me = self.client.whoami()
        try:
            followers = {p.username for p in self.client.followers(me.username, amount=50)}
            following = {p.username for p in self.client.following(me.username, amount=50)}
        except Exception as exc:  # noqa: BLE001
            log.warning("could not read the follower lists: %s", exc)
            return 0
        count = 0
        for name in sorted(followers - following):
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

        Notes cannot be liked — Instagram never built a like control for
        them — so this only reads them and records them.
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

    # ----------------------------------------------------------------- run
    def run_once(self) -> dict:
        """Run every enabled automatic action once."""
        summary = {"stories_viewed": 0, "stories_liked": 0, "posts_liked": 0, "followed_back": 0}
        try:
            summary["stories_viewed"] = self.auto_view_stories()
        except Exception as exc:  # noqa: BLE001
            log.warning("auto view stories failed: %s", exc)
        try:
            summary["stories_liked"] = self.auto_like_stories()
        except Exception as exc:  # noqa: BLE001
            log.warning("auto like stories failed: %s", exc)
        try:
            summary["posts_liked"] = self.auto_like_posts()
        except Exception as exc:  # noqa: BLE001
            log.warning("auto like posts failed: %s", exc)
        try:
            summary["followed_back"] = self.auto_follow_back()
        except Exception as exc:  # noqa: BLE001
            log.warning("auto follow back failed: %s", exc)
        try:
            self.auto_read_notes()
        except Exception as exc:  # noqa: BLE001
            log.warning("auto read notes failed: %s", exc)
        return summary

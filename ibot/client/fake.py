"""An in-memory fake Instagram.

The tests run against this, which means the whole bot — inbox polling, the
rulebook, the guardrails, the command router — is exercised without a network
connection and without an account. That is the difference between a suite that
runs in a second on a Render free tier and one that needs a live login.
"""

from __future__ import annotations

import itertools
import time
from typing import Any

from .base import (
    BaseClient,
    Comment,
    Media,
    Message,
    Note,
    Profile,
    SendResult,
    Story,
    Thread,
)


class FakeClient(BaseClient):
    """A tiny, deterministic Instagram."""

    name = "fake"

    def __init__(self, own_user_id: str = "1", own_username: str = "me"):
        self.own_user_id = own_user_id
        self.own_username = own_username
        self._ids = itertools.count(1000)
        self._threads: dict[str, Thread] = {}
        self._notes: list[Note] = []
        self._stories: dict[str, list[Story]] = {}
        self._media: dict[str, Media] = {}
        self._comments: dict[str, list[Comment]] = {}
        self._profiles: dict[str, Profile] = {
            own_username: Profile(
                user_id=own_user_id,
                username=own_username,
                full_name="Me",
                follower_count=10,
                following_count=5,
                media_count=2,
            )
        }
        self._following: set[str] = set()
        self._graph: bool | None = None
        self._graph_followers: set[str] = set()
        self._graph_following: set[str] = set()
        self._seen: list[str] = []
        self.sent: list[tuple[str, str]] = []
        self.liked: list[str] = []
        self.followed: list[str] = []
        self.unfollowed: list[str] = []
        self.reacted: list[tuple[str, str, str]] = []
        self.blocked: list[str] = []
        self.muted: list[str] = []
        self.archived: list[str] = []
        self.approved: list[str] = []
        self.declined: list[str] = []
        self.published: list[tuple[str, str, str]] = []
        self.fail_next: Exception | None = None

    # -------------------------------------------------------------- helpers
    def _next_id(self) -> str:
        return str(next(self._ids))

    def _maybe_fail(self):
        if self.fail_next is not None:
            exc, self.fail_next = self.fail_next, None
            raise exc

    def add_thread(
        self,
        thread_id: str,
        username: str,
        texts: list[str],
        user_id: str | None = None,
        pending: bool = False,
        self_thread: bool = False,
    ) -> Thread:
        """Create a thread with messages from ``username``."""
        uid = user_id or (self.own_user_id if self_thread else str(hash(username) % 100000))
        thread = Thread(
            id=thread_id,
            title=username,
            usernames=[] if self_thread else [username],
            user_ids=[] if self_thread else [uid],
            is_self=self_thread,
            pending=pending,
            unread=True,
        )
        for text in texts:
            thread.messages.append(
                Message(
                    id=self._next_id(),
                    thread_id=thread_id,
                    text=text,
                    user_id=uid,
                    username=self.own_username if self_thread else username,
                    timestamp=time.time(),
                    is_self=self_thread,
                )
            )
        self._threads[thread_id] = thread
        self._profiles.setdefault(
            username,
            Profile(user_id=uid, username=username, full_name=username),
        )
        return thread

    def add_message(self, thread_id: str, text: str, is_self: bool = False, username: str = ""):
        thread = self._threads[thread_id]
        uid = self.own_user_id if is_self else (thread.user_ids[0] if thread.user_ids else "2")
        msg = Message(
            id=self._next_id(),
            thread_id=thread_id,
            text=text,
            user_id=uid,
            username=self.own_username if is_self else (username or thread.usernames[:1] or [""])[0],
            timestamp=time.time(),
            is_self=is_self,
        )
        thread.messages.append(msg)
        return msg

    # ------------------------------------------------------------- account
    def whoami(self) -> Profile:
        return self._profiles[self.own_username]

    def profile(self, username: str) -> Profile:
        clean = username.lstrip("@")
        if clean not in self._profiles:
            raise KeyError(clean)
        return self._profiles[clean]

    def user_id(self, username: str) -> str:
        return self.profile(username).user_id

    def search_users(self, query: str, amount: int = 10) -> list[Profile]:
        q = query.lower()
        return [p for p in self._profiles.values() if q in p.username.lower()][:amount]

    # --------------------------------------------------------------- inbox
    def threads(self, amount: int = 20, box: str | None = None) -> list[Thread]:
        # Pending requests live in their own folder, exactly as in the API.
        return [t for t in self._threads.values() if not t.pending][:amount]

    def pending_threads(self, amount: int = 20) -> list[Thread]:
        return [t for t in self._threads.values() if t.pending][:amount]

    def send_text(self, thread_id: str, text: str) -> SendResult:
        try:
            self._maybe_fail()
        except Exception as exc:  # noqa: BLE001
            # The real client reports a failed send instead of raising.
            return SendResult(False, detail=str(exc))
        if thread_id not in self._threads:
            return SendResult(False, detail=f"no such thread {thread_id}")
        self.sent.append((thread_id, text))
        msg = self.add_message(thread_id, text, is_self=True)
        return SendResult(True, message_id=msg.id, thread_id=thread_id)

    def send_to_user(self, username: str, text: str) -> SendResult:
        self._maybe_fail()
        self.sent.append((username, text))
        return SendResult(True, message_id=self._next_id())

    def send_photo(self, thread_id: str, path: str, caption: str = "") -> SendResult:
        return self.send_text(thread_id, f"[photo {path}] {caption}".strip())

    def mark_seen(self, thread_id: str) -> bool:
        self._seen.append(thread_id)
        if thread_id in self._threads:
            self._threads[thread_id].unread = False
        return True

    def approve_request(self, thread_id: str) -> bool:
        if thread_id in self._threads:
            self._threads[thread_id].pending = False
        return True

    def react(self, thread_id: str, message_id: str, emoji: str = "❤️") -> bool:
        self.reacted.append((thread_id, message_id, emoji))
        return True

    # ------------------------------------------------------------- content
    def stories(self, username: str) -> list[Story]:
        return list(self._stories.get(username.lstrip("@"), []))

    def add_story(self, username: str, count: int = 1) -> list[Story]:
        items = [
            Story(
                pk=self._next_id(),
                user_id=self._profiles[username].user_id,
                username=username,
                taken_at=time.time(),
            )
            for _ in range(count)
        ]
        self._stories[username] = items
        return items

    def view_stories(self, story_pks: list[str]) -> bool:
        self._seen.extend(story_pks)
        return True

    def like_story(self, story_pk: str) -> bool:
        self.liked.append(story_pk)
        return True

    def media(self, reference: str) -> Media:
        for m in self._media.values():
            if m.pk == reference or reference in m.pk:
                return m
        raise KeyError(reference)

    def add_media(self, username: str, caption: str = "post") -> Media:
        m = Media(
            pk=self._next_id(),
            user_id=self._profiles[username].user_id,
            username=username,
            caption=caption,
            taken_at=time.time(),
        )
        self._media[m.pk] = m
        return m

    def like_media(self, media_pk: str) -> bool:
        self.liked.append(media_pk)
        return True

    def unlike_media(self, media_pk: str) -> bool:
        return True

    def comments(self, media_pk: str, amount: int = 20) -> list[Comment]:
        return self._comments.get(media_pk, [])[:amount]

    def comment(self, media_pk: str, text: str) -> Comment:
        c = Comment(pk=self._next_id(), text=text, user_id=self.own_user_id, username=self.own_username)
        self._comments.setdefault(media_pk, []).append(c)
        return c

    def like_comment(self, comment_pk: str) -> bool:
        self.liked.append(comment_pk)
        return True

    def user_posts(self, username: str, amount: int = 12) -> list[Media]:
        return [m for m in self._media.values() if m.username == username][:amount]

    def followers(self, username: str, amount: int = 50) -> list[Profile]:
        return self.connections("followers", username, amount)

    def following(self, username: str, amount: int = 50) -> list[Profile]:
        return self.connections("following", username, amount)

    def connections(self, kind: str, username: str, amount: int = 0) -> list[Profile]:
        """The whole follower/following list — every page of it.

        ``amount`` of 0 means all of them.
        """
        if self._graph is not None:
            names = self._graph_followers if kind == "followers" else self._graph_following
            people = [self._profiles[n] for n in sorted(names) if n in self._profiles]
        else:
            people = list(self._profiles.values())
        return people[:amount] if amount else people

    def set_graph(self, followers: list[str], following: list[str]) -> None:
        """Put a follower graph in place — used by the tests."""
        self._graph = True
        self._graph_followers = set(followers)
        self._graph_following = set(following)
        for name in list(self._graph_followers) + list(self._graph_following):
            self._profiles.setdefault(
                name,
                Profile(user_id=str(abs(hash(name)) % 100000), username=name, full_name=name),
            )

    def follow(self, username: str) -> bool:
        self.followed.append(username)
        self._following.add(username)
        return True

    def unfollow(self, username: str) -> bool:
        self._following.discard(username)
        return True

    def follow_by_id(self, user_id: str, username: str = "") -> bool:
        self.followed.append(username or user_id)
        if username:
            self._following.add(username)
        return True

    def unfollow_by_id(self, user_id: str, username: str = "") -> bool:
        self.unfollowed.append(username or user_id)
        if username:
            self._following.discard(username)
        return True

    # --------------------------------------------------------------- notes
    def notes(self) -> list[Note]:
        return list(self._notes)

    def add_note(self, username: str, text: str) -> Note:
        note = Note(
            id=self._next_id(),
            user_id=self._profiles[username].user_id,
            text=text,
            username=username,
            created_at=time.time(),
            thread_id="notes",
        )
        self._notes.append(note)
        return note

    def create_note(self, text: str) -> Note | None:
        return self.add_note(self.own_username, text)

    # --------------------------------------------------------- moderation
    def block(self, username: str) -> bool:
        self.blocked.append(username)
        return True

    def unblock(self, username: str) -> bool:
        if username in self.blocked:
            self.blocked.remove(username)
        return True

    def mute_posts(self, username: str) -> bool:
        self.muted.append(username)
        return True

    def unmute_posts(self, username: str) -> bool:
        if username in self.muted:
            self.muted.remove(username)
        return True

    def mute_stories(self, username: str) -> bool:
        return self.mute_posts(username)

    def unmute_stories(self, username: str) -> bool:
        return self.unmute_posts(username)

    def archive_thread(self, thread_id: str) -> bool:
        self.archived.append(thread_id)
        return True

    def mute_thread(self, thread_id: str) -> bool:
        return True

    def unmute_thread(self, thread_id: str) -> bool:
        return True

    def follow_requests(self, amount: int = 20) -> list[Profile]:
        return list(self._profiles.values())[:amount]

    def approve_follow_request(self, username: str) -> bool:
        self.approved.append(username)
        return True

    def decline_follow_request(self, username: str) -> bool:
        self.declined.append(username)
        return True

    # -------------------------------------------------------------- extras
    def highlights(self, username: str) -> list[dict]:
        return [{"id": "1", "title": "Highlights", "items": 3}]

    def story_viewers(self, story_pk: str) -> list[Profile]:
        return list(self._profiles.values())[:5]

    def insights(self) -> dict:
        return {"impressions": 1234, "reach": 5678}

    def reels(self, username: str, amount: int = 10) -> list[Media]:
        return [m for m in self._media.values() if m.media_type == 8][:amount]

    def thread_messages(self, thread_id: str, amount: int = 20) -> list[Message]:
        thread = self._threads.get(thread_id)
        return list(thread.messages)[-amount:] if thread else []

    # ---------------------------------------------------------- publishing
    def publish_photo(self, path: str, caption: str = "") -> Media | None:
        self.published.append(("photo", path, caption))
        return self.add_media(self.own_username, caption)

    def publish_video(self, path: str, caption: str = "") -> Media | None:
        self.published.append(("video", path, caption))
        return self.add_media(self.own_username, caption)

    def publish_story(self, path: str, caption: str = "") -> str:
        self.published.append(("story", path, caption))
        return self._next_id()

    # -------------------------------------------------------------- status
    def health(self) -> dict[str, Any]:
        return {
            "ok": True,
            "transport": "fake",
            "user_id": self.own_user_id,
            "username": self.own_username,
        }

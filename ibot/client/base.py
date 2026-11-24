"""The client contract.

Every Instagram action the bot performs goes through one interface. Two
implementations exist:

* :class:`ibot.client.ig.ApiClient` — talks to Instagram's own mobile API
  over plain HTTP. No browser, no Chromium, ~60 MB of RAM.
* :class:`ibot.client.fake.FakeClient` — an in-memory fake used by the tests.

Keeping the bot written against this contract is what makes the whole thing
testable without an account, and it is what lets a browser-based backend be
swapped in later without touching the business logic.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any


@dataclass
class Profile:
    """An Instagram account."""

    user_id: str
    username: str
    full_name: str = ""
    is_private: bool = False
    is_verified: bool = False
    follower_count: int = 0
    following_count: int = 0
    media_count: int = 0
    biography: str = ""

    @property
    def handle(self) -> str:
        return f"@{self.username}"


@dataclass
class Message:
    """One direct message."""

    id: str
    thread_id: str
    text: str
    user_id: str
    username: str
    timestamp: float = 0.0
    is_self: bool = False
    item_type: str = "text"
    media_url: str = ""

    @property
    def is_command(self) -> bool:
        """True when the message looks like a bot command."""
        return self.text.strip().startswith("/")


@dataclass
class Thread:
    """A direct conversation."""

    id: str
    title: str = ""
    usernames: list[str] = field(default_factory=list)
    user_ids: list[str] = field(default_factory=list)
    messages: list[Message] = field(default_factory=list)
    is_group: bool = False
    is_self: bool = False
    unread: bool = False
    pending: bool = False
    last_activity_at: float = 0.0

    def other_users(self, own_user_id: str) -> list[str]:
        """Usernames in the thread that are not us."""
        return [u for u in self.usernames if u != own_user_id]

    @property
    def peer(self) -> str:
        """Best-effort name of the person on the other side."""
        others = self.other_users("")
        return others[0] if others else self.title


@dataclass
class Story:
    """One story item."""

    pk: str
    user_id: str
    username: str = ""
    taken_at: float = 0.0
    media_type: int = 1
    item_type: str = "photo"
    seen: bool = False


@dataclass
class Note:
    """A note — the short-lived text posts in the DM inbox."""

    id: str
    user_id: str
    text: str = ""
    username: str = ""
    created_at: float = 0.0
    expires_at: float = 0.0


@dataclass
class Media:
    """A post, reel or carousel."""

    pk: str
    user_id: str
    username: str = ""
    caption: str = ""
    taken_at: float = 0.0
    like_count: int = 0
    comment_count: int = 0
    media_type: int = 1
    has_liked: bool = False

    @property
    def url(self) -> str:
        return f"https://www.instagram.com/p/{self.pk}/"


@dataclass
class Comment:
    """A comment on a post."""

    pk: str
    text: str
    user_id: str
    username: str = ""
    created_at: float = 0.0
    has_liked: bool = False


@dataclass
class SendResult:
    """Outcome of a send attempt."""

    ok: bool
    message_id: str = ""
    detail: str = ""
    thread_id: str = ""


class BaseClient:
    """The operations the bot needs from Instagram.

    Subclasses raise :class:`NotImplementedError` for anything they do not
    support, so a partial backend fails loudly instead of silently doing
    nothing.
    """

    name = "base"

    # ------------------------------------------------------------- account
    def whoami(self) -> Profile:
        raise NotImplementedError

    def profile(self, username: str) -> Profile:
        raise NotImplementedError

    def user_id(self, username: str) -> str:
        raise NotImplementedError

    def search_users(self, query: str, amount: int = 10) -> list[Profile]:
        raise NotImplementedError

    # --------------------------------------------------------------- inbox
    def threads(self, amount: int = 20, box: str | None = None) -> list[Thread]:
        raise NotImplementedError

    def pending_threads(self, amount: int = 20) -> list[Thread]:
        raise NotImplementedError

    def send_text(self, thread_id: str, text: str) -> SendResult:
        raise NotImplementedError

    def send_to_user(self, username: str, text: str) -> SendResult:
        raise NotImplementedError

    def send_photo(self, thread_id: str, path: str, caption: str = "") -> SendResult:
        raise NotImplementedError

    def mark_seen(self, thread_id: str) -> bool:
        raise NotImplementedError

    def approve_request(self, thread_id: str) -> bool:
        raise NotImplementedError

    def react(self, thread_id: str, message_id: str, emoji: str = "❤️") -> bool:
        raise NotImplementedError

    # ------------------------------------------------------------- content
    def stories(self, username: str) -> list[Story]:
        raise NotImplementedError

    def view_stories(self, story_pks: list[str]) -> bool:
        raise NotImplementedError

    def like_story(self, story_pk: str) -> bool:
        raise NotImplementedError

    def media(self, reference: str) -> Media:
        raise NotImplementedError

    def like_media(self, media_pk: str) -> bool:
        raise NotImplementedError

    def unlike_media(self, media_pk: str) -> bool:
        raise NotImplementedError

    def comments(self, media_pk: str, amount: int = 20) -> list[Comment]:
        raise NotImplementedError

    def comment(self, media_pk: str, text: str) -> Comment | None:
        raise NotImplementedError

    def like_comment(self, comment_pk: str) -> bool:
        raise NotImplementedError

    def user_posts(self, username: str, amount: int = 12) -> list[Media]:
        raise NotImplementedError

    def followers(self, username: str, amount: int = 50) -> list[Profile]:
        raise NotImplementedError

    def following(self, username: str, amount: int = 50) -> list[Profile]:
        raise NotImplementedError

    def follow(self, username: str) -> bool:
        raise NotImplementedError

    def unfollow(self, username: str) -> bool:
        raise NotImplementedError

    # ------------------------------------------------------------- notes
    def notes(self) -> list[Note]:
        raise NotImplementedError

    def create_note(self, text: str) -> Note | None:
        raise NotImplementedError

    # --------------------------------------------------------- moderation
    def block(self, username: str) -> bool:
        raise NotImplementedError

    def unblock(self, username: str) -> bool:
        raise NotImplementedError

    def mute_posts(self, username: str) -> bool:
        raise NotImplementedError

    def unmute_posts(self, username: str) -> bool:
        raise NotImplementedError

    def mute_stories(self, username: str) -> bool:
        raise NotImplementedError

    def unmute_stories(self, username: str) -> bool:
        raise NotImplementedError

    def archive_thread(self, thread_id: str) -> bool:
        raise NotImplementedError

    def mute_thread(self, thread_id: str) -> bool:
        raise NotImplementedError

    def unmute_thread(self, thread_id: str) -> bool:
        raise NotImplementedError

    def follow_requests(self, amount: int = 20) -> list[Profile]:
        raise NotImplementedError

    def approve_follow_request(self, username: str) -> bool:
        raise NotImplementedError

    def decline_follow_request(self, username: str) -> bool:
        raise NotImplementedError

    # -------------------------------------------------------------- extras
    def highlights(self, username: str) -> list[dict]:
        raise NotImplementedError

    def story_viewers(self, story_pk: str) -> list[Profile]:
        raise NotImplementedError

    def insights(self) -> dict:
        raise NotImplementedError

    def reels(self, username: str, amount: int = 10) -> list[Media]:
        raise NotImplementedError

    def thread_messages(self, thread_id: str, amount: int = 20) -> list[Message]:
        raise NotImplementedError

    # ---------------------------------------------------------- publishing
    def publish_photo(self, path: str, caption: str = "") -> Media | None:
        raise NotImplementedError

    def publish_video(self, path: str, caption: str = "") -> Media | None:
        raise NotImplementedError

    def publish_story(self, path: str, caption: str = "") -> str:
        raise NotImplementedError

    # -------------------------------------------------------------- status
    def health(self) -> dict[str, Any]:
        raise NotImplementedError

    def close(self) -> None:
        return None

    # ------------------------------------------------------------- helpers
    @staticmethod
    def _epoch(value: Any) -> float:
        """Convert an Instagram timestamp to epoch seconds.

        Instagram returns either a POSIX timestamp or a timezone-aware
        datetime depending on the endpoint, so both are handled here.
        """
        if value is None:
            return 0.0
        if isinstance(value, (int, float)):
            return float(value)
        try:
            return value.timestamp()
        except AttributeError:
            return time.time()

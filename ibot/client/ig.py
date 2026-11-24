"""The live Instagram client — HTTP only, no browser.

This is the whole point of the project. The previous generation of this bot
drove a real Chromium against instagram.com: it worked, but Chromium alone
costs 300–500 MB of RAM, which is more than a free Render instance has in
total, so the bot was killed the moment it tried to do two things at once.

Instagram's own mobile API answers the same questions in a few kilobytes of
JSON. This module speaks that protocol directly, which means:

* **~60 MB of RAM** instead of ~500 MB.
* **Milliseconds** instead of page loads — a poll costs 2 requests.
* **No browser fingerprint**, no headless detector, no "unusual login
  activity" caused by a Chromium instance from a datacentre IP.

What is left is the same guardrail logic, the same rulebook and the same
audit trail — just a much smaller transport.
"""

from __future__ import annotations

import re
import threading
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Callable

from .. import log
from ..bulk import BulkEngine
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

log = log.get(__name__)


@contextmanager
def turbo(client):
    """Drop the human pause for a burst of read-only calls.

    Instagram is happy to answer a hundred requests a second. The pause
    instagrapi inserts between calls is there to *look* human, not to be
    correct — but a full scan of a large account is hundreds of pages of
    JSON, and two seconds of pause on each one turns a forty-second job into
    four minutes. The pause comes straight back the moment the scan is over,
    and the visible actions keep their own, much slower, rhythm.
    """
    touched: list[tuple[Any, str, Any]] = []
    private = getattr(client, "private", None)
    for owner, attr, value in (
        (private, "request_timeout", 0),
        (private, "timeout", 0),
        (private, "delay_range", [0, 0]),
        (client, "delay_range", [0, 0]),
    ):
        if owner is None:
            continue
        try:
            touched.append((owner, attr, getattr(owner, attr)))
            setattr(owner, attr, value)
        except Exception:  # noqa: BLE001
            pass
    try:
        yield
    finally:
        for owner, attr, value in touched:
            try:
                setattr(owner, attr, value)
            except Exception:  # noqa: BLE001
                pass


class InstagramError(RuntimeError):
    """Any failure that came back from Instagram.

    ``needs_reauth`` tells the session layer that the session was revoked and
    should be rebuilt before the next call.
    """

    def __init__(self, message: str, needs_reauth: bool = False, kind: str = "error"):
        super().__init__(message)
        self.needs_reauth = needs_reauth
        self.kind = kind


def _is_reauth_error(exc: Exception) -> bool:
    """True when the exception means 'this session is dead, log in again'."""
    name = type(exc).__name__
    if name in {
        "LoginRequired",
        "ClientLoginRequired",
        "ClientUnauthorizedError",
        "ReloginAttemptExceeded",
        "PleaseWaitFewMinutes",
        "ClientForbiddenError",
    }:
        return True
    text = str(exc).lower()
    return "login required" in text or "not logged in" in text or "checkpoint" in text


class ApiClient(BaseClient):
    """Wraps an authenticated ``instagrapi`` client."""

    name = "api"

    def __init__(self, client, cfg=None):
        self.cl = client
        self.cfg = cfg
        self._own_user_id = str(getattr(client, "user_id", "") or "")
        # Optional risk guard. When it is attached, every call goes through
        # the budget check and every failure is looked at.
        self.risk = None
        # The bulk engine — the part that can walk a whole follower list.
        self.bulk = BulkEngine(cfg, self)
        # Visible actions are serialised. Reads run in parallel; writes do
        # not, because Instagram does not like two of them at once.
        self._write_lock = threading.Lock()
        # The names that were looked up already.
        self._id_cache: dict[str, str] = {}

    # ------------------------------------------------------------- plumbing
    def _call(self, fn: Callable, *args, **kwargs):
        """Run an instagrapi call and translate its failures."""
        if self.risk is not None:
            from ..risk import RiskStop

            try:
                self.risk.before_request()
            except RiskStop as exc:
                raise InstagramError(str(exc), kind="risk") from exc
            self.risk.note_request()
        try:
            return fn(*args, **kwargs)
        except Exception as exc:  # noqa: BLE001 - deliberately broad
            if self.risk is not None:
                self.risk.note_failure(exc)
            if _is_reauth_error(exc):
                raise InstagramError(
                    f"session expired: {type(exc).__name__}: {exc}",
                    needs_reauth=True,
                    kind="auth",
                ) from exc
            raise InstagramError(f"{type(exc).__name__}: {exc}", kind="api") from exc

    @property
    def own_user_id(self) -> str:
        if not self._own_user_id:
            self._own_user_id = str(self.cl.user_id or "")
        return self._own_user_id

    # ------------------------------------------------------------- account
    def whoami(self) -> Profile:
        me = self._call(self.cl.user_info, self.own_user_id)
        return self._to_profile(me)

    def _to_profile(self, user) -> Profile:
        return Profile(
            user_id=str(getattr(user, "pk", "") or ""),
            username=getattr(user, "username", "") or "",
            full_name=getattr(user, "full_name", "") or "",
            is_private=bool(getattr(user, "is_private", False)),
            is_verified=bool(getattr(user, "is_verified", False)),
            follower_count=int(getattr(user, "follower_count", 0) or 0),
            following_count=int(getattr(user, "following_count", 0) or 0),
            media_count=int(getattr(user, "media_count", 0) or 0),
            biography=getattr(user, "biography", "") or "",
        )

    def profile(self, username: str) -> Profile:
        uid = self.user_id(username)
        user = self._call(self.cl.user_info, uid)
        return self._to_profile(user)

    def user_id(self, username: str) -> str:
        clean = (username or "").strip().lstrip("@")
        if not clean:
            raise InstagramError("no username given", kind="input")
        # A name belongs to one id for as long as it exists, so the answer
        # is remembered. Walking 335 accounts for their stories costs two
        # requests each without this — one of them only to look the id up.
        cached = self._id_cache.get(clean)
        if cached:
            return cached
        try:
            resolved = str(self._call(self.cl.user_id_from_username, clean))
        except InstagramError as exc:
            raise InstagramError(f"@{clean} was not found", kind="notfound") from exc
        if len(self._id_cache) > 5000:
            self._id_cache.clear()
        self._id_cache[clean] = resolved
        return resolved

    def search_users(self, query: str, amount: int = 10) -> list[Profile]:
        results = self._call(self.cl.direct_search, query) or []
        out = []
        for u in results[:amount]:
            out.append(
                Profile(
                    user_id=str(getattr(u, "pk", "") or ""),
                    username=getattr(u, "username", "") or "",
                    full_name=getattr(u, "full_name", "") or "",
                    is_private=bool(getattr(u, "is_private", False)),
                )
            )
        return out

    # --------------------------------------------------------------- inbox
    def _to_thread(self, raw, pending: bool = False) -> Thread:
        users = list(getattr(raw, "users", None) or [])
        messages = []
        for m in getattr(raw, "messages", None) or []:
            uid = str(getattr(m, "user_id", "") or "")
            messages.append(
                Message(
                    id=str(getattr(m, "id", "") or ""),
                    thread_id=str(getattr(m, "thread_id", raw.id) or raw.id),
                    text=getattr(m, "text", "") or "",
                    user_id=uid,
                    username=next((u.username for u in users if str(u.pk) == uid), ""),
                    timestamp=self._epoch(getattr(m, "timestamp", None)),
                    is_self=bool(uid and uid == self.own_user_id),
                    item_type=str(getattr(m, "item_type", "") or "text"),
                )
            )
        usernames = [u.username for u in users if getattr(u, "username", None)]
        thread_id = str(getattr(raw, "id", "") or "")
        is_self = bool(users) and all(str(u.pk) == self.own_user_id for u in users)
        if not users:
            # A note-to-self thread has no participants; Instagram titles it
            # with the account's own display name.
            is_self = True
        return Thread(
            id=thread_id,
            title=getattr(raw, "thread_title", "") or "",
            usernames=usernames,
            user_ids=[str(u.pk) for u in users],
            messages=messages,
            is_group=bool(getattr(raw, "is_group", False)),
            is_self=is_self,
            unread=not bool(getattr(raw, "read_state", 0) in (1, 2)),
            pending=pending or bool(getattr(raw, "pending", False)),
            last_activity_at=self._epoch(getattr(raw, "last_activity_at", None)),
        )

    def threads(self, amount: int = 20, box: str | None = None) -> list[Thread]:
        kwargs: dict[str, Any] = {"amount": amount}
        if box:
            kwargs["box"] = box
        raw = self._call(self.cl.direct_threads, **kwargs) or []
        return [self._to_thread(t) for t in raw]

    def pending_threads(self, amount: int = 20) -> list[Thread]:
        raw = self._call(self.cl.direct_pending_inbox, amount) or []
        return [self._to_thread(t, pending=True) for t in raw]

    def send_text(self, thread_id: str, text: str) -> SendResult:
        if not text or not text.strip():
            return SendResult(False, detail="nothing to send")
        try:
            msg = self._call(self.cl.direct_send, text, thread_ids=[int(thread_id)])
        except (InstagramError, ValueError) as exc:
            return SendResult(False, detail=str(exc))
        return SendResult(True, message_id=str(getattr(msg, "id", "") or ""), thread_id=thread_id)

    def send_to_user(self, username: str, text: str) -> SendResult:
        try:
            uid = self.user_id(username)
            msg = self._call(self.cl.direct_send, text, user_ids=[int(uid)])
        except (InstagramError, ValueError) as exc:
            return SendResult(False, detail=str(exc))
        return SendResult(True, message_id=str(getattr(msg, "id", "") or ""), thread_id="")

    def send_photo(self, thread_id: str, path: str, caption: str = "") -> SendResult:
        p = Path(path).expanduser()
        if not p.exists():
            return SendResult(False, detail=f"file not found: {path}")
        try:
            msg = self._call(self.cl.direct_send_photo, p, thread_ids=[int(thread_id)])
        except (InstagramError, ValueError) as exc:
            return SendResult(False, detail=str(exc))
        return SendResult(True, message_id=str(getattr(msg, "id", "") or ""), thread_id=thread_id)

    def send_file(self, thread_id: str, path: str) -> SendResult:
        """Send a document / file (JSON lists, etc.) into a DM thread."""
        p = Path(path).expanduser()
        if not p.exists():
            return SendResult(False, detail=f"file not found: {path}")
        # Prefer direct_send_file when the underlying client has it
        try:
            if hasattr(self.cl, "direct_send_file"):
                msg = self._call(self.cl.direct_send_file, p, thread_ids=[int(thread_id)])
                return SendResult(True, message_id=str(getattr(msg, "id", "") or ""), thread_id=thread_id)
        except (InstagramError, ValueError, AttributeError):
            pass
        # Fallback: try photo (works for images; for .json it may fail)
        try:
            msg = self._call(self.cl.direct_send_photo, p, thread_ids=[int(thread_id)])
            return SendResult(True, message_id=str(getattr(msg, "id", "") or ""), thread_id=thread_id)
        except (InstagramError, ValueError) as exc:
            return SendResult(False, detail=str(exc))

    def mark_seen(self, thread_id: str) -> bool:
        try:
            return bool(self._call(self.cl.direct_send_seen, int(thread_id)))
        except (InstagramError, ValueError):
            return False

    def approve_request(self, thread_id: str) -> bool:
        try:
            return bool(self._call(self.cl.direct_request_approve, int(thread_id)))
        except (InstagramError, ValueError):
            return False

    def react(self, thread_id: str, message_id: str, emoji: str = "❤️") -> bool:
        try:
            return bool(
                self._call(
                    self.cl.direct_send_reaction,
                    int(thread_id),
                    int(message_id),
                    emoji,
                )
            )
        except (InstagramError, ValueError):
            return False

    # ------------------------------------------------------------- content
    def _story_from_raw(self, s, uid: str = "", username: str = "") -> Story:
        media_type = int(getattr(s, "media_type", 1) or 1)
        return Story(
            pk=str(getattr(s, "pk", "") or getattr(s, "id", "") or ""),
            user_id=str(uid or getattr(getattr(s, "user", None), "pk", "") or ""),
            username=username or str(getattr(getattr(s, "user", None), "username", "") or ""),
            taken_at=self._epoch(getattr(s, "taken_at", None)),
            media_type=media_type,
            item_type="video" if media_type == 2 else "photo",
        )

    def stories_by_id(self, user_id: str, username: str = "") -> list[Story]:
        """Stories for a known numeric id — no usernameinfo lookup."""
        uid = str(user_id or "").strip()
        if not uid:
            return []
        try:
            raw = self._call(self.cl.user_stories, uid) or []
        except InstagramError:
            return []
        return [self._story_from_raw(s, uid=uid, username=username) for s in raw]

    def stories(self, username: str) -> list[Story]:
        """Stories by username (does one id lookup). Prefer stories_by_id when pk is known."""
        uid = self.user_id(username)
        return self.stories_by_id(uid, username=username)

    def stories_tray(self, fill_items: bool = True) -> dict[str, list[Story]]:
        """Pull the story tray exactly like the official app.

        1. One ``feed/reels_tray/`` call → only people who currently have stories
        2. If tray entries have no items (common), fetch each by user id
           (still only people with stories — not your whole following list)

        Returns {username: [Story, ...]}.
        """
        found: dict[str, list[Story]] = {}
        need_fetch: list[tuple[str, str]] = []  # (uid, username)
        try:
            raw = None
            # Only call if it's actually a callable — never treat a string attr as a method
            reels_fn = getattr(self.cl, "reels_tray", None)
            if callable(reels_fn):
                try:
                    raw = self._call(reels_fn)
                except Exception as exc:
                    log.debug("reels_tray() failed: %s", exc)
                    raw = None
            if raw is None:
                priv = getattr(self.cl, "private_request", None)
                if callable(priv):
                    try:
                        raw = self._call(priv, "feed/reels_tray/")
                    except Exception as exc:
                        log.debug("private_request feed/reels_tray/ failed: %s", exc)
                        raw = None
            if not raw:
                return found

            if isinstance(raw, dict):
                tray = raw.get("tray")
            else:
                tray = getattr(raw, "tray", None)
            if not tray:
                return found

            my_id = str(getattr(self, "_own_user_id", "") or "")
            try:
                own = getattr(self, "own_user_id", None)
                if callable(own):
                    my_id = str(own() or my_id)
                elif own is not None:
                    my_id = str(own or my_id)
            except Exception:
                pass

            for item in tray:
                if isinstance(item, dict):
                    user = item.get("user") or {}
                    uname = str(user.get("username") or "")
                    uid = str(user.get("pk") or user.get("id") or item.get("id") or "")
                    items = item.get("items") or []
                else:
                    user = getattr(item, "user", None)
                    uname = str(getattr(user, "username", "") or "") if user else ""
                    uid = str(
                        getattr(user, "pk", None)
                        or getattr(item, "id", None)
                        or ""
                    )
                    items = getattr(item, "items", None) or []

                if not uid and not uname:
                    continue
                if my_id and uid and str(uid) == my_id:
                    continue

                if items:
                    stories = [self._story_from_raw(s, uid=uid, username=uname) for s in items]
                    stories = [s for s in stories if s.pk]
                    if stories:
                        found[uname or uid] = stories
                elif fill_items:
                    need_fetch.append((uid, uname))

            if need_fetch:
                def _fetch(pair):
                    uid, uname = pair
                    try:
                        return uname or uid, self.stories_by_id(uid, username=uname)
                    except Exception:
                        return uname or uid, []

                workers = min(12, max(1, len(need_fetch)))
                with ThreadPoolExecutor(max_workers=workers) as pool:
                    for key, stories in pool.map(_fetch, need_fetch):
                        if stories:
                            found[key] = stories
            return found
        except InstagramError as exc:
            log.warning("reels_tray failed: %s", exc)
            return found
        except TypeError as exc:
            log.warning("reels_tray type error: %s", exc)
            return found

    def like_tray_stories(self) -> tuple[int, int]:
        """Pull tray → like every active story. Returns (liked, accounts)."""
        tray = self.stories_tray(fill_items=True)
        liked = 0
        for uname, stories in tray.items():
            for s in stories:
                if not s.pk:
                    continue
                try:
                    if self.like_story(s.pk):
                        liked += 1
                except Exception:
                    continue
        return liked, len(tray)

    def view_tray_stories(self) -> tuple[int, int]:
        """Pull tray → mark every active story seen. Returns (viewed, accounts)."""
        tray = self.stories_tray(fill_items=True)
        all_pks: list[str] = []
        for stories in tray.values():
            all_pks.extend(s.pk for s in stories if s.pk)
        if all_pks:
            # story_seen accepts batches; chunk to stay safe
            for i in range(0, len(all_pks), 50):
                self.view_stories(all_pks[i : i + 50])
        return len(all_pks), len(tray)

    def stories_for_ids(self, people: list[tuple[str, str]]) -> dict[str, list[Story]]:
        """Fetch stories for many (user_id, username) pairs — light batch path.

        Prefer GraphQL batch (up to 25 ids) when available, else per-id user_stories
        with low concurrency. Never does usernameinfo lookups.
        """
        found: dict[str, list[Story]] = {}
        if not people:
            return found

        # Try GraphQL batch: users_stories_gql(user_ids) — batches of 25
        gql = getattr(self.cl, "users_stories_gql", None)
        if callable(gql):
            ids = []
            id_to_name: dict[str, str] = {}
            for uid, uname in people:
                uid = str(uid or "").strip()
                if not uid:
                    continue
                ids.append(int(uid) if uid.isdigit() else uid)
                id_to_name[str(uid)] = uname
            for i in range(0, len(ids), 25):
                batch = ids[i : i + 25]
                try:
                    users = self._call(gql, batch) or []
                except InstagramError as exc:
                    log.debug("users_stories_gql batch failed: %s", exc)
                    continue
                for u in users:
                    uname = str(getattr(u, "username", "") or id_to_name.get(str(getattr(u, "pk", "")), ""))
                    stories_raw = getattr(u, "stories", None) or []
                    if not stories_raw:
                        continue
                    uid = str(getattr(u, "pk", "") or "")
                    found[uname or uid] = [
                        self._story_from_raw(s, uid=uid, username=uname) for s in stories_raw
                    ]
            if found:
                return found

        # Fallback: per-id, modest concurrency, no username lookup
        def one(pair):
            uid, uname = pair
            try:
                stories = self.stories_by_id(uid, username=uname)
                return uname or uid, stories
            except Exception:
                return uname or uid, []

        workers = min(8, max(1, len(people)))
        with ThreadPoolExecutor(max_workers=workers) as pool:
            for uname, stories in pool.map(one, people):
                if stories:
                    found[uname] = stories
        return found

    def view_stories(self, story_pks: list[str]) -> bool:
        if not story_pks:
            return False
        pks = []
        for pk in story_pks:
            try:
                pks.append(int(pk))
            except (TypeError, ValueError):
                continue
        if not pks:
            return False
        seen_fn = getattr(self.cl, "story_seen", None)
        if not callable(seen_fn):
            return False
        try:
            self._call(seen_fn, pks)
            return True
        except (InstagramError, TypeError):
            return False

    def like_story(self, story_pk: str) -> bool:
        like_fn = getattr(self.cl, "story_like", None)
        if not callable(like_fn):
            return False
        try:
            return bool(self._call(like_fn, str(story_pk)))
        except (InstagramError, TypeError):
            return False

    def media(self, reference: str) -> Media:
        """Look a post up by URL, short code or raw pk."""
        ref = (reference or "").strip()
        if not ref:
            raise InstagramError("no post reference given", kind="input")
        pk = None
        if "instagram.com" in ref:
            pk = self._call(self.cl.media_pk_from_url, ref)
        elif re.fullmatch(r"\d{5,}", ref):
            pk = ref
        else:
            pk = self._call(self.cl.media_pk_from_code, ref.strip("/"))
        raw = self._call(self.cl.media_info, str(pk))
        return self._to_media(raw)

    def _to_media(self, raw) -> Media:
        user = getattr(raw, "user", None)
        caption = getattr(raw, "caption", "") or getattr(raw, "caption_text", "") or ""
        return Media(
            pk=str(getattr(raw, "pk", "") or ""),
            user_id=str(getattr(user, "pk", "") or ""),
            username=getattr(user, "username", "") or "",
            caption=caption,
            taken_at=self._epoch(getattr(raw, "taken_at", None)),
            like_count=int(getattr(raw, "like_count", 0) or 0),
            comment_count=int(getattr(raw, "comment_count", 0) or 0),
            media_type=int(getattr(raw, "media_type", 1) or 1),
            has_liked=bool(getattr(raw, "has_liked", False)),
        )

    def like_media(self, media_pk: str) -> bool:
        try:
            return bool(self._call(self.cl.media_like, str(media_pk)))
        except InstagramError:
            return False

    def unlike_media(self, media_pk: str) -> bool:
        try:
            return bool(self._call(self.cl.media_unlike, str(media_pk)))
        except InstagramError:
            return False

    def comments(self, media_pk: str, amount: int = 20) -> list[Comment]:
        raw = self._call(self.cl.media_comments, str(media_pk), amount) or []
        out = []
        for c in raw:
            user = getattr(c, "user", None)
            out.append(
                Comment(
                    pk=str(getattr(c, "pk", "") or ""),
                    text=getattr(c, "text", "") or "",
                    user_id=str(getattr(user, "pk", "") or ""),
                    username=getattr(user, "username", "") or "",
                    created_at=self._epoch(getattr(c, "created_at", None)),
                    has_liked=bool(getattr(c, "has_liked", False)),
                )
            )
        return out

    def comment(self, media_pk: str, text: str) -> Comment | None:
        try:
            raw = self._call(self.cl.media_comment, str(media_pk), text)
        except InstagramError:
            return None
        if raw is None:
            return None
        user = getattr(raw, "user", None)
        return Comment(
            pk=str(getattr(raw, "pk", "") or ""),
            text=getattr(raw, "text", "") or "",
            user_id=str(getattr(user, "pk", "") or ""),
            username=getattr(user, "username", "") or "",
            created_at=self._epoch(getattr(raw, "created_at", None)),
        )

    def like_comment(self, comment_pk: str) -> bool:
        try:
            return bool(self._call(self.cl.comment_like, int(comment_pk)))
        except (InstagramError, ValueError):
            return False

    def user_posts(self, username: str, amount: int = 12) -> list[Media]:
        uid = self.user_id(username)
        raw = self._call(self.cl.user_medias, uid, amount) or []
        return [self._to_media(m) for m in raw]

    def connections(self, kind: str, username: str, amount: int = 0) -> list[Profile]:
        """Every follower or every following account.

        ``amount`` of 0 means *the whole list* — every page of the cursor
        chain, not the first one. That is the difference between knowing
        thirty of your followers and knowing all of them.
        """
        uid = self.user_id(username)
        result = self.bulk.list_connections(kind, uid, limit=amount)
        return [
            Profile(
                user_id=str(u.get("pk") or ""),
                username=str(u.get("username") or ""),
                full_name=str(u.get("full_name") or ""),
                is_private=bool(u.get("is_private")),
                is_verified=bool(u.get("is_verified")),
            )
            for u in result.users
        ]

    def followers(self, username: str, amount: int = 50) -> list[Profile]:
        return self.connections("followers", username, amount)

    def following(self, username: str, amount: int = 50) -> list[Profile]:
        return self.connections("following", username, amount)

    def follow_by_id(self, user_id: str, username: str = "") -> bool:
        """Follow by numeric id. Username is optional and only used as last resort."""
        uid = str(user_id or "").strip()
        if not uid:
            if username:
                try:
                    uid = self.user_id(username)
                except InstagramError:
                    return False
            else:
                return False
        try:
            with self._write_lock:
                if hasattr(self.cl, "user_follow"):
                    return bool(self._call(self.cl.user_follow, uid))
                fn = getattr(self.cl, "user_friendship_v1", None)
                if fn is not None:
                    return bool(self._call(fn, uid))
        except InstagramError:
            return False
        return False

    def unfollow_by_id(self, user_id: str, username: str = "") -> bool:
        """Unfollow by numeric id. Never requires a username lookup when pk is known.

        Previous code preferred username → user_id_from_username, which fails for
        many real accounts (restricted, soft-banned, renamed, edge cases) even
        when the pk from the following list is still perfectly valid.
        """
        uid = str(user_id or "").strip()
        if not uid or not uid.isdigit():
            # Only fall back to username when we truly have no usable id
            if username:
                try:
                    uid = self.user_id(username)
                except InstagramError:
                    return False
            else:
                return False
        try:
            with self._write_lock:
                # Primary: standard instagrapi unfollow by id
                if hasattr(self.cl, "user_unfollow"):
                    return bool(self._call(self.cl.user_unfollow, uid))
                # Alternate friendship endpoint some builds expose
                fn = getattr(self.cl, "user_friendship_v1_unfollow", None)
                if fn is not None:
                    return bool(self._call(fn, uid))
                # Last resort private request
                fn2 = getattr(self.cl, "private_request", None)
                if fn2 is not None:
                    self._call(fn2, f"friendships/destroy/{uid}/")
                    return True
        except InstagramError:
            return False
        return False

    def follow(self, username: str) -> bool:
        try:
            uid = self.user_id(username)
        except InstagramError:
            return False
        return self.follow_by_id(uid, username)

    def unfollow(self, username: str) -> bool:
        """Unfollow by username. Resolves id once, then uses the robust by-id path."""
        try:
            uid = self.user_id(username)
        except InstagramError:
            return False
        return self.unfollow_by_id(uid, username)

    # --------------------------------------------------------------- notes
    def notes(self) -> list[Note]:
        raw = self._call(self.cl.get_notes) or []
        out = []
        for n in raw:
            user = getattr(n, "user", None)
            out.append(
                Note(
                    id=str(getattr(n, "id", "") or ""),
                    user_id=str(getattr(n, "user_id", "") or ""),
                    text=getattr(n, "text", "") or "",
                    username=getattr(user, "username", "") or "",
                    created_at=self._epoch(getattr(n, "created_at", None)),
                    expires_at=self._epoch(getattr(n, "expires_at", None)),
                    thread_id=str(getattr(n, "thread_id", "") or ""),
                    media_id=str(getattr(n, "media_id", "") or ""),
                )
            )
        return out

    def create_note(self, text: str) -> Note | None:
        try:
            raw = self._call(self.cl.create_note, text)
        except InstagramError:
            return None
        if raw is None:
            return None
        user = getattr(raw, "user", None)
        return Note(
            id=str(getattr(raw, "id", "") or ""),
            user_id=str(getattr(raw, "user_id", "") or ""),
            text=getattr(raw, "text", "") or text,
            username=getattr(user, "username", "") or "",
            created_at=self._epoch(getattr(raw, "created_at", None)),
            expires_at=self._epoch(getattr(raw, "expires_at", None)),
        )

    # --------------------------------------------------------- moderation
    def block(self, username: str) -> bool:
        uid = self.user_id(username)
        try:
            return bool(self._call(self.cl.user_block, uid))
        except InstagramError:
            return False

    def unblock(self, username: str) -> bool:
        uid = self.user_id(username)
        try:
            return bool(self._call(self.cl.user_unblock, uid))
        except InstagramError:
            return False

    def mute_posts(self, username: str) -> bool:
        uid = self.user_id(username)
        try:
            return bool(self._call(self.cl.mute_posts_from_follow, uid))
        except InstagramError:
            return False

    def unmute_posts(self, username: str) -> bool:
        uid = self.user_id(username)
        try:
            return bool(self._call(self.cl.unmute_posts_from_follow, uid))
        except InstagramError:
            return False

    def mute_stories(self, username: str) -> bool:
        uid = self.user_id(username)
        try:
            return bool(self._call(self.cl.mute_stories_from_follow, uid))
        except InstagramError:
            return False

    def unmute_stories(self, username: str) -> bool:
        uid = self.user_id(username)
        try:
            return bool(self._call(self.cl.unmute_stories_from_follow, uid))
        except InstagramError:
            return False

    def archive_thread(self, thread_id: str) -> bool:
        try:
            return bool(self._call(self.cl.direct_thread_hide, int(thread_id)))
        except (InstagramError, ValueError):
            return False

    def mute_thread(self, thread_id: str) -> bool:
        try:
            return bool(self._call(self.cl.direct_thread_mute, int(thread_id)))
        except (InstagramError, ValueError):
            return False

    def unmute_thread(self, thread_id: str) -> bool:
        try:
            return bool(self._call(self.cl.direct_thread_unmute, int(thread_id)))
        except (InstagramError, ValueError):
            return False

    def follow_requests(self, amount: int = 20) -> list[Profile]:
        raw = self._call(self.cl.user_follow_requests, amount) or []
        return [
            Profile(
                user_id=str(getattr(u, "pk", "") or ""),
                username=getattr(u, "username", "") or "",
                full_name=getattr(u, "full_name", "") or "",
            )
            for u in raw
        ]

    def approve_follow_request(self, username: str) -> bool:
        uid = self.user_id(username)
        try:
            return bool(self._call(self.cl.user_follow_requests_approve, [uid]))
        except InstagramError:
            try:
                return bool(self._call(self.cl.user_follow_request_approve, uid))
            except InstagramError:
                return False

    def decline_follow_request(self, username: str) -> bool:
        uid = self.user_id(username)
        try:
            return bool(self._call(self.cl.user_follow_requests_decline, [uid]))
        except InstagramError:
            try:
                return bool(self._call(self.cl.user_follow_request_decline, uid))
            except InstagramError:
                return False

    # -------------------------------------------------------------- extras
    def highlights(self, username: str) -> list[dict]:
        uid = self.user_id(username)
        raw = self._call(self.cl.user_highlights, uid) or []
        out = []
        for h in raw:
            items = getattr(h, "items", None) or []
            out.append(
                {
                    "id": str(getattr(h, "pk", "") or getattr(h, "id", "") or ""),
                    "title": getattr(h, "title", "") or "",
                    "items": len(items),
                }
            )
        return out

    def story_viewers(self, story_pk: str) -> list[Profile]:
        raw = self._call(self.cl.story_viewers, int(story_pk)) or []
        return [
            Profile(
                user_id=str(getattr(u, "pk", "") or ""),
                username=getattr(u, "username", "") or "",
                full_name=getattr(u, "full_name", "") or "",
            )
            for u in raw
        ]

    def insights(self) -> dict:
        raw = self._call(self.cl.insights_account) or {}
        out: dict[str, Any] = {}
        for key, value in (raw or {}).items():
            if isinstance(value, (int, float, str, bool)):
                out[key] = value
        return out

    def reels(self, username: str, amount: int = 10) -> list[Media]:
        uid = self.user_id(username)
        raw = self._call(self.cl.user_medias, uid, amount) or []
        reels = [m for m in raw if int(getattr(m, "media_type", 1) or 1) == 8]
        return [self._to_media(m) for m in reels]

    def thread_messages(self, thread_id: str, amount: int = 20) -> list[Message]:
        raw = self._call(self.cl.direct_messages, int(thread_id), amount) or []
        out = []
        for m in raw:
            uid = str(getattr(m, "user_id", "") or "")
            out.append(
                Message(
                    id=str(getattr(m, "id", "") or ""),
                    thread_id=str(getattr(m, "thread_id", thread_id) or thread_id),
                    text=getattr(m, "text", "") or "",
                    user_id=uid,
                    username="",
                    timestamp=self._epoch(getattr(m, "timestamp", None)),
                    is_self=bool(uid and uid == self.own_user_id),
                    item_type=str(getattr(m, "item_type", "") or "text"),
                )
            )
        return out

    # ---------------------------------------------------------- publishing
    def publish_photo(self, path: str, caption: str = "") -> Media | None:
        p = Path(path).expanduser()
        if not p.exists():
            return None
        try:
            media = self._call(self.cl.photo_upload, p, caption)
        except InstagramError:
            return None
        return self._to_media(media) if media is not None else None

    def publish_video(self, path: str, caption: str = "") -> Media | None:
        p = Path(path).expanduser()
        if not p.exists():
            return None
        try:
            media = self._call(self.cl.video_upload, p, caption)
        except InstagramError:
            return None
        return self._to_media(media) if media is not None else None

    def publish_story(self, path: str, caption: str = "") -> str:
        p = Path(path).expanduser()
        if not p.exists():
            return ""
        try:
            story = self._call(self.cl.photo_upload_to_story, p, caption)
        except InstagramError:
            return ""
        return str(getattr(story, "pk", "") or "")

    # -------------------------------------------------------------- status
    def health(self) -> dict[str, Any]:
        me = self.whoami()
        return {
            "ok": True,
            "transport": "private-api",
            "user_id": me.user_id,
            "username": me.username,
            "full_name": me.full_name,
        }

    def close(self) -> None:
        # Never call logout(): it revokes the session server-side and the next
        # start would need a fresh cookie. Closing the HTTP pool is enough.
        try:
            self.cl.private.close()
        except Exception:  # noqa: BLE001
            pass

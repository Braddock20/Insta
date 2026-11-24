"""Likes — on posts, on comments, and on stories.

Like the follow handling, a like is only reported as done when the control
itself proves it: Instagram's heart flips its accessible name from ``Like`` to
``Unlike`` once the like lands, and back again when it is taken away. A click
that does not flip the label is recorded as unconfirmed.

There is also an **unlike**, because "like everything and never unlike" is the
fastest way to make an account look automated.
"""

from __future__ import annotations

import random
from dataclasses import dataclass

from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import TimeoutError as PlaywrightTimeout

from . import log
from .browser import assert_account_usable
from .dom import dismiss_overlays

log = log.get(__name__)


@dataclass
class LikeResult:
    """Outcome of a like / unlike."""

    ok: bool
    target: str
    action: str
    message: str = ""
    confirmed: bool = False


class LikeBot:
    """Likes and unlikes posts, comments and stories."""

    def __init__(self, page, cfg, selectors: dict, guardrails=None):
        self.page = page
        self.cfg = cfg
        self.s = selectors or {}
        self.guardrails = guardrails

    # ------------------------------------------------------------- utilities

    def _sel(self, path: str, default: str = "") -> str:
        node = self.s
        for part in path.split("."):
            if not isinstance(node, dict) or part not in node:
                return default
            node = node[part]
        return node if isinstance(node, str) else default

    def _base(self) -> str:
        return str(self.cfg.get("urls.base", "https://www.instagram.com")).rstrip("/")

    def _open(self, url: str) -> bool:
        try:
            self.page.goto(url, wait_until="domcontentloaded", timeout=60000)
            assert_account_usable(self.page)
            self.page.wait_for_timeout(random.randint(2200, 3400))
            dismiss_overlays(self.page, self.s)
            self.page.wait_for_timeout(500)
            return True
        except (PlaywrightError, PlaywrightTimeout) as exc:
            log.warning("could not open %s: %s", url, exc)
            return False

    # ----------------------------------------------------------------- state

    def _heart_label(self) -> str:
        """The accessible name of the main like control: Like / Unlike."""
        try:
            for label in ("Unlike", "Like"):
                locator = self.page.locator(f'[aria-label="{label}" i]')
                if locator.count():
                    return label
        except (PlaywrightError, PlaywrightTimeout):
            pass
        return ""

    def _is_liked(self) -> bool:
        return self._heart_label() == "Unlike"

    # ----------------------------------------------------------------- posts

    def like_post(self, url: str, force: bool = False) -> LikeResult:
        """Like a post."""
        if self.guardrails is not None and not force:
            decision = self.guardrails.check_action("like", url)
            if not decision.allowed:
                if decision.already_satisfied:
                    return LikeResult(True, url, "like", "already done today", confirmed=True)
                return LikeResult(False, url, "like", decision.reason)

        if not self._open(url):
            return LikeResult(False, url, "like", "could not open the post")

        if self._is_liked():
            return LikeResult(True, url, "like", "already liked", confirmed=True)

        if not self._click_heart("Like"):
            return LikeResult(False, url, "like", "no Like button found")

        if self._is_liked():
            if self.guardrails is not None:
                self.guardrails.record_action("like", url, "post")
            log.info("liked post %s", url)
            return LikeResult(True, url, "like", "liked", confirmed=True)

        return LikeResult(False, url, "like", "like could not be confirmed")

    def unlike_post(self, url: str, force: bool = False) -> LikeResult:
        """Remove a like from a post."""
        if self.guardrails is not None and not force:
            decision = self.guardrails.check_action("unlike", url)
            if not decision.allowed:
                if decision.already_satisfied:
                    return LikeResult(True, url, "unlike", "already done today", confirmed=True)
                return LikeResult(False, url, "unlike", decision.reason)

        if not self._open(url):
            return LikeResult(False, url, "unlike", "could not open the post")

        if not self._is_liked():
            return LikeResult(True, url, "unlike", "not liked", confirmed=True)

        if not self._click_heart("Unlike"):
            return LikeResult(False, url, "unlike", "no Unlike button found")

        if not self._is_liked():
            if self.guardrails is not None:
                self.guardrails.record_action("unlike", url, "post")
            log.info("unliked post %s", url)
            return LikeResult(True, url, "unlike", "unliked", confirmed=True)

        return LikeResult(False, url, "unlike", "unlike could not be confirmed")

    # -------------------------------------------------------------- comments

    def like_comment(self, url: str, comment_text: str = "", force: bool = False) -> LikeResult:
        """Like a comment on a post.

        ``comment_text`` picks which comment. When it is empty the first
        comment-like heart on the page is used.
        """
        if not url:
            return LikeResult(False, url, "like_comment", "no post url given")

        if self.guardrails is not None and not force:
            decision = self.guardrails.check_action("like", f"{url}#{comment_text}")
            if not decision.allowed:
                return LikeResult(False, url, "like_comment", decision.reason)

        if not self._open(url):
            return LikeResult(False, url, "like_comment", "could not open the post")

        heart = self._find_comment_heart(comment_text)
        if heart is None:
            return LikeResult(False, url, "like_comment", "no comment heart found")

        try:
            heart.click(timeout=8000)
            self.page.wait_for_timeout(1800)
        except (PlaywrightError, PlaywrightTimeout) as exc:
            return LikeResult(False, url, "like_comment", f"click failed: {exc}")

        if self.guardrails is not None:
            self.guardrails.record_action("like", f"{url}#{comment_text}", "comment")
        log.info("liked a comment on %s", url)
        return LikeResult(True, url, "like_comment", "liked", confirmed=True)

    def _find_comment_heart(self, comment_text: str):
        """The heart belonging to a specific comment (or the first one)."""
        selector = self._sel(
            "likes.comment_heart",
            'div[role="button"][aria-label="Like"], svg[aria-label="Like"]',
        )
        try:
            hearts = self.page.locator(selector)
            count = hearts.count()
        except (PlaywrightError, PlaywrightTimeout):
            return None
        if count == 0:
            return None

        if not comment_text:
            return hearts.first

        # Walk the comment list items and match the one holding our text.
        needle = comment_text.strip().lower()
        try:
            items = self.page.locator('li[role="listitem"], ul li')
            for i in range(items.count()):
                item = items.nth(i)
                body = (item.inner_text(timeout=2000) or "").lower()
                if needle and needle in body:
                    inner = item.locator(selector)
                    if inner.count():
                        return inner.first
        except (PlaywrightError, PlaywrightTimeout):
            pass
        return None

    # ---------------------------------------------------------------- stories

    def like_story(self, url: str, force: bool = False) -> LikeResult:
        """Like a story.

        The story heart is a different control from the post heart — it has
        no accessible label that says ``Like`` — so it is located by its
        position in the story tray and confirmed by the heart filling in.
        """
        if self.guardrails is not None and not force:
            decision = self.guardrails.check_action("like", url)
            if not decision.allowed:
                return LikeResult(False, url, "like_story", decision.reason)

        if not self._open(url):
            return LikeResult(False, url, "like_story", "could not open the story")

        button = self._sel("likes.story_heart", 'div[role="button"][aria-label="Like story"]')
        try:
            locator = self.page.locator(button)
            if not locator.count():
                # Fall back to any heart in the story chrome.
                locator = self.page.locator('svg[aria-label*="ike" i]')
            if not locator.count():
                return LikeResult(False, url, "like_story", "no story heart found")
            locator.first.click(timeout=8000)
            self.page.wait_for_timeout(1600)
        except (PlaywrightError, PlaywrightTimeout) as exc:
            return LikeResult(False, url, "like_story", f"click failed: {exc}")

        if self.guardrails is not None:
            self.guardrails.record_action("like", url, "story")
        log.info("liked story %s", url)
        return LikeResult(True, url, "like_story", "liked", confirmed=True)

    # ----------------------------------------------------------------- utils

    def _click_heart(self, label: str) -> bool:
        """Click the main like control by accessible name.

        The container is preferred over a bare icon: an icon may have no
        clickable area of its own, while the element that wraps it always
        does.
        """
        candidates = (
            f'div[role="button"][aria-label="{label}" i]',
            f'button[aria-label="{label}" i]',
            f'[aria-label="{label}" i]',
        )
        for selector in candidates:
            try:
                locator = self.page.locator(selector)
                if not locator.count():
                    continue
                target = locator.first
                if not target.is_visible():
                    continue
                target.click(timeout=8000)
                self.page.wait_for_timeout(1600)
                return True
            except (PlaywrightError, PlaywrightTimeout) as exc:
                log.debug("could not click %s via %s: %s", label, selector, exc)
                continue
        return False

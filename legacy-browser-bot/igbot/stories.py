"""Stories — reading, viewing, replying to, and publishing them.

Four things live here:

* **list** who currently has a story, and how many;
* **view** a story (Instagram marks it seen, which is a visible action);
* **reply** to a story — Instagram routes that through the DM composer, so a
  story reply is a real DM and goes through the same confirmation rules;
* **post** a story from a local image or video.

A story reply is a DM with extra steps, so it reuses the thread composer and
waits for a new outgoing bubble before it claims success. A story that
cannot be opened, or a story reply that cannot be confirmed, is reported as a
failure rather than quietly retried.
"""

from __future__ import annotations

import random
import re
from dataclasses import dataclass, field
from pathlib import Path

from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import TimeoutError as PlaywrightTimeout

from . import log
from .browser import assert_account_usable
from .dom import dismiss_overlays

log = log.get(__name__)


@dataclass
class Story:
    """One story in the tray."""

    username: str = ""
    count: int = 0
    seen: bool = False
    url: str = ""


@dataclass
class StoryResult:
    """Outcome of a story action."""

    ok: bool
    action: str
    target: str = ""
    message: str = ""
    confirmed: bool = False


# The story tray renders one item per account, each with the account name and
# a small ring of the story count.
_STORY_TRAY_JS = r"""
() => {
  const text = (el) => (el.innerText || '').replace(/\s+/g, ' ').trim();
  const out = [];
  const seen = new Set();

  // The most reliable signal is the link itself: a story always points at
  // "/<username>/stories/".
  for (const a of document.querySelectorAll('a[href*="/stories/"]')) {
    const href = a.getAttribute('href') || '';
    let name = '';
    const m = href.match(/\/stories\/([A-Za-z0-9._]+)/);
    if (m) name = m[1];
    if (!name) {
      const img = a.querySelector('img[alt]');
      const alt = img ? (img.getAttribute('alt') || '') : '';
      const am = alt.match(/^(.+?)'s (?:story )?picture/i);
      if (am) name = am[1];
    }
    if (!name || seen.has(name)) continue;

    const label = (a.getAttribute('aria-label') || text(a) || '').trim();
    let count = 0;
    const cMatch = label.match(/(\d+)\s+(?:story|stories)/i);
    if (cMatch) count = parseInt(cMatch[1], 10);

    seen.add(name);
    out.push({username: name, count: count, href: href, seen: /seen/i.test(label)});
  }

  // Some builds render the tray as avatars with no link, so fall back to the
  // alt text of the story images themselves.
  for (const img of document.querySelectorAll('img[alt]')) {
    const alt = img.getAttribute('alt') || '';
    const am = alt.match(/^(.+?)'s story picture/i);
    if (!am) continue;
    const name = am[1];
    if (!name || seen.has(name)) continue;
    seen.add(name);
    out.push({username: name, count: 0, href: '', seen: false});
  }
  return out;
}
"""


class StoryBot:
    """Reads and interacts with Instagram stories."""

    def __init__(self, page, cfg, selectors: dict, guardrails=None, dom=None):
        self.page = page
        self.cfg = cfg
        self.s = selectors or {}
        self.guardrails = guardrails
        self.dom = dom  # optional DOM helper, reused for the DM composer

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

    # ---------------------------------------------------------------- reading

    def list_stories(self) -> list[Story]:
        """Who has a story right now."""
        path = self._sel("stories.tray_url", "/stories/")
        try:
            self.page.goto(self._base() + path, wait_until="domcontentloaded", timeout=60000)
            assert_account_usable(self.page)
            self.page.wait_for_timeout(random.randint(2500, 3800))
            dismiss_overlays(self.page, self.s)
            self.page.wait_for_timeout(600)
        except (PlaywrightError, PlaywrightTimeout) as exc:
            log.warning("could not open the story tray: %s", exc)
            return []

        try:
            raw = self.page.evaluate(_STORY_TRAY_JS)
        except PlaywrightError as exc:
            log.warning("could not read the story tray: %s", exc)
            return []

        stories: list[Story] = []
        for item in raw or []:
            username = str(item.get("username") or "")
            if not username:
                continue
            stories.append(
                Story(
                    username=username,
                    count=int(item.get("count") or 0),
                    seen=bool(item.get("seen")),
                    url=str(item.get("href") or ""),
                )
            )
        return stories

    def story_url(self, username: str) -> str:
        """The canonical story URL for a user."""
        username = (username or "").strip().lstrip("@")
        path = self._sel("stories.user_url", "/{username}/stories/")
        return self._base() + path.format(username=username)

    # ---------------------------------------------------------------- actions

    def view_story(self, username: str, force: bool = False) -> StoryResult:
        """Open someone's story. Instagram marks it as seen."""
        username = (username or "").strip().lstrip("@")
        if not username:
            return StoryResult(False, "view_story", "", "no username given")

        if self.guardrails is not None and not force:
            decision = self.guardrails.check_action("story_view", username)
            if not decision.allowed:
                return StoryResult(False, "view_story", username, decision.reason)

        url = self.story_url(username)
        try:
            self.page.goto(url, wait_until="domcontentloaded", timeout=60000)
            self.page.wait_for_timeout(random.randint(2500, 3800))
        except (PlaywrightError, PlaywrightTimeout) as exc:
            return StoryResult(False, "view_story", username, f"could not open: {exc}")

        if "/accounts/login" in str(self.page.url or ""):
            return StoryResult(False, "view_story", username, "redirected to login")

        if self.guardrails is not None:
            self.guardrails.record_action("story_view", username, "viewed")
        log.info("viewed the story of @%s", username)
        return StoryResult(True, "view_story", username, "viewed", confirmed=True)

    def reply_to_story(self, username: str, text: str, force: bool = False) -> StoryResult:
        """Reply to someone's story with a DM.

        Instagram's story reply opens a DM thread with that account, so the
        message is sent through the normal composer and confirmed by a new
        outgoing bubble.
        """
        username = (username or "").strip().lstrip("@")
        if not username:
            return StoryResult(False, "reply_story", "", "no username given")
        if not (text or "").strip():
            return StoryResult(False, "reply_story", username, "no reply text given")

        if self.guardrails is not None and not force:
            decision = self.guardrails.check_action("story_reply", username)
            if not decision.allowed:
                return StoryResult(False, "reply_story", username, decision.reason)

        url = self.story_url(username)
        try:
            self.page.goto(url, wait_until="domcontentloaded", timeout=60000)
            self.page.wait_for_timeout(random.randint(2500, 3800))
        except (PlaywrightError, PlaywrightTimeout) as exc:
            return StoryResult(False, "reply_story", username, f"could not open: {exc}")

        if not self._open_reply_composer():
            return StoryResult(False, "reply_story", username, "no Send message control found")

        if self.dom is None:
            return StoryResult(False, "reply_story", username, "no composer helper available")

        before = len(self.dom.read_messages())
        if not self.dom.type_message(text):
            return StoryResult(False, "reply_story", username, "could not type the reply")
        if not self.dom.click_send():
            return StoryResult(False, "reply_story", username, "no send button appeared")

        confirmed, reason = self.dom.wait_for_send_confirmation(text, before)
        if confirmed:
            if self.guardrails is not None:
                self.guardrails.record_action("story_reply", username, text[:80])
            log.info("replied to the story of @%s", username)
            return StoryResult(True, "reply_story", username, f"replied ({reason})", confirmed=True)

        return StoryResult(False, "reply_story", username, f"could not be confirmed: {reason}")

    def _open_reply_composer(self) -> bool:
        """Click the story's "Send message" control."""
        for label in ("Send message", "Reply", "Message"):
            try:
                locator = self.page.get_by_role("button", name=label, exact=True)
                if locator.count() and locator.first.is_visible():
                    locator.first.click(timeout=8000)
                    self.page.wait_for_timeout(2500)
                    return True
            except (PlaywrightError, PlaywrightTimeout):
                continue
            try:
                locator = self.page.locator(f'div[role="button"]:has-text("{label}")')
                if locator.count() and locator.first.is_visible():
                    locator.first.click(timeout=8000)
                    self.page.wait_for_timeout(2500)
                    return True
            except (PlaywrightError, PlaywrightTimeout):
                continue
        return False

    # -------------------------------------------------------------- publishing

    def post_story(self, media: str | Path, caption: str = "", force: bool = False) -> StoryResult:
        """Publish a story from a local image or video."""
        media_path = Path(media).expanduser()
        if not media_path.exists():
            return StoryResult(False, "post_story", "", f"media file not found: {media_path}")

        if self.guardrails is not None and not force:
            decision = self.guardrails.check_action("story_post", str(media_path))
            if not decision.allowed:
                return StoryResult(False, "post_story", str(media_path), decision.reason)

        try:
            self.page.goto(self._base() + "/", wait_until="domcontentloaded", timeout=60000)
            self.page.wait_for_timeout(3000)
        except (PlaywrightError, PlaywrightTimeout) as exc:
            return StoryResult(False, "post_story", str(media_path), f"could not load: {exc}")

        try:
            create = self.page.locator(self._sel("stories.create_button", '[aria-label="Create"]'))
            if not create.count():
                create = self.page.locator('[aria-label="New post"], [role="button"]:has-text("Create")')
            if not create.count():
                return StoryResult(False, "post_story", str(media_path), "no Create button")
            create.first.click(timeout=10000)
            self.page.wait_for_timeout(2500)
        except (PlaywrightError, PlaywrightTimeout) as exc:
            return StoryResult(False, "post_story", str(media_path), f"could not open Create: {exc}")

        opened = False
        for label in ("Story", "Add to story", "Create story"):
            try:
                option = self.page.locator(f'[role="menu"] :text-is("{label}")')
                if not option.count():
                    option = self.page.locator(f'div[role="button"]:has-text("{label}")')
                if option.count() and option.first.is_visible():
                    option.first.click(timeout=8000)
                    self.page.wait_for_timeout(3500)
                    opened = True
                    break
            except (PlaywrightError, PlaywrightTimeout):
                continue
        if not opened:
            return StoryResult(False, "post_story", str(media_path), "no Story option in the Create menu")

        try:
            file_input = self.page.locator('input[type="file"]')
            if not file_input.count():
                return StoryResult(False, "post_story", str(media_path), "no file input found")
            file_input.first.set_input_files(str(media_path))
            self.page.wait_for_timeout(4000)
        except (PlaywrightError, PlaywrightTimeout) as exc:
            return StoryResult(False, "post_story", str(media_path), f"could not attach media: {exc}")

        try:
            share = self.page.locator(self._sel("stories.share_button", 'button:has-text("Share")'))
            if not share.count():
                share = self.page.locator('div[role="button"]:has-text("Share")')
            if not share.count():
                return StoryResult(False, "post_story", str(media_path), "no Share button")
            share.first.click(timeout=12000)
            self.page.wait_for_timeout(6000)
        except (PlaywrightError, PlaywrightTimeout) as exc:
            return StoryResult(False, "post_story", str(media_path), f"could not share: {exc}")

        if self.guardrails is not None:
            self.guardrails.record_action("story_post", str(media_path), "published")
        log.info("published a story from %s", media_path.name)
        return StoryResult(True, "post_story", str(media_path), "published", confirmed=True)

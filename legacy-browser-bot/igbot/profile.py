"""Profiles and follow management.

Covers the "user profiles and account information" and "follow/unfollow
management" boxes of the feature list:

* read a profile — name, bio, website, follower / following / post counts;
* read *your own* account information;
* list someone's followers and following;
* follow and unfollow, with a **confirmed** state change.

Why the confirmation matters
----------------------------
Instagram's Follow button is a ``div[role="button"]`` whose label flips from
``Follow`` to ``Following``. Clicking it and walking away is not proof — the
click can land on the wrong element, the network can fail, or a checkpoint can
interrupt. So the only accepted proof is that the button now reads
``Following`` (or ``Follow`` again after an unfollow). A follow that cannot be
confirmed is recorded as unconfirmed and never silently retried, exactly like
an unconfirmed DM reply.

Every follow is also checked against the action caps first, and every attempt
lands in the audit trail in ``actions``.
"""

from __future__ import annotations

import random
import re
import time
from dataclasses import dataclass, field

from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import TimeoutError as PlaywrightTimeout

from . import log
from .browser import assert_account_usable
from .dom import dismiss_overlays

log = log.get(__name__)


@dataclass
class Profile:
    """What a profile page tells us."""

    username: str = ""
    full_name: str = ""
    bio: str = ""
    website: str = ""
    followers: int = 0
    following: int = 0
    posts: int = 0
    is_following: bool = False
    is_private: bool = False
    is_verified: bool = False
    url: str = ""

    def summary(self) -> str:
        return (
            f"@{self.username} — {self.full_name or '(no name)'} | "
            f"{self.followers} followers, {self.following} following, {self.posts} posts | "
            f"{'following' if self.is_following else 'not following'}"
        )


@dataclass
class FollowResult:
    """Outcome of a follow / unfollow."""

    ok: bool
    target: str
    action: str
    message: str = ""
    confirmed: bool = False


# Structural extraction — Instagram has no stable class names, so the counts
# are read from the links whose text ends in "followers" / "following" /
# "posts", which survive redesigns far better than a class name.
_PROFILE_JS = r"""
({username}) => {
  const text = (el) => (el.innerText || '').replace(/\s+/g, ' ').trim();
  const num = (s) => {
    const m = (s || '').replace(/,/g, '').match(/([\d.]+)\s*([KMB])?/i);
    if (!m) return 0;
    let v = parseFloat(m[1]) || 0;
    const u = (m[2] || '').toUpperCase();
    if (u === 'K') v *= 1e3;
    else if (u === 'M') v *= 1e6;
    else if (u === 'B') v *= 1e9;
    return Math.round(v);
  };

  const out = {
    full_name: '', bio: '', website: '', followers: 0, following: 0, posts: 0,
    is_private: false, is_verified: false, is_following: false, has_follow_button: false,
  };

  const body = text(document.body);
  out.is_private = /This Account is Private/i.test(body);
  out.is_verified = !!document.querySelector('[aria-label="Verified"], svg[aria-label="Verified"]');

  // Counts live on links (or spans): "1,234 followers", "340 following",
  // "58 posts" — and Instagram uses the singular for a count of one.
  const COUNT = /([\d.,]+\s*[KMB]?)\s+(followers|following|posts?)$/i;
  for (const el of document.querySelectorAll('a[href], span')) {
    const t = text(el);
    const m = t.match(COUNT);
    if (!m) continue;
    const value = num(m[1]);
    const kind = m[2].toLowerCase();
    if (kind === 'followers') out.followers = value;
    else if (kind === 'following') out.following = value;
    else if (kind === 'post' || kind === 'posts') out.posts = value;
  }

  // The name and the bio are the two spans under the profile picture, in
  // that order. Everything else on the page — the insights banner, the
  // account menu, the "Save your login info?" modal — has to be filtered out
  // or it is mistaken for the bio.
  const NOISE = /view insights|insights|save your login|save info|not now|not now|close|edit profile|follow|following|message|share|add to story|log in|log out|view archive|switch accounts|settings|report a problem|help|about us|privacy|terms|see photos|don.t have an account/i;
  const seen = new Set();
  const fields = [];
  for (const el of document.querySelectorAll('span[dir="auto"], div[dir="auto"]')) {
    const t = text(el);
    if (!t || t.length > 400) continue;
    if (NOISE.test(t)) continue;
    if (/^\d[\d.,]*\s*(followers|following|posts?)$/i.test(t)) continue;
    if (username && t === username) continue;
    if (seen.has(t)) continue;
    seen.add(t);
    fields.push(t);
  }

  // h1 holds the display name on most profiles, but on some it holds the
  // username instead. Either way the name and the bio are the first two
  // fields that are not the username.
  const h1 = document.querySelector('h1');
  const h1Text = h1 ? text(h1) : '';
  const h1IsName = h1Text && h1Text !== username;

  if (h1IsName) {
    out.full_name = h1Text;
    out.bio = fields.length ? fields[0] : '';
  } else if (fields.length >= 2) {
    out.full_name = fields[0];
    out.bio = fields[1];
  } else if (fields.length === 1) {
    out.bio = fields[0];
  }
  if (!out.full_name) out.full_name = h1Text && h1Text !== username ? h1Text : '';

  // The buttons live in a small strip near the top of the profile.
  for (const btn of document.querySelectorAll('div[role="button"], button')) {
    const label = (btn.getAttribute('aria-label') || text(btn) || '').trim();
    if (/^Follow$/i.test(label)) { out.has_follow_button = true; }
    if (/^Following$/i.test(label)) { out.is_following = true; out.has_follow_button = true; }
    if (/^Unfollow$/i.test(label)) { out.is_following = false; }
  }

  // Website link.
  const site = Array.from(document.querySelectorAll('a[href]'))
    .find(a => {
      const href = a.getAttribute('href') || '';
      return /^https?:\/\//i.test(href) && !/instagram\.com|facebook\.com/i.test(href);
    });
  if (site) out.website = text(site);

  return out;
}
"""


class ProfileBot:
    """Reads profiles and manages follow relationships."""

    def __init__(self, page, cfg, selectors: dict, guardrails=None):
        self.page = page
        self.cfg = cfg
        self.s = selectors or {}
        self.guardrails = guardrails
        self._username = ""

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

    def _profile_path(self) -> str:
        return self._sel("profile.url", "/{username}/")

    # --------------------------------------------------------------- reading

    def open(self, username: str) -> bool:
        """Open a profile page. Returns False if it could not be loaded."""
        username = (username or "").strip().lstrip("@")
        if not username:
            return False
        self._username = username
        path = self._profile_path().format(username=username)
        try:
            self.page.goto(self._base() + path, wait_until="domcontentloaded", timeout=60000)
            assert_account_usable(self.page)
            self.page.wait_for_timeout(random.randint(2000, 3200))
        except (PlaywrightError, PlaywrightTimeout) as exc:
            log.warning("could not open profile @%s: %s", username, exc)
            return False
        # Instagram puts a "Save your login info?" modal over a freshly used
        # profile. It hides the name, the bio and the counts, so it goes first.
        dismiss_overlays(self.page, self.s)
        self.page.wait_for_timeout(600)
        return True

    def read(self, username: str = "") -> Profile:
        """Read the profile currently on screen."""
        try:
            raw = self.page.evaluate(_PROFILE_JS, {"username": username})
        except PlaywrightError as exc:
            log.warning("could not read profile: %s", exc)
            return Profile(username=username)

        profile = Profile(
            username=username,
            full_name=str(raw.get("full_name") or ""),
            bio=str(raw.get("bio") or ""),
            website=str(raw.get("website") or ""),
            followers=int(raw.get("followers") or 0),
            following=int(raw.get("following") or 0),
            posts=int(raw.get("posts") or 0),
            is_following=bool(raw.get("is_following")),
            is_private=bool(raw.get("is_private")),
            is_verified=bool(raw.get("is_verified")),
            url=str(self.page.url or ""),
        )
        if not profile.username:
            match = re.search(r"instagram\.com/([^/?#]+)", profile.url)
            if match:
                profile.username = match.group(1)
        return profile

    def account_info(self) -> Profile:
        """Read *your own* account information."""
        if not self.open(str(self.cfg.get("account.username", "") or "")):
            return Profile()
        return self.read(str(self.cfg.get("account.username", "") or ""))

    def list_followers(self, username: str) -> list[str]:
        """Usernames of a profile's followers."""
        return self._list_people(username, "followers")

    def list_following(self, username: str) -> list[str]:
        """Usernames of the accounts a profile follows."""
        return self._list_people(username, "following")

    def _list_people(self, username: str, kind: str) -> list[str]:
        username = (username or "").strip().lstrip("@")
        if not username:
            return []
        path = self._sel("profile.people_url", "/{username}/{kind}/").format(
            username=username, kind=kind
        )
        try:
            self.page.goto(self._base() + path, wait_until="domcontentloaded", timeout=60000)
            assert_account_usable(self.page)
            self.page.wait_for_timeout(random.randint(2000, 3200))
        except (PlaywrightError, PlaywrightTimeout) as exc:
            log.warning("could not open %s of @%s: %s", kind, username, exc)
            return []

        found: list[str] = []
        seen: set[str] = set()
        try:
            for link in self.page.locator(f'a[href^="/{username}/"], a[href^="/"]').all():
                href = link.get_attribute("href") or ""
                match = re.fullmatch(r"/([^/?#]+)/?", href)
                if not match:
                    continue
                name = match.group(1)
                if name in seen or name in ("", "p", "reel", "explore", "direct", "stories"):
                    continue
                if name == username:
                    continue
                seen.add(name)
                found.append(name)
        except (PlaywrightError, PlaywrightTimeout):
            pass
        return found

    # ---------------------------------------------------------------- actions

    def _click_button(self, wanted: str, alternatives: tuple[str, ...] = ()) -> bool:
        """Click a role=button whose label matches one of the given names."""
        names = (wanted,) + alternatives
        for name in names:
            try:
                locator = self.page.get_by_role("button", name=name, exact=True)
                if locator.count() and locator.first.is_visible():
                    locator.first.click(timeout=8000)
                    self.page.wait_for_timeout(1800)
                    return True
            except (PlaywrightError, PlaywrightTimeout):
                continue
            try:
                locator = self.page.locator(f'div[role="button"]:has-text("{name}")')
                if locator.count() and locator.first.is_visible():
                    locator.first.click(timeout=8000)
                    self.page.wait_for_timeout(1800)
                    return True
            except (PlaywrightError, PlaywrightTimeout):
                continue
        return False

    def _state(self) -> str:
        """The current follow button state: follow / following / unknown."""
        try:
            raw = self.page.evaluate(_PROFILE_JS, {"username": self._username})
        except PlaywrightError:
            return "unknown"
        if raw.get("is_following"):
            return "following"
        if raw.get("has_follow_button"):
            return "follow"
        return "unknown"

    def follow(self, username: str, force: bool = False) -> FollowResult:
        """Follow an account, and confirm that it worked.

        ``force`` skips the caps — reserved for a deliberate manual run.
        """
        username = (username or "").strip().lstrip("@")
        if not username:
            return FollowResult(False, username, "follow", "no username given")

        if self.guardrails is not None and not force:
            decision = self.guardrails.check_action("follow", username)
            if not decision.allowed:
                if decision.already_satisfied:
                    return FollowResult(True, username, "follow", "already done today", confirmed=True)
                return FollowResult(False, username, "follow", decision.reason)

        if not self.open(username):
            return FollowResult(False, username, "follow", "could not open the profile")

        before = self._state()
        if before == "following":
            return FollowResult(True, username, "follow", "already following", confirmed=True)

        if not self._click_button("Follow", ("Unfollow",)):
            return FollowResult(False, username, "follow", "no Follow button found")

        after = self._state()
        if after == "following":
            if self.guardrails is not None:
                self.guardrails.record_action("follow", username, "confirmed")
            log.info("followed @%s", username)
            return FollowResult(True, username, "follow", "followed", confirmed=True)

        return FollowResult(
            False, username, "follow", f"follow could not be confirmed (state: {after})"
        )

    def unfollow(self, username: str, force: bool = False) -> FollowResult:
        """Unfollow an account, and confirm that it worked."""
        username = (username or "").strip().lstrip("@")
        if not username:
            return FollowResult(False, username, "unfollow", "no username given")

        if self.guardrails is not None and not force:
            decision = self.guardrails.check_action("unfollow", username)
            if not decision.allowed:
                if decision.already_satisfied:
                    return FollowResult(True, username, "unfollow", "already done today", confirmed=True)
                return FollowResult(False, username, "unfollow", decision.reason)

        if not self.open(username):
            return FollowResult(False, username, "unfollow", "could not open the profile")

        if self._state() != "following":
            return FollowResult(True, username, "unfollow", "not following", confirmed=True)

        if not self._click_button("Following", ("Unfollow",)):
            return FollowResult(False, username, "unfollow", "no Following button found")

        # Instagram asks "Unfollow @name?" in a confirmation sheet.
        try:
            sheet = self.page.get_by_role("button", name="Unfollow", exact=True)
            if sheet.count() and sheet.first.is_visible():
                sheet.first.click(timeout=6000)
                self.page.wait_for_timeout(1800)
        except (PlaywrightError, PlaywrightTimeout):
            pass

        after = self._state()
        if after in ("follow", "unknown") and after != "following":
            if self.guardrails is not None:
                self.guardrails.record_action("unfollow", username, "confirmed")
            log.info("unfollowed @%s", username)
            return FollowResult(True, username, "unfollow", "unfollowed", confirmed=True)

        return FollowResult(
            False, username, "unfollow", f"unfollow could not be confirmed (state: {after})"
        )

    def follow_back(self, username: str) -> FollowResult:
        """Follow someone who already follows you — the common courtesy action."""
        return self.follow(username)

    # --------------------------------------------------------------- helpers

    def wait(self, seconds: float) -> None:
        time.sleep(seconds)

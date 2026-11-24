"""Notifications and activity monitoring.

Instagram splits this into two places:

* the **activity** page — who followed you, who liked your posts, who
  commented, who mentioned you;
* the **notifications** bell — the same information in a dropdown.

The bot reads the activity page, because it is a full page with stable
markup, while the bell is a transient dropdown that disappears when you look
at it.

Every item is fingerprinted, so the same activity is never reported twice and
a restart never re-announces what was already seen. New items are also
recorded in the ``activity`` table, which is what makes "who followed me
today" answerable after the fact.

Nothing here acts on the notifications by itself — it only reports them. The
reaction rules in :mod:`igbot.automation` decide what to do about them.
"""

from __future__ import annotations

import random
import re
from dataclasses import dataclass

from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import TimeoutError as PlaywrightTimeout

from . import log
from .browser import assert_account_usable
from .dom import dismiss_overlays
from .store import fingerprint_for

log = log.get(__name__)

# The words Instagram uses to describe each kind of activity.
_KIND_PATTERNS = (
    ("security", re.compile(r"trying to log into|new login|unusual activity|changed your password", re.I)),
    ("follow", re.compile(r"\bstarted following you(?!\w)|\bfollowed you(?!\w)", re.I)),
    ("like", re.compile(r"\bliked your(?!\w)", re.I)),
    ("comment", re.compile(r"\bcommented on your(?!\w)|\bcommented:(?!\w)", re.I)),
    ("mention", re.compile(r"\bmentioned you(?!\w)", re.I)),
    ("story_reply", re.compile(r"\bsent you a message(?!\w)|\breplied to your story(?!\w)", re.I)),
)


@dataclass
class ActivityItem:
    """One row in the activity feed."""

    kind: str
    username: str = ""
    text: str = ""
    when: str = ""
    url: str = ""

    @property
    def fingerprint(self) -> str:
        return fingerprint_for(self.kind, self.username, self.text)

    def describe(self) -> str:
        who = f"@{self.username}" if self.username else "someone"
        return f"{self.kind:<12} {who:<22} {self.text[:60]}"


# The notifications page is a React app whose rows change shape with every
# redesign, so the rows are found by their *words* rather than by a selector.
# The outermost element that mentions an activity is the row; anything nested
# inside it is a fragment of the same row.
_ACTIVITY_JS = r"""
() => {
  const text = (el) => (el.innerText || '').replace(/\s+/g, ' ').trim();
  const VERB = /liked your|started following you|followed you|commented on your|mentioned you|replied to your story|sent you a message|requested to follow/i;
  const SECURITY = /trying to log into|new login|security (?:alert|check)|unusual activity|changed your password|review your account|it was you/i;

  // Instagram's own navigation words, which are not accounts.
  const SKIP = new Set(['direct','reel','reels','explore','p','stories','accounts','settings',
    'about','help','developer','legal','privacy','terms','challenge','one','all','posts',
    'mentions','followers','shop','saved','create','instagram','facebook','discover',
    'notifications','suggested','today','activity','archived','messages']);

  const out = [];
  const seen = new Set();

  const add = (username, row, href) => {
    if (!row || row.length < 8) return;
    const isActivity = VERB.test(row);
    const isSecurity = SECURITY.test(row);
    if (!isActivity && !isSecurity) return;

    const key = username + '|' + row.slice(0, 100);
    if (seen.has(key)) return;
    seen.add(key);

    // The trailing relative time, if any.
    const whenMatch = row.match(/(\d+\s*[mhdw]|just now|yesterday|today|[\d]+\s*(?:min|hour|day|week)s? ago)\s*$/i);
    const when = whenMatch ? whenMatch[1] : '';
    const withoutTime = when ? row.slice(0, row.length - when.length).trim() : row;

    out.push({username: username, text: withoutTime, when: when, href: href, security: isSecurity});
  };

  // Pass one: anchor on the profile links. The row is the nearest ancestor
  // that says more than the link itself — which is how a single event is
  // separated from the container that holds every event.
  for (const a of document.querySelectorAll('a[href]')) {
    const href = (a.getAttribute('href') || '').trim();
    const m = href.match(/^\/([A-Za-z0-9._]+)\/?$/);
    if (!m) continue;
    const username = m[1];
    if (SKIP.has(username.toLowerCase())) continue;

    let node = a;
    let parent = a.parentElement;
    while (parent) {
      const pt = text(parent);
      if (pt.length > text(node).length + 2 && pt.length <= 320) break;
      node = parent;
      parent = parent.parentElement;
    }
    add(username, text(node), href);
  }

  // Pass two: security notices. They carry no profile link, so pass one
  // cannot reach them — and they are the most important thing on the page.
  for (const el of document.querySelectorAll('div, span, p, li')) {
    const body = text(el);
    if (!body || body.length < 12 || body.length > 320) continue;
    if (!SECURITY.test(body)) continue;

    let parent = el.parentElement;
    let nested = false;
    for (let i = 0; i < 4 && parent; i++) {
      if (SECURITY.test(text(parent))) { nested = true; break; }
      parent = parent.parentElement;
    }
    if (nested) continue;
    add('', body, '');
  }

  return out;
}
"""


class NotificationBot:
    """Reads the activity feed and tracks what has already been seen."""

    def __init__(self, page, cfg, selectors: dict, store=None):
        self.page = page
        self.cfg = cfg
        self.s = selectors or {}
        self.store = store

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

    def open_activity(self) -> bool:
        """Open the activity page."""
        # Instagram moved the activity feed to the notifications page; the
        # old path redirects there. Both are tried, in order.
        for path in self._activity_paths():
            try:
                self.page.goto(self._base() + path, wait_until="domcontentloaded", timeout=60000)
                assert_account_usable(self.page)
                self.page.wait_for_timeout(random.randint(2500, 3800))
                dismiss_overlays(self.page, self.s)
                self.page.wait_for_timeout(600)
            except (PlaywrightError, PlaywrightTimeout) as exc:
                log.warning("could not open %s: %s", path, exc)
                continue
            if "/accounts/login" in str(self.page.url or ""):
                log.warning("%s redirected to login", path)
                continue
            return True
        return False

    def _activity_paths(self) -> list[str]:
        """The pages that may hold the activity feed, most likely first."""
        configured = self._sel("activity.url", "/notifications/")
        paths = [configured]
        for extra in ("/notifications/", "/accounts/activity/"):
            if extra not in paths:
                paths.append(extra)
        return paths

    def read(self) -> list[ActivityItem]:
        """Read the activity feed, newest first."""
        if not self.open_activity():
            return []
        try:
            raw = self.page.evaluate(_ACTIVITY_JS)
        except PlaywrightError as exc:
            log.warning("could not read the activity feed: %s", exc)
            return []

        items: list[ActivityItem] = []
        for entry in raw or []:
            body = str(entry.get("text") or "")
            kind = "other"
            if entry.get("security"):
                kind = "security"
            else:
                for name, pattern in _KIND_PATTERNS:
                    if pattern.search(body):
                        kind = name
                        break
            items.append(
                ActivityItem(
                    kind=kind,
                    username=str(entry.get("username") or ""),
                    text=body,
                    when=str(entry.get("when") or ""),
                    url=str(entry.get("href") or ""),
                )
            )
        return items

    # ---------------------------------------------------------------- tracking

    def new_items(self, items: list[ActivityItem] | None = None) -> list[ActivityItem]:
        """Activity that has not been seen before.

        Also records everything it returns, so the next call only reports what
        is genuinely new.
        """
        found = items if items is not None else self.read()
        if self.store is None:
            return found

        fresh: list[ActivityItem] = []
        for item in found:
            fingerprint = item.fingerprint
            if self.store.record_activity(
                item.kind, username=item.username, detail=item.text[:200], fingerprint=fingerprint
            ):
                fresh.append(item)
        return fresh

    def counts(self) -> dict[str, int]:
        """How many of each kind of activity are recorded."""
        counts = {name: 0 for name, _ in _KIND_PATTERNS}
        counts["other"] = 0
        if self.store is None:
            return counts
        for row in self.store.recent_activity(limit=500):
            kind = str(row["kind"] or "other")
            counts[kind] = counts.get(kind, 0) + 1
        return counts

    def recent(self, limit: int = 25) -> list[ActivityItem]:
        """Recently recorded activity, newest first."""
        if self.store is None:
            return []
        items: list[ActivityItem] = []
        for row in self.store.recent_activity(limit=limit):
            items.append(
                ActivityItem(
                    kind=str(row["kind"] or "other"),
                    username=str(row["username"] or ""),
                    text=str(row["detail"] or ""),
                )
            )
        return items

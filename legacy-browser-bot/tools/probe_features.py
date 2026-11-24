#!/usr/bin/env python3
"""Probe the real Instagram pages behind the new automation features.

Dumps the structure of a profile, the activity feed and the story tray so
the extractors can be pinned against what the site actually serves.

Read-only: it never clicks, sends, follows, likes, or publishes.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from igbot import config  # noqa: E402
from igbot import log as logmod  # noqa: E402
from igbot.browser import Browser  # noqa: E402
from igbot.dom import dismiss_overlays  # noqa: E402

logmod.setup("INFO")


ROWS_JS = r"""
() => {
  const text = (el) => (el.innerText || '').replace(/\s+/g, ' ').trim();
  const out = {tabs: [], rows: []};
  for (const t of document.querySelectorAll('[role="tab"]')) {
    out.tabs.push(text(t));
  }
  // Any element whose own text mentions an activity verb.
  const VERB = /liked your|started following|followed you|commented on your|mentioned you|replied to your story|sent you a message/i;
  const all = document.querySelectorAll('div, span, li, a');
  for (const el of all) {
    const t = text(el);
    if (!t || t.length > 300 || t.length < 10) continue;
    if (!VERB.test(t)) continue;
    // Only keep the *outermost* match, so we get one row per event.
    let parent = el.parentElement, nested = false;
    for (let i = 0; i < 4 && parent; i++) {
      if (VERB.test(text(parent))) { nested = true; break; }
      parent = parent.parentElement;
    }
    if (nested) continue;
    const a = el.querySelector('a[href]');
    out.rows.push({
      tag: el.tagName.toLowerCase(),
      role: el.getAttribute('role') || '',
      text: t.slice(0, 130),
      href: a ? (a.getAttribute('href') || '').slice(0, 40) : '',
    });
  }
  return out;
}
"""

HOME_STORIES_JS = r"""
() => {
  const out = [];
  for (const img of document.querySelectorAll('img[alt]')) {
    const alt = img.getAttribute('alt') || '';
    const m = alt.match(/^(.+?)'s story picture/i);
    if (!m) continue;
    const link = img.closest('a[href]');
    out.push({username: m[1], href: link ? (link.getAttribute('href') || '') : '',
              alt: alt.slice(0, 60)});
  }
  // Also: elements that carry a story count in their label.
  for (const el of document.querySelectorAll('[aria-label]')) {
    const aria = el.getAttribute('aria-label') || '';
    const m = aria.match(/^(.+?),\s*(\d+)\s+stor(?:y|ies)/i);
    if (m) out.push({username: m[1], count: parseInt(m[2], 10), aria: aria.slice(0, 70)});
  }
  return out;
}
"""

COUNTS_JS = r"""
() => {
  const text = (el) => (el.innerText || '').replace(/\s+/g, ' ').trim();
  const out = {links: [], spans: [], buttons: [], h1: ''};
  const h1 = document.querySelector('h1');
  if (h1) out.h1 = text(h1);
  for (const a of document.querySelectorAll('a[href]')) {
    const t = text(a);
    if (!t) continue;
    out.links.push({href: (a.getAttribute('href')||'').slice(0,60), text: t.slice(0,60)});
  }
  for (const s of document.querySelectorAll('span[dir="auto"], span')) {
    const t = text(s);
    if (t && t.length > 3) out.spans.push(t.slice(0, 90));
  }
  for (const b of document.querySelectorAll('div[role="button"], button')) {
    const t = text(b);
    const aria = b.getAttribute('aria-label') || '';
    if (t || aria) out.buttons.push({text: t.slice(0,40), aria: aria.slice(0,40)});
  }
  return out;
}
"""

ACTIVITY_JS = r"""
() => {
  const text = (el) => (el.innerText || '').replace(/\s+/g, ' ').trim();
  const out = {url: location.href, body: text(document.body).slice(0, 600), lists: []};
  const items = document.querySelectorAll('li, [role="list"] > *, div[role="button"]');
  for (let i = 0; i < Math.min(items.length, 40); i++) {
    const el = items[i];
    const t = text(el);
    if (!t || t.length < 8) continue;
    const a = el.querySelector('a[href]');
    out.lists.push({
      tag: el.tagName.toLowerCase(),
      role: el.getAttribute('role') || '',
      text: t.slice(0, 110),
      href: a ? (a.getAttribute('href')||'').slice(0,50) : '',
    });
  }
  return out;
}
"""

TRAY_JS = r"""
() => {
  const text = (el) => (el.innerText || '').replace(/\s+/g, ' ').trim();
  const out = [];
  const nodes = document.querySelectorAll('a[href], div[role="button"], img[alt]');
  for (const el of nodes) {
    const alt = el.getAttribute('alt') || '';
    const aria = el.getAttribute('aria-label') || '';
    const t = text(el);
    const href = el.getAttribute('href') || '';
    if (!alt && !aria && !t) continue;
    out.push({tag: el.tagName.toLowerCase(), href: href.slice(0,50), alt: alt.slice(0,70),
              aria: aria.slice(0,70), text: t.slice(0,60)});
  }
  return out;
}
"""


def dump(title: str, payload) -> None:
    print(f"\n{'='*70}\n{title}\n{'='*70}")
    print(json.dumps(payload, indent=1, default=str)[:6000])


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", default=str(Path(__file__).resolve().parents[1]))
    ap.add_argument("--user", default="")
    ap.add_argument("--out", default="")
    args = ap.parse_args()

    cfg = config.load(args.root)
    logmod.setup("INFO")

    browser = Browser(cfg, headless=True)
    page = browser.launch()

    cookie_source = str(cfg.resolve("account.cookies_file"))
    browser.import_cookies(cookie_source)
    status = browser.session_status()
    print(f"[probe] session: {status.describe()}")
    if not status.logged_in:
        print("[probe] cannot continue without a session")
        return 1

    base = str(cfg.get("urls.base", "https://www.instagram.com")).rstrip("/")
    out: dict = {}

    user = args.user or str(cfg.get("account.username", "") or "")
    if user:
        page.goto(f"{base}/{user}/", wait_until="domcontentloaded", timeout=60000)
        page.wait_for_timeout(4000)
        dismiss_overlays(page, {})
        page.wait_for_timeout(1500)
        out["profile"] = page.evaluate(COUNTS_JS)
        out["profile"]["url"] = page.url
        dump(f"PROFILE @{user}", out["profile"])

    page.goto(f"{base}/accounts/activity/", wait_until="domcontentloaded", timeout=60000)
    page.wait_for_timeout(4000)
    dismiss_overlays(page, {})
    page.wait_for_timeout(1500)
    out["activity"] = page.evaluate(ACTIVITY_JS)
    out["activity"]["url"] = page.url
    dump("ACTIVITY", out["activity"])

    page.goto(f"{base}/stories/", wait_until="domcontentloaded", timeout=60000)
    page.wait_for_timeout(4000)
    dismiss_overlays(page, {})
    page.wait_for_timeout(1500)
    out["tray"] = {"url": page.url, "items": page.evaluate(TRAY_JS)}
    dump("STORY TRAY", out["tray"])

    page.goto(f"{base}/", wait_until="domcontentloaded", timeout=60000)
    page.wait_for_timeout(4000)
    dismiss_overlays(page, {})
    page.wait_for_timeout(1500)
    out["home_stories"] = page.evaluate(HOME_STORIES_JS)
    dump("HOME FEED STORIES", out["home_stories"])

    page.goto(f"{base}/notifications/", wait_until="domcontentloaded", timeout=60000)
    page.wait_for_timeout(4000)
    dismiss_overlays(page, {})
    page.wait_for_timeout(1500)
    out["rows"] = page.evaluate(ROWS_JS)
    dump("NOTIFICATION ROWS", out["rows"])

    if args.out:
        Path(args.out).write_text(json.dumps(out, indent=1, default=str), encoding="utf-8")
        print(f"\n[probe] written to {args.out}")

    browser.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

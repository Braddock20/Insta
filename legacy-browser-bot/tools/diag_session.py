#!/usr/bin/env python3
"""Trace how Instagram responds to our session.

Logs navigation status codes, Set-Cookie headers, and how the cookie jar
changes at each step, so we can tell *why* the inbox bounces to a login page.

Read-only: never clicks, sends, follows, or publishes.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from igbot import config  # noqa: E402
from igbot import log as logmod  # noqa: E402
from igbot.browser import Browser  # noqa: E402

logmod.setup("INFO")

NETSCAPE = Path(__file__).resolve().parents[1] / "secrets" / "cookies.txt"


def jar(browser: Browser) -> dict[str, str]:
    return {c["name"]: c["value"] for c in browser.context.cookies()}


def main() -> int:
    cfg = config.load(Path(__file__).resolve().parents[1])
    browser = Browser(cfg, headless=True)
    page = browser.launch()

    navs: list[tuple[str, int, list[str]]] = []

    def on_response(resp):
        try:
            if resp.request.resource_type not in ("document", "xhr", "fetch"):
                return
            url = resp.url
            if "instagram.com" not in url:
                return
            try:
                headers = resp.headers
            except Exception:
                headers = {}
            sc = headers.get("set-cookie", "")
            if resp.status >= 300 or sc:
                navs.append((url[:110], resp.status, [sc[:200]] if sc else []))
        except Exception:
            pass

    page.on("response", on_response)

    try:
        browser.import_cookies(str(NETSCAPE))
        before = jar(browser)
        print(f"\njar after import: {sorted(before)}")

        print("\n--- navigating to homepage ---")
        page.goto("https://www.instagram.com/", wait_until="domcontentloaded", timeout=60000)
        page.wait_for_timeout(7000)
        print("url         :", page.url)
        after_home = jar(browser)
        print("jar keys    :", sorted(after_home))
        for name in ("sessionid", "csrftoken", "mid", "ig_did", "datr", "rur"):
            b, a = before.get(name, ""), after_home.get(name, "")
            if b != a:
                print(f"  CHANGED {name}: {'<removed>' if not a else a[:60]!r}")
        print("sessionid still present:", "sessionid" in after_home)

        # Is the homepage actually rendering the logged-in shell?
        try:
            body = page.inner_text("body", timeout=6000)
        except Exception:
            body = ""
        print("homepage shows 'Log in':", "log in" in body.lower())
        print("homepage shows 'Messages':", "messages" in body.lower())
        print("direct links:", page.locator('a[href*="/direct/"]').count())
        page.screenshot(path="/workspace/work/probe/home.png")

        print("\n--- navigating to /direct/inbox/ ---")
        page.goto("https://www.instagram.com/direct/inbox/", wait_until="domcontentloaded", timeout=60000)
        page.wait_for_timeout(8000)
        print("url         :", page.url)
        after_inbox = jar(browser)
        print("jar keys    :", sorted(after_inbox))
        print("sessionid still present:", "sessionid" in after_inbox)
        page.screenshot(path="/workspace/work/probe/inbox.png")

        print("\n--- responses of interest ---")
        for url, status, sc in navs:
            print(f"  [{status}] {url}")
            for s in sc:
                print(f"        set-cookie: {s}")

        print("\n--- try the in-app Messages link instead of the URL ---")
        page.goto("https://www.instagram.com/", wait_until="domcontentloaded", timeout=60000)
        page.wait_for_timeout(6000)
        link = page.locator('a[href*="/direct/"]').first
        if link.count():
            link.click(timeout=10000)
            page.wait_for_timeout(8000)
            print("url after clicking Messages:", page.url)
            page.screenshot(path="/workspace/work/probe/after_click.png")
        else:
            print("no /direct/ link found on the homepage")
    finally:
        browser.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

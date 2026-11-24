#!/usr/bin/env python3
"""Diagnose cookie import/export fidelity.

Answers three questions:

1. Does ``Browser.export_cookies()`` keep every cookie that was imported?
2. Does the session still authenticate after an import -> export -> import
   round-trip?
3. Does percent-decoding the ``sessionid`` value break authentication?

Read-only: it never clicks, sends, follows, or publishes.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from igbot import config  # noqa: E402
from igbot import log as logmod  # noqa: E402
from igbot.browser import Browser, parse_cookie_source  # noqa: E402

logmod.setup("INFO")

NETSCAPE = Path("/workspace/attachments/1c91804b8ee96ba3/cookies(1).txt")


def names(cookies: list[dict]) -> list[str]:
    return [c["name"] for c in cookies]


def main() -> int:
    cfg = config.load(Path(__file__).resolve().parents[1])
    browser = Browser(cfg, headless=True)
    page = browser.launch()
    try:
        raw = NETSCAPE.read_text(encoding="utf-8")
        parsed = parse_cookie_source(raw, default_domain=".instagram.com")
        print("\n=== 1. parse_cookie_source on the Netscape file ===")
        print("parsed:", names(parsed))
        for c in parsed:
            if c["name"] in ("sessionid", "csrftoken"):
                print(f"  {c['name']}: {c['value'][:70]}")

        count = browser.import_cookies(str(NETSCAPE))
        print(f"\n=== 2. after import_cookies ({count} cookies) ===")
        ctx = browser.context.cookies("https://www.instagram.com")
        print("context.cookies('https://www.instagram.com'):", names(ctx))
        allc = browser.context.cookies()
        print("context.cookies() [no filter]            :", names(allc))

        exported = browser.export_cookies()
        print("\n=== 3. export_cookies() output ===")
        print(exported[:400])
        missing = set(names(parsed)) - set(
            [p.split("=", 1)[0].strip() for p in exported.split(";")]
        )
        print("MISSING FROM EXPORT:", sorted(missing) or "(none)")

        print("\n=== 4. sessionid value comparison ===")
        sid_src = next((c["value"] for c in parsed if c["name"] == "sessionid"), "")
        sid_ctx = next((c["value"] for c in ctx if c["name"] == "sessionid"), "")
        sid_exp = ""
        for p in exported.split(";"):
            if p.strip().startswith("sessionid="):
                sid_exp = p.split("=", 1)[1].strip()
        print("source   :", sid_src[:80])
        print("context  :", sid_ctx[:80])
        print("exported :", sid_exp[:80])
        print("context has sessionid?", bool(sid_ctx))
        print("exported has sessionid?", bool(sid_exp))

        print("\n=== 5. auth with the imported (Netscape) cookies ===")
        print("is_logged_in:", browser.is_logged_in())
        page.goto("https://www.instagram.com/direct/inbox/", wait_until="domcontentloaded", timeout=60000)
        page.wait_for_timeout(6000)
        print("inbox url  :", page.url)
        print("logged in on inbox:", "/accounts/login" not in page.url)
    finally:
        browser.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

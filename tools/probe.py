"""See what Instagram is willing to tell you.

Useful when you are setting the bot up on a new machine: it checks the
cookie, the session, the inbox, the notes and the stories, and prints what
came back — including the failures, which are the interesting part.

    python tools/probe.py --root /path/to/insta-baileys
"""

from __future__ import annotations

import argparse
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from ibot.config import load  # noqa: E402
from ibot.session import LoginError, SessionManager  # noqa: E402


def timed(label: str, fn):
    start = time.time()
    try:
        value = fn()
    except Exception as exc:  # noqa: BLE001
        print(f"  {label:<22} ❌ {type(exc).__name__}: {exc}")
        return None
    ms = int((time.time() - start) * 1000)
    print(f"  {label:<22} ✅ {ms} ms")
    return value


def main() -> int:
    parser = argparse.ArgumentParser(description="probe the Instagram API")
    parser.add_argument("--root", default=os.environ.get("IGBOT_ROOT", "."))
    parser.add_argument("--sessionid", help="a sessionid cookie to try")
    args = parser.parse_args()

    cfg = load(args.root)
    if args.sessionid:
        os.environ["IGBOT_SESSIONID"] = args.sessionid

    print("== session ==")
    session = SessionManager(cfg)
    try:
        api = session.open()
    except LoginError as exc:
        print(f"  ❌ {exc}")
        return 1
    st = session.status()
    print(f"  method                {st.mode}")
    print(f"  account               @{st.username} ({st.user_id})")
    print(f"  device                {st.device_id or 'new'}")

    print("== what the account can see ==")
    me = timed("profile", lambda: api.whoami())
    if me:
        print(f"      {me.follower_count} followers · {me.following_count} following")

    threads = timed("inbox", lambda: api.threads(amount=10))
    if threads:
        print(f"      {len(threads)} conversations")
        for t in threads[:5]:
            last = t.messages[0].text if t.messages else ""
            print(f"      {t.peer}: {last[:60]}")

    pending = timed("request inbox", lambda: api.pending_threads(amount=10))
    if pending:
        print(f"      {len(pending)} waiting requests")

    notes = timed("notes", lambda: api.notes())
    if notes:
        with_text = [n for n in notes if n.text]
        print(f"      {len(notes)} notes ({len(with_text)} with text)")

    people = timed("following", lambda: api.following(st.username, amount=10))
    if people:
        names = [p.username for p in people]
        print(f"      {len(names)} accounts: {', '.join('@' + n for n in names[:5])}")

    if names:
        for name in names[:3]:
            stories = timed(f"stories @{name}", lambda n=name: api.stories(n))
            if stories:
                print(f"      {len(stories)} stories")

    print("\n== memory ==")
    try:
        import resource

        print(f"  RAM                   {resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024:.0f} MB")
    except Exception:  # noqa: BLE001
        pass

    api.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

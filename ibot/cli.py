"""The command line.

``igbot <command>``. Every command takes ``--root`` (the project folder) and
works the same whether it runs on your laptop or on a Render free instance.
"""

from __future__ import annotations

import argparse
import json
import os
import signal
import sys
import time
from pathlib import Path

from . import __version__
from . import log as log_module
from .ai import AIReplyEngine
from .client.ig import ApiClient, InstagramError
from .commands import CommandRouter
from .config import load as load_config
from .dm import DMBot
from .guardrails import Guardrails
from .keepalive import KeepAlive
from .media import MediaBot
from .realtime import RealtimeBridge
from .risk import RiskGuard
from .rules import Rulebook
from .session import LoginError, SessionManager
from .store import Store

log = log_module.get(__name__)


# --------------------------------------------------------------------- setup
def _prepare(args) -> tuple:
    """Build the objects every command needs."""
    root = Path(args.root or os.environ.get("IGBOT_ROOT", ".")).expanduser().resolve()
    cfg = load_config(root)
    if getattr(args, "baseline", False):
        cfg.set("behaviour.baseline_only", True)
    if getattr(args, "poll", None):
        cfg.set("behaviour.poll_seconds", int(args.poll))

    log_module.setup(
        level=str(cfg.get("logging.level", "INFO")),
        logfile=str(cfg.resolve("logging.file")) if cfg.get("logging.file") else None,
    )
    store = Store(cfg.resolve("state.database"))
    guardrails = Guardrails(store, cfg)
    rulebook = Rulebook.from_config(cfg)
    ai = AIReplyEngine(cfg) if bool(cfg.get("reply.ai.enabled", False)) else None
    return cfg, store, guardrails, rulebook, ai


def _client(cfg, risk=None) -> tuple:
    """Open a session and return ``(session, api_client)``."""
    session = SessionManager(cfg)
    api = session.open()
    if risk is not None:
        api.risk = risk
    return session, api


def _short(text: str, size: int = 70) -> str:
    text = " ".join((text or "").split())
    return text if len(text) <= size else text[: size - 1] + "…"


# ------------------------------------------------------------------ commands
def cmd_login(args) -> int:
    cfg, store, *_ = _prepare(args)
    if args.sessionid:
        os.environ["IGBOT_SESSIONID"] = args.sessionid
    if args.cookie_file:
        cfg.set("account.cookies_file", args.cookie_file)
    if args.password:
        os.environ["IGBOT_PASSWORD"] = args.password
    if args.username:
        cfg.set("account.username", args.username)

    session = SessionManager(cfg)
    try:
        api = session.open()
    except LoginError as exc:
        print(f"❌ {exc}")
        return 1
    me = api.whoami()
    st = session.status()
    print(f"✅ logged in as @{me.username} ({me.full_name})")
    print(f"   method: {st.mode}")
    print(f"   session saved to: {session.settings_path}")
    api.close()
    return 0


def cmd_doctor(args) -> int:
    cfg, store, guardrails, rulebook, ai = _prepare(args)
    ok = True

    session = SessionManager(cfg)
    print("== session ==")
    try:
        api = session.open()
    except LoginError as exc:
        print(f"❌ {exc}")
        return 1
    st = session.status()
    print(f"   method      : {st.mode}")
    print(f"   account     : @{st.username} (id {st.user_id})")
    print(f"   device id   : {st.device_id or 'new'}")

    print("== api ==")
    start = time.time()
    try:
        me = api.whoami()
        print(f"   ✅ profile in {int((time.time() - start) * 1000)} ms")
        print(f"   followers   : {me.follower_count}")
    except InstagramError as exc:
        ok = False
        print(f"   ❌ {exc}")

    print("== inbox ==")
    try:
        threads = api.threads(amount=5)
        print(f"   ✅ {len(threads)} conversations")
        for t in threads[:5]:
            last = t.messages[0].text if t.messages else ""
            print(f"      {t.peer}: {_short(last, 50)}")
    except InstagramError as exc:
        ok = False
        print(f"   ❌ {exc}")

    print("== notes ==")
    try:
        notes = api.notes()
        print(f"   ✅ {len(notes)} notes")
    except InstagramError as exc:
        print(f"   ⚠️  {exc}")

    print("== memory ==")
    try:
        import resource

        usage = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024
        print(f"   RAM in use  : {usage:.0f} MB")
    except Exception:  # noqa: BLE001
        pass

    api.close()
    print("\n" + ("✅ everything checks out" if ok else "⚠️  some checks failed"))
    return 0 if ok else 1


def cmd_inbox(args) -> int:
    cfg, store, *_ = _prepare(args)
    session, api = _client(cfg)
    threads = api.threads(amount=args.limit)
    if not threads:
        print("the inbox is empty")
        return 0
    for t in threads:
        last = t.messages[0] if t.messages else None
        who = t.peer
        flag = "📥" if t.unread else "  "
        print(f"{flag} {who}: {_short(last.text if last else '', 80)}")
    api.close()
    return 0


def cmd_baseline(args) -> int:
    cfg, store, guardrails, rulebook, ai = _prepare(args)
    cfg.set("behaviour.baseline_only", True)
    session, api = _client(cfg)
    bot = DMBot(api, cfg, store, guardrails, rulebook, ai)
    result = bot.poll_once()
    print(result.summary())
    api.close()
    return 0


def cmd_send(args) -> int:
    cfg, store, *_ = _prepare(args)
    session, api = _client(cfg)
    sent = api.send_to_user(args.username, " ".join(args.message))
    print("✅ sent" if sent.ok else f"❌ {sent.detail}")
    api.close()
    return 0 if sent.ok else 1


def cmd_stories(args) -> int:
    cfg, store, *_ = _prepare(args)
    session, api = _client(cfg)
    if args.username:
        names = [args.username.lstrip("@")]
    else:
        me = api.whoami()
        names = [p.username for p in api.following(me.username, amount=25)]
    for name in names:
        try:
            stories = api.stories(name)
        except InstagramError:
            continue
        if stories:
            print(f"@{name}: {len(stories)} story")
    api.close()
    return 0


def cmd_storyview(args) -> int:
    cfg, store, guardrails, *_ = _prepare(args)
    session, api = _client(cfg)
    name = args.username.lstrip("@") if args.username else api.whoami().username
    stories = api.stories(name)
    if not stories:
        print(f"@{name} has no stories")
        api.close()
        return 1
    ok = api.view_stories([s.pk for s in stories])
    print(f"✅ viewed {len(stories)} stories from @{name}" if ok else "❌ could not mark them seen")
    api.close()
    return 0 if ok else 1


def cmd_storylike(args) -> int:
    cfg, store, guardrails, *_ = _prepare(args)
    session, api = _client(cfg)
    name = args.username.lstrip("@") if args.username else api.whoami().username
    stories = api.stories(name)
    if not stories:
        print(f"@{name} has no stories")
        api.close()
        return 1
    ok = api.like_story(stories[0].pk)
    print(f"✅ liked @{name}'s story" if ok else "❌ the like was refused")
    api.close()
    return 0 if ok else 1


def cmd_notes(args) -> int:
    cfg, store, *_ = _prepare(args)
    session, api = _client(cfg)
    notes = api.notes()
    if not notes:
        print("no notes right now")
    for n in notes[: args.limit]:
        if n.text:
            print(f"@{n.username}: {_short(n.text, 100)}")
    api.close()
    return 0


def cmd_profile(args) -> int:
    cfg, store, *_ = _prepare(args)
    session, api = _client(cfg)
    p = api.profile(args.username.lstrip("@"))
    print(f"@{p.username} ({p.full_name})")
    print(f"{p.follower_count} followers · {p.following_count} following · {p.media_count} posts")
    if p.biography:
        print(p.biography)
    api.close()
    return 0


def cmd_follow(args) -> int:
    cfg, store, guardrails, *_ = _prepare(args)
    session, api = _client(cfg)
    name = args.username.lstrip("@")
    ok = api.follow(name)
    if ok:
        guardrails.record_action("follow", target=name)
    print(f"✅ following @{name}" if ok else f"❌ could not follow @{name}")
    api.close()
    return 0 if ok else 1


def cmd_unfollow(args) -> int:
    cfg, store, guardrails, *_ = _prepare(args)
    session, api = _client(cfg)
    name = args.username.lstrip("@")
    ok = api.unfollow(name)
    print(f"✅ unfollowed @{name}" if ok else f"❌ could not unfollow @{name}")
    api.close()
    return 0 if ok else 1


def cmd_like(args) -> int:
    cfg, store, guardrails, *_ = _prepare(args)
    session, api = _client(cfg)
    m = api.media(args.url)
    ok = api.like_media(m.pk)
    if ok:
        guardrails.record_action("like", target=m.username)
    print(f"✅ liked @{m.username}'s post" if ok else "❌ the like was refused")
    api.close()
    return 0 if ok else 1


def cmd_autopilot(args) -> int:
    cfg, store, guardrails, *_ = _prepare(args)
    session, api = _client(cfg)
    media = MediaBot(api, cfg, store, guardrails)
    summary = media.run_once()
    for key, value in summary.items():
        print(f"{key}: {value}")
    api.close()
    return 0


def cmd_watch(args) -> int:
    cfg, store, guardrails, rulebook, ai = _prepare(args)
    risk = RiskGuard(cfg, store)
    session, api = _client(cfg, risk=risk)

    media = MediaBot(api, cfg, store, guardrails, risk=risk)

    # The push channel. When Instagram delivers a message over the socket
    # the bot answers it straight away instead of waiting for the poll.
    # It is built *before* the router so the router can report on it, and it
    # is started *after* the bot exists so the callback always has a target.
    # (The router used to be given no bridge at all, so /realtime answered
    # "the push channel is not attached" on a perfectly healthy channel.)
    bridge = None
    if bool(cfg.get("realtime.enabled", True)):
        bridge = RealtimeBridge(api.cl, cfg, on_message=lambda push: bot.handle_realtime(push))

    router = CommandRouter(
        api,
        cfg,
        store=store,
        guardrails=guardrails,
        session=session,
        media=media,
        risk=risk,
        protocol=session.protocol,
        realtime=bridge,
    )
    bot = DMBot(api, cfg, store, guardrails, rulebook, ai, command_router=router, risk=risk)

    if args.prime:
        bot.prime()
        print("✅ the inbox was recorded as seen; nothing will be answered")

    if bridge is not None:
        bridge.start()
        print("📡 the push channel is connecting — answers will come in seconds")

    keepalive = None
    httpd = None

    # The control API runs in the same process as the bot, so a platform
    # health check has something to talk to and one command is enough to
    # run the whole thing.
    if bool(cfg.get("api.enabled", False)):
        from .api import APIContext, serve, token_from_env

        port = int(cfg.get("api.port", 8000) or 8000)
        ctx = APIContext(
            cfg=cfg,
            store=store,
            guardrails=guardrails,
            token=token_from_env(),
            started_at=time.time(),
            session_state=session.health,
            log_file=cfg.resolve("logging.file") if cfg.get("logging.file") else None,
        )
        httpd = serve(ctx, port=port)
        import threading

        threading.Thread(target=httpd.serve_forever, name="api", daemon=True).start()
        print(f"🌐 the control API is listening on port {port}")

    if bool(cfg.get("keepalive.enabled", False)):
        port = int(cfg.get("api.port", 8000) or 8000)
        host = cfg.get("keepalive.host", "127.0.0.1")
        url = str(cfg.get("keepalive.url", "") or "")
        if not url:
            # On Render the public URL is handed to us in the environment.
            external = os.environ.get("RENDER_EXTERNAL_URL", "").strip("/")
            url = f"https://{external}/api/health" if external else f"http://{host}:{port}/api/health"
        keepalive = KeepAlive(url, interval=int(cfg.get("keepalive.interval_seconds", 240) or 240))
        keepalive.start()

    stopping = {"flag": False}

    def _stop(signum, frame):  # noqa: ARG001
        stopping["flag"] = True

    signal.signal(signal.SIGINT, _stop)
    signal.signal(signal.SIGTERM, _stop)

    poll = int(cfg.get("behaviour.poll_seconds", 45) or 45)
    auto_every = int(cfg.get("behaviour.automations_every_seconds", 900) or 900)

    print(f"🤖 watching as @{session.status().username} (every {poll}s, Ctrl-C to stop)")
    last_auto = 0.0
    while not stopping["flag"]:
        try:
            result = bot.poll_once()
            if result.inbound or result.replied or result.commands:
                print(f"[{time.strftime('%H:%M:%S')}] {result.summary()}")
        except InstagramError as exc:
            log.error("poll failed: %s", exc)
            if getattr(exc, "needs_reauth", False):
                print("🔄 the session expired — logging in again")
                try:
                    session.rebuild()
                    api = session.client()
                    api.risk = risk
                    bot.client = api
                    router.client = api
                    media.client = api
                except LoginError as rexc:
                    log.error("could not log in again: %s", rexc)
                    break
        except Exception as exc:  # noqa: BLE001
            log.exception("poll failed: %s", exc)

        if time.time() - last_auto > auto_every:
            last_auto = time.time()
            try:
                media.run_once()
            except Exception as exc:  # noqa: BLE001
                log.warning("the automatic actions failed: %s", exc)

        if args.once:
            break

        # Wait for the next tick, but wake immediately if a message arrives
        # over the push channel.
        if bot.wake.wait(timeout=poll):
            bot.wake.clear()

    if keepalive:
        keepalive.stop()
    if bridge is not None:
        bridge.stop()
    if httpd is not None:
        try:
            httpd.shutdown()
        except Exception:  # noqa: BLE001
            pass
    print("👋 stopped")
    api.close()
    return 0


def cmd_serve(args) -> int:
    from .api import APIContext, serve, token_from_env

    cfg, store, guardrails, *_ = _prepare(args)
    session = SessionManager(cfg)
    port = int(args.port or cfg.get("api.port", 8000) or 8000)
    token = token_from_env()

    ctx = APIContext(
        cfg=cfg,
        store=store,
        guardrails=guardrails,
        token=token,
        started_at=time.time(),
        session_state=session.health,
        log_file=cfg.resolve("logging.file") if cfg.get("logging.file") else None,
    )
    server = serve(ctx, port=port)
    print(f"🌐 the control API is listening on port {port}")
    if not token:
        print("   ⚠️  set IGBOT_API_TOKEN — without it only /api/health is open")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.shutdown()
    return 0


def cmd_selftest(args) -> int:
    """Send a message to yourself so you can see the whole path work."""
    cfg, store, *_ = _prepare(args)
    session, api = _client(cfg)
    me = api.whoami()
    threads = api.threads(amount=30)
    target = None
    for t in threads:
        if t.is_self:
            target = t
            break
    if target is None:
        print("❌ no note-to-self conversation was found")
        api.close()
        return 1
    text = f"🤖 InstaBaileys self test — {time.strftime('%Y-%m-%d %H:%M:%S')}"
    sent = api.send_text(target.id, text)
    if sent.ok:
        print(f"✅ sent to your own conversation ({target.id})")
    else:
        print(f"❌ {sent.detail}")
    api.close()
    return 0 if sent.ok else 1


def cmd_stats(args) -> int:
    cfg, store, guardrails, *_ = _prepare(args)
    stats = guardrails.stats()
    for key, value in stats.items():
        print(f"{key}: {value}")
    print("\nrecent actions:")
    for row in store.recent_actions(limit=10):
        print(f"  {row['kind']} {row['target'] or ''} {row['detail'] or ''}".rstrip())
    return 0


def cmd_protocol(args) -> int:
    """Show or re-check the app identity the bot is wearing."""
    cfg, store, *_ = _prepare(args)
    session = SessionManager(cfg)
    api = session.open()
    protocol = session.protocol

    if args.probe:
        print("probing every known identity…")
        results = protocol.probe(api.cl)
        for r in results:
            mark = "✅" if r["ok"] else "❌"
            print(f"{mark} {r['version']} (app id {r['app_id']}) — {r['detail']} [{r['ms']} ms]")
    else:
        state = protocol.state
        print(f"app id      : {state.identity.app_id}")
        print(f"version     : {state.identity.version} ({state.identity.version_code})")
        print(f"device      : {state.identity.manufacturer} {state.identity.model}")
        print(f"working     : {state.working} — {state.detail}")
        print("\nknown identities:")
        for c in protocol.candidates():
            print(f"  {c['version']:<18} app id {c['app_id']}  {c['label']}")
    api.close()
    return 0


def cmd_realtime(args) -> int:
    """Test the push channel."""
    cfg, store, *_ = _prepare(args)
    session, api = _client(cfg)

    got = []
    bridge = RealtimeBridge(api.cl, cfg, on_message=lambda m: got.append(m))
    print("connecting to the push channel…")
    bridge.start()

    deadline = time.time() + (args.seconds or 60)
    while time.time() < deadline:
        if bridge.connected and not args.expect:
            break
        if got:
            break
        time.sleep(0.5)

    print(json.dumps(bridge.health(), indent=2, default=str))
    if got:
        print(f"\n✅ {len(got)} message(s) arrived over the push channel")
        for m in got[:5]:
            print(f"   thread {m.thread_id}: {m.text[:60]!r}")
    elif bridge.connected:
        print("\n✅ the push channel is connected and subscribed (no message arrived in the window)")
    else:
        print("\n⚠️  the push channel did not connect — the bot will fall back to polling")
    bridge.stop()
    api.close()
    return 0


def cmd_risk(args) -> int:
    """Show the risk dashboard."""
    cfg, store, *_ = _prepare(args)
    risk = RiskGuard(cfg, store)
    for key, value in risk.score().items():
        print(f"{key}: {value}")
    return 0


# -------------------------------------------------------------------- parser
def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="igbot",
        description="InstaBaileys — an Instagram bot that speaks the API, not the browser.",
    )
    p.add_argument("--root", default=os.environ.get("IGBOT_ROOT", "."), help="project folder")
    p.add_argument("--version", action="version", version=f"InstaBaileys {__version__}")
    sub = p.add_subparsers(dest="command", required=True)

    login = sub.add_parser("login", help="authenticate and save the session")
    login.add_argument("--sessionid", help="the sessionid cookie value")
    login.add_argument("--cookie-file", help="a cookie header / cookies.txt file")
    login.add_argument("--username", help="your Instagram username")
    login.add_argument("--password", help="your Instagram password")
    login.set_defaults(func=cmd_login)

    doctor = sub.add_parser("doctor", help="check the session, the API and the inbox")
    doctor.set_defaults(func=cmd_doctor)

    inbox = sub.add_parser("inbox", help="print the inbox")
    inbox.add_argument("--limit", type=int, default=25)
    inbox.set_defaults(func=cmd_inbox)

    baseline = sub.add_parser("baseline", help="read the inbox without answering")
    baseline.set_defaults(func=cmd_baseline)

    send = sub.add_parser("send", help="send someone a message")
    send.add_argument("username")
    send.add_argument("message", nargs="+")
    send.set_defaults(func=cmd_send)

    stories = sub.add_parser("stories", help="who has stories")
    stories.add_argument("username", nargs="?")
    stories.set_defaults(func=cmd_stories)

    storyview = sub.add_parser("storyview", help="view a story (marks it seen)")
    storyview.add_argument("username", nargs="?")
    storyview.set_defaults(func=cmd_storyview)

    storylike = sub.add_parser("storylike", help="like a story")
    storylike.add_argument("username", nargs="?")
    storylike.set_defaults(func=cmd_storylike)

    notes = sub.add_parser("notes", help="the notes in your inbox")
    notes.add_argument("--limit", type=int, default=20)
    notes.set_defaults(func=cmd_notes)

    profile = sub.add_parser("profile", help="read a profile")
    profile.add_argument("username")
    profile.set_defaults(func=cmd_profile)

    follow = sub.add_parser("follow", help="follow an account")
    follow.add_argument("username")
    follow.set_defaults(func=cmd_follow)

    unfollow = sub.add_parser("unfollow", help="unfollow an account")
    unfollow.add_argument("username")
    unfollow.set_defaults(func=cmd_unfollow)

    like = sub.add_parser("like", help="like a post")
    like.add_argument("url")
    like.set_defaults(func=cmd_like)

    autopilot = sub.add_parser("autopilot", help="run the automatic actions once")
    autopilot.set_defaults(func=cmd_autopilot)

    watch = sub.add_parser("watch", help="the 24/7 loop")
    watch.add_argument("--once", action="store_true", help="one pass and exit")
    watch.add_argument("--prime", action="store_true", help="record the inbox as seen and exit")
    watch.add_argument("--poll", type=int, help="seconds between polls")
    watch.add_argument("--baseline", action="store_true", help="read but never answer")
    watch.set_defaults(func=cmd_watch)

    serve = sub.add_parser("serve", help="run the control API")
    serve.add_argument("--port", type=int)
    serve.set_defaults(func=cmd_serve)

    selftest = sub.add_parser("selftest", help="send a test message to yourself")
    selftest.set_defaults(func=cmd_selftest)

    stats = sub.add_parser("stats", help="show the counters")
    stats.set_defaults(func=cmd_stats)

    protocol = sub.add_parser("protocol", help="the app identity the bot is wearing")
    protocol.add_argument("--probe", action="store_true", help="test every known identity")
    protocol.set_defaults(func=cmd_protocol)

    realtime = sub.add_parser("realtime", help="test the push channel")
    realtime.add_argument("--seconds", type=int, help="how long to listen")
    realtime.add_argument("--expect", action="store_true", help="wait for a real message")
    realtime.set_defaults(func=cmd_realtime)

    risk = sub.add_parser("risk", help="the risk dashboard")
    risk.set_defaults(func=cmd_risk)

    return p


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return int(args.func(args) or 0)
    except KeyboardInterrupt:
        print("\ninterrupted")
        return 130
    except LoginError as exc:
        print(f"❌ {exc}")
        return 1
    except InstagramError as exc:
        print(f"❌ {exc}")
        return 1


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())

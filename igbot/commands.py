"""The commands added on top of the original DM bot.

Kept in their own module so ``cli.py`` stays readable. Every command follows
the same shape: load the config, open the session, build the helpers the
action needs, run it, print the outcome.

The helpers this module needs from ``cli`` are imported lazily *inside* the
functions, because ``cli`` imports this module to register the commands.
"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path

from . import config as config_mod
from . import log as log_mod


def _common(cfg):
    """The pieces nearly every command needs."""
    from .cli import _load_selectors, _settle
    from .guardrails import Guardrails
    from .store import Store

    selectors = _load_selectors(cfg.root)
    store = Store(cfg.path("state", "igbot.db"))
    guard = Guardrails(store, cfg)
    return selectors, store, guard, _settle(cfg)


def _session(cfg, args):
    from .cli import _open_session, _resolve_headless

    return _open_session(cfg, headless=_resolve_headless(args))


def _build_tools(page, cfg, selectors, store, guard):
    """Construct the action helpers that share one browser page."""
    from .automation import AutomationEngine
    from .dom import DOM
    from .likes import LikeBot
    from .notifications import NotificationBot
    from .profile import ProfileBot
    from .stories import StoryBot

    dom = DOM(page, selectors, str(cfg.get("urls.base", "https://www.instagram.com")))
    profile_bot = ProfileBot(page, cfg, selectors, guard)
    like_bot = LikeBot(page, cfg, selectors, guard)
    story_bot = StoryBot(page, cfg, selectors, guard, dom)
    notifications = NotificationBot(page, cfg, selectors, store)
    return dom, profile_bot, like_bot, story_bot, notifications


# --------------------------------------------------------------------- follow


def cmd_follow(args) -> int:
    cfg = config_mod.load(args.root)
    log_mod.setup(cfg.get("logging.level", "INFO"), str(cfg.resolve("logging.file")))
    selectors, store, guard, _ = _common(cfg)

    browser, page = _session(cfg, args)
    try:
        from .profile import ProfileBot

        bot = ProfileBot(page, cfg, selectors, guard)
        result = bot.follow(args.username, force=args.force)
        print(f"{result.action} @{result.target}: {result.message}")
        if not result.ok:
            return 1
        return 0
    finally:
        browser.close()


def cmd_unfollow(args) -> int:
    cfg = config_mod.load(args.root)
    log_mod.setup(cfg.get("logging.level", "INFO"), str(cfg.resolve("logging.file")))
    selectors, store, guard, _ = _common(cfg)

    browser, page = _session(cfg, args)
    try:
        from .profile import ProfileBot

        bot = ProfileBot(page, cfg, selectors, guard)
        result = bot.unfollow(args.username, force=args.force)
        print(f"{result.action} @{result.target}: {result.message}")
        return 0 if result.ok else 1
    finally:
        browser.close()


def cmd_profile(args) -> int:
    cfg = config_mod.load(args.root)
    log_mod.setup(cfg.get("logging.level", "INFO"), str(cfg.resolve("logging.file")))
    selectors, store, guard, _ = _common(cfg)

    browser, page = _session(cfg, args)
    try:
        from .profile import ProfileBot

        bot = ProfileBot(page, cfg, selectors, guard)
        username = args.username
        if not username:
            profile = bot.account_info()
            if not profile.username:
                print("Set account.username in config.yaml, or pass a username.", file=sys.stderr)
                return 2
        else:
            if not bot.open(username):
                print(f"Could not open @{username}.", file=sys.stderr)
                return 1
            profile = bot.read(username)

        print(profile.summary())
        if profile.bio:
            print(f"  bio      : {profile.bio[:160]}")
        if profile.website:
            print(f"  website  : {profile.website}")
        if profile.is_private:
            print("  (this account is private)")
        if profile.is_verified:
            print("  (verified)")
        return 0
    finally:
        browser.close()


def cmd_people(args) -> int:
    """List followers or following."""
    cfg = config_mod.load(args.root)
    log_mod.setup(cfg.get("logging.level", "INFO"), str(cfg.resolve("logging.file")))
    selectors, store, guard, _ = _common(cfg)

    browser, page = _session(cfg, args)
    try:
        from .profile import ProfileBot

        bot = ProfileBot(page, cfg, selectors, guard)
        if args.what == "followers":
            names = bot.list_followers(args.username)
        else:
            names = bot.list_following(args.username)
        print(f"{len(names)} {args.what} of @{args.username}:")
        for name in names[: args.limit]:
            print(f"  @{name}")
        return 0
    finally:
        browser.close()


# ----------------------------------------------------------------------- like


def cmd_like(args) -> int:
    cfg = config_mod.load(args.root)
    log_mod.setup(cfg.get("logging.level", "INFO"), str(cfg.resolve("logging.file")))
    selectors, store, guard, _ = _common(cfg)

    browser, page = _session(cfg, args)
    try:
        from .likes import LikeBot

        bot = LikeBot(page, cfg, selectors, guard)
        if args.unlike:
            result = bot.unlike_post(args.url, force=args.force)
        elif args.comment:
            result = bot.like_comment(args.url, args.comment_text or "", force=args.force)
        else:
            result = bot.like_post(args.url, force=args.force)
        print(f"{result.action}: {result.message}")
        return 0 if result.ok else 1
    finally:
        browser.close()


# -------------------------------------------------------------------- stories


def cmd_stories(args) -> int:
    cfg = config_mod.load(args.root)
    log_mod.setup(cfg.get("logging.level", "INFO"), str(cfg.resolve("logging.file")))
    selectors, store, guard, _ = _common(cfg)

    browser, page = _session(cfg, args)
    try:
        from .dom import DOM
        from .stories import StoryBot

        dom = DOM(page, selectors, str(cfg.get("urls.base", "https://www.instagram.com")))
        bot = StoryBot(page, cfg, selectors, guard, dom)

        if args.story_action == "list":
            stories = bot.list_stories()
            print(f"{len(stories)} account(s) with a story:")
            for story in stories:
                print(f"  @{story.username:<24} {story.count} story(ies)")
            return 0

        if args.story_action == "view":
            result = bot.view_story(args.username, force=args.force)
            print(f"view @{result.target}: {result.message}")
            return 0 if result.ok else 1

        if args.story_action == "reply":
            text = args.text
            if not text and args.text_file:
                text = Path(args.text_file).read_text(encoding="utf-8").strip()
            result = bot.reply_to_story(args.username, text, force=args.force)
            print(f"reply to @{result.target}: {result.message}")
            return 0 if result.ok else 1

        if args.story_action == "post":
            media = args.media or str(cfg.resolve("publishing.media_file"))
            result = bot.post_story(media, args.caption or "", force=args.force)
            print(f"story: {result.message}")
            return 0 if result.ok else 1

        print("Unknown story action.", file=sys.stderr)
        return 2
    finally:
        browser.close()


# ------------------------------------------------------------------- activity


def cmd_activity(args) -> int:
    cfg = config_mod.load(args.root)
    log_mod.setup(cfg.get("logging.level", "INFO"), str(cfg.resolve("logging.file")))
    selectors, store, guard, _ = _common(cfg)

    if args.stored:
        from .notifications import NotificationBot

        bot = NotificationBot(None, cfg, selectors, store)
        items = bot.recent(args.limit)
        print(f"{len(items)} stored activity item(s):")
        for item in items:
            print(f"  {item.describe()}")
        return 0

    browser, page = _session(cfg, args)
    try:
        from .notifications import NotificationBot

        bot = NotificationBot(page, cfg, selectors, store)
        items = bot.read()
        print(f"{len(items)} activity item(s):")
        for item in items:
            print(f"  {item.describe()}")
        print()
        counts = bot.counts()
        print("totals: " + ", ".join(f"{k}={v}" for k, v in counts.items() if v))
        return 0
    finally:
        browser.close()


# ---------------------------------------------------------------------- tasks


def cmd_tasks(args) -> int:
    cfg = config_mod.load(args.root)
    log_mod.setup(cfg.get("logging.level", "INFO"), str(cfg.resolve("logging.file")))
    from .scheduler import Scheduler
    from .store import Store

    store = Store(cfg.path("state", "igbot.db"))
    scheduler = Scheduler(store, cfg)

    if args.task_action == "list":
        tasks = scheduler.list_tasks(args.limit)
        print(f"{len(tasks)} task(s), {scheduler.pending()} pending:")
        for task in tasks:
            when = time.strftime("%Y-%m-%d %H:%M", time.localtime(task.run_at))
            print(f"  [{task.id:>4}] {task.kind:<22} {task.status:<9} due {when}")
            if task.payload:
                print(f"         {json.dumps(task.payload, default=str)[:140]}")
        return 0

    if args.task_action == "add":
        payload = {}
        if args.payload:
            try:
                payload = json.loads(args.payload)
            except ValueError as exc:
                print(f"--payload is not valid JSON: {exc}", file=sys.stderr)
                return 2
        task_id = scheduler.enqueue(
            args.kind, payload=payload, delay_seconds=args.delay_seconds,
            fingerprint=args.fingerprint,
        )
        if task_id is None:
            print("An identical task is already queued.")
            return 1
        print(f"Queued task {task_id} ({args.kind}).")
        return 0

    if args.task_action == "cancel":
        scheduler.cancel(args.task_id)
        print(f"Cancelled task {args.task_id}.")
        return 0

    if args.task_action == "run":
        print("Tasks run inside the watch loop; start 'igbot watch' for them to fire.")
        return 0

    print("Unknown task action.", file=sys.stderr)
    return 2


# ---------------------------------------------------------------- automations


def cmd_automations(args) -> int:
    cfg = config_mod.load(args.root)
    log_mod.setup(cfg.get("logging.level", "INFO"), str(cfg.resolve("logging.file")))
    from .automation import AutomationEngine
    from .scheduler import Scheduler
    from .store import Store

    store = Store(cfg.path("state", "igbot.db"))
    scheduler = Scheduler(store, cfg)
    engine = AutomationEngine(cfg, scheduler)

    print("igbot automations")
    print("=" * 40)
    print(f"enabled : {engine.enabled}")
    print(f"rules   : {len(engine.rules())}")
    for rule in engine.rules():
        state = "on " if rule.enabled else "off"
        print(f"  [{state}] {rule.name:<22} {rule.trigger:<12} -> {rule.action}")
        if rule.params:
            print(f"         {json.dumps(rule.params, default=str)[:120]}")

    print("\nschedules:")
    schedules = scheduler.schedules_from_config()
    if not schedules:
        print("  (none configured)")
    for schedule in schedules:
        next_at = schedule.next_run()
        when = time.strftime("%Y-%m-%d %H:%M", time.localtime(next_at)) if next_at else "?"
        print(f"  {schedule.name:<22} {schedule.kind:<22} {schedule.spec:<12} next: {when}")
    return 0


# ------------------------------------------------------------------------ api


def cmd_serve(args) -> int:
    """Run the HTTP control API on its own (no browser needed)."""
    cfg = config_mod.load(args.root)
    log_mod.setup(cfg.get("logging.level", "INFO"), str(cfg.resolve("logging.file")))
    from .api import APIContext, serve, token_from_env
    from .guardrails import Guardrails
    from .scheduler import Scheduler
    from .store import Store

    store = Store(cfg.path("state", "igbot.db"))
    guard = Guardrails(store, cfg)
    scheduler = Scheduler(store, cfg)

    token = args.token or token_from_env()
    if not token:
        print(
            "No API token configured. Set IGBOT_API_TOKEN in the environment "
            "or pass --token. The API stays closed without one.",
            file=sys.stderr,
        )
        return 2

    ctx = APIContext(
        cfg=cfg,
        store=store,
        scheduler=scheduler,
        guardrails=guard,
        token=token,
        started_at=time.time(),
        log_file=cfg.resolve("logging.file"),
    )
    from .cli import _api_port

    port = args.port or _api_port(cfg)
    server = serve(ctx, port=port)
    print(f"control API on http://0.0.0.0:{server.server_address[1]} (token required)")
    print("  GET /api/health   (no token needed)")
    print("  GET /api/stats    GET /api/tasks   GET /api/activity   GET /api/actions")
    print("  POST /api/follow  POST /api/unfollow  POST /api/like  POST /api/tasks")
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        print("\nstopped.", file=sys.stderr)
    finally:
        server.shutdown()
    return 0

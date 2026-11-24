# InstaBaileys

**The Baileys for Instagram.** Baileys made WhatsApp programmable without a
browser. InstaBaileys does the same for Instagram: it speaks Instagram's own
API over plain HTTP, so the whole bot runs in about **70 MB of RAM** instead
of half a gigabyte, answers in seconds instead of minutes, and stays logged
in.

It is a working bot, not a framework: it answers DMs, reads notes, views and
likes stories, follows people back, and takes **48 commands** by DM to
yourself.

```
   customer DM ──▶ Instagram API ──▶ InstaBaileys ──▶ rulebook / AI ──▶ reply
                                       │
                                       └──▶ guards (caps, cooldowns, pacing)
```

---

## Read this first: the honest risk

This talks to Instagram's **private mobile API** — the same protocol the
phone app uses, but without the app. It is unofficial, and unofficial
automaton can break when Instagram changes something.

What this project does about it:

* **It never mass-acts.** Every visible action has an hourly and a daily cap
  by default. An account that likes 400 posts an hour gets restricted; an
  account that likes twenty and answers its DMs does not.
* **It looks like one device.** The device fingerprint and session are saved
  and restored, so the account does not appear as a new phone on every
  restart. This is the single biggest reason accounts get logged out.
* **It fails quiet.** When Instagram changes an endpoint, the bot reports
  the failure and keeps the state. It never spams retries at an API that is
  refusing it.
* **Your password is optional.** A session cookie is enough. The bot never
  needs to see your password at all.

If your account is your business, start with `baseline_only: true` for a few
days and watch what the bot *would* do before letting it act.

---

## Why not a browser?

The previous generation of this bot drove a real Chromium against
instagram.com. It worked, and it was also the reason the bot kept dying on
Render's free tier:

| | Browser build | InstaBaileys |
|---|---|---|
| RAM | 400–500 MB (Chromium alone) | ~70 MB |
| Render free tier (512 MB) | Out of memory, service killed | Fits with room to spare |
| Time to answer | 20–60 s per page load | ~1 s per call |
| Login | Fresh device fingerprint each start | One saved identity, reused |
| Detection | Headless browser fingerprints | An ordinary app client |
| Dependencies | Chromium + 200 MB of drivers | 2 Python packages |

That is the whole reason this rewrite exists. The guardrails, the rulebook,
the scheduler, the audit trail and the command surface came across
unchanged — only the transport was replaced.

---

## Quick start

### 1. Get the cookie

Open Instagram in Chrome, log in, press `F12` → **Application** → Cookies →
`https://www.instagram.com`, and copy the value of `sessionid`.

Or paste this in the developer console and copy the result:

```js
copy(document.cookie)
```

Save it into `secrets/cookies.txt` (one line), or set it as the environment
variable `IGBOT_SESSIONID` — which is what you do on Render.

### 2. Install

```bash
cd insta-baileys
./install.sh          # creates .venv and installs instagrapi + PyYAML
```

Python 3.10 or newer. No browser is downloaded. The install takes about
20 seconds.

After the install you have an `igbot` command in `.venv/bin`. If you would
rather not install the package, `python -m ibot <command>` does the same
thing.

### 3. Check that it works

```bash
.venv/bin/igbot login
.venv/bin/igbot doctor
```

`doctor` prints the account, the latency of the API, the number of
conversations, the number of notes and — importantly — **how much RAM the
process is using**.

### 4. Watch first, answer later

```bash
.venv/bin/igbot watch --prime   # record the inbox, answer nothing
```

### 5. Let it answer

```yaml
# config.yaml
behaviour:
  baseline_only: false
```

```bash
.venv/bin/igbot watch
```

---

## The commands

Send them to **yourself** in a DM. That is the one channel that exists on
every account and needs no extra app.

```
/help          show the list          /inbox         the conversations
/whoami        the account            /threads       the inbox with ids
/status        counters and limits    /unread        unread conversations
/health        the API round trip     /last          the last messages
/relogin       rebuild the session    /requests      message requests
/ping          API latency            /accept        accept a request
/uptime        how long it runs       /reply         answer in this chat
/about         what this is           /send          DM someone
/version        the version            /react         react to a message
/clear         forget what was said

/stories       who has stories        /storyview     view (marks seen)
/storylike     like a story           /storyreply    reply to a story

/post          read a post            /like          like a post
/unlike        unlike a post          /comments      read the comments
/comment       comment on a post      /feed          recent posts

/profile       read a profile         /follow        follow
/followers     who follows you        /unfollow      unfollow
/following     who you follow         /followback    follow new followers

/notes         the notes in your inbox /note         one person's note

/auto          the automatic actions  /autolike      like posts
/autoview      view stories           /autostory     like stories
/autofollow    follow back            /autocomment   answer comments
/autonote      read the notes

/stats         the counters           /caps          today's budget
/tasks         the queue              /cancel        cancel a task
```

Examples:

```
/like https://www.instagram.com/p/Cxyz/
/followback
/storylike someaccount
/autoview on
```

The toggles are remembered in the database, so `/autoview on` survives a
restart.

---

## What it does automatically

| Toggle | What it does |
|---|---|
| `/autoview` | Views the stories of the accounts you follow (Instagram marks them seen). |
| `/autostory` | Likes the newest story of the accounts you follow. |
| `/autolike` | Likes the newest post of the accounts you follow. |
| `/autofollow` | Follows the accounts that followed you and that you do not follow back. |
| `/autonote` | Reads the notes that arrive and records them. |
| `/autocomment` | Answers the comments on your posts. |

Every one of them is off by default, goes through the daily caps, and never
touches the same account twice in a day.

### What Instagram does not allow

**Notes cannot be liked.** There is no like control on a note — not on the
web, not in the API. Instagram never built one. The bot reads notes and can
answer them with a DM, but there is nothing to like, and pretending
otherwise would be a lie in the code.

---

## Configuration

Everything lives in `config.yaml`, with the comments inline. The parts you
will actually change:

### The reply book

```yaml
reply:
  default_reply: "Thanks for reaching out! We'll get back to you shortly."
  marker: ""          # put "🤖" in front of every answer if you like
  rules:
    - name: pricing
      match: ["price", "pricing", "how much"]
      reply: "Happy to help! What are you looking to order?"
```

Matching is on **word boundaries**, so `hi` never fires on *"shipping"* or
*"which"*. The most specific rule wins.

### AI answers (optional)

```yaml
reply:
  ai:
    enabled: true
    endpoint: "https://api.openai.com/v1/chat/completions"
    model: "gpt-4o-mini"
```

Any OpenAI-compatible endpoint works. If the AI is slow or down, the rulebook
answers instead — your customer is never left without a reply. Set
`OPENAI_API_KEY` in the environment.

### Speed and limits

```yaml
behaviour:
  poll_seconds: 45          # how often the inbox is read
  reply_delay_min: 12       # people do not reply instantly
  reply_delay_max: 55
  cooldown_seconds: 900     # minimum gap between two answers in one chat
  hourly_cap: 8             # per conversation
  daily_cap: 25             # per conversation
  global_daily_cap: 120     # across all conversations
```

The caps are the reason the bot does not get restricted. Lower them if the
account is new.

### The visible actions

```yaml
behaviour:
  action_caps:
    like:       { hourly: 20, daily: 100 }
    follow:     { hourly: 5,  daily: 20 }
    story_view: { hourly: 20, daily: 80 }
    story_like: { hourly: 20, daily: 80 }
```

Tighter than the DM caps on purpose.

---

## Deploying to Render (free tier)

The free plan gives **512 MB of RAM**. This bot uses about 70 MB, so it
fits with room to spare.

**Option A — the blueprint.** Push this folder to GitHub, then in Render:
*New → Blueprint*, pick the repo, and set the two secrets:

| Variable | Value |
|---|---|
| `IGBOT_SESSIONID` | your `sessionid` cookie |
| `IGBOT_API_TOKEN` | any long random string |

The `render.yaml` in this folder does the rest.

**Option B — a Docker service.** *New → Web Service → Connect repo*, pick
this repo, and set the same two environment variables. The `Dockerfile` is
already here.

### Keeping it awake

A free Render service sleeps after 15 minutes without traffic, and waking up
takes ~30 seconds — which is exactly when a customer writes. Two things
prevent that:

1. `.github/workflows/keepalive.yml` pings `/api/health` every 14 minutes.
   Add a repository secret `BOT_URL` = `https://<your-service>.onrender.com`
   and turn the workflow on. GitHub Actions does this for free.
2. The bot's own keepalive (on by default) checks the endpoint from inside
   the service, which catches a wedged process.

### Persistent state

The session, the device identity and the database live in `state/`. On a
free service the disk is wiped on every deploy, so:

* Put the **cookie** in `IGBOT_SESSIONID` — that is your real login, and it
  survives anything.
* The saved session is a convenience: it keeps the device fingerprint
  stable. When it is gone, the bot logs in again with the cookie and writes
  a new one. It costs you one "new device" event, nothing more.

If you want the state to survive deploys, mount a disk at `/app/state`.

### One command runs everything

```bash
igbot watch
```

`watch` runs the inbox loop, the automatic actions and (when
`api.enabled: true`) the control API in the same process, so a platform
health check has something to talk to.

---

## The HTTP control API

Set `IGBOT_API_TOKEN` and the API opens:

| Route | What it does |
|---|---|
| `GET /api/health` | Liveness. Open without a token. |
| `GET /api/stats` | The counters. |
| `GET /api/tasks` | The queue. |
| `GET /api/activity` | What the bot has done. |
| `GET /api/actions` | The visible actions, with targets. |
| `GET /api/profile/<user>` | A profile. |
| `POST /api/follow` | Follow someone. |
| `POST /api/unfollow` | Unfollow someone. |
| `POST /api/like` | Like a post. |
| `POST /api/tasks` | Queue a task. |

Every route except `/api/health` needs `Authorization: Bearer <token>`.

---

## How it stays logged in

This is the part every bot gets wrong, so it is worth being explicit.

1. **The device identity is saved.** `state/session.json` holds the device
   settings, the `uuids` and the user agent. On every start they are
   restored, so Instagram sees the same phone. A bot that generates a new
   device on every restart looks exactly like someone stealing the account.
2. **The cookie wins.** Logging in uses the session cookie, not the
   password. A cookie login does not trip the login-rate limiter and cannot
   be challenged.
3. **The bot never logs out.** Calling Instagram's logout revokes the
   session server-side. The bot closes its HTTP pool and leaves the session
   alone.
4. **A dead session is detected.** Any call that comes back "login required"
   rebuilds the session once and retries, instead of pretending to work.
   The old build's check was fooled by a link that also exists on the
   logged-out page.
5. **The request rate is low.** One inbox read every 45 seconds, a random
   pause between calls, and hard caps on every visible action.

---

## Architecture

```
ibot/
├── client/
│   ├── base.py     the contract: threads, messages, stories, notes, media
│   ├── ig.py       the live client — Instagram's API over HTTP
│   └── fake.py     an in-memory fake, so the tests need no account
├── session.py      login, the saved device identity, re-auth
├── dm.py           the inbox loop: dedupe, guardrails, reply
├── commands.py     the 48 commands
├── media.py        the automatic visible actions
├── guardrails.py   cooldowns, caps, the audit trail
├── rules.py        the rulebook
├── store.py        SQLite state
├── scheduler.py    the task queue and the schedules
├── api.py          the HTTP control API
├── keepalive.py    the Render keep-warm
└── cli.py          the command line
```

Everything the bot does goes through `client/base.py`. That is why the whole
suite runs in six seconds without a network, and why the transport can be
swapped without touching the business logic.

---

## Tests

```bash
.venv/bin/python -m pytest -q
```

The suite runs against the fake client: no account, no network, no browser.
It covers the reply path, the dedupe, the caps, the rulebook, the command
router, the automatic actions, the store and the HTTP API.

---

## Troubleshooting

**"no way to authenticate"** — set `IGBOT_SESSIONID` or put the cookie in
`secrets/cookies.txt`. The cookie file is looked up relative to the project
folder, so the bot finds it wherever it is started from.

**The session keeps dying** — check `igbot doctor`. If `method` says
`password`, no cookie was found. If it says `sessionid` and things still
fail, the cookie has been revoked: copy a fresh one.

**Instagram asks for a verification code** — that only happens on a password
login from a new device. Use the cookie instead; it never triggers a
challenge.

**No answers** — `baseline_only` is probably still `true`. Check with
`/status` in your own DM.

**A reply was not sent** — the log says exactly why: the cooldown, the
hourly cap, the daily cap, or "baseline mode is on". The cap that stopped
it is named.

**The API is slow** — Instagram's own inbox endpoint takes about ten
seconds; that is on their side. The bot reads every 45 seconds, so an
answer lands 10–60 seconds after the message. Lower `poll_seconds` if you
want it faster, at the cost of more requests. (Instagram's realtime
MQTT channel — the push connection the phone app uses — was tested against
this account and delivered no events at all with a web session cookie, so
the bot polls instead. If you ever find a way to make that channel work
with a web cookie, it would remove the ten-second read entirely.)

---

## The name

Baileys is the library that made WhatsApp programmable without WhatsApp
Web. This is the same idea for Instagram, so the name follows: the
library, the protocol, the bot.

It is free to use, fork, and put your name on. It is a good foundation for
an Instagram bot that behaves like a person and stays online.

---

## Licence

MIT. Use it, sell it, put it in production, make it better.

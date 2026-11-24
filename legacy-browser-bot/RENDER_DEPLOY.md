# Deploying igbot to Render

Two ways to run it, with different trade-offs. Pick based on whether you need
the bot to be **always on** or you want to spend nothing.

| | Free web service | Background worker (paid) |
|---|---|---|
| Cost | $0 | from $7/month |
| Always running | **No** — sleeps after 15 min idle | Yes |
| Persistent disk (SQLite, browser profile) | **No** | Yes |
| HTTP control API | Yes | No (workers get no inbound traffic) |
| Instagram risk | Higher — a cold start looks like a new device | Lower — one long-lived device |

Render's own words: *"Other service types don't support Free instances"*
([Render docs](https://render.com/docs/free)). Background Workers start at
Starter, which is $7/month. Free web services are $0 with 512 MB RAM and 0.1
CPU, and the workspace gets 750 free instance hours per month.

For a customer-facing bot, **the worker is the right choice**: a bot that
wakes up every 15 minutes and logs in from scratch looks exactly like
automated behaviour to Instagram, and it cannot keep a dedupe database. Use
the free web service to *try* it, not to run it.

---

## Deploy (free web service)

1. Push this folder to a **private** GitHub repository.
2. In Render, choose **New > Blueprint** and connect that repository.
3. Render reads `render.yaml` and creates the `igbot` web service.
4. When prompted, enter:
   - `IGBOT_COOKIE` — the complete Cookie header from your authenticated
     browser session. This is your account; treat it like a password.
   - `IGBOT_API_TOKEN` — any long random string. It protects the control
     API. If you leave it empty, every route except `/api/health` stays
     closed.
5. Deploy, then open the service's **Logs** tab.

You should see a successful session check, `baseline_only is ON`, and
`watching every 90s`.

### Keeping it awake

A free web service spins down after 15 minutes without inbound traffic, and
takes about a minute to come back. Point a free cron service at the health
endpoint every 10 minutes:

- **cron-job.org**, **UptimeRobot**, or **cron-job.dev** — all free.
- URL: `https://<your-service>.onrender.com/api/health`
- Schedule: every 10 minutes.

This works, but it is a workaround. Each spin-down loses the local
filesystem, so the bot starts with an empty SQLite database — which is exactly
why the bot **primes** itself on a cold start: it records everything already
in the inbox as seen and answers none of it. Without that pass it would
answer the whole inbox again after every sleep.

### The browser profile

`IGBOT_PROFILE_DIR` holds the Chromium profile. On a free service it is
wiped on every cold start, so Instagram sees a brand-new device each time.
On a paid service with a persistent disk it survives, which is much better.

---

## Deploy (background worker — recommended)

Use the **same image**, but as a worker with a disk. Replace the service in
`render.yaml` with:

```yaml
services:
  - type: worker
    name: igbot
    runtime: docker
    plan: starter
    dockerfilePath: ./Dockerfile
    dockerContext: .
    autoDeploy: false
    disk:
      name: igbot-state
      mountPath: /app/state
      sizeGB: 1
    envVars:
      - key: IGBOT_COOKIE
        sync: false
      - key: IGBOT_BASELINE_ONLY
        value: "true"
      - key: IGBOT_POLL_SECONDS
        value: "60"
      - key: IGBOT_LOG_LEVEL
        value: INFO
      - key: IGBOT_PROFILE_DIR
        value: /app/state/profile
      - key: IGBOT_LOG_FILE
        value: /app/state/igbot.log
```

The worker runs continuously, keeps the SQLite database and the browser
profile on the disk, and is the configuration that behaves like a real
person's device.

To keep the HTTP control API as well, run the worker **and** a web service
from the same repository, or put the API behind a separate service that
talks to the same disk. A worker cannot receive inbound HTTP traffic, so
`/api/health` is only reachable from a web service.

---

## Turning the replies on

1. Run in `baseline_only` for a few days. It reads the inbox and logs what it
   sees; it answers nothing.
2. Watch the log. When you trust it, set `IGBOT_BASELINE_ONLY` to `false`
   and redeploy.
3. The reaction rules (`automations.rules`) are off by default too. Turn
   each one on individually.

## Before deploying

- Use a private repository.
- Never commit `secrets/cookies.txt`, browser profiles, SQLite databases or
  logs. `.gitignore` already covers them — check it before the first push.
- Never paste a session cookie into source code, Git, a README, an issue or
  a chat. A cookie that has been shared is a cookie to burn: log out of
  Instagram everywhere and get a new one.
- The cookie in the ZIP you were given has already been exposed. Revoke it.

## Stopping the burn

Render bills compute by the second while a service is running. Suspend the
service when you are not testing it — a suspended service costs nothing.

## Troubleshooting

| Symptom | Fix |
|---|---|
| `Instagram did not accept that cookie` | The session was revoked or expired. Instagram rotates the session cookie when you log in somewhere else. Copy a fresh one. |
| `no Primary tab found` | Instagram changed the layout. Run `tools/probe_live.py`, compare against `igbot/selectors.yaml`, fix the selectors. |
| The bot never replies | `IGBOT_BASELINE_ONLY` is still `true`. |
| The service sleeps constantly | Your keep-alive ping is not reaching `/api/health`, or it is more than 15 minutes apart. |
| The bot answers the same message twice after a restart | The state was lost (free tier). The priming pass should prevent this — check the log for `primed the inbox`. |
| Exit code 137 / Chromium killed | The instance ran out of RAM. Free instances have 512 MB; Chromium plus Playwright is close to the limit. |
| `Browser executable missing` | The Docker build did not finish `playwright install --with-deps chromium`. |

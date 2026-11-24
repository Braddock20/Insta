# The previous build (browser-based) — superseded

This is the generation of the bot that drove a real Chromium against
instagram.com. It is kept here for reference, and because a lot of the logic
in the current build came from it: the guardrails, the rulebook, the task
queue, the audit trail and the HTTP API are all descendants of this code.

**Do not run this on Render.** Chromium needs 400-500 MB on its own, which is
the entire memory budget of a free instance, so the service was killed the
moment it tried to do two things at once. It also re-logged in with a fresh
device fingerprint on every start, which is the classic reason an account
gets logged out.

The current build in the parent folder replaces the browser with Instagram's
own API over plain HTTP:

| | this folder | the parent folder |
|---|---|---|
| RAM | 400-500 MB | ~70 MB |
| Install | downloads Chromium (~180 MB) | 2 Python packages |
| Answers | 20-60 s per page load | ~1 s per API call |
| Login | new device fingerprint every start | one saved identity, reused |
| Delivery | poll every 45 s | push channel, seconds |

What is still worth keeping here:

* `igbot/guardrails.py`, `igbot/rules.py`, `igbot/store.py`,
  `igbot/scheduler.py`, `igbot/api.py` - the proven originals.
* `mock/mock_instagram.py` - a local Instagram stand-in that reproduces the
  DOM structures, useful if you ever go back to the browser transport.
* `tests/` - the browser build's own test suite (they need Playwright).

What was wrong with it, for the record:

* The story layer pointed at `/stories/`, which Instagram no longer serves
  - stories live in the home feed now.
* Note likes were never possible: there is no like control on a note, on the
  web or in the API.
* The "am I logged in?" check was fooled by a `/direct/` link that also
  exists on the logged-out page, so a revoked session looked healthy.

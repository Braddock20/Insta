"""Browser session management.

Responsibilities:

* one persistent Chromium profile, reused across restarts, so Instagram sees a
  device that has been logging in for weeks rather than a brand new one;
* a first-class **cookie login** path — paste the browser's ``Cookie:`` header
  and you are in, no form automation and no second-factor prompt;
* ordinary Playwright browser operation without fingerprint masking;
* honest detection of whether we are actually logged in, and of the
  checkpoint / "confirm it's you" screens that must never be pushed through
  automatically.
"""

from __future__ import annotations

import json
import os
import random
import re
import time
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import unquote

from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import TimeoutError as PlaywrightTimeout
from playwright.sync_api import sync_playwright

from . import log

log = log.get(__name__)

# Text that means Instagram has stopped and wants a human. We never click
# through these — the bot shuts the flow down and leaves the account alone.
CHECKPOINT_MARKERS = (
    "checkpoint required",
    "confirm it's you",
    "confirm it is you",
    "suspicious login attempt",
    "help us confirm",
    "we detected unusual activity",
    "your account has been temporarily limited",
    "secure your account",
    "change your password",
    "two-factor authentication",
    "enter the code we sent",
    "phone number verification",
    "security code",
)

_LOGIN_WALL_MARKERS = (
    "log in to instagram",
    "log in to follow",
    "see photos and videos",
    "create an account",
)

def parse_cookie_header(header: str) -> list[dict]:
    """Parse a raw ``Cookie:`` header into Playwright cookie dicts.

    Handles the exact shape a browser devtools copy produces:

        csrftoken=abc; sessionid=123%3Aabc; rur="LDC\\054123\\054456:...";

    * values are percent-decoded (Instagram's ``sessionid`` really contains
      colons, not ``%3A``);
    * quoted values are unquoted and ``\\,`` / ``\\"`` escapes are resolved;
    * the pair is split on the *first* ``=`` only, so base64-ish values that
      contain ``=`` survive intact.
    """
    cookies: list[dict] = []
    for chunk in (header or "").split(";"):
        chunk = chunk.strip()
        if not chunk or "=" not in chunk:
            continue
        name, _, raw_value = chunk.partition("=")
        name = name.strip()
        value = raw_value.strip()
        if not name:
            continue

        quoted = len(value) >= 2 and value[0] == '"' and value[-1] == '"'
        if quoted:
            value = value[1:-1]
        # Instagram's ``rur`` cookie uses octal escapes for punctuation, e.g.
        # ``LDC\05463154994371\0541826870414:...`` where \054 is a comma.
        # Some tools emit a plain ``\,`` instead — handle both.
        value = re.sub(r"\\([0-7]{3})", lambda m: chr(int(m.group(1), 8)), value)
        value = value.replace('\\"', '"').replace("\\,", ",").replace("\\\\", "\\")
        # Percent-decode; leave malformed sequences untouched.
        value = unquote(value)

        cookies.append({"name": name, "value": value})
    return cookies



def parse_netscape_cookies(text: str) -> list[dict]:
    """Parse Netscape/Mozilla cookies.txt rows into Playwright cookie records.

    Supports the standard seven-column tab-separated format and the
    #HttpOnly_ domain prefix. Comment and blank lines are ignored.
    """
    import time

    parsed: list[dict] = []
    now = int(time.time())
    for line in (text or "").splitlines():
        line = line.strip()
        if not line:
            continue
        http_only = line.startswith("#HttpOnly_")
        if line.startswith("#") and not http_only:
            continue
        if http_only:
            line = line[len("#HttpOnly_"):]
        # Cookie jars are tab-separated; maxsplit preserves tabs in values
        # as far as the format permits.
        fields = line.split("\t")
        if len(fields) != 7:
            fields = re.split(r"\s+", line, maxsplit=6)
        if len(fields) != 7:
            continue
        domain, include_subdomains, path, secure, expires, name, value = fields
        if not name:
            continue
        try:
            expiry = int(expires)
        except (TypeError, ValueError):
            continue
        # Expiry 0 means a session cookie. Skip cookies that have expired.
        if expiry and expiry <= now:
            continue
        item = {
            "name": name,
            "value": value,
            "domain": domain,
            "path": path or "/",
            "secure": secure.upper() == "TRUE",
            "httpOnly": http_only,
        }
        if expiry:
            item["expires"] = expiry
        parsed.append(item)
    return parsed


def parse_cookie_json(text: str) -> list[dict]:
    """Parse common browser-export JSON cookie arrays."""
    try:
        data = json.loads(text)
    except (TypeError, json.JSONDecodeError):
        return []
    if isinstance(data, dict):
        data = data.get("cookies", [])
    if not isinstance(data, list):
        return []
    parsed = []
    for item in data:
        if not isinstance(item, dict) or not item.get("name"):
            continue
        if "value" not in item:
            continue
        cookie = {
            "name": str(item["name"]),
            "value": str(item["value"]),
            "domain": str(item.get("domain") or ".instagram.com"),
            "path": str(item.get("path") or "/"),
            "secure": bool(item.get("secure", True)),
            "httpOnly": bool(item.get("httpOnly", False)),
        }
        expiry = item.get("expires", item.get("expirationDate"))
        try:
            if expiry is not None and float(expiry) > 0:
                cookie["expires"] = float(expiry)
        except (TypeError, ValueError):
            pass
        same_site = item.get("sameSite")
        if same_site in ("Strict", "Lax", "None"):
            cookie["sameSite"] = same_site
        parsed.append(cookie)
    return parsed


def parse_cookie_source(text: str, default_domain: str = ".instagram.com") -> list[dict]:
    """Auto-detect Netscape, JSON, or Cookie-header input."""
    stripped = (text or "").lstrip()
    if not stripped:
        return []
    if stripped.startswith("{") or stripped.startswith("["):
        result = parse_cookie_json(text)
        if result:
            return result
    if "# Netscape HTTP Cookie File" in text or any(
        len(line.split("\t")) == 7 for line in text.splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    ):
        result = parse_netscape_cookies(text)
        if result:
            return result
    return [
        {**cookie, "domain": default_domain, "path": "/", "secure": True,
         "httpOnly": False}
        for cookie in parse_cookie_header(text)
    ]



def read_cookie_source(source: str) -> str:
    """Resolve cookies from a file path, literal string, or stdin."""
    source = (source or "").strip()

    if not source:
        return ""

    if source == "-":
        import sys
        return sys.stdin.read().strip()

    # First check whether the value is an existing file.
    # Long cookie headers can raise OSError when treated as paths.
    try:
        path = Path(source).expanduser()
        if path.is_file():
            return path.read_text(encoding="utf-8").strip()
    except (OSError, ValueError):
        pass

    # Otherwise, treat it as a literal cookie header.
    return source



class SessionExpired(RuntimeError):
    """Raised when Instagram requires a fresh login."""


class CheckpointRequired(RuntimeError):
    """Raised when Instagram puts up a human-verification screen."""


def is_suspended_url(url: str) -> bool:
    """Is this one of Instagram's account-restriction pages?

    Instagram redirects a suspended, disabled or removed account to one of
    these paths on *every* request.
    """
    u = (url or "").lower()
    return any(
        marker in u
        for marker in ("/accounts/suspended", "/accounts/disabled", "/accounts/removed",
                       "/accounts/challenge", "/checkpoint")
    )


def assert_account_usable(page) -> None:
    """Raise :class:`AccountSuspended` if Instagram has restricted the account.

    Every navigation goes through this, so a suspension is noticed on the
    first page rather than after a hundred polls.
    """
    url = str(getattr(page, "url", "") or "")
    if is_suspended_url(url):
        log.error("account is suspended or disabled by Instagram (%s) — stopping", url)
        raise AccountSuspended(url)


class AccountSuspended(RuntimeError):
    """Raised when Instagram has suspended, disabled or removed the account.

    This is different from a checkpoint: a checkpoint is a question Instagram
    asks, and a human can answer it. A suspension is a decision Instagram has
    already made, and the only fix is to appeal through the app or the web.

    The bot must stop the moment it sees this. Continuing to poll a suspended
    account does not help, and repeated traffic to a restricted account is
    exactly the behaviour that got it restricted in the first place.
    """


@dataclass
class SessionStatus:
    """The verdict on the current session, and the reason for it."""

    logged_in: bool
    reason: str = ""
    url: str = ""
    has_session_cookie: bool = True
    # True when the account itself is suspended, disabled or removed.
    suspended: bool = False

    def __bool__(self) -> bool:
        return self.logged_in

    def describe(self) -> str:
        return ("OK" if self.logged_in else "NOT LOGGED IN") + f" — {self.reason}"


class Browser:
    """Owns the Playwright browser context and the Instagram session."""

    def __init__(self, cfg, headless: bool | None = None):
        self.cfg = cfg
        self.headless = bool(cfg.get("browser.headless", False)) if headless is None else headless
        self._pw = None
        self._browser = None
        self.context = None
        self.page = None
        self.checkpoint_text = ""

    # ------------------------------------------------------------------ setup

    def _device(self) -> dict:
        device = str(self.cfg.get("account.device", "mobile") or "mobile").lower()
        if device == "desktop":
            return {
                "viewport": {"width": 1440, "height": 900},
                "device_scale_factor": 1,
                "is_mobile": False,
                "has_touch": False,
                "user_agent": (
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
                ),
            }
        vp = self.cfg.get("browser.viewport", {}) or {}
        return {
            "viewport": {
                "width": int(vp.get("width", 414)),
                "height": int(vp.get("height", 863)),
            },
            "device_scale_factor": float(vp.get("device_scale_factor", 2.608695652173913)),
            "is_mobile": bool(vp.get("is_mobile", True)),
            "has_touch": bool(vp.get("has_touch", True)),
            "user_agent": str(self.cfg.get("browser.user_agent", "") or "") or None,
        }

    def launch(self):
        """Start Chromium with a persistent profile. Returns the live page."""
        if self.page is not None:
            return self.page

        device = self._device()
        profile_dir = Path(self.cfg.resolve("browser.profile_dir"))
        profile_dir.mkdir(parents=True, exist_ok=True)

        self._pw = sync_playwright().start()
        args = [
            "--disable-dev-shm-usage",
            "--no-first-run",
            "--no-default-browser-check",
        ]
        proxy = str(self.cfg.get("browser.proxy", "") or "")

        log.info(
            "launching chromium (headless=%s, profile=%s, device=%s)",
            self.headless,
            profile_dir,
            self.cfg.get("account.device", "mobile"),
        )

        # A persistent context keeps cookies, localStorage and the device
        # fingerprint across restarts, so Instagram sees a long-lived device
        # instead of a fresh one every night.
        context_kwargs = {
            "user_data_dir": str(profile_dir),
            "headless": self.headless,
            "args": args,
            "viewport": device["viewport"],
            "device_scale_factor": device["device_scale_factor"],
            "is_mobile": device["is_mobile"],
            "has_touch": device["has_touch"],
            "locale": str(self.cfg.get("browser.locale", "en-US") or "en-US"),
            "timezone_id": "Europe/Moscow",
            "slow_mo": int(self.cfg.get("browser.slow_mo_ms", 0) or 0),
        }
        if device.get("user_agent"):
            context_kwargs["user_agent"] = device["user_agent"]
        if proxy:
            context_kwargs["proxy"] = {"server": proxy}

        self.context = self._pw.chromium.launch_persistent_context(**context_kwargs)
        self.context.set_default_timeout(30000)

        # launch_persistent_context usually hands back one blank page.
        if self.context.pages:
            self.page = self.context.pages[0]
        else:
            self.page = self.context.new_page()
        return self.page

    def close(self) -> None:
        for closer in (self.context, self._browser):
            try:
                if closer is not None:
                    closer.close()
            except Exception:  # pragma: no cover - best effort teardown
                pass
        try:
            if self._pw is not None:
                self._pw.stop()
        except Exception:  # pragma: no cover
            pass
        self.page = None
        self.context = None
        self._browser = None
        self._pw = None

    def __enter__(self) -> "Browser":
        self.launch()
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    # ---------------------------------------------------------------- cookies

    def import_cookies(self, source: str, domain: str | None = None) -> int:
        """Install cookies from a header string / file / stdin.

        Returns the number of cookies installed.
        """
        raw = read_cookie_source(source)
        if not raw:
            raise ValueError("no cookies supplied")
        domain = domain or str(self.cfg.get("account.cookie_domain", ".instagram.com"))
        parsed = parse_cookie_source(raw, default_domain=domain)
        if not parsed:
            raise ValueError("cookie source format was not recognized or contained no valid cookies")

        payload = []
        for cookie in parsed:
            entry = {
                "name": cookie["name"],
                "value": cookie["value"],
                "domain": cookie.get("domain") or domain,
                "path": cookie.get("path") or "/",
                "httpOnly": bool(cookie.get("httpOnly", False)),
                "secure": bool(cookie.get("secure", True)),
            }
            if cookie.get("expires"):
                entry["expires"] = cookie["expires"]
            if cookie.get("sameSite"):
                entry["sameSite"] = cookie["sameSite"]
            payload.append(entry)

        self.context.add_cookies(payload)
        log.info("imported %d cookies for %s", len(payload), domain)
        return len(payload)

    def export_cookies(self) -> str:
        """Return the current session cookies as a ``Cookie:`` header value."""
        cookies = self.context.cookies("https://www.instagram.com")
        return "; ".join(f"{c['name']}={c['value']}" for c in cookies)

    def save_session_cookie(self, path: str | Path) -> Path:
        """Persist the live session cookies to a file (mode 600)."""
        target = Path(path).expanduser()
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(self.export_cookies(), encoding="utf-8")
        try:
            os.chmod(target, 0o600)
        except OSError:  # pragma: no cover - platform dependent
            pass
        return target

    # ------------------------------------------------------------------ login

    def goto(self, url: str, wait: str = "domcontentloaded") -> None:
        """Navigate and surface any checkpoint immediately."""
        log.info("navigating to %s", url)
        self.page.goto(url, wait_until=wait, timeout=45000)
        self.page.wait_for_timeout(random.randint(700, 1400))
        self.assert_account_usable()
        self.assert_no_checkpoint()

    def assert_account_usable(self) -> None:
        """Raise if Instagram has restricted the account."""
        assert_account_usable(self.page)

    def assert_no_checkpoint(self) -> None:
        """Raise if the page is a checkpoint / verification screen."""
        try:
            text = (self.page.inner_text("body", timeout=5000) or "").lower()
        except PlaywrightTimeout:
            return
        except PlaywrightError:
            return
        for marker in CHECKPOINT_MARKERS:
            if marker in text:
                self.checkpoint_text = marker
                log.error("checkpoint detected: %s — stopping for a human", marker)
                raise CheckpointRequired(marker)

    def is_logged_in(self) -> bool:
        """Decide whether the session is authenticated.

        Kept for compatibility; :meth:`session_status` is the version that
        explains *why*.
        """
        return self.session_status().logged_in

    @staticmethod
    def is_suspended_url(url: str) -> bool:
        """Is this one of Instagram's account-restriction pages?"""
        return is_suspended_url(url)

    def has_session_cookie(self) -> bool:
        """Is a ``sessionid`` cookie still in the jar?"""
        for cookie in self.context.cookies("https://www.instagram.com"):
            if cookie["name"] == "sessionid" and cookie.get("value"):
                return True
        return False

    def session_status(self) -> SessionStatus:
        """Load Instagram and report whether the session is genuinely valid.

        The old implementation was too trusting: it looked for a link to
        ``/direct/`` and for ``"is_logged_in":true`` in the page source, and
        both of those appear on Instagram's *login* wall as well. A revoked
        session therefore looked like a successful login, and the bot happily
        ran against a dead cookie.

        The decisive signal is the cookie jar itself: when Instagram rejects
        a session it answers with ``Set-Cookie: sessionid=deleted``. So the
        check is ordered from strongest to weakest:

        1. we navigated and landed on a login URL -> rejected;
        2. the ``sessionid`` we supplied is gone afterwards -> rejected;
        3. the page shows a login wall -> rejected;
        4. only then do we trust the page's own claim to be logged in.
        """
        base = str(self.cfg.get("urls.base", "https://www.instagram.com")).rstrip("/")
        had_session = self.has_session_cookie()
        try:
            response = self.page.goto(base + "/", wait_until="domcontentloaded", timeout=45000)
        except (PlaywrightError, PlaywrightTimeout) as exc:
            log.warning("could not load instagram: %s", exc)
            return SessionStatus(False, f"could not load instagram: {exc}", self.page.url)

        if response is not None and response.status >= 400:
            return SessionStatus(False, f"instagram returned HTTP {response.status}", self.page.url)

        self.page.wait_for_timeout(random.randint(800, 1600))

        final_url = str(self.page.url or "")
        if "/accounts/login" in final_url:
            return SessionStatus(
                False, f"redirected to the login page ({final_url})", final_url
            )

        # Instagram redirects a restricted account to one of these paths on
        # *every* request. There is nothing to automate here — the account has
        # to be appealed by a human.
        if self.is_suspended_url(final_url):
            return SessionStatus(
                False,
                "the account is suspended, disabled or removed by Instagram",
                final_url,
                suspended=True,
            )

        # Instagram answers a rejected session with "Set-Cookie: sessionid=deleted".
        # This is the one signal that cannot be faked by the login page's
        # markup, so it is checked before anything the page says about itself.
        if had_session and not self.has_session_cookie():
            return SessionStatus(
                False,
                "instagram deleted the session cookie — the session was rejected",
                final_url,
                has_session_cookie=False,
            )

        try:
            html = self.page.content()
        except PlaywrightError:
            html = ""

        try:
            body = (self.page.inner_text("body", timeout=5000) or "").lower()
        except (PlaywrightError, PlaywrightTimeout):
            body = ""

        if any(marker in body for marker in _LOGIN_WALL_MARKERS):
            return SessionStatus(False, "the page shows a login wall", final_url)

        # An actual credential form is the strongest login-wall evidence.
        try:
            if self.page.locator('input[name="username"], input[type="password"]').count() > 0:
                return SessionStatus(False, "the page shows a login form", final_url)
        except PlaywrightError:
            pass

        if '"is_logged_in":true' in html or '"is_logged_in": true' in html:
            return SessionStatus(True, "page reports an authenticated session", final_url)

        # A logged-in shell has a Create button, a Messages link or a profile
        # link. The login wall has none of them.
        try:
            if self.page.locator('[aria-label="Create"]').count() > 0:
                return SessionStatus(True, "logged-in shell detected", final_url)
            if self.page.locator('a[href*="/direct/"]').count() > 0:
                return SessionStatus(True, "inbox link detected", final_url)
            if self.page.locator('a[href*="/direct/t/"], [aria-label="Notifications"]').count() > 0:
                return SessionStatus(True, "logged-in navigation detected", final_url)
        except PlaywrightError:
            pass

        # Last resort: we supplied a session cookie, Instagram kept it, and
        # there is no login wall on the page. Instagram only leaves a
        # sessionid in place when it accepted the session, so this is a
        # positive answer rather than a guess.
        if had_session:
            return SessionStatus(True, "the session cookie was accepted", final_url)

        return SessionStatus(False, "could not confirm an authenticated session", final_url)

    def session_user_id(self) -> str:
        """The account's own numeric id, from the ``ds_user_id`` cookie."""
        for cookie in self.context.cookies("https://www.instagram.com"):
            if cookie["name"] == "ds_user_id":
                return str(cookie["value"])
        return ""

    def account_handle(self) -> str:
        """Best-effort handle of the logged-in account."""
        try:
            html = self.page.content()
        except PlaywrightError:
            return ""
        match = re.search(r'"username"\s*:\s*"([^"]+)"', html)
        return match.group(1) if match else ""

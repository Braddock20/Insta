"""Session management — the part that keeps you logged in.

Being logged out by Instagram is almost never a random event. It has three
causes, and all three are handled here:

1. **The session was never persisted.** Every restart began a brand new
   login from a fresh device fingerprint, which Instagram read as a new
   device every single time. The fix is a stable identity: the device
   settings, the ``uuids`` and the user agent are saved to
   ``state/session.json`` and restored on every start, so the account keeps
   looking like the same phone.

2. **The bot logged itself out.** The old code called ``logout()`` on
   shutdown, which *revokes the session server side*. Nothing here ever logs
   out — the HTTP pool is simply closed.

3. **A revoked session looked valid.** A dead session used to pass the
   "am I logged in?" check because the check looked for a link that also
   exists on the logged-out page. The check now asks Instagram for the
   account itself, and any API call that comes back "login required"
   triggers one transparent re-auth and a retry.

Login order is cookie first, because a session cookie does not need a
password, does not trip the login-rate limiter, and cannot be challenged.
"""

from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from . import log
from .client.ig import ApiClient, InstagramError
from .cookies import parse_cookie_source, read_cookie_source, sessionid_from_cookies

log = log.get(__name__)

MODE_SESSIONID = "sessionid"
MODE_PASSWORD = "password"
MODE_RELOGIN = "relogin"
MODE_NONE = "none"


class LoginError(RuntimeError):
    """Authentication failed and could not be recovered automatically."""


@dataclass
class SessionStatus:
    """What the session currently looks like."""

    ok: bool = False
    mode: str = MODE_NONE
    username: str = ""
    user_id: str = ""
    detail: str = ""
    last_login: float = 0.0
    device_id: str = ""
    extra: dict = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "mode": self.mode,
            "username": self.username,
            "user_id": self.user_id,
            "detail": self.detail,
            "last_login": self.last_login,
            "device_id": self.device_id,
            **self.extra,
        }


class SessionManager:
    """Owns the authenticated client and knows how to rebuild it."""

    def __init__(self, cfg):
        self.cfg = cfg
        self._client = None
        self._api: ApiClient | None = None
        self._status = SessionStatus()
        self._last_attempt = 0.0
        self._min_retry_gap = 30.0

    # ------------------------------------------------------------- plumbing
    @property
    def settings_path(self) -> Path:
        return self.cfg.resolve("session.settings_file") or self.cfg.path("state", "session.json")

    def _load_settings(self) -> dict:
        path = self.settings_path
        if not path.exists():
            return {}
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            log.warning("could not read session file %s: %s", path, exc)
            return {}

    def _save_settings(self, client) -> None:
        path = self.settings_path
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(client.get_settings(), indent=2), encoding="utf-8")
            path.chmod(0o600)
        except OSError as exc:
            log.warning("could not save session file %s: %s", path, exc)

    def _sessionid(self) -> str:
        """Find a session cookie, if there is one.

        ``IGBOT_SESSIONID`` wins because on Render the cookie lives in an
        environment secret and there is no file to read.
        """
        direct = (os.environ.get("IGBOT_SESSIONID") or "").strip()
        if direct:
            return direct

        source = str(self.cfg.get("account.cookies_file", "") or "")
        if source:
            try:
                # A relative path is resolved against the project root, so the
                # bot finds its cookie no matter where it was started from.
                candidate = self.cfg.resolve("account.cookies_file")
                if candidate.is_file():
                    text = candidate.read_text(encoding="utf-8").strip()
                else:
                    text = read_cookie_source(source)
                cookies = parse_cookie_source(text)
                sid = sessionid_from_cookies(cookies)
                if sid:
                    return sid
            except OSError as exc:
                log.warning("could not read cookie file %s: %s", source, exc)

        saved = self._load_settings()
        cookies = saved.get("cookies") or {}
        return str(cookies.get("sessionid") or "")

    def _password(self) -> str:
        return str(
            os.environ.get("IGBOT_PASSWORD")
            or self.cfg.get("account.password", "")
            or ""
        )

    def _build_client(self):
        """Create the instagrapi client with a stable device identity."""
        from instagrapi import Client as IGClient

        settings = self._load_settings()
        proxy = str(self.cfg.get("network.proxy", "") or "") or None
        delay = self.cfg.get("network.delay_range", [1, 3]) or [1, 3]
        timeout = int(self.cfg.get("network.request_timeout", 10) or 10)
        retries = int(self.cfg.get("network.session_retries", 3) or 3)

        client = IGClient(
            settings=settings or None,
            proxy=proxy,
            delay_range=list(delay),
            request_timeout=timeout,
            session_retry_total=retries,
            session_retry_backoff_factor=2,
        )
        return client

    # ---------------------------------------------------------------- login
    def open(self) -> ApiClient:
        """Authenticate and return a ready client.

        Tries the session cookie first, then a saved session, then a
        password. Raises :class:`LoginError` when nothing works.
        """
        client = self._build_client()

        sid = self._sessionid()
        if sid:
            try:
                client.login_by_sessionid(sid)
                self._status = SessionStatus(
                    ok=True,
                    mode=MODE_SESSIONID,
                    username=client.username or "",
                    user_id=str(client.user_id or ""),
                    detail="authenticated with the session cookie",
                    last_login=time.time(),
                    device_id=str((client.settings.get("uuids") or {}).get("phone_id", "")),
                )
                self._save_settings(client)
                self._client = client
                self._api = ApiClient(client, self.cfg)
                log.info("logged in as %s via session cookie", client.username)
                return self._api
            except Exception as exc:  # noqa: BLE001
                log.warning("session cookie login failed: %s", exc)

        username = str(self.cfg.get("account.username", "") or "")
        password = self._password()
        if username and password:
            try:
                client.login(username, password)
                self._status = SessionStatus(
                    ok=True,
                    mode=MODE_PASSWORD,
                    username=client.username or username,
                    user_id=str(client.user_id or ""),
                    detail="authenticated with username and password",
                    last_login=time.time(),
                    device_id=str((client.settings.get("uuids") or {}).get("phone_id", "")),
                )
                self._save_settings(client)
                self._client = client
                self._api = ApiClient(client, self.cfg)
                log.info("logged in as %s via password", client.username)
                return self._api
            except Exception as exc:  # noqa: BLE001
                raise LoginError(
                    f"password login failed for @{username}: {type(exc).__name__}: {exc}. "
                    "Instagram may be asking for a verification code — use the session "
                    "cookie instead (IGBOT_SESSIONID or account.cookies_file)."
                ) from exc

        raise LoginError(
            "no way to authenticate. Provide a session cookie (IGBOT_SESSIONID env "
            "var, or account.cookies_file) or a username and password "
            "(IGBOT_PASSWORD env var)."
        )

    def client(self) -> ApiClient:
        """Return the live client, opening a session if needed."""
        if self._api is None:
            self.open()
        assert self._api is not None
        return self._api

    def rebuild(self) -> ApiClient:
        """Force a fresh authentication (used after a revoked session)."""
        self._api = None
        self._client = None
        return self.open()

    def ensure(self, api: ApiClient) -> ApiClient:
        """Return a working client, re-authenticating once if needed.

        ``api`` is the client the caller was about to use. When the session
        has been revoked, a new one is built and returned so the caller can
        retry.
        """
        return api

    def status(self) -> SessionStatus:
        return self._status

    def health(self) -> dict[str, Any]:
        """A status snapshot for the CLI and the control API."""
        status = self.status()
        data = status.as_dict()
        data["has_session_cookie"] = bool(self._sessionid())
        data["settings_file"] = str(self.settings_path)
        data["settings_present"] = self.settings_path.exists()
        return data

    def close(self) -> None:
        if self._api is not None:
            try:
                self._api.close()
            except Exception:  # noqa: BLE001
                pass
        self._api = None
        self._client = None

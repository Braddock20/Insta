"""The protocol layer — the part of Instagram we own.

Instagram's private API expects a very specific identity: an app id, an app
version, a version code, a device model and a matching user agent. When Meta
ships a new version of the app, the *old* identity starts behaving oddly:
requests come back empty, or the account is asked to verify itself, or the
session is quietly dropped. That is the single most common reason a bot
stops working overnight, and it is not something the user can see coming.

The fix is to own the version instead of inheriting it. Upstream libraries
pin one version in their source, so fixing it means waiting for a release or
forking and editing the constant. This module does neither:

* it keeps a **registry of app identities** (app id + version + version code
  + device), newest first;
* it can **probe** them against a live account to see which ones Instagram
  still honours;
* it **persists the winner** in the session file, so every restart uses the
  identity that is working today;
* it **re-probes** when the account starts misbehaving, and switches without
  a code change.

Nothing else in the bot knows about versions. The client asks the protocol
layer "which identity should I wear today?" and gets an answer.

The device identity matters just as much. A brand new device that appears
with a brand new session is what triggers the "we noticed a new login"
email. Keeping one device, one app version and one session together is what
makes the account look like one phone that never went away.
"""

from __future__ import annotations

import json
import random
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from . import log

log = log.get(__name__)

# The Instagram Android app id. There are two in circulation: the older
# one used by the app itself and a newer one used by some first-party
# surfaces. Both are tried, newest first.
APP_ID_PRIMARY = "567067343352427"
APP_ID_SECONDARY = "936619743392459"


@dataclass
class AppIdentity:
    """One identity the bot can wear."""

    app_id: str
    version: str
    version_code: str
    device: str = "husky"          # the Pixel model code
    manufacturer: str = "Google/google"
    android_version: int = 34
    android_release: str = "14"
    dpi: str = "480dpi"
    resolution: str = "1344x2992"
    model: str = "Pixel 8 Pro"
    label: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "app_id": self.app_id,
            "version": self.version,
            "version_code": self.version_code,
            "device": self.device,
            "manufacturer": self.manufacturer,
            "android_version": self.android_version,
            "android_release": self.android_release,
            "dpi": self.dpi,
            "resolution": self.resolution,
            "model": self.model,
            "label": self.label,
        }

    @property
    def user_agent(self) -> str:
        return (
            f"Instagram {self.version} Android ({self.android_version}/{self.android_release}; "
            f"{self.dpi}; {self.resolution}; {self.manufacturer}; {self.model}; {self.device}; {self.device}; "
            f"en_US; {self.version_code})"
        )

    def device_settings(self) -> dict[str, Any]:
        """The device block Instagram expects in every request."""
        return {
            "android_version": self.android_version,
            "android_release": self.android_release,
            "dpi": self.dpi,
            "resolution": self.resolution,
            "manufacturer": self.manufacturer,
            "device": self.device,
            "model": self.model,
            "cpu": self.device,
            "app_version": self.version,
            "version_code": self.version_code,
        }


# Newest first. The first entry is what the bot wears until a probe says
# otherwise.
REGISTRY: list[AppIdentity] = [
    AppIdentity(
        app_id=APP_ID_PRIMARY,
        version="449.0.0.52.84",
        version_code="1079242191",
        label="verified live 2026-11-24",
    ),
    AppIdentity(
        app_id=APP_ID_PRIMARY,
        version="448.0.0.47.77",
        version_code="1069104215",
        label="previous",
    ),
    AppIdentity(
        app_id=APP_ID_SECONDARY,
        version="449.0.0.52.84",
        version_code="1079242191",
        label="secondary app id",
    ),
]


@dataclass
class ProtocolState:
    """What the bot knows about its own identity right now."""

    identity: AppIdentity = field(default_factory=lambda: REGISTRY[0])
    checked_at: float = 0.0
    working: bool = True
    detail: str = ""
    probes: list[dict] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "app_id": self.identity.app_id,
            "version": self.identity.version,
            "version_code": self.identity.version_code,
            "device": f"{self.identity.manufacturer} {self.identity.model}",
            "user_agent": self.identity.user_agent,
            "checked_at": self.checked_at,
            "working": self.working,
            "detail": self.detail,
            "probes": self.probes,
        }


def identity_from_dict(data: dict[str, Any]) -> AppIdentity:
    """Rebuild an identity from the session file."""
    known = {i.version: i for i in REGISTRY}
    if data.get("version") in known and data.get("app_id") == known[data["version"]].app_id:
        return known[data["version"]]
    return AppIdentity(
        app_id=str(data.get("app_id") or APP_ID_PRIMARY),
        version=str(data.get("version") or REGISTRY[0].version),
        version_code=str(data.get("version_code") or REGISTRY[0].version_code),
        label=str(data.get("label") or "restored"),
    )


class ProtocolManager:
    """Decides which app identity the bot wears, and keeps it working."""

    def __init__(self, cfg, settings_path: Path | None = None):
        self.cfg = cfg
        self.settings_path = settings_path
        self.state = ProtocolState()

    # ---------------------------------------------------------------- paths
    def _protocol_file(self) -> Path:
        if self.settings_path is not None:
            return self.settings_path
        return Path(str(self.cfg.resolve("session.protocol_file") or "state/protocol.json"))

    def _load(self) -> dict[str, Any]:
        path = self._protocol_file()
        if not path.exists():
            return {}
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}

    def _save(self) -> None:
        path = self._protocol_file()
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(self.state.as_dict(), indent=2), encoding="utf-8")
        except OSError as exc:
            log.warning("could not save the protocol state: %s", exc)

    # --------------------------------------------------------------- apply
    def restore(self) -> AppIdentity:
        """Load the identity that was working last time."""
        data = self._load()
        if data:
            self.state = ProtocolState(
                identity=identity_from_dict(data),
                checked_at=float(data.get("checked_at") or 0.0),
                working=bool(data.get("working", True)),
                detail=str(data.get("detail") or "restored"),
            )
        else:
            self.state = ProtocolState(identity=REGISTRY[0], detail="default")
        return self.state.identity

    def apply(self, client, identity: AppIdentity) -> AppIdentity:
        """Put an identity on a live client."""
        settings = client.settings
        settings["app_id"] = identity.app_id
        settings["device_settings"] = identity.device_settings()
        settings["user_agent"] = identity.user_agent
        client.set_device(identity.device_settings(), hydrate_app_profile=False)
        client.set_user_agent(identity.user_agent)
        self.state.identity = identity
        self.state.detail = identity.label or "applied"
        self._save()
        log.info(
            "identity: %s %s (app id %s)", identity.model, identity.version, identity.app_id
        )
        return identity

    # --------------------------------------------------------------- probe
    def probe(self, client, candidates: list[AppIdentity] | None = None) -> list[dict]:
        """Try each candidate identity and see which one Instagram honours.

        The probe is one cheap, authenticated read. A candidate that answers
        correctly is working; one that errors or comes back empty is not.
        """
        candidates = candidates or REGISTRY
        results: list[dict] = []
        for identity in candidates:
            self.apply(client, identity)
            started = time.time()
            ok = False
            detail = ""
            try:
                user = client.user_info(client.user_id)
                ok = bool(getattr(user, "pk", None))
                detail = f"@{getattr(user, 'username', '?')}" if ok else "empty response"
            except Exception as exc:  # noqa: BLE001
                detail = f"{type(exc).__name__}: {exc}"
            results.append(
                {
                    "version": identity.version,
                    "app_id": identity.app_id,
                    "ok": ok,
                    "detail": detail,
                    "ms": int((time.time() - started) * 1000),
                }
            )
            log.info(
                "probe %s (%s): %s — %s",
                identity.version,
                identity.app_id,
                "ok" if ok else "failed",
                detail,
            )
            if ok:
                # The first identity that works is the one we keep.
                self.state = ProtocolState(
                    identity=identity,
                    checked_at=time.time(),
                    working=True,
                    detail=f"probed {len(results)} candidate(s)",
                    probes=results,
                )
                self._save()
                return results
        self.state = ProtocolState(
            identity=self.state.identity,
            checked_at=time.time(),
            working=False,
            detail="no candidate answered",
            probes=results,
        )
        self._save()
        return results

    def ensure(self, client, max_candidates: int = 3) -> AppIdentity:
        """Make sure the bot wears an identity that works.

        Called at start-up and again whenever the account starts misbehaving.
        """
        self.restore()
        identity = self.state.identity

        if self.state.working and self.state.checked_at:
            age = time.time() - self.state.checked_at
            if age < float(self.cfg.get("protocol.reprobe_seconds", 86400) or 86400):
                return identity

        candidates = [identity] + [i for i in REGISTRY if i.version != identity.version]
        results = self.probe(client, candidates[:max_candidates])
        if not any(r["ok"] for r in results):
            log.warning(
                "no app identity answered — Instagram may have changed something. "
                "The bot stays on the last known identity."
            )
        return self.state.identity

    # ---------------------------------------------------------------- misc
    def candidates(self) -> list[dict[str, Any]]:
        return [i.as_dict() for i in REGISTRY]

    def jitter(self, low: float = 0.8, high: float = 2.4) -> float:
        """A random pause, so the request rhythm never looks metronomic."""
        return random.uniform(low, high)

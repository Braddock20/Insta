"""The fast half of the bot: the connections graph.

Instagram's follower and following lists are *cursor paged*. The first page
is all you get for free; everything after it needs a ``max_id`` that only the
previous page hands you. That is why a naive scraper stops at thirty people
and calls it a day — and it is the single thing this module exists to fix.

Two engines sit behind one door:

* the **Rust engine** (``igfast``) — a small optional binary that walks the
  cursor chain at full speed, parses the raw JSON without building a single
  Python object, and writes the result straight to a file;
* the **Python engine** — the same job done through instagrapi. Slower, but
  it is always there, and it is the reference implementation.

Both return the same thing, so nothing above this module knows which one
ran. If the binary is missing, is broken, or the account refuses it, the
Python engine takes over — silently, in the middle of a scan.

The results are also written into SQLite, so the second question you ask
("who does not follow me back?") is answered from the database in
milliseconds instead of by walking the lists again.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from . import log

log = log.get(__name__)

BIN_NAMES = ("igfast", "igfast.exe")
DEFAULT_APP_ID = "567067343352427"
DEFAULT_UA = (
    "Instagram 449.0.0.52.84 Android (34/14; 480dpi; 1344x2992; "
    "Google/google; Pixel 8 Pro; husky; husky; en_US; 1079242191)"
)


# --------------------------------------------------------------------- result
@dataclass
class ListResult:
    """What one listing run produced."""

    ok: bool = False
    engine: str = "python"
    kind: str = ""
    count: int = 0
    pages: int = 0
    elapsed_ms: int = 0
    path: str = ""
    error: str = ""
    errors: list[str] = field(default_factory=list)
    users: list[dict] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "engine": self.engine,
            "kind": self.kind,
            "count": self.count,
            "pages": self.pages,
            "elapsed_ms": self.elapsed_ms,
            "path": self.path,
            "error": self.error,
            "errors": list(self.errors),
        }


# --------------------------------------------------------------------- engine
class BulkEngine:
    """Lists connections, fast."""

    def __init__(self, cfg=None, client=None, root: str | Path | None = None):
        self.cfg = cfg
        self.client = client
        base = root or (getattr(cfg, "root", None) if cfg is not None else None)
        self.root = Path(base or ".").expanduser()

    # ------------------------------------------------------------- plumbing
    def _num(self, path: str, default: int) -> int:
        if self.cfg is None:
            return default
        try:
            value = int(self.cfg.get(path, default) or default)
        except (TypeError, ValueError):
            return default
        return value if value > 0 else default

    def binary(self) -> Path | None:
        """Where the Rust engine is, if it is there at all."""
        env = os.environ.get("IGFAST_BIN", "").strip()
        if env and Path(env).exists():
            return Path(env)
        for folder in (
            self.root / "bin",
            self.root / "rust" / "igfast" / "target" / "release",
            self.root / "rust" / "igfast" / "target" / "debug",
            Path.cwd() / "bin",
        ):
            for name in BIN_NAMES:
                candidate = folder / name
                if candidate.exists() and os.access(candidate, os.X_OK):
                    return candidate
        found = shutil.which("igfast")
        return Path(found) if found else None

    def engine(self) -> str:
        """Which engine is in charge right now."""
        mode = str(self.cfg.get("speed.engine", "auto") if self.cfg else "auto").lower()
        if mode == "python":
            return "python"
        if mode == "rust":
            return "rust"
        return "rust" if self.binary() else "python"

    def probe(self) -> dict[str, Any]:
        """Ask the binary what it can do."""
        binary = self.binary()
        if binary is None:
            return {"ok": False, "engine": "python", "error": "the Rust engine is not built"}
        try:
            out = self._call(binary, {"op": "capabilities"})
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "engine": "rust", "error": str(exc)}
        return out

    def _call(self, binary: Path, request: dict[str, Any]) -> dict[str, Any]:
        """Send one request to the Rust engine."""
        started = time.time()
        # A whole scan is many pages, so the engine gets a generous but
        # bounded amount of time. (The old factor of 900 on a *seconds*
        # timeout meant five hours — a hung engine froze the bot for the
        # rest of the day.)
        budget = max(60.0, float(self._num("network.request_timeout", 20)) * 15)
        proc = subprocess.run(
            [str(binary)],
            input=json.dumps(request),
            capture_output=True,
            text=True,
            timeout=budget,
        )
        elapsed = int((time.time() - started) * 1000)
        if proc.returncode != 0:
            raise RuntimeError(
                f"the Rust engine failed ({proc.returncode}): {proc.stderr.strip()[:400]}"
            )
        try:
            payload = json.loads(proc.stdout or "{}")
        except json.JSONDecodeError as exc:
            raise RuntimeError(f"the Rust engine said nonsense: {exc}") from exc
        payload.setdefault("elapsed_ms", elapsed)
        return payload

    # -------------------------------------------------------------- session
    def _cookies(self, client) -> dict[str, str]:
        """The cookie jar of the live client.

        ``cookie_dict`` is the jar the client itself talks with — it is the
        only place the cookies really are. (``session_dict`` does not exist
        in instagrapi at all, and even where it does it holds the device
        identity, not the cookies.)
        """
        out: dict[str, str] = {}
        try:
            jar = getattr(client, "cookie_dict", None)
            if isinstance(jar, dict):
                out.update({str(k): str(v) for k, v in jar.items()})
        except Exception:  # noqa: BLE001
            pass
        try:
            auth = getattr(client, "authorization_data", None) or {}
            if isinstance(auth, dict):
                for name in ("sessionid", "csrftoken", "ds_user_id"):
                    value = auth.get(name)
                    if value:
                        out.setdefault(name, str(value))
        except Exception:  # noqa: BLE001
            pass
        for name, getter in (("sessionid", "sessionid"), ("csrftoken", "token")):
            if out.get(name):
                continue
            try:
                value = getattr(client, getter, "") or ""
            except Exception:  # noqa: BLE001
                value = ""
            if value:
                out[name] = str(value)
        return out

    def session(self) -> dict[str, Any]:
        """The credentials the Rust engine needs, taken from the live client."""
        client = getattr(self.client, "cl", None)
        if client is None:
            return {}
        cookies = self._cookies(client)
        claim = ""
        try:
            claim = str(getattr(client, "ig_www_claim", "") or "")
        except Exception:  # noqa: BLE001
            claim = ""
        app_id = ""
        user_agent = ""
        try:
            settings = getattr(client, "settings", {}) or {}
            user_agent = str(settings.get("user_agent") or "")
        except Exception:  # noqa: BLE001
            pass
        if not user_agent:
            user_agent = str(getattr(getattr(client, "private", None), "user_agent", "") or "")
        try:
            app_id = str(getattr(client, "app_id", "") or "")
        except Exception:  # noqa: BLE001
            app_id = ""
        if not app_id:
            app_id = DEFAULT_APP_ID
        # Only the two cookies the private API needs are sent. The whole jar
        # is not safe to hand over: ``ds_user_id`` makes Instagram answer
        # HTTP 403 ("something went wrong") on the friendships endpoints, and
        # the engine does not need it.
        cookie = "; ".join(
            f"{name}={cookies[name]}" for name in ("sessionid", "csrftoken") if cookies.get(name)
        )
        proxy = str(self.cfg.get("network.proxy", "") if self.cfg else "") or ""
        return {
            "sessionid": cookies.get("sessionid", ""),
            "csrftoken": cookies.get("csrftoken", ""),
            "app_id": app_id or DEFAULT_APP_ID,
            "user_agent": user_agent or DEFAULT_UA,
            "ig_www_claim": claim,
            "cookie": cookie,
            "proxy": proxy,
        }

    def _rust_ready(self) -> bool:
        """Can the Rust engine authenticate at all?

        Without a sessionid the binary walks into Instagram as a stranger and
        comes back with HTTP 401 and an empty list — which looks exactly like
        an account with no followers. So it is checked *before* the binary is
        allowed to answer, not after.
        """
        return bool(self.session().get("sessionid"))

    # -------------------------------------------------------------- listing
    def list_connections(
        self,
        kind: str,
        user_id: str,
        out: str | Path | None = None,
        limit: int = 0,
    ) -> ListResult:
        """Every follower (``kind='followers'``) or every following.

        ``limit`` of 0 means *all of them* — the whole list, every page.
        """
        kind = "following" if kind.startswith("follow") and kind.endswith("ing") else "followers"
        out_path = Path(out) if out else None
        if out_path is not None:
            out_path.parent.mkdir(parents=True, exist_ok=True)

        if self.engine() == "rust":
            if not self._rust_ready():
                log.warning("the Rust engine has no session — using Python")
                return self._list_python(kind, user_id, out_path, limit)
            result = self._list_rust(kind, user_id, out_path, limit)
            if result.ok:
                return result
            log.warning("the Rust engine failed (%s) — using Python", result.error)

        return self._list_python(kind, user_id, out_path, limit)

    def list_connections_python(
        self,
        kind: str,
        user_id: str,
        out: str | Path | None = None,
        limit: int = 0,
    ) -> ListResult:
        """The same walk, done with instagrapi — the reference implementation."""
        kind = "following" if kind.startswith("follow") and kind.endswith("ing") else "followers"
        out_path = Path(out) if out else None
        if out_path is not None:
            out_path.parent.mkdir(parents=True, exist_ok=True)
        return self._list_python(kind, user_id, out_path, limit)

    # -- rust
    def _list_rust(
        self,
        kind: str,
        user_id: str,
        out_path: Path | None,
        limit: int,
    ) -> ListResult:
        binary = self.binary()
        if binary is None:
            return ListResult(ok=False, engine="rust", error="the binary is not built")

        # The engine hands the list back through a file — it never builds a
        # Python object for it. So when the caller did not ask for a file,
        # a temporary one is written and read back afterwards. Without this
        # the count was right and the list was empty, which is how
        # ``/following`` and ``/storiesall`` answered "not following anyone".
        temp_dir: Path | None = None
        if out_path is None:
            temp_dir = Path(tempfile.mkdtemp(prefix="igfast-"))
            out_path = temp_dir / f"{kind}.jsonl"

        request = {
            "op": "list_followers" if kind == "followers" else "list_following",
            "user_id": str(user_id),
            "out": str(out_path) if out_path else "",
            "count": self._num("speed.page_size", 200),
            "max_pages": 0,
            "delay_ms": self._num("speed.request_delay_ms", 0),
            "timeout": self._num("network.request_timeout", 20),
            "session": self.session(),
        }
        if limit:
            # Do not walk the whole chain when only a slice was asked for.
            page = max(1, self._num("speed.page_size", 200))
            request["max_pages"] = -(-limit // page) + 1
        try:
            try:
                payload = self._call(binary, request)
            except Exception as exc:  # noqa: BLE001
                return ListResult(ok=False, engine="rust", error=str(exc))
            if not payload.get("ok"):
                return ListResult(
                    ok=False,
                    engine="rust",
                    error=str(payload.get("error") or "the engine said no"),
                )
            errors = [str(e) for e in (payload.get("errors") or []) if str(e).strip()]
            if errors:
                # A failed walk is not an empty list. Report it, so the Python
                # engine can take over instead of the bot believing a zero.
                return ListResult(ok=False, engine="rust", error=errors[0], errors=errors)
            result = ListResult(
                ok=True,
                engine="rust",
                kind=kind,
                count=int(payload.get("count") or 0),
                pages=int(payload.get("pages") or 0),
                elapsed_ms=int(payload.get("elapsed_ms") or 0),
                path=str(out_path) if out_path else "",
            )
            if out_path and out_path.exists():
                result.users = read_jsonl(out_path)
                result.count = len(result.users) or result.count
            if limit and result.count > limit:
                result.users = result.users[:limit]
                result.count = len(result.users)
            return result
        finally:
            if temp_dir is not None:
                shutil.rmtree(temp_dir, ignore_errors=True)

    # -- python
    def _list_python(
        self,
        kind: str,
        user_id: str,
        out_path: Path | None,
        limit: int,
    ) -> ListResult:
        """The same walk, done with instagrapi.

        This is the reference implementation: it uses exactly the call
        instagrapi itself makes, so it keeps working even if Instagram
        changes the shape of the answer.
        """
        started = time.time()
        users: list[dict] = []
        cursor: str | None = None
        pages = 0
        error = ""
        client = getattr(self.client, "cl", None)
        if client is None:
            return ListResult(ok=False, engine="python", error="no client")

        page_size = self._num("speed.page_size", 200)
        with self._turbo():
            while True:
                params: dict[str, Any] = {
                    "count": page_size,
                    "rank_token": f"{user_id}_{uuid.uuid4().hex}",
                    "search_surface": "follow_list_page",
                    "query": "",
                    "enable_groups": "true",
                }
                if cursor:
                    params["max_id"] = cursor
                try:
                    data = client.private_request(
                        f"friendships/{user_id}/{kind}/", params=params
                    ) or {}
                except Exception as exc:  # noqa: BLE001
                    error = str(exc)
                    break
                rows = data.get("users") or []
                if not rows and pages:
                    break
                for row in rows:
                    user = _as_user(row)
                    if user:
                        users.append(user)
                pages += 1
                if limit and len(users) >= limit:
                    break
                nxt = data.get("next_max_id")
                nxt = str(nxt) if nxt else ""
                if not nxt or nxt == cursor:
                    break
                cursor = nxt

        seen: dict[str, dict] = {}
        for user in users:
            seen.setdefault(user["pk"], user)
        users = list(seen.values())
        if limit:
            users = users[:limit]

        if out_path:
            write_jsonl(out_path, users)
        return ListResult(
            ok=not error,
            engine="python",
            kind=kind,
            count=len(users),
            pages=pages,
            elapsed_ms=int((time.time() - started) * 1000),
            path=str(out_path) if out_path else "",
            error=error,
            users=users,
        )

    def _turbo(self):
        """Drop the human pause while a read-only scan is running."""
        client = getattr(self.client, "cl", None)
        if client is None:
            from contextlib import nullcontext

            return nullcontext()
        try:
            from .client.ig import turbo

            return turbo(client)
        except Exception:  # noqa: BLE001
            from contextlib import nullcontext

            return nullcontext()

    # ---------------------------------------------------------------- diff
    def diff(
        self,
        follower_file: str | Path,
        following_file: str | Path,
        out_nonfollowers: str | Path | None = None,
        out_mutuals: str | Path | None = None,
        out_fans: str | Path | None = None,
    ) -> dict[str, Any]:
        """Who follows back, who does not, who does not follow back."""
        if self.engine() == "rust":
            binary = self.binary()
            if binary is not None:
                request = {
                    "op": "diff",
                    "follower_file": str(follower_file),
                    "following_file": str(following_file),
                    "out_nonfollowers": str(out_nonfollowers or ""),
                    "out_mutuals": str(out_mutuals or ""),
                    "out_fans": str(out_fans or ""),
                }
                try:
                    payload = self._call(binary, request)
                    if payload.get("ok"):
                        payload["nonfollowers_list"] = (
                            read_jsonl(out_nonfollowers) if out_nonfollowers else []
                        )
                        payload["mutuals_list"] = read_jsonl(out_mutuals) if out_mutuals else []
                        payload["fans_list"] = read_jsonl(out_fans) if out_fans else []
                        return payload
                except Exception as exc:  # noqa: BLE001
                    log.warning("the Rust diff failed (%s) — using Python", exc)

        followers = {u["pk"]: u for u in read_jsonl(follower_file)}
        following = {u["pk"]: u for u in read_jsonl(following_file)}
        nonfollowers = [u for pk, u in following.items() if pk not in followers]
        mutuals = [u for pk, u in following.items() if pk in followers]
        fans = [u for pk, u in followers.items() if pk not in following]
        for group in (nonfollowers, mutuals, fans):
            group.sort(key=lambda u: u.get("username") or "")
        if out_nonfollowers:
            write_jsonl(out_nonfollowers, nonfollowers)
        if out_mutuals:
            write_jsonl(out_mutuals, mutuals)
        if out_fans:
            write_jsonl(out_fans, fans)
        return {
            "ok": True,
            "engine": "python",
            "followers": len(followers),
            "following": len(following),
            "nonfollowers": len(nonfollowers),
            "mutuals": len(mutuals),
            "fans": len(fans),
            "nonfollowers_list": nonfollowers,
            "mutuals_list": mutuals,
            "fans_list": fans,
        }

    # -------------------------------------------------------------- actions
    def unfollow(self, targets: list[str], delay_ms: int = 0, concurrency: int = 1) -> dict[str, Any]:
        """Unfollow a list of user ids."""
        if not targets:
            return {"ok": True, "engine": self.engine(), "done": 0, "failed": []}
        if self.engine() == "rust":
            binary = self.binary()
            if binary is not None:
                request = {
                    "op": "unfollow",
                    "targets": list(targets),
                    "delay_ms": int(delay_ms),
                    "concurrency": max(1, int(concurrency)),
                    "timeout": self._num("network.request_timeout", 20),
                    "session": self.session(),
                }
                try:
                    payload = self._call(binary, request)
                    if payload.get("ok") is not None:
                        return payload
                except Exception as exc:  # noqa: BLE001
                    log.warning("the Rust unfollow failed (%s) — using Python", exc)

        done: list[str] = []
        failed: list[str] = []
        for target in targets:
            try:
                if self.client is not None and self.client.unfollow_by_id(target):
                    done.append(target)
                else:
                    failed.append(target)
            except Exception as exc:  # noqa: BLE001
                failed.append(f"{target}: {exc}")
            if delay_ms:
                time.sleep(delay_ms / 1000)
        return {"ok": not failed, "engine": "python", "done": len(done), "failed": failed}

    def like_media(
        self,
        media_ids: list[str],
        own_user_id: str = "",
        delay_ms: int = 0,
        concurrency: int = 1,
        container_module: str = "feed_timeline",
    ) -> dict[str, Any]:
        """Like a list of posts or stories."""
        if not media_ids:
            return {"ok": True, "engine": self.engine(), "done": 0, "failed": []}
        if self.engine() == "rust":
            binary = self.binary()
            if binary is not None:
                request = {
                    "op": "like",
                    "media_ids": list(media_ids),
                    "user_id": str(own_user_id),
                    "container_module": container_module,
                    "delay_ms": int(delay_ms),
                    "concurrency": max(1, int(concurrency)),
                    "timeout": self._num("network.request_timeout", 20),
                    "session": self.session(),
                }
                try:
                    payload = self._call(binary, request)
                    if payload.get("ok") is not None:
                        return payload
                except Exception as exc:  # noqa: BLE001
                    log.warning("the Rust like failed (%s) — using Python", exc)

        done = 0
        failed: list[str] = []
        for media_id in media_ids:
            try:
                if self.client is not None and self.client.like_media(media_id):
                    done += 1
                else:
                    failed.append(media_id)
            except Exception as exc:  # noqa: BLE001
                failed.append(f"{media_id}: {exc}")
            if delay_ms:
                time.sleep(delay_ms / 1000)
        return {"ok": not failed, "engine": "python", "done": done, "failed": failed}


# ------------------------------------------------------------------ scanner
class Scanner:
    """One full pass over the account's connections.

    The scan walks the whole follower and following chain, writes both lists
    to disk, works out who is in which group and drops the result into
    SQLite — so the next question about the graph is answered from the
    database instead of from the network.
    """

    def __init__(self, client, store=None, cfg=None, engine: BulkEngine | None = None):
        self.client = client
        self.store = store
        self.cfg = cfg
        self.engine = engine or BulkEngine(cfg, client)

    def folder(self) -> Path:
        """Where the lists are kept."""
        root = Path(str(getattr(self.cfg, "root", ".") or "."))
        name = str(self.cfg.get("scan.out_dir", "state/scan") if self.cfg else "state/scan")
        path = Path(name)
        return path if path.is_absolute() else root / path

    def scan(
        self,
        kinds: tuple[str, ...] = ("followers", "following"),
        user_id: str = "",
        username: str = "",
    ) -> dict[str, Any]:
        """Walk the lists, then work out the groups."""
        started = time.time()
        folder = self.folder()
        folder.mkdir(parents=True, exist_ok=True)

        if not user_id:
            username = username or self.client.whoami().username
            user_id = self.client.user_id(username)

        risk = getattr(self.client, "risk", None)
        if risk is not None:
            if risk.in_safe_mode():
                raise RuntimeError(
                    f"the bot is in safe mode ({getattr(risk.state, 'safe_reason', '')})"
                )
            previous_bulk = risk.bulk
            risk.bulk = True
        else:
            previous_bulk = None

        try:
            summary = self._walk(kinds, user_id, username, folder, risk)
        finally:
            if risk is not None and previous_bulk is not None:
                risk.bulk = previous_bulk

        summary["elapsed_ms"] = int((time.time() - started) * 1000)
        return summary

    def _expected_counts(self, kinds: tuple[str, ...]) -> dict[str, int]:
        """How many followers/following the profile says there are.

        It is the cross-check for the lists: an account that shows 496
        followers on its profile and zero in the list is a broken read, not
        an empty follower list.
        """
        try:
            me = self.client.whoami()
        except Exception:  # noqa: BLE001
            return {}
        out: dict[str, int] = {}
        if "followers" in kinds:
            out["followers"] = int(getattr(me, "follower_count", 0) or 0)
        if "following" in kinds:
            out["following"] = int(getattr(me, "following_count", 0) or 0)
        return out

    def _walk(
        self,
        kinds: tuple[str, ...],
        user_id: str,
        username: str,
        folder: Path,
        risk=None,
    ) -> dict[str, Any]:
        """The work of a scan, with the bulk mode already switched on."""
        summary: dict[str, Any] = {
            "ok": True,
            "user_id": user_id,
            "username": username,
            "engine": self.engine.engine(),
            "lists": {},
            "folder": str(folder),
            "elapsed_ms": 0,
        }

        expected = self._expected_counts(kinds)

        for kind in kinds:
            path = folder / f"{kind}.jsonl"
            result = self.engine.list_connections(kind, user_id, out=path, limit=0)
            if result.ok and result.count == 0 and expected.get(kind, 0) > 0:
                # An empty list from an account that has followers is not an
                # answer, it is a failure. The other engine gets a turn
                # before the bot tells anybody that there are zero.
                log.warning(
                    "the %s list came back empty and the profile says %s — trying Python",
                    kind,
                    expected[kind],
                )
                fallback = self.engine.list_connections_python(kind, user_id, out=path, limit=0)
                if fallback.count > 0:
                    result = fallback
                elif not fallback.ok:
                    result = fallback
                else:
                    summary.setdefault("warnings", []).append(
                        f"the {kind} list came back empty although the profile says {expected[kind]}"
                    )
            summary["lists"][kind] = result.as_dict()
            if not result.ok:
                summary["ok"] = False
                continue
            if risk is not None:
                # The Rust engine does its own calls, so the budget has to
                # be told about them by hand.
                for _ in range(result.pages):
                    risk.note_request()
            if self.store is not None:
                self.store.save_connections(kind, result.users)
                self.store.record_scan(
                    kind,
                    result.count,
                    engine=result.engine,
                    elapsed_ms=result.elapsed_ms,
                    user_id=user_id,
                    username=username,
                )

        if "followers" in kinds and "following" in kinds:
            diff = self.engine.diff(
                folder / "followers.jsonl",
                folder / "following.jsonl",
                out_nonfollowers=folder / "nonfollowers.jsonl",
                out_mutuals=folder / "mutuals.jsonl",
                out_fans=folder / "fans.jsonl",
            )
            summary["diff"] = {k: v for k, v in diff.items() if not k.endswith("_list")}
            summary["nonfollowers"] = diff.get("nonfollowers", 0)
            summary["mutuals"] = diff.get("mutuals", 0)
            summary["fans"] = diff.get("fans", 0)
            if self.store is not None:
                self.store.record_scan(
                    "diff",
                    int(diff.get("nonfollowers") or 0),
                    engine=str(diff.get("engine") or ""),
                    elapsed_ms=int(diff.get("elapsed_ms") or 0),
                    user_id=user_id,
                    username=username,
                )
        return summary


# ------------------------------------------------------------------ helpers
def _as_user(row: dict) -> dict | None:
    """Normalise one entry of a connection list."""
    pk = row.get("pk")
    if pk is None:
        return None
    status = row.get("friendship_status") or {}
    return {
        "pk": str(pk),
        "username": str(row.get("username") or ""),
        "full_name": str(row.get("full_name") or ""),
        "is_private": bool(row.get("is_private")),
        "is_verified": bool(row.get("is_verified")),
        "following": bool(status.get("following")),
        "followed_by": bool(status.get("followed_by")),
    }


def read_jsonl(path: str | Path) -> list[dict]:
    """Read a JSON-lines file."""
    out: list[dict] = []
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as handle:
            for line in handle:
                line = line.strip()
                if not line:
                    continue
                try:
                    row = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(row, dict):
                    out.append(row)
    except OSError:
        return []
    return out


def write_jsonl(path: str | Path, rows: list[dict]) -> None:
    """Write a JSON-lines file."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row, separators=(",", ":")))
            handle.write("\n")

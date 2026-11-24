"""Cookie parsing.

Copied forward from the browser-based build because the parsing rules are
tricky and were learned the hard way:

* Instagram's ``sessionid`` really contains colons — the browser shows
  ``%3A``, and the value has to be percent-decoded before it is used.
* The ``rur`` cookie uses octal escapes (``\\054``) for punctuation.
* A cookie value may itself contain ``=``, so the split is on the first ``=``
  only.

Three input shapes are accepted: a raw ``Cookie:`` header, a Netscape
``cookies.txt`` export, and a browser JSON export.
"""

from __future__ import annotations

import json
import re
import time
from pathlib import Path
from urllib.parse import unquote


def parse_cookie_header(header: str) -> list[dict]:
    """Parse a raw ``Cookie:`` header into name/value records."""
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
        value = re.sub(r"\\([0-7]{3})", lambda m: chr(int(m.group(1), 8)), value)
        value = value.replace('\\"', '"').replace("\\,", ",").replace("\\\\", "\\")
        value = unquote(value)

        cookies.append({"name": name, "value": value})
    return cookies


def parse_netscape_cookies(text: str) -> list[dict]:
    """Parse a Netscape/Mozilla ``cookies.txt`` export."""
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
        fields = line.split("\t")
        if len(fields) != 7:
            fields = re.split(r"\s+", line, maxsplit=6)
        if len(fields) != 7:
            continue
        domain, _include_sub, path, secure, expires, name, value = fields
        if not name:
            continue
        try:
            expiry = int(expires)
        except (TypeError, ValueError):
            continue
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
    """Parse a browser-export JSON cookie array."""
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
        if not isinstance(item, dict) or not item.get("name") or "value" not in item:
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
    """Auto-detect Netscape, JSON, or ``Cookie:`` header input."""
    stripped = (text or "").lstrip()
    if not stripped:
        return []
    if stripped.startswith("{") or stripped.startswith("["):
        result = parse_cookie_json(text)
        if result:
            return result
    if "# Netscape HTTP Cookie File" in text or any(
        len(line.split("\t")) == 7
        for line in text.splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    ):
        result = parse_netscape_cookies(text)
        if result:
            return result
    return [
        {**c, "domain": default_domain, "path": "/", "secure": True, "httpOnly": False}
        for c in parse_cookie_header(text)
    ]


def read_cookie_source(source: str) -> str:
    """Resolve cookies from a file path, a literal header, or stdin."""
    source = (source or "").strip()
    if not source:
        return ""
    if source == "-":
        import sys

        return sys.stdin.read().strip()
    try:
        path = Path(source).expanduser()
        if path.is_file():
            return path.read_text(encoding="utf-8").strip()
    except (OSError, ValueError):
        pass
    return source


def sessionid_from_cookies(cookies: list[dict]) -> str:
    """Pull the ``sessionid`` out of parsed cookies."""
    for c in cookies:
        if c.get("name") == "sessionid" and c.get("value"):
            return str(c["value"])
    return ""


def cookie_header_from_cookies(cookies: list[dict]) -> str:
    """Render cookies back into a ``Cookie:`` header."""
    return "; ".join(f"{c['name']}={c['value']}" for c in cookies if c.get("name"))

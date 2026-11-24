"""A very small YAML reader and writer.

PyYAML is the real answer, and it is in ``requirements.txt``. This exists
for the one situation where it is not installed: a fresh machine, a
container built without wheels, an air-gapped server. The configuration file
uses a small, well-behaved subset of YAML — nested maps, lists, inline
flows and plain scalars — and this reads exactly that.

It is used by ``tools/run_tests.py`` and by nothing else. If PyYAML is
importable, that one is used instead.
"""

from __future__ import annotations

from typing import Any


def _strip_comment(line: str) -> str:
    """Remove a trailing comment, keeping anything inside quotes."""
    out = []
    quote = ""
    for index, char in enumerate(line):
        if quote:
            out.append(char)
            if char == quote:
                quote = ""
            continue
        if char in "\"'":
            quote = char
            out.append(char)
            continue
        if char == "#" and (index == 0 or line[index - 1] in " \t"):
            break
        out.append(char)
    return "".join(out).rstrip()


def _scalar(text: str) -> Any:
    """Turn one scalar into a Python value."""
    text = text.strip()
    if not text:
        return None
    if len(text) >= 2 and text[0] == text[-1] and text[0] in "\"'":
        return text[1:-1]
    low = text.lower()
    if low in ("true", "yes", "on"):
        return True
    if low in ("false", "no", "off"):
        return False
    if low in ("null", "~"):
        return None
    try:
        return int(text)
    except ValueError:
        pass
    try:
        return float(text)
    except ValueError:
        pass
    return text


def _flow(text: str) -> Any:
    """Read an inline flow — ``[a, b]`` or ``{k: v}``."""
    text = text.strip()
    if text.startswith("[") and text.endswith("]"):
        inner = text[1:-1].strip()
        if not inner:
            return []
        return [_flow(part) for part in _split(inner)]
    if text.startswith("{") and text.endswith("}"):
        inner = text[1:-1].strip()
        if not inner:
            return {}
        out: dict[str, Any] = {}
        for part in _split(inner):
            if ":" not in part:
                continue
            key, _, value = part.partition(":")
            out[str(_scalar(key))] = _flow(value)
        return out
    return _scalar(text)


def _split(text: str) -> list[str]:
    """Split on the commas that are not inside quotes or brackets."""
    parts: list[str] = []
    depth = 0
    quote = ""
    current = ""
    for char in text:
        if quote:
            current += char
            if char == quote:
                quote = ""
            continue
        if char in "\"'":
            quote = char
            current += char
            continue
        if char in "[{":
            depth += 1
        elif char in "]}":
            depth -= 1
        if char == "," and depth == 0:
            parts.append(current.strip())
            current = ""
            continue
        current += char
    if current.strip():
        parts.append(current.strip())
    return parts


def _block(lines: list[tuple[int, str]], start: int, indent: int) -> tuple[Any, int]:
    """Read one block — a map or a list — and return it with the next line."""
    index = start
    if index >= len(lines):
        return None, index

    if lines[index][1].startswith("- "):
        items: list[Any] = []
        while index < len(lines) and lines[index][0] == indent and lines[index][1].startswith("- "):
            body = lines[index][1][2:].strip()
            if body:
                if ":" in body and not body.startswith(("[", "{")):
                    key, _, value = body.partition(":")
                    entry: dict[str, Any] = {str(_scalar(key)): _flow(value) if value.strip() else None}
                    index += 1
                    while index < len(lines) and lines[index][0] > indent:
                        more, index = _block(lines, index, lines[index][0])
                        if isinstance(more, dict):
                            entry.update(more)
                        else:
                            break
                    items.append(entry)
                    continue
                items.append(_flow(body))
                index += 1
                continue
            index += 1
            nested, index = _block(lines, index, lines[index][0]) if index < len(lines) else (None, index)
            items.append(nested)
        return items, index

    data: dict[str, Any] = {}
    while index < len(lines):
        level, text = lines[index]
        if level < indent:
            break
        if level > indent:
            index += 1
            continue
        if ":" not in text:
            index += 1
            continue
        key, _, value = text.partition(":")
        key = str(_scalar(key))
        value = value.strip()
        if value:
            data[key] = _flow(value)
            index += 1
            continue
        index += 1
        if index < len(lines) and lines[index][0] > level:
            nested, index = _block(lines, index, lines[index][0])
            data[key] = nested
        else:
            data[key] = None
    return data, index


def safe_load(text: str) -> Any:
    """Read a YAML document."""
    lines: list[tuple[int, str]] = []
    for raw in text.splitlines():
        clean = _strip_comment(raw)
        if not clean.strip():
            continue
        if clean.strip() in ("---", "..."):
            continue
        indent = len(clean) - len(clean.lstrip(" "))
        lines.append((indent, clean.strip()))
    if not lines:
        return {}
    value, _ = _block(lines, 0, lines[0][0])
    return value if value is not None else {}


def safe_dump(data: Any, sort_keys: bool = False, allow_unicode: bool = True) -> str:
    """Write a YAML document — good enough for a config file."""
    lines: list[str] = []

    def write(value: Any, indent: int) -> None:
        pad = " " * indent
        if isinstance(value, dict):
            for key, item in value.items():
                if isinstance(item, (dict, list)) and item:
                    lines.append(f"{pad}{key}:")
                    write(item, indent + 2)
                else:
                    lines.append(f"{pad}{key}: {_dump_scalar(item)}")
        elif isinstance(value, list):
            for item in value:
                if isinstance(item, (dict, list)) and item:
                    lines.append(f"{pad}-")
                    write(item, indent + 2)
                else:
                    lines.append(f"{pad}- {_dump_scalar(item)}")

    def _dump_scalar(value: Any) -> str:
        if value is None:
            return "null"
        if isinstance(value, bool):
            return "true" if value else "false"
        if isinstance(value, (int, float)):
            return str(value)
        text = str(value)
        if text == "" or any(ch in text for ch in ":#[]{}") or text.strip() != text:
            return '"' + text.replace('"', '\\"') + '"'
        return text

    write(data, 0)
    return "\n".join(lines) + "\n"

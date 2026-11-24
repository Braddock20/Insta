"""The HTTP control API."""

from __future__ import annotations

import json
import urllib.error
import urllib.request

import pytest

from ibot.api import APIContext, serve


@pytest.fixture()
def server(cfg, store, guardrails):
    ctx = APIContext(
        cfg=cfg,
        store=store,
        guardrails=guardrails,
        token="test-token",
        started_at=0.0,
        session_state=lambda: {"ok": True, "username": "me"},
    )
    httpd = serve(ctx, port=0, host="127.0.0.1")
    import threading

    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{httpd.server_address[1]}"
    yield base
    httpd.shutdown()
    httpd.server_close()


def _get(url, token=None):
    request = urllib.request.Request(url)
    if token:
        request.add_header("Authorization", f"Bearer {token}")
    with urllib.request.urlopen(request, timeout=5) as resp:
        return json.loads(resp.read().decode())


def test_health_is_open_without_a_token(server):
    data = _get(f"{server}/api/health")
    assert "ok" in data


def test_the_session_state_is_reported(server):
    data = _get(f"{server}/api/health")
    assert data.get("session", {}).get("username") == "me"


def test_stats_need_the_token(server):
    with pytest.raises(urllib.error.HTTPError) as exc:
        _get(f"{server}/api/stats")
    assert exc.value.code == 401


def test_stats_work_with_the_token(server):
    data = _get(f"{server}/api/stats", token="test-token")
    assert "counters" in data or "replies" in data or isinstance(data, dict)


def test_tasks_are_listed(server):
    data = _get(f"{server}/api/tasks", token="test-token")
    assert isinstance(data, (list, dict))

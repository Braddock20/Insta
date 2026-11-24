"""The protocol layer and the push channel."""

from __future__ import annotations

import json

from ibot.protocol import REGISTRY, AppIdentity, ProtocolManager, identity_from_dict
from ibot.realtime import RealtimeMessage, RealtimeBridge


# ----------------------------------------------------------------- protocol
def test_the_registry_is_not_empty_and_is_newest_first():
    assert len(REGISTRY) >= 2
    assert REGISTRY[0].version >= REGISTRY[-1].version


def test_an_identity_builds_the_user_agent_instagram_expects():
    identity = REGISTRY[0]
    ua = identity.user_agent
    assert ua.startswith("Instagram ")
    assert identity.version in ua
    assert identity.version_code in ua


def test_an_identity_builds_the_device_settings():
    settings = REGISTRY[0].device_settings()
    assert settings["app_version"] == REGISTRY[0].version
    assert settings["version_code"] == REGISTRY[0].version_code
    assert settings["manufacturer"]


def test_an_identity_survives_a_round_trip():
    identity = REGISTRY[0]
    restored = identity_from_dict(identity.as_dict())
    assert restored.version == identity.version
    assert restored.app_id == identity.app_id
    assert restored.user_agent == identity.user_agent


def test_the_protocol_state_is_saved_and_restored(tmp_path):
    cfg = type("C", (), {"get": lambda self, k, d=None: d, "resolve": lambda self, k: tmp_path / "p.json"})()
    manager = ProtocolManager(cfg, settings_path=tmp_path / "protocol.json")
    manager.state.identity = REGISTRY[0]
    manager.state.checked_at = 123.0
    manager._save()

    other = ProtocolManager(cfg, settings_path=tmp_path / "protocol.json")
    identity = other.restore()
    assert identity.version == REGISTRY[0].version
    assert (tmp_path / "protocol.json").exists()


def test_an_unknown_version_still_produces_a_usable_identity():
    identity = identity_from_dict({"app_id": "1", "version": "999.0.0.1", "version_code": "7"})
    assert identity.version == "999.0.0.1"
    assert identity.user_agent.startswith("Instagram 999.0.0.1")


# ----------------------------------------------------------------- realtime
def _payload(text="hello", user_id=42, thread_id="340"):
    return {
        "event": "patch",
        "delta_type": "deltaNewMessage",
        "message": {
            "path": f"/direct_v2/threads/{thread_id}/items/999",
            "op": "add",
            "thread_id": thread_id,
            "item_id": "999",
            "message_id": "mid.$abc",
            "user_id": user_id,
            "timestamp": 1795511589456177,
            "item_type": "text",
            "text": text,
        },
    }


def test_a_push_payload_is_parsed():
    bridge = RealtimeBridge.__new__(RealtimeBridge)
    bridge.client = type("C", (), {"user_id": "1"})()
    message = bridge._parse(_payload())
    assert isinstance(message, RealtimeMessage)
    assert message.thread_id == "340"
    assert message.text == "hello"
    assert message.user_id == "42"
    assert message.is_self is False
    assert message.message_id == "mid.$abc"


def test_the_thread_id_is_taken_from_the_path_when_it_is_missing():
    bridge = RealtimeBridge.__new__(RealtimeBridge)
    bridge.client = type("C", (), {"user_id": "1"})()
    payload = _payload()
    payload["message"].pop("thread_id")
    message = bridge._parse(payload)
    assert message.thread_id == "340"


def test_our_own_message_is_marked_as_self():
    bridge = RealtimeBridge.__new__(RealtimeBridge)
    bridge.client = type("C", (), {"user_id": "42"})()
    message = bridge._parse(_payload(user_id=42))
    assert message.is_self is True


def test_a_command_is_recognised():
    assert RealtimeMessage("1", "2", "3", text="/help").is_command
    assert not RealtimeMessage("1", "2", "3", text="hello").is_command


def test_junk_payloads_are_ignored():
    bridge = RealtimeBridge.__new__(RealtimeBridge)
    bridge.client = type("C", (), {"user_id": "1"})()
    assert bridge._parse(None) is None
    assert bridge._parse({}) is None
    assert bridge._parse({"message": "not a dict"}) is None


def test_the_timestamp_is_in_seconds():
    bridge = RealtimeBridge.__new__(RealtimeBridge)
    bridge.client = type("C", (), {"user_id": "1"})()
    message = bridge._parse(_payload())
    assert 1_700_000_000 < message.timestamp < 2_000_000_000

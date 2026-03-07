"""Tests for helioy-bus server tools.

Tests run against a temporary BUS_DIR so they never touch ~/.helioy/bus/.
"""

from __future__ import annotations

import json
import time
from unittest.mock import patch

import pytest

# Point the module at a temp directory before import
BUS_DIR_PLACEHOLDER = None  # set per-test via monkeypatching


@pytest.fixture(autouse=True)
def isolated_bus(tmp_path, monkeypatch):
    """Redirect all bus paths to a temporary directory for each test."""
    bus_dir = tmp_path / "bus"
    bus_dir.mkdir()

    # Patch the module-level constants
    import server.bus_server as bm

    monkeypatch.setattr(bm, "BUS_DIR", bus_dir)
    monkeypatch.setattr(bm, "REGISTRY_DB", bus_dir / "registry.db")
    monkeypatch.setattr(bm, "INBOX_DIR", bus_dir / "inbox")

    yield bus_dir


# ── Registry ──────────────────────────────────────────────────────────────────


def test_register_agent_basic():
    import server.bus_server as bm

    result = bm.register_agent(pwd="/tmp/myproject")
    assert result["agent_id"] == "myproject"
    assert "registered_at" in result


def test_register_agent_with_tmux_target_uses_compound_id():
    import server.bus_server as bm

    result = bm.register_agent(pwd="/tmp/myproject", tmux_target="7:1.2")
    assert result["agent_id"] == "myproject:7:1.2"


def test_register_agent_explicit_id():
    import server.bus_server as bm

    result = bm.register_agent(pwd="/tmp/myproject", agent_id="custom-id")
    assert result["agent_id"] == "custom-id"


def test_register_creates_inbox(tmp_path):
    import server.bus_server as bm

    bm.register_agent(pwd="/tmp/myproject")
    inbox = bm.INBOX_DIR / "myproject"
    assert inbox.is_dir()


def test_list_agents_empty():
    import server.bus_server as bm

    agents = bm.list_agents()
    assert agents == []


def test_list_agents_after_register():
    import server.bus_server as bm

    bm.register_agent(pwd="/tmp/alpha")
    bm.register_agent(pwd="/tmp/beta")

    agents = bm.list_agents()
    ids = [a["agent_id"] for a in agents]
    assert "alpha" in ids
    assert "beta" in ids


def test_unregister_agent():
    import server.bus_server as bm

    bm.register_agent(pwd="/tmp/myproject")
    result = bm.unregister_agent("myproject")
    assert result["unregistered"] == "myproject"
    assert bm.list_agents() == []


def test_heartbeat_updates_last_seen():
    import server.bus_server as bm

    bm.register_agent(pwd="/tmp/myproject")
    time.sleep(0.01)
    result = bm.heartbeat("myproject")
    assert result["agent_id"] == "myproject"
    assert "last_seen" in result


# ── Mailbox ───────────────────────────────────────────────────────────────────


def test_send_message_to_registered_agent():
    import server.bus_server as bm

    bm.register_agent(pwd="/tmp/beta")
    result = bm.send_message(to="beta", content="hello from alpha", from_agent="alpha", nudge=False)

    assert result["delivered"] is True
    assert "beta" in result["recipients"]
    assert result["message_id"] is not None


def test_send_message_writes_json_file():
    import server.bus_server as bm

    bm.register_agent(pwd="/tmp/beta")
    bm.send_message(to="beta", content="test content", from_agent="alpha", nudge=False)

    inbox = bm.INBOX_DIR / "beta"
    files = list(inbox.glob("*.json"))
    assert len(files) == 1

    msg = json.loads(files[0].read_text())
    assert msg["from"] == "alpha"
    assert msg["to"] == "beta"
    assert msg["content"] == "test content"
    assert "id" in msg
    assert "sent_at" in msg


def test_send_message_atomic_write():
    """Message file is only present after atomic rename, never partially written."""
    import server.bus_server as bm

    bm.register_agent(pwd="/tmp/beta")
    inbox = bm.INBOX_DIR / "beta"

    bm.send_message(to="beta", content="atomic", from_agent="alpha", nudge=False)

    # No .tmp files should remain
    tmp_files = list(inbox.glob("*.tmp"))
    assert tmp_files == []


def test_send_message_recipient_not_found():
    import server.bus_server as bm

    result = bm.send_message(to="ghost", content="hello", nudge=False)
    assert result["delivered"] is False
    assert "error" in result


def test_send_message_broadcast():
    import server.bus_server as bm

    bm.register_agent(pwd="/tmp/alpha")
    bm.register_agent(pwd="/tmp/beta")

    result = bm.send_message(to="*", content="broadcast", from_agent="hub", nudge=False)
    assert result["delivered"] is True
    assert set(result["recipients"]) == {"alpha", "beta"}


def test_get_messages_returns_and_archives():
    import server.bus_server as bm

    bm.register_agent(pwd="/tmp/beta")
    bm.send_message(to="beta", content="msg1", from_agent="alpha", nudge=False)
    bm.send_message(to="beta", content="msg2", from_agent="alpha", nudge=False)

    messages = bm.get_messages("beta")
    assert len(messages) == 2
    contents = {m["content"] for m in messages}
    assert contents == {"msg1", "msg2"}

    # After reading, inbox should be empty (messages archived)
    inbox = bm.INBOX_DIR / "beta"
    assert list(inbox.glob("*.json")) == []

    # Archive should contain the messages
    archive = inbox / "archive"
    assert len(list(archive.glob("*.json"))) == 2


def test_get_messages_empty_inbox():
    import server.bus_server as bm

    result = bm.get_messages("nobody")
    assert result == []


def test_get_messages_idempotent():
    """Second call returns nothing (messages already archived)."""
    import server.bus_server as bm

    bm.register_agent(pwd="/tmp/beta")
    bm.send_message(to="beta", content="once", from_agent="alpha", nudge=False)

    bm.get_messages("beta")
    second = bm.get_messages("beta")
    assert second == []


# ── Liveness ──────────────────────────────────────────────────────────────────


def test_list_agents_prunes_dead_tmux_targets():
    """Agents with dead tmux targets are removed on list_agents."""
    import server.bus_server as bm

    # Register with a tmux_target that will report as dead
    bm.register_agent(pwd="/tmp/dead-pane", tmux_target="nosession:0.0")

    with patch.object(bm, "_tmux_pane_alive", return_value=False):
        agents = bm.list_agents()

    # Dead agent should be pruned
    assert not any(a["agent_id"] == "dead-pane" for a in agents)


def test_list_agents_keeps_agents_without_tmux_target():
    """Agents without a tmux_target are never pruned."""
    import server.bus_server as bm

    bm.register_agent(pwd="/tmp/notmux")  # no tmux_target
    agents = bm.list_agents()
    assert any(a["agent_id"] == "notmux" for a in agents)


def test_send_message_nudge_skips_dead_pane():
    """No nudge attempt if pane doesn't exist."""
    import server.bus_server as bm

    bm.register_agent(pwd="/tmp/beta", tmux_target="nosession:0.0", agent_id="beta")

    with patch.object(bm, "_tmux_pane_alive", return_value=False) as mock_alive, \
         patch.object(bm, "_tmux_nudge") as mock_nudge:
        result = bm.send_message(to="beta", content="ping", nudge=True)

    mock_nudge.assert_not_called()
    assert result["nudged"] is False


def test_send_message_nudge_suppressed_with_flag():
    """nudge=False never calls _tmux_nudge."""
    import server.bus_server as bm

    bm.register_agent(pwd="/tmp/beta", tmux_target="main:0.0", agent_id="beta")

    with patch.object(bm, "_tmux_pane_alive", return_value=True), \
         patch.object(bm, "_tmux_nudge") as mock_nudge:
        result = bm.send_message(to="beta", content="ping", nudge=False)

    mock_nudge.assert_not_called()
    assert result["nudged"] is False


def test_send_message_nudges_live_pane():
    """nudge=True with a live pane calls _tmux_nudge and reports nudged=True."""
    import server.bus_server as bm

    bm.register_agent(pwd="/tmp/beta", tmux_target="main:0.0", agent_id="beta")

    with patch.object(bm, "_tmux_pane_alive", return_value=True), \
         patch.object(bm, "_tmux_nudge", return_value=True) as mock_nudge:
        result = bm.send_message(to="beta", content="ping", nudge=True)

    mock_nudge.assert_called_once_with("main:0.0")
    assert result["nudged"] is True
    assert "beta" in result["recipients"]

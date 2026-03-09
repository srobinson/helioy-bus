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


# ── agent_type / role-based addressing ────────────────────────────────────────


def test_register_agent_stores_agent_type():
    """register_agent stores agent_type and list_agents returns it."""
    import server.bus_server as bm

    bm.register_agent(pwd="/tmp/api", agent_id="api", agent_type="backend-engineer")
    agents = bm.list_agents()
    agent = next(a for a in agents if a["agent_id"] == "api")
    assert agent["agent_type"] == "backend-engineer"


def test_register_agent_default_type_is_general():
    """agent_type defaults to 'general' when not specified."""
    import server.bus_server as bm

    bm.register_agent(pwd="/tmp/myproject")
    agents = bm.list_agents()
    assert agents[0]["agent_type"] == "general"


def test_send_message_role_addressing_delivers_to_matching_agents():
    """send_message(to='role:backend-engineer') delivers to all agents with that type."""
    import server.bus_server as bm

    bm.register_agent(pwd="/tmp/api1", agent_id="api1", agent_type="backend-engineer")
    bm.register_agent(pwd="/tmp/api2", agent_id="api2", agent_type="backend-engineer")
    bm.register_agent(pwd="/tmp/ui", agent_id="ui", agent_type="frontend-engineer")

    result = bm.send_message(
        to="role:backend-engineer", content="hello backends", from_agent="orchestrator", nudge=False
    )

    assert result["delivered"] is True
    assert set(result["recipients"]) == {"api1", "api2"}
    assert "ui" not in result["recipients"]


def test_send_message_role_addressing_excludes_sender():
    """Role send excludes the sender even if it has the matching agent_type."""
    import server.bus_server as bm

    bm.register_agent(pwd="/tmp/be1", agent_id="be1", agent_type="backend-engineer")
    bm.register_agent(pwd="/tmp/be2", agent_id="be2", agent_type="backend-engineer")

    result = bm.send_message(
        to="role:backend-engineer", content="hello", from_agent="be1", nudge=False
    )

    assert "be1" not in result["recipients"]
    assert "be2" in result["recipients"]


def test_send_message_role_not_found_returns_error():
    """Role send with no matching agents returns error, not delivered."""
    import server.bus_server as bm

    bm.register_agent(pwd="/tmp/fe", agent_id="fe", agent_type="frontend-engineer")

    result = bm.send_message(
        to="role:backend-engineer", content="hello", from_agent="orchestrator", nudge=False
    )

    assert result["delivered"] is False
    assert result["message_id"] is None
    assert "error" in result


def test_send_message_role_creates_inbox_files():
    """Role send writes one inbox file per matching agent."""
    import server.bus_server as bm

    bm.register_agent(pwd="/tmp/be1", agent_id="be1", agent_type="backend-engineer")
    bm.register_agent(pwd="/tmp/be2", agent_id="be2", agent_type="backend-engineer")

    bm.send_message(
        to="role:backend-engineer", content="task", from_agent="hub", nudge=False
    )

    for agent_id in ("be1", "be2"):
        inbox = bm.INBOX_DIR / agent_id
        files = list(inbox.glob("*.json"))
        assert len(files) == 1, f"Expected 1 message in {agent_id} inbox, got {len(files)}"


# ── End-to-end lifecycle scenarios ────────────────────────────────────────────


def test_repo_mode_lifecycle():
    """Simulates the repo-mode lifecycle: register multiple general agents,
    send between them, receive, then unregister."""
    import server.bus_server as bm

    # Register two "warroom" agents with pane-title-style IDs
    bm.register_agent(
        pwd="/tmp/fmm", agent_id="fmm:general:7:2.1", agent_type="general", tmux_target="7:2.1"
    )
    bm.register_agent(
        pwd="/tmp/helioy-bus",
        agent_id="helioy-bus:general:7:2.2",
        agent_type="general",
        tmux_target="7:2.2",
    )

    with patch.object(bm, "_tmux_pane_alive", return_value=True):
        agents = bm.list_agents()
    assert len(agents) == 2
    assert all(a["agent_type"] == "general" for a in agents)

    # Direct message between two repo-mode agents
    result = bm.send_message(
        to="helioy-bus:general:7:2.2",
        content="hi from fmm",
        from_agent="fmm:general:7:2.1",
        nudge=False,
    )
    assert result["delivered"] is True
    assert "helioy-bus:general:7:2.2" in result["recipients"]

    messages = bm.get_messages("helioy-bus:general:7:2.2")
    assert len(messages) == 1
    assert messages[0]["content"] == "hi from fmm"

    # Unregister
    bm.unregister_agent("fmm:general:7:2.1")
    bm.unregister_agent("helioy-bus:general:7:2.2")
    with patch.object(bm, "_tmux_pane_alive", return_value=True):
        assert bm.list_agents() == []


def test_role_mode_lifecycle():
    """Simulates the crew/role-mode lifecycle: register specialist agents,
    send via role addressing, receive, verify isolation."""
    import server.bus_server as bm

    # Register agents as warroom.sh would: pane-title-style IDs with agent_type
    bm.register_agent(
        pwd="/tmp/helioy-bus",
        agent_id="helioy-bus:backend-engineer:7:3.1",
        agent_type="backend-engineer",
        tmux_target="7:3.1",
    )
    bm.register_agent(
        pwd="/tmp/helioy-bus",
        agent_id="helioy-bus:frontend-engineer:7:3.2",
        agent_type="frontend-engineer",
        tmux_target="7:3.2",
    )
    bm.register_agent(
        pwd="/tmp/helioy-bus",
        agent_id="helioy-bus:general:7:3.3",
        agent_type="general",
        tmux_target="7:3.3",
    )

    # Role-based send: only backend-engineer should receive
    result = bm.send_message(
        to="role:backend-engineer",
        content="implement the auth endpoint",
        from_agent="orchestrator",
        nudge=False,
    )
    assert result["delivered"] is True
    assert result["recipients"] == ["helioy-bus:backend-engineer:7:3.1"]

    # frontend-engineer and general should not have received anything
    msgs_fe = bm.get_messages("helioy-bus:frontend-engineer:7:3.2")
    msgs_gen = bm.get_messages("helioy-bus:general:7:3.3")
    assert msgs_fe == []
    assert msgs_gen == []

    # Backend agent reads its message
    msgs_be = bm.get_messages("helioy-bus:backend-engineer:7:3.1")
    assert len(msgs_be) == 1
    assert msgs_be[0]["content"] == "implement the auth endpoint"


def test_coexistence_of_both_modes():
    """Both warroom (general) and crew (specialist) agents coexist and can
    message each other directly or via broadcast."""
    import server.bus_server as bm

    # Warroom agents (repo-mode, general)
    bm.register_agent(
        pwd="/tmp/fmm", agent_id="fmm:general:7:2.1", agent_type="general", tmux_target="7:2.1"
    )
    bm.register_agent(
        pwd="/tmp/helioy-bus",
        agent_id="helioy-bus:general:7:2.2",
        agent_type="general",
        tmux_target="7:2.2",
    )

    # Crew agents (role-mode, specialist)
    bm.register_agent(
        pwd="/tmp/helioy-bus",
        agent_id="helioy-bus:backend-engineer:7:3.1",
        agent_type="backend-engineer",
        tmux_target="7:3.1",
    )
    bm.register_agent(
        pwd="/tmp/helioy-bus",
        agent_id="helioy-bus:frontend-engineer:7:3.2",
        agent_type="frontend-engineer",
        tmux_target="7:3.2",
    )

    with patch.object(bm, "_tmux_pane_alive", return_value=True):
        agents = bm.list_agents()
    assert len(agents) == 4

    # Broadcast from orchestrator reaches all four agents
    result = bm.send_message(
        to="*", content="standup time", from_agent="orchestrator", nudge=False
    )
    assert set(result["recipients"]) == {
        "fmm:general:7:2.1",
        "helioy-bus:general:7:2.2",
        "helioy-bus:backend-engineer:7:3.1",
        "helioy-bus:frontend-engineer:7:3.2",
    }

    # Role-based send reaches only specialists
    result2 = bm.send_message(
        to="role:backend-engineer",
        content="deploy the API",
        from_agent="fmm:general:7:2.1",
        nudge=False,
    )
    assert result2["recipients"] == ["helioy-bus:backend-engineer:7:3.1"]


def test_adhoc_session_fallback_identity():
    """An ad-hoc claude session (no warroom) registers with basename identity."""
    import server.bus_server as bm

    # Simulate ad-hoc registration as bus-register.sh would derive it
    bm.register_agent(pwd="/tmp/myproject", agent_id="myproject", agent_type="general")

    agents = bm.list_agents()
    assert len(agents) == 1
    agent = agents[0]
    assert agent["agent_id"] == "myproject"
    assert agent["agent_type"] == "general"

    # Can receive direct messages
    result = bm.send_message(
        to="myproject", content="hello from peer", from_agent="other", nudge=False
    )
    assert result["delivered"] is True

    bm.unregister_agent("myproject")
    assert bm.list_agents() == []


def test_profile_migration_from_shell_hook_created_db(tmp_path, monkeypatch):
    """register_agent succeeds even when the DB was first created by bus-register.sh
    (which doesn't include the profile column).

    Reproduces the missing-migration bug: if the shell hook runs first and
    creates the agents table without the profile column, the MCP server must
    add it via ALTER TABLE before attempting the INSERT OR REPLACE.
    """
    import sqlite3
    import server.bus_server as bm

    bus_dir = tmp_path / "bus_legacy"
    bus_dir.mkdir()
    monkeypatch.setattr(bm, "BUS_DIR", bus_dir)
    monkeypatch.setattr(bm, "REGISTRY_DB", bus_dir / "registry.db")
    monkeypatch.setattr(bm, "INBOX_DIR", bus_dir / "inbox")

    # Simulate the DB as bus-register.sh creates it: no profile column.
    conn = sqlite3.connect(str(bus_dir / "registry.db"))
    conn.executescript("""
        PRAGMA journal_mode=WAL;
        CREATE TABLE IF NOT EXISTS agents (
            agent_id      TEXT PRIMARY KEY,
            cwd           TEXT NOT NULL,
            tmux_target   TEXT NOT NULL DEFAULT '',
            pid           INTEGER,
            session_id    TEXT NOT NULL DEFAULT '',
            agent_type    TEXT NOT NULL DEFAULT 'general',
            registered_at TEXT NOT NULL,
            last_seen     TEXT NOT NULL
        );
    """)
    conn.commit()
    conn.close()

    # MCP register_agent must succeed — migration adds profile column.
    result = bm.register_agent(
        pwd="/tmp/myproject",
        agent_id="myproject",
        agent_type="general",
        profile={"owns": ["myproject"]},
    )
    assert result["agent_id"] == "myproject"

    agents = bm.list_agents()
    assert len(agents) == 1
    assert agents[0].get("profile") == {"owns": ["myproject"]}

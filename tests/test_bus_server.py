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

    # Patch the source module (_db) where db() and path constants live
    import server._db as _db_mod

    monkeypatch.setattr(_db_mod, "BUS_DIR", bus_dir)
    monkeypatch.setattr(_db_mod, "REGISTRY_DB", bus_dir / "registry.db")
    monkeypatch.setattr(_db_mod, "INBOX_DIR", bus_dir / "inbox")

    # Also patch bus_server's imported copies (used by tool functions and test assertions)
    import server.bus_server as bm

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
    import server._db as _db_mod
    import server.bus_server as bm

    bm.register_agent(pwd="/tmp/myproject")
    inbox = _db_mod.INBOX_DIR / "myproject"
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


def test_list_agents_filter_by_session():
    """tmux_filter='session' returns only agents in that session."""
    import server.bus_server as bm

    bm.register_agent(pwd="/tmp/a", tmux_target="work:0.0", agent_id="a:work:0.0")
    bm.register_agent(pwd="/tmp/b", tmux_target="work:1.0", agent_id="b:work:1.0")
    bm.register_agent(pwd="/tmp/c", tmux_target="other:0.0", agent_id="c:other:0.0")

    with patch.object(bm, "_tmux_pane_alive", return_value=True):
        agents = bm.list_agents(tmux_filter="work")

    ids = [a["agent_id"] for a in agents]
    assert "a:work:0.0" in ids
    assert "b:work:1.0" in ids
    assert "c:other:0.0" not in ids


def test_list_agents_filter_by_session_and_window():
    """tmux_filter='session:window' narrows to a specific window."""
    import server.bus_server as bm

    bm.register_agent(pwd="/tmp/a", tmux_target="work:0.0", agent_id="a:work:0.0")
    bm.register_agent(pwd="/tmp/b", tmux_target="work:0.1", agent_id="b:work:0.1")
    bm.register_agent(pwd="/tmp/c", tmux_target="work:1.0", agent_id="c:work:1.0")

    with patch.object(bm, "_tmux_pane_alive", return_value=True):
        agents = bm.list_agents(tmux_filter="work:0")

    ids = [a["agent_id"] for a in agents]
    assert "a:work:0.0" in ids
    assert "b:work:0.1" in ids
    assert "c:work:1.0" not in ids


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
    import server._db as _db_mod
    import server.bus_server as bm

    bm.register_agent(pwd="/tmp/beta")
    bm.send_message(to="beta", content="test content", from_agent="alpha", nudge=False)

    inbox = _db_mod.INBOX_DIR / "beta"
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
    import server._db as _db_mod
    import server.bus_server as bm

    bm.register_agent(pwd="/tmp/beta")
    inbox = _db_mod.INBOX_DIR / "beta"

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
    import server._db as _db_mod
    import server.bus_server as bm

    bm.register_agent(pwd="/tmp/beta")
    bm.send_message(to="beta", content="msg1", from_agent="alpha", nudge=False)
    bm.send_message(to="beta", content="msg2", from_agent="alpha", nudge=False)

    messages = bm.get_messages("beta")
    assert len(messages) == 2
    contents = {m["content"] for m in messages}
    assert contents == {"msg1", "msg2"}

    # After reading, inbox should be empty (messages archived)
    inbox = _db_mod.INBOX_DIR / "beta"
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
    import server._db as _db_mod
    import server.bus_server as bm

    bm.register_agent(pwd="/tmp/be1", agent_id="be1", agent_type="backend-engineer")
    bm.register_agent(pwd="/tmp/be2", agent_id="be2", agent_type="backend-engineer")

    bm.send_message(
        to="role:backend-engineer", content="task", from_agent="hub", nudge=False
    )

    for agent_id in ("be1", "be2"):
        inbox = _db_mod.INBOX_DIR / agent_id
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

    import server._db as _db_mod
    import server.bus_server as bm

    bus_dir = tmp_path / "bus_legacy"
    bus_dir.mkdir()
    monkeypatch.setattr(_db_mod, "BUS_DIR", bus_dir)
    monkeypatch.setattr(_db_mod, "REGISTRY_DB", bus_dir / "registry.db")
    monkeypatch.setattr(_db_mod, "INBOX_DIR", bus_dir / "inbox")
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

    # MCP register_agent must succeed -- migration adds profile column.
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


# ── Warroom: _parse_frontmatter ──────────────────────────────────────────────


def test_parse_frontmatter_basic(tmp_path):
    """Parses scalar fields from YAML frontmatter."""
    import server._warroom as wr

    md = tmp_path / "agent.md"
    md.write_text('---\nname: backend-engineer\ndescription: "Builds APIs"\nmodel: opus\n---\nBody\n')

    result = wr._parse_frontmatter(md)
    assert result is not None
    assert result["name"] == "backend-engineer"
    assert result["description"] == "Builds APIs"
    assert result["model"] == "opus"


def test_parse_frontmatter_no_frontmatter(tmp_path):
    """Returns None when file has no frontmatter."""
    import server._warroom as wr

    md = tmp_path / "plain.md"
    md.write_text("# Just a heading\nNo frontmatter here.\n")
    assert wr._parse_frontmatter(md) is None


def test_parse_frontmatter_unquoted_values(tmp_path):
    """Handles unquoted scalar values."""
    import server._warroom as wr

    md = tmp_path / "agent.md"
    md.write_text("---\nname: my-agent\nmodel: sonnet\ncolor: green\n---\n")

    result = wr._parse_frontmatter(md)
    assert result["name"] == "my-agent"
    assert result["model"] == "sonnet"
    assert result["color"] == "green"


def test_parse_frontmatter_missing_file(tmp_path):
    """Returns None for a non-existent file."""
    import server._warroom as wr

    assert wr._parse_frontmatter(tmp_path / "nope.md") is None


# ── Warroom: _scan_agent_types ───────────────────────────────────────────────


@pytest.fixture()
def fake_plugins(tmp_path, monkeypatch):
    """Create a fake plugin cache with known agent definitions."""
    import server._db as _db_mod
    import server._warroom as wr

    cache = tmp_path / "plugins" / "cache"

    # helioy-tools agents
    ht = cache / "helioy" / "helioy-tools" / "0.1.0" / "agents"
    ht.mkdir(parents=True)
    (ht / "backend-engineer.md").write_text(
        '---\nname: backend-engineer\ndescription: "Builds APIs and services"\nmodel: opus\n---\n'
    )
    (ht / "frontend-engineer.md").write_text(
        '---\nname: frontend-engineer\ndescription: "Builds UI components"\nmodel: sonnet\n---\n'
    )

    # pr-review-toolkit agents
    prt = cache / "official" / "pr-review-toolkit" / "abc123" / "agents"
    prt.mkdir(parents=True)
    (prt / "code-reviewer.md").write_text(
        '---\nname: code-reviewer\ndescription: "Reviews code"\nmodel: opus\n---\n'
    )

    # voltagent agents (lower priority namespace)
    va = cache / "voltagent" / "voltagent-lang" / "1.0.0" / "agents"
    va.mkdir(parents=True)
    (va / "backend-engineer.md").write_text(
        '---\nname: backend-engineer\ndescription: "Voltagent backend"\nmodel: sonnet\n---\n'
    )

    monkeypatch.setattr(_db_mod, "PLUGINS_CACHE", cache)
    # Clear cache so tests see fresh state
    wr._agent_types_cache.clear()
    wr._agent_types_cache_ts = 0.0

    yield cache

    # Reset cache after test
    wr._agent_types_cache.clear()
    wr._agent_types_cache_ts = 0.0


def test_scan_agent_types_finds_all(fake_plugins):
    """Scan discovers agents across multiple namespaces."""
    import server._warroom as wr

    types = wr._scan_agent_types()
    names = {t["qualified_name"] for t in types}
    assert "helioy-tools:backend-engineer" in names
    assert "helioy-tools:frontend-engineer" in names
    assert "pr-review-toolkit:code-reviewer" in names
    assert "voltagent-lang:backend-engineer" in names


def test_scan_agent_types_cached(fake_plugins):
    """Second call returns cached results (same list identity)."""
    import server._warroom as wr

    first = wr._scan_agent_types()
    second = wr._scan_agent_types()
    assert first is second


def test_scan_agent_types_deduplicates_versions(tmp_path, monkeypatch):
    """When multiple versions of the same plugin exist, keeps the newest."""
    import time as _time

    import server._db as _db_mod
    import server._warroom as wr

    cache = tmp_path / "cache"
    old = cache / "org" / "myplugin" / "v1" / "agents"
    old.mkdir(parents=True)
    (old / "my-agent.md").write_text(
        '---\nname: my-agent\ndescription: "old"\nmodel: sonnet\n---\n'
    )

    _time.sleep(0.05)  # ensure mtime differs

    new = cache / "org" / "myplugin" / "v2" / "agents"
    new.mkdir(parents=True)
    (new / "my-agent.md").write_text(
        '---\nname: my-agent\ndescription: "new"\nmodel: opus\n---\n'
    )

    monkeypatch.setattr(_db_mod, "PLUGINS_CACHE", cache)
    wr._agent_types_cache.clear()
    wr._agent_types_cache_ts = 0.0

    types = wr._scan_agent_types()
    matches = [t for t in types if t["name"] == "my-agent"]
    assert len(matches) == 1
    assert matches[0]["summary"] == "new"
    assert matches[0]["model"] == "opus"

    wr._agent_types_cache.clear()
    wr._agent_types_cache_ts = 0.0


# ── Warroom: _resolve_agent_type ─────────────────────────────────────────────


def test_resolve_qualified_name(fake_plugins):
    """Qualified name resolves to exact match."""
    import server._warroom as wr

    result = wr._resolve_agent_type("helioy-tools:backend-engineer")
    assert result is not None
    assert result["qualified_name"] == "helioy-tools:backend-engineer"


def test_resolve_short_name_priority(fake_plugins):
    """Short name resolves to helioy-tools over voltagent."""
    import server._warroom as wr

    result = wr._resolve_agent_type("backend-engineer")
    assert result is not None
    assert result["namespace"] == "helioy-tools"


def test_resolve_unique_short_name(fake_plugins):
    """Short name with only one match resolves directly."""
    import server._warroom as wr

    result = wr._resolve_agent_type("code-reviewer")
    assert result is not None
    assert result["namespace"] == "pr-review-toolkit"


def test_resolve_unknown_returns_none(fake_plugins):
    """Unknown name returns None."""
    import server._warroom as wr

    assert wr._resolve_agent_type("nonexistent-agent") is None


# ── Warroom: warroom_discover ────────────────────────────────────────────────


def test_warroom_discover_all(fake_plugins):
    """Discover with no filters returns all agents."""
    import server.bus_server as bm

    result = bm.warroom_discover()
    assert result["total"] >= 4
    assert "helioy-tools" in result["namespaces"]


def test_warroom_discover_query_filter(fake_plugins):
    """Query filters by name and description substring."""
    import server.bus_server as bm

    result = bm.warroom_discover(query="backend")
    assert result["total"] >= 1
    assert all(
        "backend" in a["name"].lower() or "backend" in a.get("summary", "").lower()
        for a in result["agents"]
    )


def test_warroom_discover_namespace_filter(fake_plugins):
    """Namespace filter restricts to a single plugin."""
    import server.bus_server as bm

    result = bm.warroom_discover(namespace="helioy-tools")
    assert all(a["namespace"] == "helioy-tools" for a in result["agents"])


def test_warroom_discover_limit(fake_plugins):
    """Limit caps the number of returned results."""
    import server.bus_server as bm

    result = bm.warroom_discover(limit=1)
    assert len(result["agents"]) == 1
    assert result["total"] >= 4  # total count unaffected by limit


# ── Warroom: presets ─────────────────────────────────────────────────────────


def test_warroom_presets_empty(tmp_path, monkeypatch):
    """Returns empty list when no presets directory exists."""
    import server.bus_server as bm

    monkeypatch.setattr(bm, "PRESETS_DIR", tmp_path / "nonexistent")
    result = bm.warroom_presets()
    assert result == {"presets": []}


def test_warroom_save_and_list_preset(tmp_path, monkeypatch):
    """Save a preset and verify it appears in the listing."""
    import server.bus_server as bm

    presets_dir = tmp_path / "presets"
    monkeypatch.setattr(bm, "PRESETS_DIR", presets_dir)

    save_result = bm.warroom_save_preset(
        name="design-team",
        agents=["ux-designer", "frontend-engineer", "visual-designer"],
        description="Full design team",
        tags=["design", "ui"],
    )
    assert save_result["saved"] == "design-team"

    list_result = bm.warroom_presets()
    assert len(list_result["presets"]) == 1
    preset = list_result["presets"][0]
    assert preset["name"] == "design-team"
    assert preset["agents"] == ["ux-designer", "frontend-engineer", "visual-designer"]
    assert preset["tags"] == ["design", "ui"]


def test_warroom_save_preset_validation():
    """Rejects invalid preset names."""
    import server.bus_server as bm

    result = bm.warroom_save_preset(name="", agents=["be"])
    assert "error" in result

    result = bm.warroom_save_preset(name="valid-name", agents=[])
    assert "error" in result


# ── Warroom: schema ──────────────────────────────────────────────────────────


def test_warroom_schema_created():
    """warrooms and warroom_members tables exist after db init."""
    from server._db import db

    with db() as conn:
        tables = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
        ).fetchall()
        table_names = {r["name"] for r in tables}
        assert "warrooms" in table_names
        assert "warroom_members" in table_names


# ── Warroom: warroom_spawn (mocked tmux) ─────────────────────────────────────


def test_warroom_spawn_validates_agent_types(fake_plugins, monkeypatch):
    """Spawn rejects unknown agent types with suggestions."""
    import server.bus_server as bm

    monkeypatch.setenv("TMUX", "/tmp/tmux-1000/default,12345,0")
    monkeypatch.setattr(bm, "_tmux_check", lambda *args: "main")

    result = bm.warroom_spawn(name="test-room", agents=["nonexistent-agent-xyz"])
    assert "error" in result
    assert result["error"] == "Unknown agent types"


def test_warroom_spawn_requires_tmux(fake_plugins, monkeypatch):
    """Spawn fails cleanly outside tmux."""
    import server.bus_server as bm

    monkeypatch.delenv("TMUX", raising=False)
    result = bm.warroom_spawn(name="test", agents=["backend-engineer"])
    assert "error" in result
    assert "tmux" in result["error"].lower()


def test_warroom_spawn_validates_name():
    """Spawn rejects invalid warroom names."""
    import server.bus_server as bm

    result = bm.warroom_spawn(name="", agents=["be"])
    assert "error" in result

    result = bm.warroom_spawn(name="has spaces", agents=["be"])
    assert "error" in result


def test_warroom_spawn_validates_agent_count():
    """Spawn rejects more than 8 agents."""
    import server.bus_server as bm

    result = bm.warroom_spawn(name="big", agents=["a"] * 9)
    assert "error" in result
    assert "8" in result["error"]


def test_warroom_spawn_validates_layout(fake_plugins, monkeypatch):
    """Spawn rejects invalid layout values."""
    import server.bus_server as bm

    monkeypatch.setenv("TMUX", "/tmp/tmux-1000/default,12345,0")
    result = bm.warroom_spawn(name="test", agents=["backend-engineer"], layout="invalid")
    assert "error" in result
    assert "layout" in result["error"].lower()


def test_warroom_spawn_with_mocked_tmux(fake_plugins, monkeypatch):
    """Full spawn flow with mocked tmux calls records warroom in DB."""
    import server.bus_server as bm
    from server._db import db

    monkeypatch.setenv("TMUX", "/tmp/tmux-1000/default,12345,0")

    call_log = []

    def mock_tmux_check(*args):
        call_log.append(args)
        cmd = args[0]
        if cmd == "display-message":
            if "session_name" in args[-1]:
                return "main"
            return "main:1.0"
        if cmd in ("new-window", "split-window"):
            return "%42"
        return ""

    monkeypatch.setattr(bm, "_tmux_check", mock_tmux_check)
    monkeypatch.setattr(bm, "_spawn_pane", lambda **kw: {
        "agent_type": kw["agent_type"],
        "qualified_name": kw["qualified_name"],
        "tmux_target": f"main:1.{0 if kw['is_first'] else 1}",
        "pane_id": f"%{42 + (0 if kw['is_first'] else 1)}",
    })

    result = bm.warroom_spawn(
        name="eng",
        agents=["backend-engineer", "frontend-engineer"],
        cwd="/tmp/project",
    )

    assert result["warroom_id"] == "eng"
    assert len(result["members"]) == 2
    assert result["members"][0]["qualified_name"] == "helioy-tools:backend-engineer"
    assert result["members"][1]["qualified_name"] == "helioy-tools:frontend-engineer"

    # Verify DB state
    with db() as conn:
        wr = conn.execute("SELECT * FROM warrooms WHERE warroom_id = 'eng'").fetchone()
        assert wr is not None
        assert wr["status"] == "active"
        members = conn.execute(
            "SELECT * FROM warroom_members WHERE warroom_id = 'eng'"
        ).fetchall()
        assert len(members) == 2


# ── Warroom: warroom_kill ────────────────────────────────────────────────────


def test_warroom_kill_removes_from_db(monkeypatch):
    """Kill removes warroom and members from the database."""
    from server._db import _now, db

    import server.bus_server as bm

    # Insert a warroom directly
    now = _now()
    with db() as conn:
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute(
            "INSERT INTO warrooms (warroom_id, tmux_session, tmux_window, cwd, created_at, status) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            ("test-wr", "main", "test-wr", "/tmp", now, "active"),
        )
        conn.execute(
            "INSERT INTO warroom_members "
            "(warroom_id, agent_type, tmux_target, pane_id, spawned_at) "
            "VALUES (?, ?, ?, ?, ?)",
            ("test-wr", "helioy-tools:backend-engineer", "main:1.0", "%10", now),
        )

    result = bm.warroom_kill(name="test-wr")
    assert "test-wr" in result["killed"]

    with db() as conn:
        assert conn.execute(
            "SELECT * FROM warrooms WHERE warroom_id = 'test-wr'"
        ).fetchone() is None
        assert conn.execute(
            "SELECT * FROM warroom_members WHERE warroom_id = 'test-wr'"
        ).fetchall() == []


def test_warroom_kill_requires_name_or_all():
    """Kill requires either a name or kill_all flag."""
    import server.bus_server as bm

    result = bm.warroom_kill()
    assert "error" in result


# ── Warroom: warroom_status ──────────────────────────────────────────────────


def test_warroom_status_cross_references_agents(monkeypatch):
    """Status cross-references warroom members with registered agents."""
    from server._db import _now, db

    import server.bus_server as bm

    now = _now()
    with db() as conn:
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute(
            "INSERT INTO warrooms (warroom_id, tmux_session, tmux_window, cwd, created_at, status) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            ("status-wr", "main", "status-wr", "/tmp", now, "active"),
        )
        conn.execute(
            "INSERT INTO warroom_members "
            "(warroom_id, agent_type, tmux_target, pane_id, spawned_at) "
            "VALUES (?, ?, ?, ?, ?)",
            ("status-wr", "helioy-tools:backend-engineer", "main:2.0", "%20", now),
        )
        # Register a matching agent
        conn.execute(
            "INSERT INTO agents "
            "(agent_id, cwd, tmux_target, pid, registered_at, last_seen) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            ("project:helioy-tools:backend-engineer:main:2.0", "/tmp", "main:2.0", 1234, now, now),
        )

    monkeypatch.setattr(bm, "_tmux_pane_alive", lambda t: True)

    statuses = bm.warroom_status(name="status-wr")
    assert len(statuses) == 1
    wr = statuses[0]
    assert wr["warroom_id"] == "status-wr"
    assert len(wr["members"]) == 1
    member = wr["members"][0]
    assert member["registered"] is True
    assert member["pane_alive"] is True
    assert member["agent_id"] == "project:helioy-tools:backend-engineer:main:2.0"


# ── Warroom: warroom_add ─────────────────────────────────────────────────────


def test_warroom_add_to_existing(fake_plugins, monkeypatch):
    """Add an agent to an existing warroom."""
    from server._db import _now, db

    import server.bus_server as bm

    now = _now()
    # Create warroom with one member
    with db() as conn:
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute(
            "INSERT INTO warrooms (warroom_id, tmux_session, tmux_window, cwd, created_at, status) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            ("add-test", "main", "add-test", "/tmp/project", now, "active"),
        )
        conn.execute(
            "INSERT INTO warroom_members "
            "(warroom_id, agent_type, tmux_target, pane_id, spawned_at) "
            "VALUES (?, ?, ?, ?, ?)",
            ("add-test", "helioy-tools:backend-engineer", "main:1.0", "%10", now),
        )

    monkeypatch.setattr(bm, "_spawn_pane", lambda **kw: {
        "agent_type": kw["agent_type"],
        "qualified_name": kw["qualified_name"],
        "tmux_target": "main:1.1",
        "pane_id": "%11",
    })

    result = bm.warroom_add(name="add-test", agent="frontend-engineer")
    assert result["warroom_id"] == "add-test"
    assert result["added"]["qualified_name"] == "helioy-tools:frontend-engineer"
    assert result["member_count"] == 2


def test_warroom_add_duplicate_rejected(fake_plugins, monkeypatch):
    """Adding a duplicate agent type returns an error."""
    from server._db import _now, db

    import server.bus_server as bm

    now = _now()
    with db() as conn:
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute(
            "INSERT INTO warrooms (warroom_id, tmux_session, tmux_window, cwd, created_at, status) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            ("dup-test", "main", "dup-test", "/tmp/project", now, "active"),
        )
        conn.execute(
            "INSERT INTO warroom_members "
            "(warroom_id, agent_type, tmux_target, pane_id, spawned_at) "
            "VALUES (?, ?, ?, ?, ?)",
            ("dup-test", "helioy-tools:backend-engineer", "main:1.0", "%10", now),
        )

    result = bm.warroom_add(name="dup-test", agent="backend-engineer")
    assert "error" in result
    assert "already" in result["error"].lower()


def test_warroom_add_nonexistent_warroom(fake_plugins):
    """Adding to a non-existent warroom returns an error."""
    import server.bus_server as bm

    result = bm.warroom_add(name="ghost-room", agent="backend-engineer")
    assert "error" in result
    assert "ghost-room" in result["error"]


def test_warroom_add_unknown_agent_type(fake_plugins, monkeypatch):
    """Adding an unknown agent type returns error with suggestions."""
    from server._db import _now, db

    import server.bus_server as bm

    now = _now()
    with db() as conn:
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute(
            "INSERT INTO warrooms (warroom_id, tmux_session, tmux_window, cwd, created_at, status) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            ("type-test", "main", "type-test", "/tmp", now, "active"),
        )

    result = bm.warroom_add(name="type-test", agent="nonexistent-xyz")
    assert "error" in result
    assert result["error"] == "Unknown agent type"


# ── Warroom: warroom_remove ──────────────────────────────────────────────────


def test_warroom_remove_agent(fake_plugins, monkeypatch):
    """Remove an agent from a warroom."""
    from server._db import _now, db

    import server.bus_server as bm

    now = _now()
    with db() as conn:
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute(
            "INSERT INTO warrooms (warroom_id, tmux_session, tmux_window, cwd, created_at, status) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            ("rm-test", "main", "rm-test", "/tmp", now, "active"),
        )
        conn.execute(
            "INSERT INTO warroom_members "
            "(warroom_id, agent_type, tmux_target, pane_id, spawned_at) "
            "VALUES (?, ?, ?, ?, ?)",
            ("rm-test", "helioy-tools:backend-engineer", "main:1.0", "%10", now),
        )
        conn.execute(
            "INSERT INTO warroom_members "
            "(warroom_id, agent_type, tmux_target, pane_id, spawned_at) "
            "VALUES (?, ?, ?, ?, ?)",
            ("rm-test", "helioy-tools:frontend-engineer", "main:1.1", "%11", now),
        )

    result = bm.warroom_remove(name="rm-test", agent="backend-engineer")
    assert result["warroom_id"] == "rm-test"
    assert result["removed"] == "helioy-tools:backend-engineer"
    assert result["remaining_members"] == 1
    assert result["warroom_killed"] is False


def test_warroom_remove_last_agent_kills_warroom(fake_plugins, monkeypatch):
    """Removing the last agent marks warroom as killed."""
    from server._db import _now, db

    import server.bus_server as bm

    now = _now()
    with db() as conn:
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute(
            "INSERT INTO warrooms (warroom_id, tmux_session, tmux_window, cwd, created_at, status) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            ("last-test", "main", "last-test", "/tmp", now, "active"),
        )
        conn.execute(
            "INSERT INTO warroom_members "
            "(warroom_id, agent_type, tmux_target, pane_id, spawned_at) "
            "VALUES (?, ?, ?, ?, ?)",
            ("last-test", "helioy-tools:backend-engineer", "main:1.0", "%10", now),
        )

    result = bm.warroom_remove(name="last-test", agent="backend-engineer")
    assert result["remaining_members"] == 0
    assert result["warroom_killed"] is True

    # Verify DB state
    with db() as conn:
        wr = conn.execute("SELECT status FROM warrooms WHERE warroom_id = 'last-test'").fetchone()
        assert wr["status"] == "killed"


def test_warroom_remove_nonexistent_agent(fake_plugins):
    """Removing an agent not in the warroom returns an error."""
    from server._db import _now, db

    import server.bus_server as bm

    now = _now()
    with db() as conn:
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute(
            "INSERT INTO warrooms (warroom_id, tmux_session, tmux_window, cwd, created_at, status) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            ("no-agent", "main", "no-agent", "/tmp", now, "active"),
        )

    result = bm.warroom_remove(name="no-agent", agent="backend-engineer")
    assert "error" in result


# ── Token tracking: schema migration ─────────────────────────────────────────


def test_token_usage_column_exists():
    """token_usage column exists in agents table after db init."""
    from server._db import db

    with db() as conn:
        # Insert a row and verify token_usage defaults to '{}'
        conn.execute(
            "INSERT INTO agents (agent_id, cwd, registered_at, last_seen) VALUES (?, ?, ?, ?)",
            ("test-token", "/tmp", "2026-01-01", "2026-01-01"),
        )
        row = conn.execute(
            "SELECT token_usage FROM agents WHERE agent_id = 'test-token'"
        ).fetchone()
        assert row["token_usage"] == "{}"


def test_token_usage_migration_from_older_schema(tmp_path, monkeypatch):
    """token_usage column is added via migration to older databases."""
    import sqlite3

    import server._db as _db_mod

    bus_dir = tmp_path / "bus_old"
    bus_dir.mkdir()
    monkeypatch.setattr(_db_mod, "BUS_DIR", bus_dir)
    monkeypatch.setattr(_db_mod, "REGISTRY_DB", bus_dir / "registry.db")

    # Create DB without token_usage column
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
            profile       TEXT,
            registered_at TEXT NOT NULL,
            last_seen     TEXT NOT NULL
        );
    """)
    conn.execute(
        "INSERT INTO agents (agent_id, cwd, registered_at, last_seen) VALUES (?, ?, ?, ?)",
        ("old-agent", "/tmp", "2026-01-01", "2026-01-01"),
    )
    conn.commit()
    conn.close()

    # Open with _init_db migration
    with _db_mod.db() as conn:
        row = conn.execute(
            "SELECT token_usage FROM agents WHERE agent_id = 'old-agent'"
        ).fetchone()
        assert row["token_usage"] == "{}"


# ── Token tracking: list_agents includes token_usage ─────────────────────────


def test_list_agents_includes_token_usage():
    """list_agents returns parsed token_usage JSON."""
    from server._db import db

    import server.bus_server as bm

    token_data = '{"total_input": 50000, "total_output": 3000, "limit": 200000, "percent": 25.0}'
    bm.register_agent(pwd="/tmp/tracked", agent_id="tracked")
    with db() as conn:
        conn.execute(
            "UPDATE agents SET token_usage = ? WHERE agent_id = 'tracked'",
            (token_data,),
        )

    agents = bm.list_agents()
    agent = next(a for a in agents if a["agent_id"] == "tracked")
    assert isinstance(agent["token_usage"], dict)
    assert agent["token_usage"]["total_input"] == 50000
    assert agent["token_usage"]["percent"] == 25.0


def test_list_agents_empty_token_usage():
    """list_agents handles empty token_usage gracefully."""
    import server.bus_server as bm

    bm.register_agent(pwd="/tmp/fresh", agent_id="fresh")
    agents = bm.list_agents()
    agent = next(a for a in agents if a["agent_id"] == "fresh")
    # Empty '{}' string should remain as-is (not parsed into dict since it's falsy-ish)
    assert agent["token_usage"] in ("{}", {})


# ── Token tracking: warroom_status includes token_usage ──────────────────────


def test_warroom_status_includes_token_usage(monkeypatch):
    """warroom_status includes token_usage in member dicts."""
    from server._db import _now, db

    import server.bus_server as bm

    now = _now()
    token_data = '{"total_input": 85000, "total_output": 5000, "limit": 200000, "percent": 42.5}'

    with db() as conn:
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute(
            "INSERT INTO warrooms (warroom_id, tmux_session, tmux_window, cwd, created_at, status) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            ("token-wr", "main", "token-wr", "/tmp", now, "active"),
        )
        conn.execute(
            "INSERT INTO warroom_members "
            "(warroom_id, agent_type, tmux_target, pane_id, spawned_at) "
            "VALUES (?, ?, ?, ?, ?)",
            ("token-wr", "helioy-tools:backend-engineer", "main:3.0", "%30", now),
        )
        conn.execute(
            "INSERT INTO agents "
            "(agent_id, cwd, tmux_target, pid, registered_at, last_seen, token_usage) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            ("proj:be:main:3.0", "/tmp", "main:3.0", 1234, now, now, token_data),
        )

    monkeypatch.setattr(bm, "_tmux_pane_alive", lambda t: True)

    statuses = bm.warroom_status(name="token-wr")
    member = statuses[0]["members"][0]
    assert isinstance(member["token_usage"], dict)
    assert member["token_usage"]["total_input"] == 85000
    assert member["token_usage"]["percent"] == 42.5


# ── Token tracking: whoami includes token_usage ──────────────────────────────


def test_whoami_includes_token_usage(monkeypatch):
    """whoami returns parsed token_usage."""
    from server._db import db

    import server.bus_server as bm

    token_data = '{"total_input": 20000, "total_output": 1500, "limit": 200000, "percent": 10.0}'
    bm.register_agent(pwd="/tmp/myproj", agent_id="myproj")
    with db() as conn:
        conn.execute(
            "UPDATE agents SET token_usage = ? WHERE agent_id = 'myproj'",
            (token_data,),
        )

    monkeypatch.setattr(bm, "_self_agent_id", lambda: "myproj")
    result = bm.whoami()
    assert isinstance(result["token_usage"], dict)
    assert result["token_usage"]["total_input"] == 20000

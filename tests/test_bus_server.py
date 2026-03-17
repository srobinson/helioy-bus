"""Tests for helioy-bus messaging and registry tools.

Tests run against a temporary BUS_DIR via the shared isolated_bus fixture
in conftest.py.
"""

from __future__ import annotations

import json
import time
from unittest.mock import patch

import pytest


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
    result = bm.send_message(
        to="beta", content="hello", from_agent="alpha", nudge=False,
    )
    assert result["delivered"] is True
    assert "beta" in result["recipients"]


def test_send_message_writes_json_file():
    import server._db as _db_mod
    import server.bus_server as bm

    bm.register_agent(pwd="/tmp/target")
    bm.send_message(to="target", content="payload", from_agent="src", nudge=False)

    inbox = _db_mod.INBOX_DIR / "target"
    files = list(inbox.glob("*.json"))
    assert len(files) == 1
    data = json.loads(files[0].read_text())
    assert data["content"] == "payload"
    assert data["from"] == "src"


def test_send_message_atomic_write():
    """No .tmp files left after a successful send."""
    import server._db as _db_mod
    import server.bus_server as bm

    bm.register_agent(pwd="/tmp/target")
    bm.send_message(to="target", content="x", from_agent="y", nudge=False)

    inbox = _db_mod.INBOX_DIR / "target"
    tmp_files = list(inbox.glob("*.tmp"))
    assert tmp_files == []


def test_send_message_recipient_not_found():
    import server.bus_server as bm

    result = bm.send_message(to="ghost", content="hi", from_agent="me", nudge=False)
    assert result["delivered"] is False
    assert "error" in result


def test_send_message_broadcast():
    import server.bus_server as bm

    bm.register_agent(pwd="/tmp/a")
    bm.register_agent(pwd="/tmp/b")
    result = bm.send_message(to="*", content="hello all", from_agent="sender", nudge=False)
    assert set(result["recipients"]) == {"a", "b"}


def test_get_messages_returns_and_archives():
    import server._db as _db_mod
    import server.bus_server as bm

    bm.register_agent(pwd="/tmp/reader")
    bm.send_message(to="reader", content="msg1", from_agent="w", nudge=False)
    bm.send_message(to="reader", content="msg2", from_agent="w", nudge=False)

    messages = bm.get_messages("reader")
    assert len(messages) == 2
    assert messages[0]["content"] == "msg1"
    assert messages[1]["content"] == "msg2"

    # Messages archived
    inbox = _db_mod.INBOX_DIR / "reader"
    assert list(inbox.glob("*.json")) == []
    assert len(list((inbox / "archive").glob("*.json"))) == 2


def test_get_messages_empty_inbox():
    import server.bus_server as bm

    bm.register_agent(pwd="/tmp/empty")
    messages = bm.get_messages("empty")
    assert messages == []


def test_get_messages_idempotent():
    import server.bus_server as bm

    bm.register_agent(pwd="/tmp/reader")
    bm.send_message(to="reader", content="once", from_agent="w", nudge=False)

    first = bm.get_messages("reader")
    assert len(first) == 1

    second = bm.get_messages("reader")
    assert second == []


# ── Liveness pruning ──────────────────────────────────────────────────────────


def test_list_agents_prunes_dead_tmux_targets():
    import server.bus_server as bm

    bm.register_agent(pwd="/tmp/alive", tmux_target="main:0.0", agent_id="alive:main:0.0")
    bm.register_agent(pwd="/tmp/dead", tmux_target="main:0.1", agent_id="dead:main:0.1")

    with patch.object(
        bm, "_tmux_pane_alive", side_effect=lambda t: t == "main:0.0"
    ):
        agents = bm.list_agents()

    ids = [a["agent_id"] for a in agents]
    assert "alive:main:0.0" in ids
    assert "dead:main:0.1" not in ids


def test_list_agents_keeps_agents_without_tmux_target():
    import server.bus_server as bm

    bm.register_agent(pwd="/tmp/notmux", agent_id="notmux")
    agents = bm.list_agents()
    assert len(agents) == 1
    assert agents[0]["agent_id"] == "notmux"


# ── Nudge behavior ───────────────────────────────────────────────────────────


def test_send_message_nudge_skips_dead_pane():
    import server.bus_server as bm

    bm.register_agent(pwd="/tmp/dead", tmux_target="main:9.9", agent_id="dead:main:9.9")
    with patch.object(bm, "_tmux_pane_alive", return_value=False):
        result = bm.send_message(
            to="dead:main:9.9", content="wake up", from_agent="nudger"
        )
    assert result["delivered"] is True
    assert result["nudged"] is False


def test_send_message_nudge_suppressed_with_flag():
    import server.bus_server as bm

    bm.register_agent(pwd="/tmp/quiet", tmux_target="main:0.0", agent_id="quiet:main:0.0")
    with (
        patch.object(bm, "_tmux_pane_alive", return_value=True),
        patch.object(bm, "_tmux_nudge", return_value=True) as mock_nudge,
    ):
        result = bm.send_message(
            to="quiet:main:0.0", content="shh", from_agent="sender", nudge=False
        )
    assert result["nudged"] is False
    mock_nudge.assert_not_called()


def test_send_message_nudges_live_pane():
    import server.bus_server as bm

    bm.register_agent(pwd="/tmp/live", tmux_target="main:0.0", agent_id="live:main:0.0")
    with (
        patch.object(bm, "_tmux_pane_alive", return_value=True),
        patch.object(bm, "_tmux_nudge", return_value=True),
        patch.object(bm, "_nudge_allowed", return_value=True),
    ):
        result = bm.send_message(
            to="live:main:0.0", content="ping", from_agent="sender"
        )
    assert result["nudged"] is True


# ── Agent types & identity ────────────────────────────────────────────────────


def test_register_agent_stores_agent_type():
    import server.bus_server as bm

    bm.register_agent(pwd="/tmp/be", agent_id="be", agent_type="backend-engineer")
    agents = bm.list_agents()
    agent = next(a for a in agents if a["agent_id"] == "be")
    assert agent["agent_type"] == "backend-engineer"


def test_register_agent_default_type_is_general():
    import server.bus_server as bm

    bm.register_agent(pwd="/tmp/gen", agent_id="gen")
    agents = bm.list_agents()
    agent = next(a for a in agents if a["agent_id"] == "gen")
    assert agent["agent_type"] == "general"


# ── Role-based messaging ─────────────────────────────────────────────────────


def test_send_message_role_addressing_delivers_to_matching_agents():
    import server.bus_server as bm

    bm.register_agent(pwd="/tmp/be1", agent_id="be1", agent_type="backend-engineer")
    bm.register_agent(pwd="/tmp/be2", agent_id="be2", agent_type="backend-engineer")
    bm.register_agent(pwd="/tmp/fe", agent_id="fe", agent_type="frontend-engineer")

    result = bm.send_message(
        to="role:backend-engineer", content="build it", from_agent="orch", nudge=False
    )
    assert result["delivered"] is True
    assert set(result["recipients"]) == {"be1", "be2"}


def test_send_message_role_addressing_excludes_sender():
    import server.bus_server as bm

    bm.register_agent(pwd="/tmp/be", agent_id="be", agent_type="backend-engineer")
    result = bm.send_message(
        to="role:backend-engineer", content="self", from_agent="be", nudge=False
    )
    assert result["delivered"] is False


def test_send_message_role_not_found_returns_error():
    import server.bus_server as bm

    bm.register_agent(pwd="/tmp/x", agent_id="x", agent_type="general")
    result = bm.send_message(
        to="role:nonexistent", content="x", from_agent="y", nudge=False
    )
    assert result["delivered"] is False
    assert "error" in result


def test_send_message_role_creates_inbox_files():
    import server._db as _db_mod
    import server.bus_server as bm

    bm.register_agent(pwd="/tmp/be", agent_id="be", agent_type="backend-engineer")
    bm.register_agent(pwd="/tmp/fe", agent_id="fe", agent_type="frontend-engineer")

    bm.send_message(
        to="role:backend-engineer", content="task", from_agent="orch", nudge=False
    )

    be_inbox = _db_mod.INBOX_DIR / "be"
    fe_inbox = _db_mod.INBOX_DIR / "fe"
    assert len(list(be_inbox.glob("*.json"))) == 1
    # frontend-engineer should not receive the message
    fe_files = list(fe_inbox.glob("*.json")) if fe_inbox.exists() else []
    assert fe_files == []


# ── Lifecycle integration tests ──────────────────────────────────────────────


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
    """list_agents returns parsed token_usage JSON (simplified format)."""
    from server._db import db

    import server.bus_server as bm

    token_data = '{"tokens": 81751, "updated": "2026-03-17T08:17:51Z"}'
    bm.register_agent(pwd="/tmp/tracked", agent_id="tracked")
    with db() as conn:
        conn.execute(
            "UPDATE agents SET token_usage = ? WHERE agent_id = 'tracked'",
            (token_data,),
        )

    agents = bm.list_agents()
    agent = next(a for a in agents if a["agent_id"] == "tracked")
    assert isinstance(agent["token_usage"], dict)
    assert agent["token_usage"]["tokens"] == 81751
    assert agent["token_usage"]["updated"] == "2026-03-17T08:17:51Z"


def test_list_agents_empty_token_usage():
    """list_agents handles empty token_usage gracefully."""
    import server.bus_server as bm

    bm.register_agent(pwd="/tmp/fresh", agent_id="fresh")
    agents = bm.list_agents()
    agent = next(a for a in agents if a["agent_id"] == "fresh")
    # Empty '{}' string should remain as-is (not parsed into dict since it's falsy-ish)
    assert agent["token_usage"] in ("{}", {})


# ── Token tracking: whoami includes token_usage ──────────────────────────────


def test_whoami_includes_token_usage(monkeypatch):
    """whoami returns parsed token_usage (simplified format)."""
    from server._db import db

    import server.bus_server as bm

    token_data = '{"tokens": 20000, "updated": "2026-03-17T08:17:51Z"}'
    bm.register_agent(pwd="/tmp/myproj", agent_id="myproj")
    with db() as conn:
        conn.execute(
            "UPDATE agents SET token_usage = ? WHERE agent_id = 'myproj'",
            (token_data,),
        )

    monkeypatch.setattr(bm, "_self_agent_id", lambda: "myproj")
    result = bm.whoami()
    assert isinstance(result["token_usage"], dict)
    assert result["token_usage"]["tokens"] == 20000

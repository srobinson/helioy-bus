"""Lifecycle + DB tests: repo/role/coexistence/adhoc flows, profile + token_usage migration, init_db idempotence."""

from __future__ import annotations

from unittest.mock import patch

from server._tmux import gateway


# ── Lifecycle integration tests ──────────────────────────────────────────────


def test_repo_mode_lifecycle(set_sender):
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

    with patch.object(gateway, "pane_alive", return_value=True):
        agents = bm.list_agents()
    assert len(agents) == 2
    assert all(a["agent_type"] == "general" for a in agents)

    # Direct message between two repo-mode agents
    set_sender("fmm:general:7:2.1")
    result = bm.send_message(
        to="helioy-bus:general:7:2.2",
        content="hi from fmm",
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
    with patch.object(gateway, "pane_alive", return_value=True):
        assert bm.list_agents() == []


def test_role_mode_lifecycle(set_sender):
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
    set_sender("helioy-bus:general:7:3.3")
    result = bm.send_message(
        to="role:backend-engineer",
        content="implement the auth endpoint",
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


def test_coexistence_of_both_modes(set_sender):
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

    with patch.object(gateway, "pane_alive", return_value=True):
        agents = bm.list_agents()
    assert len(agents) == 4

    # Broadcast from a non-registered sender reaches all four agents
    set_sender("external-orchestrator")
    result = bm.send_message(to="*", content="standup time", nudge=False)
    assert set(result["recipients"]) == {
        "fmm:general:7:2.1",
        "helioy-bus:general:7:2.2",
        "helioy-bus:backend-engineer:7:3.1",
        "helioy-bus:frontend-engineer:7:3.2",
    }

    # Role-based send from a registered agent reaches only matching specialists
    set_sender("fmm:general:7:2.1")
    result2 = bm.send_message(
        to="role:backend-engineer",
        content="deploy the API",
        nudge=False,
    )
    assert result2["recipients"] == ["helioy-bus:backend-engineer:7:3.1"]


def test_adhoc_session_fallback_identity(set_sender):
    """An ad-hoc claude session (no warroom, no tmux) registers with the
    canonical 2-segment identity: {repo}:{agent_type}. Bare-basename is a
    legacy shape rejected by the canonical contract (ALP-1786)."""
    import server.bus_server as bm

    # Simulate ad-hoc registration as bus-register.sh would derive it
    bm.register_agent(pwd="/tmp/myproject", agent_type="general")

    agents = bm.list_agents()
    assert len(agents) == 1
    agent = agents[0]
    assert agent["agent_id"] == "myproject:general"
    assert agent["agent_type"] == "general"

    # Can receive direct messages
    set_sender("other")
    result = bm.send_message(
        to="myproject:general", content="hello from peer", nudge=False
    )
    assert result["delivered"] is True

    bm.unregister_agent("myproject:general")
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


# ── DB hygiene: init-once ─────────────────────────────────────────────────────


def test_init_db_idempotent():
    """Calling db() multiple times does not error; schema is bootstrapped once."""
    from server._db import db

    # First call creates the schema
    with db() as conn:
        conn.execute("SELECT 1 FROM agents")

    # Second call must work without re-running _init_db on the fresh temp db
    with db() as conn:
        conn.execute("SELECT 1 FROM agents")
        conn.execute("SELECT 1 FROM warrooms")

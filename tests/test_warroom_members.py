"""Tests for warroom_kill, warroom_add, warroom_remove, status cross-reference, and token-usage surface.

Tests run against a temporary BUS_DIR via the shared isolated_bus fixture in conftest.py.
"""

from __future__ import annotations

# Tests patch the tmux gateway singleton directly. warroom_server no
# longer re-exports `gateway` since the service extraction.
from server._tmux import gateway

from tests.conftest import _insert_member


# ── Warroom: warroom_kill ────────────────────────────────────────────────────


def test_warroom_kill_removes_from_db(monkeypatch):
    """Kill removes warroom and members from the database."""
    from server._db import _now, db

    import server.warroom_server as wm

    # Insert a warroom directly
    now = _now()
    with db() as conn:
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute(
            "INSERT INTO warrooms (warroom_id, tmux_session, tmux_window, cwd, created_at, status) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            ("test-wr", "main", "test-wr", "/tmp", now, "active"),
        )
        _insert_member(
            conn, warroom_id="test-wr", role="helioy-tools:backend-engineer",
            tmux_target="main:1.0", pane_id="%10", now=now,
        )

    result = wm.warroom_kill(name="test-wr")
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
    import server.warroom_server as wm

    result = wm.warroom_kill()
    assert "error" in result


# ── Warroom: warroom_status ──────────────────────────────────────────────────


def test_warroom_status_cross_references_agents(monkeypatch):
    """Status cross-references warroom members with registered agents."""
    from server._db import _now, db

    import server.warroom_server as wm

    now = _now()
    with db() as conn:
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute(
            "INSERT INTO warrooms (warroom_id, tmux_session, tmux_window, cwd, created_at, status) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            ("status-wr", "main", "status-wr", "/tmp", now, "active"),
        )
        member_id = _insert_member(
            conn, warroom_id="status-wr", role="helioy-tools:backend-engineer",
            tmux_target="main:2.0", pane_id="%20", now=now,
        )
        conn.execute(
            "INSERT INTO agents "
            "(agent_id, cwd, tmux_target, pid, registered_at, last_seen) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            ("project:helioy-tools:backend-engineer:main:2.0", "/tmp", "main:2.0", 1234, now, now),
        )

    monkeypatch.setattr(gateway, "pane_alive", lambda t: True)

    statuses = wm.warroom_status(name="status-wr")
    assert len(statuses) == 1
    wr = statuses[0]
    assert wr["warroom_id"] == "status-wr"
    assert len(wr["members"]) == 1
    member = wr["members"][0]
    assert member["registered"] is True
    assert member["pane_alive"] is True
    assert member["agent_instance_id"] == "project:helioy-tools:backend-engineer:main:2.0"
    assert member["warroom_member_id"] == member_id
    assert member["desired_role"] == "helioy-tools:backend-engineer"
    assert member["desired_runtime"] == "claude"
    assert member["desired_repo"] is None
    # Reconciler runs before status and promotes pending→active when the
    # inserted agent row matches the member's tmux_target.
    assert member["state"] == "active"
    assert member["spawn_order"] == 0


# ── Warroom: warroom_add ─────────────────────────────────────────────────────


def test_warroom_add_to_existing(fake_plugins, monkeypatch):
    """Add an agent to an existing warroom."""
    from server._db import _now, db

    import server.warroom_server as wm

    now = _now()
    # Create warroom with one member
    with db() as conn:
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute(
            "INSERT INTO warrooms (warroom_id, tmux_session, tmux_window, cwd, created_at, status) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            ("add-test", "main", "add-test", "/tmp/project", now, "active"),
        )
        _insert_member(
            conn, warroom_id="add-test", role="helioy-tools:backend-engineer",
            tmux_target="main:1.0", pane_id="%10", now=now,
        )

    monkeypatch.setattr(gateway, "spawn_pane", lambda **kw: {
        "agent_type": kw["agent_type"],
        "qualified_name": kw["qualified_name"],
        "tmux_target": "main:1.1",
        "pane_id": "%11",
    })

    result = wm.warroom_add(name="add-test", agent="frontend-engineer")
    assert result["warroom_id"] == "add-test"
    assert result["added"]["qualified_name"] == "helioy-tools:frontend-engineer"
    assert result["added"]["desired_role"] == "helioy-tools:frontend-engineer"
    assert "warroom_member_id" in result["added"]
    assert result["added"]["spawn_order"] == 1
    assert result["member_count"] == 2


def test_warroom_add_allows_duplicate_role(fake_plugins, monkeypatch):
    """Adding the same role twice creates a second distinct member."""
    from server._db import _now, db

    import server.warroom_server as wm

    now = _now()
    with db() as conn:
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute(
            "INSERT INTO warrooms (warroom_id, tmux_session, tmux_window, cwd, created_at, status) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            ("dup-test", "main", "dup-test", "/tmp/project", now, "active"),
        )
        first_id = _insert_member(
            conn, warroom_id="dup-test", role="helioy-tools:backend-engineer",
            tmux_target="main:1.0", pane_id="%10", now=now, spawn_order=0,
        )

    pane_counter = [1]

    def mock_spawn_pane(**kw):
        idx = pane_counter[0]
        pane_counter[0] += 1
        return {
            "agent_type": kw["agent_type"],
            "qualified_name": kw["qualified_name"],
            "tmux_target": f"main:1.{idx}",
            "pane_id": f"%1{idx}",
        }

    monkeypatch.setattr(gateway, "spawn_pane", mock_spawn_pane)

    result = wm.warroom_add(name="dup-test", agent="backend-engineer")
    assert "error" not in result
    assert result["member_count"] == 2
    assert result["added"]["warroom_member_id"] != first_id
    assert result["added"]["spawn_order"] == 1

    with db() as conn:
        roles = conn.execute(
            "SELECT desired_role, spawn_order FROM warroom_members "
            "WHERE warroom_id = ? ORDER BY spawn_order",
            ("dup-test",),
        ).fetchall()
        assert len(roles) == 2
        assert roles[0]["desired_role"] == roles[1]["desired_role"]
        assert roles[0]["spawn_order"] == 0
        assert roles[1]["spawn_order"] == 1


def test_warroom_add_nonexistent_warroom(fake_plugins):
    """Adding to a non-existent warroom returns an error."""
    import server.warroom_server as wm

    result = wm.warroom_add(name="ghost-room", agent="backend-engineer")
    assert "error" in result
    assert "ghost-room" in result["error"]


def test_warroom_add_unknown_agent_type(fake_plugins, monkeypatch):
    """Adding an unknown agent type returns error with suggestions."""
    from server._db import _now, db

    import server.warroom_server as wm

    now = _now()
    with db() as conn:
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute(
            "INSERT INTO warrooms (warroom_id, tmux_session, tmux_window, cwd, created_at, status) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            ("type-test", "main", "type-test", "/tmp", now, "active"),
        )

    result = wm.warroom_add(name="type-test", agent="nonexistent-xyz")
    assert "error" in result
    assert result["error"] == "Unknown agent type"


# ── Warroom: warroom_remove ──────────────────────────────────────────────────


def test_warroom_remove_agent(fake_plugins, monkeypatch):
    """Remove an agent from a warroom by role name."""
    from server._db import _now, db

    import server.warroom_server as wm

    now = _now()
    with db() as conn:
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute(
            "INSERT INTO warrooms (warroom_id, tmux_session, tmux_window, cwd, created_at, status) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            ("rm-test", "main", "rm-test", "/tmp", now, "active"),
        )
        be_id = _insert_member(
            conn, warroom_id="rm-test", role="helioy-tools:backend-engineer",
            tmux_target="main:1.0", pane_id="%10", now=now, spawn_order=0,
        )
        _insert_member(
            conn, warroom_id="rm-test", role="helioy-tools:frontend-engineer",
            tmux_target="main:1.1", pane_id="%11", now=now, spawn_order=1,
        )

    result = wm.warroom_remove(name="rm-test", agent="backend-engineer")
    assert result["warroom_id"] == "rm-test"
    assert result["removed"]["desired_role"] == "helioy-tools:backend-engineer"
    assert result["removed"]["warroom_member_id"] == be_id
    assert result["remaining_members"] == 1
    assert result["warroom_killed"] is False


def test_warroom_remove_by_member_id(fake_plugins, monkeypatch):
    """Remove targets the exact stable member_id when provided."""
    from server._db import _now, db

    import server.warroom_server as wm

    now = _now()
    with db() as conn:
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute(
            "INSERT INTO warrooms (warroom_id, tmux_session, tmux_window, cwd, created_at, status) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            ("rm-id", "main", "rm-id", "/tmp", now, "active"),
        )
        first_id = _insert_member(
            conn, warroom_id="rm-id", role="helioy-tools:backend-engineer",
            tmux_target="main:1.0", pane_id="%10", now=now, spawn_order=0,
        )
        second_id = _insert_member(
            conn, warroom_id="rm-id", role="helioy-tools:backend-engineer",
            tmux_target="main:1.1", pane_id="%11", now=now, spawn_order=1,
        )

    result = wm.warroom_remove(name="rm-id", member_id=second_id)
    assert "error" not in result
    assert result["removed"]["warroom_member_id"] == second_id
    assert result["remaining_members"] == 1

    with db() as conn:
        rows = conn.execute(
            "SELECT warroom_member_id FROM warroom_members WHERE warroom_id = 'rm-id'"
        ).fetchall()
        assert [r["warroom_member_id"] for r in rows] == [first_id]


def test_warroom_remove_ambiguous_role_returns_candidates(fake_plugins, monkeypatch):
    """Removing by role when multiple members share it returns candidate ids."""
    from server._db import _now, db

    import server.warroom_server as wm

    now = _now()
    with db() as conn:
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute(
            "INSERT INTO warrooms (warroom_id, tmux_session, tmux_window, cwd, created_at, status) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            ("amb", "main", "amb", "/tmp", now, "active"),
        )
        a_id = _insert_member(
            conn, warroom_id="amb", role="helioy-tools:backend-engineer",
            tmux_target="main:1.0", pane_id="%10", now=now, spawn_order=0,
        )
        b_id = _insert_member(
            conn, warroom_id="amb", role="helioy-tools:backend-engineer",
            tmux_target="main:1.1", pane_id="%11", now=now, spawn_order=1,
        )

    result = wm.warroom_remove(name="amb", agent="backend-engineer")
    assert "error" in result
    assert "ambiguous" in result["error"].lower()
    candidate_ids = {c["warroom_member_id"] for c in result["candidates"]}
    assert candidate_ids == {a_id, b_id}


def test_warroom_remove_last_agent_kills_warroom(fake_plugins, monkeypatch):
    """Removing the last agent marks warroom as killed."""
    from server._db import _now, db

    import server.warroom_server as wm

    now = _now()
    with db() as conn:
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute(
            "INSERT INTO warrooms (warroom_id, tmux_session, tmux_window, cwd, created_at, status) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            ("last-test", "main", "last-test", "/tmp", now, "active"),
        )
        _insert_member(
            conn, warroom_id="last-test", role="helioy-tools:backend-engineer",
            tmux_target="main:1.0", pane_id="%10", now=now,
        )

    result = wm.warroom_remove(name="last-test", agent="backend-engineer")
    assert result["remaining_members"] == 0
    assert result["warroom_killed"] is True

    # Verify DB state
    with db() as conn:
        wr = conn.execute("SELECT status FROM warrooms WHERE warroom_id = 'last-test'").fetchone()
        assert wr["status"] == "killed"


def test_warroom_remove_nonexistent_agent(fake_plugins):
    """Removing an agent not in the warroom returns an error."""
    from server._db import _now, db

    import server.warroom_server as wm

    now = _now()
    with db() as conn:
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute(
            "INSERT INTO warrooms (warroom_id, tmux_session, tmux_window, cwd, created_at, status) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            ("no-agent", "main", "no-agent", "/tmp", now, "active"),
        )

    result = wm.warroom_remove(name="no-agent", agent="backend-engineer")
    assert "error" in result


# ── Token tracking: warroom_status includes token_usage ──────────────────────


def test_warroom_status_includes_token_usage(monkeypatch):
    """warroom_status includes token_usage in member dicts (simplified format)."""
    from server._db import _now, db

    import server.warroom_server as wm

    now = _now()
    token_data = '{"tokens": 85000, "updated": "2026-03-17T08:17:51Z"}'

    with db() as conn:
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute(
            "INSERT INTO warrooms (warroom_id, tmux_session, tmux_window, cwd, created_at, status) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            ("token-wr", "main", "token-wr", "/tmp", now, "active"),
        )
        _insert_member(
            conn, warroom_id="token-wr", role="helioy-tools:backend-engineer",
            tmux_target="main:3.0", pane_id="%30", now=now,
        )
        conn.execute(
            "INSERT INTO agents "
            "(agent_id, cwd, tmux_target, pid, registered_at, last_seen, token_usage) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            ("proj:be:main:3.0", "/tmp", "main:3.0", 1234, now, now, token_data),
        )

    monkeypatch.setattr(gateway, "pane_alive", lambda t: True)

    statuses = wm.warroom_status(name="token-wr")
    member = statuses[0]["members"][0]
    assert isinstance(member["token_usage"], dict)
    assert member["token_usage"]["tokens"] == 85000
    assert member["token_usage"]["updated"] == "2026-03-17T08:17:51Z"

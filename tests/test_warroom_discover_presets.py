"""Tests for warroom_discover, preset CRUD, schema init, and legacy member migration.

Tests run against a temporary BUS_DIR via the shared isolated_bus fixture in conftest.py.
"""

from __future__ import annotations

import pytest

import server._db as _db_mod


# ── Warroom: warroom_discover ────────────────────────────────────────────────


def test_warroom_discover_all(fake_plugins):
    """Discover with no filters returns all agents."""
    import server.warroom_server as wm

    result = wm.warroom_discover()
    assert result["total"] >= 4
    assert "helioy-tools" in result["namespaces"]


def test_warroom_discover_query_filter(fake_plugins):
    """Query filters by name and description substring."""
    import server.warroom_server as wm

    result = wm.warroom_discover(query="backend")
    assert result["total"] >= 1
    assert all(
        "backend" in a["name"].lower() or "backend" in a.get("summary", "").lower()
        for a in result["agents"]
    )


def test_warroom_discover_namespace_filter(fake_plugins):
    """Namespace filter restricts to a single plugin."""
    import server.warroom_server as wm

    result = wm.warroom_discover(namespace="helioy-tools")
    assert all(a["namespace"] == "helioy-tools" for a in result["agents"])


def test_warroom_discover_limit(fake_plugins):
    """Limit caps the number of returned results."""
    import server.warroom_server as wm

    result = wm.warroom_discover(limit=1)
    assert len(result["agents"]) == 1
    assert result["total"] >= 4  # total count unaffected by limit


# ── Warroom: presets ─────────────────────────────────────────────────────────


def test_warroom_presets_empty(tmp_path, monkeypatch):
    """Returns empty list when no presets directory exists."""
    import server.warroom_server as wm

    monkeypatch.setattr(_db_mod, "PRESETS_DIR", tmp_path / "nonexistent")
    result = wm.warroom_presets()
    assert result == {"presets": []}


def test_warroom_save_and_list_preset(tmp_path, monkeypatch):
    """Save a preset and verify it appears in the listing."""
    import server.warroom_server as wm

    presets_dir = tmp_path / "presets"
    monkeypatch.setattr(_db_mod, "PRESETS_DIR", presets_dir)

    save_result = wm.warroom_save_preset(
        name="design-team",
        agents=["ux-designer", "frontend-engineer", "visual-designer"],
        description="Full design team",
        tags=["design", "ui"],
    )
    assert save_result["saved"] == "design-team"

    list_result = wm.warroom_presets()
    assert len(list_result["presets"]) == 1
    preset = list_result["presets"][0]
    assert preset["name"] == "design-team"
    assert preset["agents"] == ["ux-designer", "frontend-engineer", "visual-designer"]
    assert preset["tags"] == ["design", "ui"]


def test_warroom_save_preset_validation():
    """Rejects invalid preset names."""
    import server.warroom_server as wm

    result = wm.warroom_save_preset(name="", agents=["be"])
    assert "error" in result

    result = wm.warroom_save_preset(name="valid-name", agents=[])
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


def test_warroom_members_runtime_is_required():
    """No runtime privilege in the core model: a member row must name its runtime.

    A missing ``runtime`` used to default to ``'claude'``, which silently
    made Codex members register as Claude. Inserting without runtime must
    now raise so the mis-registration cannot happen.
    """
    import sqlite3

    from server._db import _new_member_id, _now, db

    now = _now()
    with db() as conn:
        conn.execute(
            "INSERT INTO warrooms (warroom_id, tmux_session, tmux_window, cwd, "
            "created_at, status) VALUES (?, ?, ?, ?, ?, 'active')",
            ("t", "s", "w", "/tmp", now),
        )
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO warroom_members "
                "(warroom_member_id, warroom_id, desired_role, spawn_order, "
                " tmux_target, pane_id, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (_new_member_id(), "t", "general", 0, "s:1.0", "%1", now, now),
            )


# ── Warroom: legacy schema migration ─────────────────────────────────────────


def test_warroom_members_legacy_schema_migrates():
    """Legacy (warroom_id, agent_type) PK rows are rebuilt with stable member ids."""
    import sqlite3

    import server._db as _db_mod
    from server._db import REGISTRY_DB, _now, db

    # Build a legacy-shape DB without going through _init_db
    REGISTRY_DB.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(REGISTRY_DB))
    conn.executescript("""
        CREATE TABLE warrooms (
            warroom_id   TEXT PRIMARY KEY,
            tmux_session TEXT NOT NULL,
            tmux_window  TEXT NOT NULL,
            cwd          TEXT NOT NULL,
            created_at   TEXT NOT NULL,
            status       TEXT NOT NULL DEFAULT 'active'
        );
        CREATE TABLE warroom_members (
            warroom_id   TEXT NOT NULL REFERENCES warrooms(warroom_id) ON DELETE CASCADE,
            agent_type   TEXT NOT NULL,
            tmux_target  TEXT NOT NULL,
            pane_id      TEXT NOT NULL,
            agent_id     TEXT,
            spawned_at   TEXT NOT NULL,
            PRIMARY KEY (warroom_id, agent_type)
        );
    """)
    now = _now()
    conn.execute(
        "INSERT INTO warrooms (warroom_id, tmux_session, tmux_window, cwd, created_at, status) "
        "VALUES (?, ?, ?, ?, ?, 'active')",
        ("legacy", "main", "legacy", "/tmp", now),
    )
    conn.executemany(
        "INSERT INTO warroom_members (warroom_id, agent_type, tmux_target, pane_id, agent_id, spawned_at) "
        "VALUES (?, ?, ?, ?, NULL, ?)",
        [
            ("legacy", "helioy-tools:backend-engineer", "main:1.0", "%10", now),
            ("legacy", "helioy-tools:frontend-engineer", "main:1.1", "%11", now),
        ],
    )
    conn.commit()
    conn.close()

    # Force the next db() call to re-run _init_db (and the migration)
    _db_mod._db_initialized = False

    with db() as conn:
        cols = {r["name"] for r in conn.execute("PRAGMA table_info(warroom_members)").fetchall()}
        assert "warroom_member_id" in cols
        assert "desired_role" in cols
        assert "desired_repo" in cols
        assert "spawn_order" in cols
        assert "desired_runtime" in cols
        assert "state" in cols
        assert "agent_instance_id" in cols
        assert "created_at" in cols
        assert "updated_at" in cols

        rows = conn.execute(
            "SELECT * FROM warroom_members WHERE warroom_id = 'legacy' ORDER BY spawn_order"
        ).fetchall()
        assert len(rows) == 2
        assert {r["desired_role"] for r in rows} == {
            "helioy-tools:backend-engineer",
            "helioy-tools:frontend-engineer",
        }
        assert all(r["desired_runtime"] == "claude" for r in rows)
        assert all(r["desired_repo"] is None for r in rows)
        # NULL agent_id in the legacy row maps to state='pending'.
        assert all(r["state"] == "pending" for r in rows)
        assert all(r["agent_instance_id"] is None for r in rows)
        assert sorted(r["spawn_order"] for r in rows) == [0, 1]
        assert len({r["warroom_member_id"] for r in rows}) == 2
        # legacy temp table is gone
        legacy = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='warroom_members_legacy'"
        ).fetchone()
        assert legacy is None

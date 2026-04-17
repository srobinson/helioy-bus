"""Schema migration coverage for warroom_members.

The canonical schema uses `desired_runtime`, `desired_role`, `desired_repo`,
`state`, `agent_instance_id`, `created_at`, `updated_at`. Two prior shapes
exist in the wild and both must upgrade cleanly:

1. Pre-stable-id: (warroom_id, agent_type) composite PK, no runtime column.
   Covered by `tests/test_warroom_server.py::test_warroom_members_legacy_schema_migrates`.
2. Intermediate stable-id: warroom_member_id present but columns named
   `runtime`/`role`/`repo`/`agent_id`/`spawned_at`. Covered here.
"""

import sqlite3


def test_warroom_members_intermediate_schema_migrates():
    """Intermediate (stable-id) rows upgrade to desired_* + state/updated_at."""
    import server._db as _db_mod
    from server._db import REGISTRY_DB, _new_member_id, _now, db

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
            warroom_member_id TEXT PRIMARY KEY,
            warroom_id        TEXT NOT NULL REFERENCES warrooms(warroom_id) ON DELETE CASCADE,
            runtime           TEXT NOT NULL,
            role              TEXT NOT NULL,
            repo              TEXT,
            spawn_order       INTEGER NOT NULL,
            tmux_target       TEXT NOT NULL,
            pane_id           TEXT NOT NULL,
            agent_id          TEXT,
            spawned_at        TEXT NOT NULL
        );
    """)
    now = _now()
    conn.execute(
        "INSERT INTO warrooms (warroom_id, tmux_session, tmux_window, cwd, created_at, status) "
        "VALUES (?, ?, ?, ?, ?, 'active')",
        ("intermediate", "main", "intermediate", "/tmp", now),
    )
    registered_member_id = _new_member_id()
    pending_member_id = _new_member_id()
    conn.executemany(
        "INSERT INTO warroom_members "
        "(warroom_member_id, warroom_id, runtime, role, repo, spawn_order, "
        " tmux_target, pane_id, agent_id, spawned_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        [
            # Already-registered: carries an agent_id → must land as state='active'.
            (registered_member_id, "intermediate", "claude", "general",
             "repo-a", 0, "main:1.0", "%10", "proj:general:main:1.0", now),
            # Never-registered: agent_id NULL → must land as state='pending'.
            (pending_member_id, "intermediate", "codex", "general",
             None, 1, "main:1.1", "%11", None, now),
        ],
    )
    conn.commit()
    conn.close()

    _db_mod._db_initialized = False

    with db() as conn:
        cols = {r["name"] for r in conn.execute(
            "PRAGMA table_info(warroom_members)"
        ).fetchall()}
        assert "desired_runtime" in cols
        assert "desired_role" in cols
        assert "desired_repo" in cols
        assert "state" in cols
        assert "agent_instance_id" in cols
        assert "created_at" in cols
        assert "updated_at" in cols
        assert "runtime" not in cols
        assert "role" not in cols
        assert "agent_id" not in cols
        assert "spawned_at" not in cols

        rows = {
            r["warroom_member_id"]: dict(r)
            for r in conn.execute(
                "SELECT * FROM warroom_members WHERE warroom_id = 'intermediate'"
            ).fetchall()
        }
        assert set(rows.keys()) == {registered_member_id, pending_member_id}

        registered = rows[registered_member_id]
        assert registered["desired_runtime"] == "claude"
        assert registered["desired_role"] == "general"
        assert registered["desired_repo"] == "repo-a"
        assert registered["state"] == "active"
        assert registered["agent_instance_id"] == "proj:general:main:1.0"
        assert registered["created_at"] == now
        assert registered["updated_at"] == now

        pending = rows[pending_member_id]
        assert pending["desired_runtime"] == "codex"
        assert pending["desired_repo"] is None
        assert pending["state"] == "pending"
        assert pending["agent_instance_id"] is None

        legacy = conn.execute(
            "SELECT name FROM sqlite_master "
            "WHERE type='table' AND name='warroom_members_legacy'"
        ).fetchone()
        assert legacy is None


def test_warrooms_adds_layout_runtime_policy_metadata_columns():
    """Legacy warrooms rows gain the new spec columns with safe defaults."""
    import server._db as _db_mod
    from server._db import REGISTRY_DB, _now, db

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
    """)
    now = _now()
    conn.execute(
        "INSERT INTO warrooms (warroom_id, tmux_session, tmux_window, cwd, "
        "created_at, status) VALUES (?, ?, ?, ?, ?, 'active')",
        ("legacy-wr", "main", "legacy-wr", "/tmp", now),
    )
    conn.commit()
    conn.close()

    _db_mod._db_initialized = False

    with db() as conn:
        cols = {r["name"] for r in conn.execute(
            "PRAGMA table_info(warrooms)"
        ).fetchall()}
        assert cols >= {"layout", "runtime_policy", "metadata"}

        row = conn.execute(
            "SELECT layout, runtime_policy, metadata "
            "FROM warrooms WHERE warroom_id = 'legacy-wr'"
        ).fetchone()
        assert row["layout"] == "tiled"
        assert row["runtime_policy"] is None
        assert row["metadata"] is None

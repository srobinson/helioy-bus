"""Tests for helioy-warroom server tools.

Warroom lifecycle: discover, spawn, kill, status, add, remove, presets.
Also covers the shared _warroom module (frontmatter parsing, agent type
scanning/resolution).

Tests run against a temporary BUS_DIR via the shared isolated_bus fixture
in conftest.py.
"""

from __future__ import annotations

import time


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
    import server._db as _db_mod
    import server._warroom as wr

    cache = tmp_path / "cache"
    old = cache / "org" / "myplugin" / "v1" / "agents"
    old.mkdir(parents=True)
    (old / "my-agent.md").write_text(
        '---\nname: my-agent\ndescription: "old"\nmodel: sonnet\n---\n'
    )

    time.sleep(0.05)  # ensure mtime differs

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

    monkeypatch.setattr(wm, "PRESETS_DIR", tmp_path / "nonexistent")
    result = wm.warroom_presets()
    assert result == {"presets": []}


def test_warroom_save_and_list_preset(tmp_path, monkeypatch):
    """Save a preset and verify it appears in the listing."""
    import server.warroom_server as wm

    presets_dir = tmp_path / "presets"
    monkeypatch.setattr(wm, "PRESETS_DIR", presets_dir)

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


# ── Warroom: warroom_spawn (mocked tmux) ─────────────────────────────────────


def test_warroom_spawn_validates_agent_types(fake_plugins, monkeypatch):
    """Spawn rejects unknown agent types with suggestions."""
    import server.warroom_server as wm

    monkeypatch.setenv("TMUX", "/tmp/tmux-1000/default,12345,0")
    monkeypatch.setattr(wm, "_tmux_check", lambda *args: "main")

    result = wm.warroom_spawn(name="test-room", agents=["nonexistent-agent-xyz"])
    assert "error" in result
    assert result["error"] == "Unknown agent types"


def test_warroom_spawn_requires_tmux(fake_plugins, monkeypatch):
    """Spawn fails cleanly outside tmux."""
    import server.warroom_server as wm

    monkeypatch.delenv("TMUX", raising=False)
    result = wm.warroom_spawn(name="test", agents=["backend-engineer"])
    assert "error" in result
    assert "tmux" in result["error"].lower()


def test_warroom_spawn_validates_name():
    """Spawn rejects invalid warroom names."""
    import server.warroom_server as wm

    result = wm.warroom_spawn(name="", agents=["be"])
    assert "error" in result

    result = wm.warroom_spawn(name="has spaces", agents=["be"])
    assert "error" in result


def test_warroom_spawn_validates_agent_count():
    """Spawn rejects more than 8 agents."""
    import server.warroom_server as wm

    result = wm.warroom_spawn(name="big", agents=["a"] * 9)
    assert "error" in result
    assert "8" in result["error"]


def test_warroom_spawn_validates_layout(fake_plugins, monkeypatch):
    """Spawn rejects invalid layout values."""
    import server.warroom_server as wm

    monkeypatch.setenv("TMUX", "/tmp/tmux-1000/default,12345,0")
    result = wm.warroom_spawn(name="test", agents=["backend-engineer"], layout="invalid")
    assert "error" in result
    assert "layout" in result["error"].lower()


def test_warroom_spawn_with_mocked_tmux(fake_plugins, monkeypatch):
    """Full spawn flow with mocked tmux calls records warroom in DB."""
    import server.warroom_server as wm
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

    monkeypatch.setattr(wm, "_tmux_check", mock_tmux_check)
    monkeypatch.setattr(wm, "_spawn_pane", lambda **kw: {
        "agent_type": kw["agent_type"],
        "qualified_name": kw["qualified_name"],
        "tmux_target": f"main:1.{0 if kw['is_first'] else 1}",
        "pane_id": f"%{42 + (0 if kw['is_first'] else 1)}",
    })

    result = wm.warroom_spawn(
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
        conn.execute(
            "INSERT INTO warroom_members "
            "(warroom_id, agent_type, tmux_target, pane_id, spawned_at) "
            "VALUES (?, ?, ?, ?, ?)",
            ("test-wr", "helioy-tools:backend-engineer", "main:1.0", "%10", now),
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

    monkeypatch.setattr(wm, "_tmux_pane_alive", lambda t: True)

    statuses = wm.warroom_status(name="status-wr")
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
        conn.execute(
            "INSERT INTO warroom_members "
            "(warroom_id, agent_type, tmux_target, pane_id, spawned_at) "
            "VALUES (?, ?, ?, ?, ?)",
            ("add-test", "helioy-tools:backend-engineer", "main:1.0", "%10", now),
        )

    monkeypatch.setattr(wm, "_spawn_pane", lambda **kw: {
        "agent_type": kw["agent_type"],
        "qualified_name": kw["qualified_name"],
        "tmux_target": "main:1.1",
        "pane_id": "%11",
    })

    result = wm.warroom_add(name="add-test", agent="frontend-engineer")
    assert result["warroom_id"] == "add-test"
    assert result["added"]["qualified_name"] == "helioy-tools:frontend-engineer"
    assert result["member_count"] == 2


def test_warroom_add_duplicate_rejected(fake_plugins, monkeypatch):
    """Adding a duplicate agent type returns an error."""
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
        conn.execute(
            "INSERT INTO warroom_members "
            "(warroom_id, agent_type, tmux_target, pane_id, spawned_at) "
            "VALUES (?, ?, ?, ?, ?)",
            ("dup-test", "helioy-tools:backend-engineer", "main:1.0", "%10", now),
        )

    result = wm.warroom_add(name="dup-test", agent="backend-engineer")
    assert "error" in result
    assert "already" in result["error"].lower()


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
    """Remove an agent from a warroom."""
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

    result = wm.warroom_remove(name="rm-test", agent="backend-engineer")
    assert result["warroom_id"] == "rm-test"
    assert result["removed"] == "helioy-tools:backend-engineer"
    assert result["remaining_members"] == 1
    assert result["warroom_killed"] is False


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
        conn.execute(
            "INSERT INTO warroom_members "
            "(warroom_id, agent_type, tmux_target, pane_id, spawned_at) "
            "VALUES (?, ?, ?, ?, ?)",
            ("last-test", "helioy-tools:backend-engineer", "main:1.0", "%10", now),
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


# ── Warroom: spawn idempotency ───────────────────────────────────────────────


def test_warroom_spawn_idempotent_replaces_existing(fake_plugins, monkeypatch):
    """Re-spawning with the same name replaces the existing warroom DB record."""
    import server.warroom_server as wm
    from server._db import db

    monkeypatch.setenv("TMUX", "/tmp/tmux-1000/default,12345,0")

    pane_counter = [0]

    def mock_spawn_pane(**kw):
        idx = pane_counter[0]
        pane_counter[0] += 1
        return {
            "agent_type": kw["agent_type"],
            "qualified_name": kw["qualified_name"],
            "tmux_target": f"main:1.{idx}",
            "pane_id": f"%{idx}",
        }

    monkeypatch.setattr(wm, "_tmux_check", lambda *a: "main")
    monkeypatch.setattr(wm, "_spawn_pane", mock_spawn_pane)

    # First spawn
    r1 = wm.warroom_spawn(name="idem-test", agents=["backend-engineer"], cwd="/tmp")
    assert r1["warroom_id"] == "idem-test"
    assert len(r1["members"]) == 1

    # Second spawn — different agent, same name
    r2 = wm.warroom_spawn(name="idem-test", agents=["frontend-engineer"], cwd="/tmp")
    assert r2["warroom_id"] == "idem-test"
    assert len(r2["members"]) == 1
    assert r2["members"][0]["qualified_name"] == "helioy-tools:frontend-engineer"

    # DB must contain exactly one warroom and one member (the new one)
    with db() as conn:
        warrooms = conn.execute(
            "SELECT * FROM warrooms WHERE warroom_id = 'idem-test'"
        ).fetchall()
        assert len(warrooms) == 1
        members = conn.execute(
            "SELECT * FROM warroom_members WHERE warroom_id = 'idem-test'"
        ).fetchall()
        assert len(members) == 1
        assert members[0]["agent_type"] == "helioy-tools:frontend-engineer"


# ── Warroom: spawn pane command line ─────────────────────────────────────────


def test_spawn_pane_role_mode_includes_skip_permissions(monkeypatch):
    """Role-mode panes launch claude with --dangerously-skip-permissions."""
    import server._tmux as tmux_mod

    call_log: list = []

    def mock_tmux_check(*args):
        call_log.append(args)
        if args[0] == "new-window":
            return "%42"
        if args[0] == "display-message":
            return "main:1.0"
        return ""

    monkeypatch.setattr(tmux_mod, "_tmux_check", mock_tmux_check)

    tmux_mod._spawn_pane(
        session="main",
        window="test-room",
        cwd="/tmp/project",
        agent_type="backend-engineer",
        qualified_name="helioy-tools:backend-engineer",
        is_first=True,
        layout="tiled",
    )

    send_keys_calls = [c for c in call_log if c[0] == "send-keys" and "claude" in str(c)]
    assert len(send_keys_calls) == 1
    cmd = send_keys_calls[0][3]  # tmux send-keys -t <pane_id> <cmd> Enter
    assert "--dangerously-skip-permissions" in cmd
    assert "--agent helioy-tools:backend-engineer" in cmd


def test_spawn_pane_repo_mode_includes_skip_permissions(monkeypatch):
    """Repo-mode panes also launch claude with --dangerously-skip-permissions."""
    import server._tmux as tmux_mod

    call_log: list = []

    def mock_tmux_check(*args):
        call_log.append(args)
        if args[0] == "new-window":
            return "%43"
        if args[0] == "display-message":
            return "main:1.0"
        return ""

    monkeypatch.setattr(tmux_mod, "_tmux_check", mock_tmux_check)

    tmux_mod._spawn_pane(
        session="main",
        window="warroom",
        cwd="/tmp/repo",
        agent_type="general",
        qualified_name=None,
        is_first=True,
        layout="tiled",
    )

    send_keys_calls = [c for c in call_log if c[0] == "send-keys" and "claude" in str(c)]
    assert len(send_keys_calls) == 1
    cmd = send_keys_calls[0][3]
    assert "--dangerously-skip-permissions" in cmd
    assert "--agent" not in cmd


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

    monkeypatch.setattr(wm, "_tmux_pane_alive", lambda t: True)

    statuses = wm.warroom_status(name="token-wr")
    member = statuses[0]["members"][0]
    assert isinstance(member["token_usage"], dict)
    assert member["token_usage"]["tokens"] == 85000
    assert member["token_usage"]["updated"] == "2026-03-17T08:17:51Z"

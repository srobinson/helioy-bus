"""Tests for warroom_spawn: validation, mocked spawn, idempotent replace, repos mode, and pane command-line behavior.

Tests run against a temporary BUS_DIR via the shared isolated_bus fixture in conftest.py.
"""

from __future__ import annotations

# Tests patch the tmux gateway singleton directly. warroom_server no
# longer re-exports `gateway` since the ALP-1789 service extraction.
from server._tmux import gateway


# ── Warroom: warroom_spawn (mocked tmux) ─────────────────────────────────────


def test_warroom_spawn_validates_agent_types(fake_plugins, monkeypatch):
    """Spawn rejects unknown agent types with suggestions."""
    import server.warroom_server as wm

    monkeypatch.setenv("TMUX", "/tmp/tmux-1000/default,12345,0")
    monkeypatch.setattr(gateway, "_run", lambda *args, **kw: "main")

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

    def mock_run(*args, **kw):
        call_log.append(args)
        cmd = args[0]
        if cmd == "display-message":
            if "session_name" in args[-1]:
                return "main"
            return "main:1.0"
        if cmd in ("new-window", "split-window"):
            return "%42"
        return ""

    monkeypatch.setattr(gateway, "_run", mock_run)
    monkeypatch.setattr(gateway, "spawn_pane", lambda **kw: {
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


def test_warroom_spawn_includes_messaging_guidance(fake_plugins, monkeypatch):
    """Spawn response includes messaging guidance discouraging broadcast."""
    import server.warroom_server as wm

    monkeypatch.setenv("TMUX", "/tmp/tmux-1000/default,12345,0")
    monkeypatch.setattr(gateway, "_run", lambda *a, **kw: "main")
    monkeypatch.setattr(gateway, "spawn_pane", lambda **kw: {
        "agent_type": kw["agent_type"],
        "qualified_name": kw["qualified_name"],
        "tmux_target": f"main:1.{0 if kw['is_first'] else 1}",
        "pane_id": f"%{42 + (0 if kw['is_first'] else 1)}",
    })

    result = wm.warroom_spawn(
        name="msg-test",
        agents=["backend-engineer", "frontend-engineer"],
        cwd="/tmp/project",
    )

    assert "messaging" in result
    msg = result["messaging"]
    assert "Never use" in msg["instruction"]
    assert "*" in msg["instruction"]
    assert "warroom_status" in msg["instruction"]
    assert msg["member_types"] == [
        "helioy-tools:backend-engineer",
        "helioy-tools:frontend-engineer",
    ]


def test_warroom_spawn_repos_includes_messaging_guidance(monkeypatch, tmp_path):
    """Repo-mode spawn response includes messaging guidance."""
    import server.warroom_server as wm

    monkeypatch.setenv("TMUX", "/tmp/tmux-1000/default,12345,0")
    monkeypatch.setattr(gateway, "_run", lambda *a, **kw: "main")

    # Create two fake git repos
    for name in ("repo-a", "repo-b"):
        repo = tmp_path / name
        repo.mkdir()
        (repo / ".git").mkdir()

    monkeypatch.setenv("HELIOY_BASE", str(tmp_path))

    pane_counter = [0]

    def mock_spawn_pane(**kw):
        idx = pane_counter[0]
        pane_counter[0] += 1
        return {
            "agent_type": kw["agent_type"],
            "qualified_name": kw.get("qualified_name"),
            "tmux_target": f"main:1.{idx}",
            "pane_id": f"%{idx}",
        }

    monkeypatch.setattr(gateway, "spawn_pane", mock_spawn_pane)

    result = wm.warroom_spawn_repos(window="repo-wr")

    assert "messaging" in result
    msg = result["messaging"]
    assert "Never use" in msg["instruction"]
    assert "*" in msg["instruction"]


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

    monkeypatch.setattr(gateway, "_run", lambda *a, **kw: "main")
    monkeypatch.setattr(gateway, "spawn_pane", mock_spawn_pane)

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
        assert members[0]["desired_role"] == "helioy-tools:frontend-engineer"


# ── Warroom: spawn pane command line ─────────────────────────────────────────


def test_spawn_pane_role_mode_includes_skip_permissions(monkeypatch):
    """Role-mode panes launch claude with --dangerously-skip-permissions."""
    import server._tmux as tmux_mod

    call_log: list = []

    def mock_run(*args, **kw):
        call_log.append(args)
        if args[0] == "new-window":
            return "%42"
        if args[0] == "display-message":
            return "main:1.0"
        return ""

    monkeypatch.setattr(tmux_mod.gateway, "_run", mock_run)

    tmux_mod.gateway.spawn_pane(
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

    def mock_run(*args, **kw):
        call_log.append(args)
        if args[0] == "new-window":
            return "%43"
        if args[0] == "display-message":
            return "main:1.0"
        return ""

    monkeypatch.setattr(tmux_mod.gateway, "_run", mock_run)

    tmux_mod.gateway.spawn_pane(
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


# ── Warroom: repo mode creates distinct members ──────────────────────────────


def test_warroom_spawn_repos_creates_distinct_members_per_repo(monkeypatch, tmp_path):
    """Repo mode persists one stable member per repo with role='general' and repo set."""
    import server.warroom_server as wm
    from server._db import db

    monkeypatch.setenv("TMUX", "/tmp/tmux-1000/default,12345,0")
    monkeypatch.setattr(gateway, "_run", lambda *a, **kw: "main")

    for name in ("repo-a", "repo-b", "repo-c"):
        repo = tmp_path / name
        repo.mkdir()
        (repo / ".git").mkdir()
    monkeypatch.setenv("HELIOY_BASE", str(tmp_path))

    pane_counter = [0]

    def mock_spawn_pane(**kw):
        idx = pane_counter[0]
        pane_counter[0] += 1
        return {
            "agent_type": kw["agent_type"],
            "qualified_name": kw.get("qualified_name"),
            "tmux_target": f"main:1.{idx}",
            "pane_id": f"%{idx}",
        }

    monkeypatch.setattr(gateway, "spawn_pane", mock_spawn_pane)

    result = wm.warroom_spawn_repos(window="repo-wr")
    assert "errors" not in result
    assert len(result["members"]) == 3
    repos = [m["desired_repo"] for m in result["members"]]
    assert sorted(repos) == ["repo-a", "repo-b", "repo-c"]
    member_ids = {m["warroom_member_id"] for m in result["members"]}
    assert len(member_ids) == 3

    with db() as conn:
        rows = conn.execute(
            "SELECT warroom_member_id, desired_role, desired_repo, spawn_order "
            "FROM warroom_members WHERE warroom_id = ? ORDER BY spawn_order",
            ("repo-wr",),
        ).fetchall()
        assert len(rows) == 3
        assert all(r["desired_role"] == "general" for r in rows)
        assert [r["desired_repo"] for r in rows] == ["repo-a", "repo-b", "repo-c"]
        assert [r["spawn_order"] for r in rows] == [0, 1, 2]
        assert len({r["warroom_member_id"] for r in rows}) == 3

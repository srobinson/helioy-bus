"""Contract tests for the second Claude runtime instance (``claude-opus``).

Kept separate from ``test_runtime_adapters.py`` to keep each file under
the repo's 700-line threshold. This module pins:

* one registered ClaudeRuntimeAdapter instance per selectable model,
  with ``claude`` (fable) remaining the default runtime
* launch commands pin HELIOY_RUNTIME so hook registration records the
  exact runtime id instead of defaulting to "claude"
* both instances discover the same plugin catalogue, each under its own
  runtime id
"""

from __future__ import annotations

from server.runtimes import RuntimeAdapter, default_adapter, for_id
from server.runtimes.claude import CLAUDE, CLAUDE_OPUS


# ── Identity metadata ────────────────────────────────────────────────────────


def test_claude_runtime_ids_map_one_instance_per_model():
    assert CLAUDE.runtime_id == "claude"
    assert CLAUDE.model == "claude-fable-5[1m]"
    assert CLAUDE_OPUS.runtime_id == "claude-opus"
    assert CLAUDE_OPUS.model == "claude-opus-5[1m]"


def test_claude_opus_registration_does_not_evict_claude_as_default():
    assert for_id("claude-opus") is CLAUDE_OPUS
    assert default_adapter() is CLAUDE


def test_claude_opus_satisfies_runtime_adapter_protocol():
    assert isinstance(CLAUDE_OPUS, RuntimeAdapter)


def test_claude_self_pid_env_is_shared_across_models():
    """Both claude runtime ids share one env name: the SessionStart hook
    keys the PID file per pane, so identity resolution only needs the
    name to find it."""
    assert CLAUDE_OPUS.self_pid_env == CLAUDE.self_pid_env == "HELIOY_BUS_CLAUDE_PID"


# ── Launch command ───────────────────────────────────────────────────────────


def test_claude_opus_launch_command_pins_runtime_and_model():
    """HELIOY_RUNTIME must carry the exact runtime id: without it the
    SessionStart hook's resolve_runtime falls back to "claude" and the
    registration misrecords the pane's runtime."""
    cmd = CLAUDE_OPUS.build_launch_command(qualified_name=None)
    assert cmd.startswith("HELIOY_RUNTIME=claude-opus claude ")
    assert '--model "claude-opus-5[1m]"' in cmd
    assert "--dangerously-skip-permissions" in cmd
    assert "--agent" not in cmd


def test_claude_opus_launch_command_role_mode_includes_agent_flag():
    cmd = CLAUDE_OPUS.build_launch_command(qualified_name="helioy-tools:backend-engineer")
    assert cmd.startswith("HELIOY_RUNTIME=claude-opus claude ")
    assert '--model "claude-opus-5[1m]"' in cmd
    assert "--agent helioy-tools:backend-engineer" in cmd


# ── Discovery ────────────────────────────────────────────────────────────────


def test_claude_opus_discovers_same_catalogue_under_own_runtime(fake_plugins):
    claude_agents = CLAUDE.discover_agent_types()
    opus_agents = CLAUDE_OPUS.discover_agent_types()

    assert [a["qualified_name"] for a in opus_agents] == [
        a["qualified_name"] for a in claude_agents
    ]
    assert all(a["runtime"] == "claude" for a in claude_agents)
    assert all(a["runtime"] == "claude-opus" for a in opus_agents)


# ── Warroom integration ──────────────────────────────────────────────────────


def test_warroom_add_accepts_claude_opus_runtime(fake_plugins, monkeypatch):
    """MoE flow: a specialist role resolves within the claude-opus
    catalogue and the member records the claude-opus runtime."""
    from server._db import _now, db
    from server._tmux import gateway

    import server.warroom_server as wm

    now = _now()
    with db() as conn:
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute(
            "INSERT INTO warrooms (warroom_id, tmux_session, tmux_window, cwd, created_at, status) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            ("opus-moe", "main", "opus-moe", "/tmp/project", now, "active"),
        )

    seen_runtimes: list = []

    def mock_spawn_pane(**kw):
        seen_runtimes.append(kw["runtime"])
        return {
            "agent_type": kw["agent_type"],
            "qualified_name": kw["qualified_name"],
            "tmux_target": "main:1.0",
            "pane_id": "%10",
        }

    monkeypatch.setattr(gateway, "spawn_pane", mock_spawn_pane)
    monkeypatch.setattr(gateway, "target_for_pane", lambda pane_id: "main:1.0", raising=False)
    monkeypatch.setattr(gateway, "set_pane_title", lambda pane_id, title: None, raising=False)
    monkeypatch.setattr(
        gateway,
        "launch_pane",
        lambda **kw: {
            "agent_type": kw["agent_type"],
            "qualified_name": kw["qualified_name"],
            "tmux_target": kw["tmux_target"],
            "pane_id": kw["pane_id"],
            "runtime": kw["runtime"],
        },
        raising=False,
    )

    result = wm.warroom_add(name="opus-moe", agent="codebase-analyst", runtime="claude-opus")
    assert "error" not in result
    assert seen_runtimes == ["claude-opus"]
    assert result["added"]["desired_runtime"] == "claude-opus"
    assert result["added"]["qualified_name"] == "helioy-tools:codebase-analyst"

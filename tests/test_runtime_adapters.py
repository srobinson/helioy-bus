"""Tests for the RuntimeAdapter contract and the Claude adapter."""

from __future__ import annotations

from pathlib import Path

import pytest

from server.runtimes import (
    ClaudeRuntimeAdapter,
    RuntimeAdapter,
    default_adapter,
    for_id,
    register,
)
from server.runtimes.claude import CLAUDE


# ── Claude adapter: identity metadata ────────────────────────────────────────


def test_claude_adapter_runtime_id_is_claude():
    assert CLAUDE.runtime_id == "claude"


def test_claude_adapter_self_pid_env_matches_historic_name():
    """Hook and proxy agreed on HELIOY_BUS_CLAUDE_PID; the adapter must match."""
    assert CLAUDE.self_pid_env == "HELIOY_BUS_CLAUDE_PID"


def test_claude_adapter_agents_cache_dir_points_to_claude_plugin_cache():
    expected = Path.home() / ".claude" / "plugins" / "cache"
    assert CLAUDE.agents_cache_dir() == expected


def test_claude_adapter_satisfies_runtime_adapter_protocol():
    """Protocol check — catches missing/rename-drifted members at import time."""
    assert isinstance(CLAUDE, RuntimeAdapter)


# ── Claude adapter: launch command ───────────────────────────────────────────


def test_build_launch_command_role_mode_includes_agent_flag():
    cmd = CLAUDE.build_launch_command(qualified_name="helioy-tools:backend-engineer")
    assert cmd.startswith("claude ")
    assert "--dangerously-skip-permissions" in cmd
    assert "--model opus" in cmd
    assert "--effort max" in cmd
    assert "--agent helioy-tools:backend-engineer" in cmd


def test_build_launch_command_repo_mode_omits_agent_flag():
    cmd = CLAUDE.build_launch_command(qualified_name=None)
    assert "--dangerously-skip-permissions" in cmd
    assert "--model opus" in cmd
    assert "--effort max" in cmd
    assert "--agent" not in cmd


# ── Registry behavior ────────────────────────────────────────────────────────


def test_default_adapter_resolves_to_claude_while_incumbent():
    assert default_adapter() is CLAUDE


def test_for_id_looks_up_registered_adapter():
    assert for_id("claude") is CLAUDE


def test_for_id_raises_on_unknown_runtime():
    with pytest.raises(KeyError):
        for_id("does-not-exist")


def test_register_accepts_additional_adapter_without_evicting_default():
    """Registering a new adapter keeps the existing default unless explicitly overridden."""

    class FakeAdapter:
        runtime_id = "fake"
        self_pid_env = "FAKE_PID"

        def build_launch_command(self, *, qualified_name):
            return "fake"

        def agents_cache_dir(self):
            return Path("/tmp/fake")

    fake = FakeAdapter()
    try:
        register(fake)
        assert for_id("fake") is fake
        assert default_adapter() is CLAUDE
    finally:
        # Clean up module-level registry state
        from server.runtimes import base as _base
        _base._adapters.pop("fake", None)


# ── Core integration: adapter drives launch command in spawn_pane ────────────


def test_spawn_pane_delegates_launch_command_to_adapter(monkeypatch):
    """TmuxGateway.spawn_pane must not hardcode the runtime command."""
    import server._tmux as tmux_mod

    call_log: list[tuple[str, ...]] = []

    def fake_run(self, *args, timeout=None):
        call_log.append(args)
        if args[0] == "new-window":
            return "%1"
        if args[0] == "display-message":
            return "alp:1.0"
        return ""

    monkeypatch.setattr(tmux_mod.TmuxGateway, "_run", fake_run)

    marker = "SENTINEL-LAUNCH-FROM-ADAPTER --agent foo:bar"

    class StubAdapter:
        runtime_id = "stub"
        self_pid_env = "STUB_PID"

        def build_launch_command(self, *, qualified_name):
            return marker

        def agents_cache_dir(self):
            return Path("/tmp/stub")

    monkeypatch.setattr(
        tmux_mod, "default_adapter", lambda: StubAdapter()
    )

    tmux_mod.gateway.spawn_pane(
        session="alp",
        window="wr",
        cwd="/tmp/repo",
        agent_type="backend-engineer",
        qualified_name="foo:bar",
        is_first=True,
        layout="tiled",
    )

    send_keys_calls = [c for c in call_log if c[0] == "send-keys"]
    assert send_keys_calls, "spawn_pane did not issue send-keys"
    assert marker in send_keys_calls[0], (
        "spawn_pane did not use the adapter's launch command"
    )


# ── Core integration: _self_agent_id reads adapter-declared env var ──────────


def test_self_agent_id_reads_env_var_from_adapter(tmp_path, monkeypatch):
    """_self_agent_id must consult the env var the active adapter declares."""
    import server._db as _db_mod
    import server._identity as identity_mod

    monkeypatch.setattr(_db_mod, "BUS_DIR", tmp_path)
    pids = tmp_path / "pids"
    pids.mkdir()
    (pids / "99999").write_text("my-repo:general:1.0")
    monkeypatch.setenv(CLAUDE.self_pid_env, "99999")
    monkeypatch.delenv("HELIOY_BUS_AGENT_TYPE", raising=False)
    monkeypatch.delenv("HELIOY_AGENT_TYPE", raising=False)

    resolved = identity_mod._self_agent_id()
    assert resolved == "my-repo:general:1.0"


# ── Core integration: warroom INSERTs use adapter.runtime_id ─────────────────


def test_warroom_spawn_records_runtime_from_adapter(monkeypatch):
    """warroom.spawn must persist adapter.runtime_id, not a hardcoded literal."""
    import server._db as _db_mod
    import server._tmux as tmux_mod
    import server.services.warroom as warroom_service

    fake_agent = {
        "qualified_name": "foo:bar",
        "name": "bar",
        "namespace": "foo",
        "summary": "",
        "model": "opus",
    }
    monkeypatch.setattr(
        warroom_service, "_scan_agent_types", lambda: [fake_agent]
    )
    monkeypatch.setattr(
        warroom_service, "_resolve_agent_type",
        lambda name: fake_agent if name in {"bar", "foo:bar"} else None,
    )
    monkeypatch.setattr(
        tmux_mod.gateway, "current_session_name", lambda: "alp"
    )
    monkeypatch.setattr(
        tmux_mod.gateway, "spawn_pane",
        lambda **kw: {
            "agent_type": fake_agent["name"],
            "qualified_name": fake_agent["qualified_name"],
            "tmux_target": "alp:1.0",
            "pane_id": "%1",
        },
    )
    monkeypatch.setenv("TMUX", "/tmp/tmux-sock")

    sentinel = "sentinel-runtime-xyz"

    class StubAdapter:
        runtime_id = sentinel
        self_pid_env = "STUB_PID"

        def build_launch_command(self, *, qualified_name):
            return "stub"

        def agents_cache_dir(self):
            return Path("/tmp")

    monkeypatch.setattr(
        warroom_service, "default_adapter", lambda: StubAdapter()
    )

    result = warroom_service.spawn(name="wr", agents=["bar"], cwd="/tmp/r")
    assert "error" not in result, result

    with _db_mod.db() as conn:
        runtimes = [
            row["runtime"]
            for row in conn.execute(
                "SELECT runtime FROM warroom_members WHERE warroom_id = ?",
                ("wr",),
            )
        ]
    assert runtimes == [sentinel]

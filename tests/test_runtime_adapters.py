"""Tests for the RuntimeAdapter contract, Claude adapter, and Codex adapter."""

from __future__ import annotations

from pathlib import Path

import pytest

from server.runtimes import (
    ClaudeRuntimeAdapter,
    CodexRuntimeAdapter,
    RuntimeAdapter,
    default_adapter,
    for_id,
    register,
)
from server.runtimes.claude import CLAUDE
from server.runtimes.codex import CODEX


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
        warroom_service, "_scan_agent_types",
        lambda runtime_id=None: [fake_agent],
    )
    monkeypatch.setattr(
        warroom_service, "_resolve_agent_type",
        lambda name, runtime_id=None: fake_agent if name in {"bar", "foo:bar"} else None,
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


# ── Codex adapter: identity metadata ─────────────────────────────────────────


def test_codex_adapter_runtime_id_is_codex():
    assert CODEX.runtime_id == "codex"


def test_codex_adapter_self_pid_env_is_helioy_bus_codex_pid():
    """Follows the HELIOY_BUS_<RUNTIME>_PID convention used by the Claude hook."""
    assert CODEX.self_pid_env == "HELIOY_BUS_CODEX_PID"


def test_codex_adapter_agents_cache_dir_points_to_codex_skills():
    # Autouse isolated_codex_cache fixture monkeypatches CODEX.agents_cache_dir
    # to a tmp dir so union-discovery tests are hermetic; instantiate a fresh
    # adapter here to assert the real default location.
    assert CodexRuntimeAdapter().agents_cache_dir() == Path.home() / ".codex" / "skills"


def test_codex_adapter_satisfies_runtime_adapter_protocol():
    assert isinstance(CODEX, RuntimeAdapter)


def test_codex_adapter_is_distinct_class_from_claude():
    """Each runtime gets its own class, not a relabel of Claude's."""
    assert isinstance(CODEX, CodexRuntimeAdapter)
    assert not isinstance(CODEX, ClaudeRuntimeAdapter)


# ── Codex adapter: launch command ────────────────────────────────────────────


def test_codex_build_launch_command_points_at_launch_wrapper():
    """Codex launch goes through codex-launch.sh so the pane auto-registers."""
    cmd = CODEX.build_launch_command(qualified_name=None)
    wrapper = Path(cmd)
    assert wrapper.name == "codex-launch.sh"
    assert wrapper.exists(), f"wrapper missing: {wrapper}"
    # Wrapper drives codex with the bypass flag internally.
    content = wrapper.read_text()
    assert "codex --dangerously-bypass-approvals-and-sandbox" in content


def test_codex_build_launch_command_ignores_qualified_name():
    """Codex has no --agent flag; role-mode must not emit one."""
    bare = CODEX.build_launch_command(qualified_name=None)
    role = CODEX.build_launch_command(qualified_name="helioy-tools:backend-engineer")
    assert bare == role
    assert "--agent" not in role


# ── Registry: coexistence of Claude and Codex ────────────────────────────────


def test_register_codex_does_not_evict_claude_as_default():
    """Codex is registered on import; Claude must remain the default runtime."""
    assert default_adapter() is CLAUDE
    assert for_id("codex") is CODEX
    assert for_id("claude") is CLAUDE


# ── Per-runtime dispatch in spawn_pane ───────────────────────────────────────


def test_spawn_pane_uses_for_id_when_runtime_specified(monkeypatch):
    """spawn_pane(runtime="codex") dispatches via for_id, not default_adapter."""
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

    result = tmux_mod.gateway.spawn_pane(
        session="alp",
        window="wr",
        cwd="/tmp/repo",
        agent_type="general",
        qualified_name=None,
        is_first=True,
        layout="tiled",
        runtime="codex",
    )

    send_keys_calls = [c for c in call_log if c[0] == "send-keys"]
    assert send_keys_calls, "spawn_pane did not issue send-keys"
    # Codex adapter now launches through the bus-registering wrapper.
    assert any("codex-launch.sh" in arg for arg in send_keys_calls[0])
    assert result["runtime"] == "codex"


def test_spawn_pane_rejects_unregistered_runtime(monkeypatch):
    """spawn_pane surfaces KeyError for unknown runtimes rather than silently defaulting."""
    import server._tmux as tmux_mod

    monkeypatch.setattr(
        tmux_mod.TmuxGateway, "_run", lambda self, *a, **kw: "%1"
    )

    with pytest.raises(KeyError):
        tmux_mod.gateway.spawn_pane(
            session="alp",
            window="wr",
            cwd="/tmp",
            agent_type="general",
            qualified_name=None,
            is_first=True,
            layout="tiled",
            runtime="nonexistent",
        )


# ── warroom service: per-warroom runtime selection ───────────────────────────


def test_warroom_spawn_persists_requested_codex_runtime(monkeypatch):
    """warroom.spawn(runtime='codex') records 'codex' in the member row."""
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
        warroom_service, "_scan_agent_types",
        lambda runtime_id=None: [fake_agent],
    )
    monkeypatch.setattr(
        warroom_service, "_resolve_agent_type",
        lambda name, runtime_id=None: fake_agent if name in {"bar", "foo:bar"} else None,
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
            "runtime": kw["runtime"],
        },
    )
    monkeypatch.setenv("TMUX", "/tmp/tmux-sock")

    result = warroom_service.spawn(
        name="wr", agents=["bar"], cwd="/tmp/r", runtime="codex"
    )
    assert "error" not in result, result

    with _db_mod.db() as conn:
        runtimes = [
            row["runtime"]
            for row in conn.execute(
                "SELECT runtime FROM warroom_members WHERE warroom_id = ?",
                ("wr",),
            )
        ]
    assert runtimes == ["codex"]


def test_warroom_spawn_threads_runtime_to_spawn_pane(monkeypatch):
    """The runtime arg must reach gateway.spawn_pane so per-pane dispatch works."""
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
        warroom_service, "_scan_agent_types",
        lambda runtime_id=None: [fake_agent],
    )
    monkeypatch.setattr(
        warroom_service, "_resolve_agent_type",
        lambda name, runtime_id=None: fake_agent if name in {"bar", "foo:bar"} else None,
    )
    monkeypatch.setattr(tmux_mod.gateway, "current_session_name", lambda: "alp")

    captured: list[str | None] = []

    def capture(**kw):
        captured.append(kw.get("runtime"))
        return {
            "agent_type": fake_agent["name"],
            "qualified_name": fake_agent["qualified_name"],
            "tmux_target": "alp:1.0",
            "pane_id": "%1",
            "runtime": kw.get("runtime") or "claude",
        }

    monkeypatch.setattr(tmux_mod.gateway, "spawn_pane", capture)
    monkeypatch.setenv("TMUX", "/tmp/tmux-sock")

    warroom_service.spawn(name="wr2", agents=["bar"], cwd="/tmp/r", runtime="codex")
    assert captured == ["codex"]


def test_warroom_spawn_rejects_unknown_runtime(monkeypatch):
    """warroom.spawn with an unregistered runtime returns a helpful error."""
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
        warroom_service, "_scan_agent_types",
        lambda runtime_id=None: [fake_agent],
    )
    monkeypatch.setattr(
        warroom_service, "_resolve_agent_type",
        lambda name, runtime_id=None: fake_agent if name in {"bar", "foo:bar"} else None,
    )
    monkeypatch.setattr(tmux_mod.gateway, "current_session_name", lambda: "alp")
    monkeypatch.setenv("TMUX", "/tmp/tmux-sock")

    result = warroom_service.spawn(
        name="wr3", agents=["bar"], cwd="/tmp/r", runtime="nonexistent"
    )
    assert "error" in result
    assert "nonexistent" in result["error"]
    assert "claude" in result["error"]
    assert "codex" in result["error"]


def test_warroom_add_persists_requested_codex_runtime(monkeypatch):
    """warroom.add(runtime='codex') mixes runtimes within one warroom."""
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
        warroom_service, "_scan_agent_types",
        lambda runtime_id=None: [fake_agent],
    )
    monkeypatch.setattr(
        warroom_service, "_resolve_agent_type",
        lambda name, runtime_id=None: fake_agent if name in {"bar", "foo:bar"} else None,
    )
    monkeypatch.setattr(tmux_mod.gateway, "current_session_name", lambda: "alp")
    monkeypatch.setattr(
        tmux_mod.gateway, "spawn_pane",
        lambda **kw: {
            "agent_type": fake_agent["name"],
            "qualified_name": fake_agent["qualified_name"],
            "tmux_target": "alp:1.1",
            "pane_id": "%2",
            "runtime": kw["runtime"],
        },
    )
    monkeypatch.setenv("TMUX", "/tmp/tmux-sock")

    # First create a Claude-default warroom, then add a Codex member.
    spawn_result = warroom_service.spawn(
        name="mix", agents=["bar"], cwd="/tmp/r"
    )
    assert "error" not in spawn_result

    add_result = warroom_service.add(
        name="mix", agent="bar", runtime="codex"
    )
    assert "error" not in add_result

    with _db_mod.db() as conn:
        runtimes = [
            row["runtime"]
            for row in conn.execute(
                "SELECT runtime FROM warroom_members "
                "WHERE warroom_id = ? ORDER BY spawn_order",
                ("mix",),
            )
        ]
    assert runtimes == ["claude", "codex"]


# ── Identity: multi-runtime self PID resolution ──────────────────────────────


def test_self_agent_id_resolves_via_codex_pid_env(tmp_path, monkeypatch):
    """When only Codex's PID env is set, _self_agent_id finds the mapping."""
    import server._db as _db_mod
    import server._identity as identity_mod

    monkeypatch.setattr(_db_mod, "BUS_DIR", tmp_path)
    pids = tmp_path / "pids"
    pids.mkdir()
    (pids / "77777").write_text("codex-repo:general:2.0")
    monkeypatch.delenv(CLAUDE.self_pid_env, raising=False)
    monkeypatch.setenv(CODEX.self_pid_env, "77777")
    monkeypatch.delenv("HELIOY_BUS_AGENT_TYPE", raising=False)
    monkeypatch.delenv("HELIOY_AGENT_TYPE", raising=False)

    resolved = identity_mod._self_agent_id()
    assert resolved == "codex-repo:general:2.0"


# ── Proxy: runtime-aware env seeding ─────────────────────────────────────────


def test_proxy_build_inner_env_seeds_default_when_no_runtime_env_set():
    """With no upstream wrapper, proxy sets the default adapter's PID env."""
    from server.proxy import build_inner_env

    env = build_inner_env({}, parent_pid=1234)
    # Default is Claude while it remains the incumbent.
    assert env[CLAUDE.self_pid_env] == "1234"
    assert CODEX.self_pid_env not in env


def test_proxy_build_inner_env_preserves_upstream_codex_pid():
    """When codex-launch.sh has set HELIOY_BUS_CODEX_PID, proxy preserves it
    and does not overwrite HELIOY_BUS_CLAUDE_PID with the spurious
    parent PID of a Codex-hosted MCP subprocess.
    """
    from server.proxy import build_inner_env

    env = build_inner_env({CODEX.self_pid_env: "5000"}, parent_pid=6000)
    assert env[CODEX.self_pid_env] == "5000"
    # Proxy must NOT set Claude's env for a Codex session.
    assert CLAUDE.self_pid_env not in env


def test_proxy_build_inner_env_preserves_upstream_claude_pid():
    """An already-set Claude PID env is not overwritten with a later ppid."""
    from server.proxy import build_inner_env

    env = build_inner_env({CLAUDE.self_pid_env: "2000"}, parent_pid=9999)
    assert env[CLAUDE.self_pid_env] == "2000"

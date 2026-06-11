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
from server.services import warroom_agents

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
    """Protocol check: catches missing/rename-drifted members at import time."""
    assert isinstance(CLAUDE, RuntimeAdapter)


def test_claude_adapter_message_suffix_is_empty():
    """Claude acts on bus messages directly; no authorization preamble."""
    assert CLAUDE.message_suffix == ""


# ── Claude adapter: launch command ───────────────────────────────────────────


def test_build_launch_command_role_mode_includes_agent_flag():
    cmd = CLAUDE.build_launch_command(qualified_name="helioy-tools:backend-engineer")
    assert cmd.startswith("claude ")
    assert "--dangerously-skip-permissions" in cmd
    assert "--model claude-fable-5" in cmd
    assert "--effort xhigh" in cmd
    assert "--agent helioy-tools:backend-engineer" in cmd


def test_build_launch_command_repo_mode_omits_agent_flag():
    cmd = CLAUDE.build_launch_command(qualified_name=None)
    assert "--dangerously-skip-permissions" in cmd
    assert "--model claude-fable-5" in cmd
    assert "--effort xhigh" in cmd
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

    monkeypatch.setattr(tmux_mod, "default_adapter", lambda: StubAdapter())

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
    assert marker in send_keys_calls[0], "spawn_pane did not use the adapter's launch command"


def test_spawn_pane_can_defer_runtime_launch(monkeypatch):
    """warroom_add needs a pane split before the runtime registers."""
    import server._tmux as tmux_mod

    call_log: list[tuple[str, ...]] = []

    def fake_run(self, *args, timeout=None):
        call_log.append(args)
        if args[0] == "split-window":
            return "%2"
        if args[0] == "display-message":
            return "alp:1.1"
        return ""

    monkeypatch.setattr(tmux_mod.TmuxGateway, "_run", fake_run)

    result = tmux_mod.gateway.spawn_pane(
        session="alp",
        window="wr",
        cwd="/tmp/repo",
        agent_type="backend-engineer",
        qualified_name="foo:bar",
        is_first=False,
        layout="tiled",
        launch=False,
    )

    assert result["tmux_target"] == "alp:1.1"
    assert result["pane_id"] == "%2"
    assert not [c for c in call_log if c[0] == "send-keys"]
    assert not [c for c in call_log if c[0] == "select-pane"]


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
        warroom_agents,
        "_scan_agent_types",
        lambda runtime_id=None: [fake_agent],
    )
    monkeypatch.setattr(
        warroom_agents,
        "_resolve_agent_type",
        lambda name, runtime_id=None: fake_agent if name in {"bar", "foo:bar"} else None,
    )
    monkeypatch.setattr(tmux_mod.gateway, "current_session_name", lambda: "alp")
    monkeypatch.setattr(
        tmux_mod.gateway,
        "spawn_pane",
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
        supports_specialist_roles = True

        def build_launch_command(self, *, qualified_name):
            return "stub"

        def agents_cache_dir(self):
            return Path("/tmp")

    monkeypatch.setattr(warroom_service, "default_adapter", lambda: StubAdapter())

    result = warroom_service.spawn(name="wr", agents=["bar"], cwd="/tmp/r")
    assert "error" not in result, result

    with _db_mod.db() as conn:
        runtimes = [
            row["desired_runtime"]
            for row in conn.execute(
                "SELECT desired_runtime FROM warroom_members WHERE warroom_id = ?",
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


def test_codex_adapter_shared_skills_dir_points_to_agents_skills():
    assert CodexRuntimeAdapter().shared_skills_dir() == Path.home() / ".agents" / "skills"


def test_codex_adapter_satisfies_runtime_adapter_protocol():
    assert isinstance(CODEX, RuntimeAdapter)


def test_codex_adapter_is_distinct_class_from_claude():
    """Each runtime gets its own class, not a relabel of Claude's."""
    assert isinstance(CODEX, CodexRuntimeAdapter)
    assert not isinstance(CODEX, ClaudeRuntimeAdapter)


def test_codex_adapter_message_suffix_grants_authorization():
    """Codex prompts the human before acting on unsolicited bus messages;
    the suffix is an in-message authorization that keeps the agent
    autonomous. Contract: non-empty, includes the authorization phrase,
    and invites a reply to sender for questions."""
    suffix = CODEX.message_suffix
    assert suffix
    assert "authorized to act" in suffix
    assert "reply to sender" in suffix


# ── Codex adapter: launch command ────────────────────────────────────────────


def test_codex_build_launch_command_points_at_launch_wrapper():
    """Codex launch goes through codex-launch.sh so the pane auto-registers."""
    cmd = CODEX.build_launch_command(qualified_name=None)
    wrapper = Path(cmd)
    assert wrapper.name == "codex-launch.sh"
    assert wrapper.exists(), f"wrapper missing: {wrapper}"
    # Wrapper forces non-interactive Codex panes through the approved launch mode.
    content = wrapper.read_text()
    assert 'codex --dangerously-bypass-approvals-and-sandbox "$@"' in content


def test_codex_build_launch_command_adds_model_instructions_file(fake_codex_instructions):
    """Codex specialist launch binds the role through model_instructions_file."""
    bare = CODEX.build_launch_command(qualified_name=None)
    role = CODEX.build_launch_command(qualified_name="codex:agent-browser")
    assert bare != role
    assert "--agent" not in role
    assert "--config" in role
    assert "model_instructions_file=" in role
    assert str(fake_codex_instructions / "agent-browser.md") in role


def test_codex_build_launch_command_injects_agent_type_env(fake_codex_instructions):
    """Specialist launch carries its role in the env.

    Codex overwrites its pane title to the cwd basename after start, so the
    pane's own SessionStart hook can no longer read the canonical title the
    warroom set. HELIOY_BUS_AGENT_TYPE lets that re-registration reconstruct
    the same identity instead of collapsing to general (which would evict the
    correct row via pane ownership). A bare/general launch carries no role env.
    """
    role = CODEX.build_launch_command(qualified_name="codex:agent-browser")
    assert "HELIOY_BUS_AGENT_TYPE=codex:agent-browser" in role
    assert "model_instructions_file=" in role

    bare = CODEX.build_launch_command(qualified_name=None)
    assert "HELIOY_BUS_AGENT_TYPE" not in bare


def test_codex_build_launch_command_resolves_frontmatter_role_name(
    isolated_codex_cache,
):
    """Codex launch follows discovered frontmatter names, not just filenames."""
    instructions = isolated_codex_cache["instructions"]
    path = instructions / "ux.md"
    path.write_text('---\nname: ux-designer\ndescription: "UX role"\n---\n')

    role = CODEX.build_launch_command(qualified_name="codex:ux-designer")

    assert str(path) in role


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


def test_spawn_pane_codex_specialist_includes_model_instructions_file(
    fake_codex_instructions, monkeypatch
):
    """Codex specialist panes receive the role instructions config flag."""
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
        agent_type="agent-browser",
        qualified_name="codex:agent-browser",
        is_first=True,
        layout="tiled",
        runtime="codex",
    )

    send_keys_calls = [c for c in call_log if c[0] == "send-keys"]
    assert send_keys_calls, "spawn_pane did not issue send-keys"
    cmd = send_keys_calls[0][3]
    assert "codex-launch.sh" in cmd
    assert "--config" in cmd
    assert "model_instructions_file=" in cmd
    assert str(fake_codex_instructions / "agent-browser.md") in cmd
    assert result["runtime"] == "codex"


def test_spawn_pane_rejects_unregistered_runtime(monkeypatch):
    """spawn_pane surfaces KeyError for unknown runtimes rather than silently defaulting."""
    import server._tmux as tmux_mod

    monkeypatch.setattr(tmux_mod.TmuxGateway, "_run", lambda self, *a, **kw: "%1")

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


def test_warroom_spawn_repos_persists_requested_codex_runtime(monkeypatch, tmp_path):
    """warroom.spawn_repos(runtime='codex') records 'codex' in the member row.

    Repo mode remains the no-specialist Codex path and must persist the
    requested runtime.
    """
    import server._db as _db_mod
    import server._tmux as tmux_mod
    import server.services.warroom as warroom_service

    for repo in ("a", "b"):
        (tmp_path / repo / ".git").mkdir(parents=True)
    monkeypatch.setenv("HELIOY_BASE", str(tmp_path))
    monkeypatch.setenv("TMUX", "/tmp/tmux-sock")
    monkeypatch.setattr(tmux_mod.gateway, "current_session_name", lambda: "alp")
    pane_counter = [0]

    def fake_spawn(**kw):
        idx = pane_counter[0]
        pane_counter[0] += 1
        return {
            "agent_type": kw.get("agent_type"),
            "qualified_name": kw.get("qualified_name"),
            "tmux_target": f"alp:1.{idx}",
            "pane_id": f"%{idx}",
            "runtime": kw.get("runtime"),
        }

    monkeypatch.setattr(tmux_mod.gateway, "spawn_pane", fake_spawn)

    result = warroom_service.spawn_repos(window="repo-wr", runtime="codex")
    assert "error" not in result, result

    with _db_mod.db() as conn:
        runtimes = [
            row["desired_runtime"]
            for row in conn.execute(
                "SELECT desired_runtime FROM warroom_members WHERE warroom_id = ?",
                ("repo-wr",),
            )
        ]
    assert runtimes == ["codex", "codex"]


def test_warroom_spawn_repos_threads_runtime_to_spawn_pane(monkeypatch, tmp_path):
    """The runtime arg must reach gateway.spawn_pane so per-pane dispatch works.

    Uses ``spawn_repos`` to prove the general-mode runtime kwarg reaches
    the lower-level tmux gateway.
    """
    import server._tmux as tmux_mod
    import server.services.warroom as warroom_service

    (tmp_path / "only-repo" / ".git").mkdir(parents=True)
    monkeypatch.setenv("HELIOY_BASE", str(tmp_path))
    monkeypatch.setenv("TMUX", "/tmp/tmux-sock")
    monkeypatch.setattr(tmux_mod.gateway, "current_session_name", lambda: "alp")

    captured: list[str | None] = []

    def capture(**kw):
        captured.append(kw.get("runtime"))
        return {
            "agent_type": kw.get("agent_type"),
            "qualified_name": kw.get("qualified_name"),
            "tmux_target": "alp:1.0",
            "pane_id": "%1",
            "runtime": kw.get("runtime"),
        }

    monkeypatch.setattr(tmux_mod.gateway, "spawn_pane", capture)

    warroom_service.spawn_repos(window="wr2", runtime="codex")
    assert captured == ["codex"]


def test_warroom_spawn_rejects_unknown_runtime(monkeypatch):
    """warroom.spawn with an unregistered runtime returns a helpful error."""
    import server._tmux as tmux_mod
    import server.services.warroom as warroom_service

    monkeypatch.setattr(tmux_mod.gateway, "current_session_name", lambda: "alp")
    monkeypatch.setenv("TMUX", "/tmp/tmux-sock")

    result = warroom_service.spawn(name="wr3", agents=["bar"], cwd="/tmp/r", runtime="nonexistent")
    assert "error" in result
    assert "nonexistent" in result["error"]
    assert "claude" in result["error"]
    assert "codex" in result["error"]


# ── Specialist-role capability ───────────────────────────────────────────────


def test_claude_adapter_supports_specialist_roles_is_true():
    """Claude enacts specialist roles via ``--agent <qualified-name>``."""
    assert CLAUDE.supports_specialist_roles is True


def test_codex_adapter_supports_specialist_roles_via_instruction_files():
    """Codex enacts specialist roles via model_instructions_file at launch.

    The adapter only discovers roles backed by instruction files, so
    warroom validation still rejects missing role names before spawning.
    """
    assert CODEX.supports_specialist_roles is True


def test_warroom_spawn_accepts_codex_specialist_with_instruction_file(
    fake_codex_instructions, monkeypatch
):
    """warroom.spawn(runtime='codex', agents=[...]) persists a Codex role."""
    import server._db as _db_mod
    import server._tmux as tmux_mod
    import server.services.warroom as warroom_service

    monkeypatch.setattr(tmux_mod.gateway, "current_session_name", lambda: "alp")
    monkeypatch.setenv("TMUX", "/tmp/tmux-sock")
    monkeypatch.setattr(
        tmux_mod.gateway,
        "spawn_pane",
        lambda **kw: {
            "agent_type": kw["agent_type"],
            "qualified_name": kw["qualified_name"],
            "tmux_target": "alp:1.0",
            "pane_id": "%1",
            "runtime": kw["runtime"],
        },
    )

    result = warroom_service.spawn(
        name="codex-role",
        agents=["agent-browser"],
        cwd="/tmp/r",
        runtime="codex",
    )
    assert "error" not in result
    assert result["members"][0]["qualified_name"] == "codex:agent-browser"
    assert result["members"][0]["desired_runtime"] == "codex"

    with _db_mod.db() as conn:
        rows = conn.execute(
            "SELECT desired_runtime, desired_role FROM warroom_members WHERE warroom_id = ?",
            ("codex-role",),
        ).fetchall()
    assert [(r["desired_runtime"], r["desired_role"]) for r in rows] == [
        ("codex", "codex:agent-browser")
    ]


def test_warroom_add_accepts_codex_specialist_with_instruction_file(
    fake_codex_instructions, monkeypatch
):
    """warroom.add(runtime='codex', agent=...) can add a Codex specialist."""
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
    codex_agent = {
        "qualified_name": "codex:agent-browser",
        "name": "agent-browser",
        "namespace": "codex",
        "summary": "",
        "model": "",
    }
    monkeypatch.setattr(
        warroom_service,
        "_scan_agent_types",
        lambda runtime_id=None: [codex_agent] if runtime_id == "codex" else [fake_agent],
    )
    monkeypatch.setattr(
        warroom_service,
        "_resolve_agent_type",
        lambda name, runtime_id=None: (
            fake_agent
            if runtime_id in (None, "claude") and name in {"bar", "foo:bar"}
            else codex_agent
            if runtime_id == "codex" and name in {"agent-browser", "codex:agent-browser"}
            else None
        ),
    )
    monkeypatch.setattr(warroom_agents, "_scan_agent_types", warroom_service._scan_agent_types)
    monkeypatch.setattr(warroom_agents, "_resolve_agent_type", warroom_service._resolve_agent_type)
    monkeypatch.setattr(tmux_mod.gateway, "current_session_name", lambda: "alp")

    pane_counter = [0]

    def fake_spawn(**kw):
        idx = pane_counter[0]
        pane_counter[0] += 1
        return {
            "agent_type": kw["agent_type"],
            "qualified_name": kw["qualified_name"],
            "tmux_target": f"alp:1.{idx}",
            "pane_id": f"%{idx}",
            "runtime": kw["runtime"],
        }

    monkeypatch.setattr(tmux_mod.gateway, "spawn_pane", fake_spawn)
    monkeypatch.setattr(
        tmux_mod.gateway,
        "target_for_pane",
        lambda pane_id: {"%0": "alp:1.0", "%1": "alp:1.1"}[pane_id],
    )
    monkeypatch.setattr(tmux_mod.gateway, "set_pane_title", lambda pane_id, title: None)
    monkeypatch.setattr(
        tmux_mod.gateway,
        "launch_pane",
        lambda **kw: {
            "agent_type": kw["agent_type"],
            "qualified_name": kw["qualified_name"],
            "tmux_target": kw["tmux_target"],
            "pane_id": kw["pane_id"],
            "runtime": kw["runtime"],
        },
    )
    monkeypatch.setenv("TMUX", "/tmp/tmux-sock")

    # Claude warroom spawn succeeds (claude supports specialist roles).
    spawn_result = warroom_service.spawn(name="mix", agents=["bar"], cwd="/tmp/r")
    assert "error" not in spawn_result

    add_result = warroom_service.add(name="mix", agent="agent-browser", runtime="codex")
    assert "error" not in add_result
    assert add_result["added"]["qualified_name"] == "codex:agent-browser"

    with _db_mod.db() as conn:
        rows = [
            (row["desired_runtime"], row["desired_role"])
            for row in conn.execute(
                "SELECT desired_runtime, desired_role FROM warroom_members WHERE warroom_id = ? "
                "ORDER BY spawn_order",
                ("mix",),
            )
        ]
    assert rows == [("claude", "foo:bar"), ("codex", "codex:agent-browser")]


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

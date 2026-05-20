"""Tests for runtime-aware agent type discovery.

Covers the adapter-level ``discover_agent_types()`` contract (Claude plugin
cache, Codex launch instruction catalogue), the shared ``_scan_agent_types``
union/scoping logic, and the warroom-level consumers (``warroom_discover`` and
``warroom.spawn`` runtime-scoped validation).

These tests were extracted from ``test_runtime_adapters.py`` to keep each
test file under the 700-line refactor threshold.
"""

from __future__ import annotations

from server.runtimes.claude import CLAUDE
from server.runtimes.codex import CODEX


# ── Adapter discovery contract: Claude plugin cache ──────────────────────────


def test_claude_adapter_discover_agent_types_returns_sorted_qualified_names(fake_plugins):
    """Claude adapter walks its plugin cache and emits namespaced agents."""
    agents = CLAUDE.discover_agent_types()
    qualified = [a["qualified_name"] for a in agents]
    assert qualified == sorted(qualified)
    assert "helioy-tools:backend-engineer" in qualified
    assert "helioy-tools:frontend-engineer" in qualified
    assert "pr-review-toolkit:code-reviewer" in qualified
    assert "voltagent-lang:backend-engineer" in qualified


def test_claude_adapter_discover_tags_entries_with_runtime_id(fake_plugins):
    """Every Claude-discovered entry carries runtime='claude' for union consumers."""
    for agent in CLAUDE.discover_agent_types():
        assert agent["runtime"] == "claude"
        assert "_mtime" not in agent  # adapter-internal fields must be stripped


def test_claude_adapter_discover_returns_empty_when_cache_missing(tmp_path, monkeypatch):
    """No plugin cache dir means an empty list, never an exception."""
    monkeypatch.setattr(CLAUDE, "agents_cache_dir", lambda: tmp_path / "missing")
    assert CLAUDE.discover_agent_types() == []


# ── Adapter discovery contract: Codex model instructions ─────────────────────


def test_codex_adapter_discover_agent_types_returns_instruction_roles(
    fake_codex_instructions,
):
    """Codex discovery exposes launchable model-instructions roles."""
    agents = CODEX.discover_agent_types()
    qualified = [a["qualified_name"] for a in agents]
    assert qualified == [
        "codex:agent-browser",
        "codex:linear",
        "codex:openai-docs",
    ]
    assert all(a["namespace"] == "codex" for a in agents)
    assert all(a["runtime"] == "codex" for a in agents)


def test_codex_adapter_discover_ignores_non_markdown_files(fake_codex_instructions):
    """Only .md instruction files become launchable Codex roles."""
    (fake_codex_instructions / "not-a-role.txt").write_text("ignore me\n")
    agents = CODEX.discover_agent_types()
    assert "codex:not-a-role" not in {a["qualified_name"] for a in agents}


def test_codex_adapter_discover_returns_empty_when_cache_missing(tmp_path, monkeypatch):
    """Codex with no instructions dir returns [] rather than raising."""
    monkeypatch.setattr(
        CODEX,
        "model_instructions_dir",
        lambda: tmp_path / "absent-codex-instructions",
    )
    assert CODEX.discover_agent_types() == []


def test_codex_adapter_discover_uses_filename_when_frontmatter_is_absent(
    isolated_codex_cache,
):
    """Plain instruction files derive their role name and summary from content."""
    instructions = isolated_codex_cache["instructions"]
    (instructions / "browser-debugger.md").write_text(
        "You are a UI debugger that reproduces issues in the browser.\n"
    )

    agents = {a["qualified_name"]: a for a in CODEX.discover_agent_types()}
    assert agents["codex:browser-debugger"]["name"] == "browser-debugger"
    assert agents["codex:browser-debugger"]["summary"] == (
        "You are a UI debugger that reproduces issues in the browser."
    )


# ── Shared _scan_agent_types: union semantics ────────────────────────────────


def test_scan_agent_types_union_sorted_by_qualified_name(fake_plugins, fake_codex_instructions):
    """runtime_id=None returns the union across every registered runtime, sorted."""
    import server._warroom as wr

    union = wr._scan_agent_types(None)
    qualified = [a["qualified_name"] for a in union]
    assert qualified == sorted(qualified)
    assert "codex:agent-browser" in qualified
    assert "codex:linear" in qualified
    assert "codex:openai-docs" in qualified
    assert "helioy-tools:backend-engineer" in qualified
    assert "pr-review-toolkit:code-reviewer" in qualified


def test_scan_agent_types_scoped_to_runtime_excludes_other(fake_plugins, fake_codex_instructions):
    """An explicit runtime_id returns only that runtime's catalogue."""
    import server._warroom as wr

    codex_only = wr._scan_agent_types("codex")
    assert all(a["runtime"] == "codex" for a in codex_only)
    assert {a["qualified_name"] for a in codex_only} == {
        "codex:agent-browser",
        "codex:linear",
        "codex:openai-docs",
    }


# ── warroom_discover: runtime filtering ──────────────────────────────────────


def test_warroom_discover_scopes_to_codex_runtime(fake_plugins, fake_codex_instructions):
    """runtime='codex' must drop Claude agents from the result."""
    import server.warroom_server as wm

    result = wm.warroom_discover(runtime="codex")
    qualified = [a["qualified_name"] for a in result["agents"]]
    assert "codex:agent-browser" in qualified
    assert "helioy-tools:backend-engineer" not in qualified
    assert result["runtimes"] == ["codex"]


def test_warroom_discover_returns_error_for_unknown_runtime(fake_plugins, fake_codex_instructions):
    """An unregistered runtime id surfaces a helpful error listing the known ids."""
    import server.warroom_server as wm

    result = wm.warroom_discover(runtime="haiku-deluxe")
    assert "error" in result
    assert "haiku-deluxe" in result["error"]
    assert "claude" in result["error"]
    assert "codex" in result["error"]


def test_warroom_discover_union_exposes_both_runtimes(fake_plugins, fake_codex_instructions):
    """No runtime filter returns the union with both runtimes in the metadata."""
    import server.warroom_server as wm

    result = wm.warroom_discover()
    assert "claude" in result["runtimes"]
    assert "codex" in result["runtimes"]


# ── warroom.spawn: runtime-scoped agent validation ───────────────────────────


def test_warroom_spawn_rejects_agent_not_in_selected_runtime(
    fake_plugins, fake_codex_instructions, monkeypatch
):
    """Spawning runtime='claude' with a Codex-only role fails validation.

    The catalogue is wide (both fixtures loaded), but spawning with
    runtime='claude' scopes _resolve_agent_type to the Claude catalogue,
    so a Codex-only qualified_name must not resolve.
    """
    import server._tmux as tmux_mod
    import server.services.warroom as warroom_service

    monkeypatch.setattr(tmux_mod.gateway, "current_session_name", lambda: "alp")
    monkeypatch.setenv("TMUX", "/tmp/tmux-sock")

    result = warroom_service.spawn(
        name="scope-test",
        agents=["codex:agent-browser"],
        cwd="/tmp/r",
        runtime="claude",
    )
    assert result.get("error") == "Unknown agent types"
    details = result.get("details", [])
    assert details and details[0]["agent"] == "codex:agent-browser"


def test_warroom_spawn_uses_qualified_runtime_when_runtime_is_omitted(
    fake_plugins, fake_codex_instructions, monkeypatch
):
    """Qualified union results are launchable without a separate add call."""
    import server._db as _db_mod
    import server._tmux as tmux_mod
    import server.services.warroom as warroom_service

    monkeypatch.setattr(tmux_mod.gateway, "current_session_name", lambda: "alp")
    monkeypatch.setenv("TMUX", "/tmp/tmux-sock")

    spawned: list[tuple[str, str]] = []

    def fake_spawn(**kw):
        index = len(spawned)
        spawned.append((kw["qualified_name"], kw["runtime"]))
        return {
            "agent_type": kw["agent_type"],
            "qualified_name": kw["qualified_name"],
            "tmux_target": f"alp:1.{index}",
            "pane_id": f"%{index}",
            "runtime": kw["runtime"],
        }

    monkeypatch.setattr(tmux_mod.gateway, "spawn_pane", fake_spawn)

    result = warroom_service.spawn(
        name="mixed-runtime",
        agents=["backend-engineer", "codex:agent-browser"],
        cwd="/tmp/r",
    )

    assert "error" not in result
    assert spawned == [
        ("helioy-tools:backend-engineer", "claude"),
        ("codex:agent-browser", "codex"),
    ]

    with _db_mod.db() as conn:
        rows = [
            (row["desired_runtime"], row["desired_role"])
            for row in conn.execute(
                "SELECT desired_runtime, desired_role FROM warroom_members "
                "WHERE warroom_id = ? ORDER BY spawn_order",
                ("mixed-runtime",),
            )
        ]
    assert rows == [
        ("claude", "helioy-tools:backend-engineer"),
        ("codex", "codex:agent-browser"),
    ]


def test_resolve_agent_type_scopes_to_codex_catalogue(fake_plugins, fake_codex_instructions):
    """The mirror of ``_resolve_agent_type`` scoping to Codex-only roles.

    ``_resolve_agent_type(name, 'codex')`` with a Codex-only instruction
    role returns a Codex-tagged descriptor.
    """
    import server._warroom as wr

    agent = wr._resolve_agent_type("agent-browser", "codex")
    assert agent is not None
    assert agent["qualified_name"] == "codex:agent-browser"
    assert agent["runtime"] == "codex"

"""Shared fixtures for helioy-bus test suite.

Tests run against a temporary BUS_DIR so they never touch ~/.helioy/bus/.
"""

from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def isolated_bus(tmp_path, monkeypatch):
    """Redirect all bus paths to a temporary directory for each test."""
    bus_dir = tmp_path / "bus"
    bus_dir.mkdir()

    # Patch the source module (_db) where db() and path constants live
    import server._db as _db_mod

    monkeypatch.setattr(_db_mod, "BUS_DIR", bus_dir)
    monkeypatch.setattr(_db_mod, "REGISTRY_DB", bus_dir / "registry.db")
    monkeypatch.setattr(_db_mod, "INBOX_DIR", bus_dir / "inbox")
    monkeypatch.setattr(_db_mod, "PRESETS_DIR", bus_dir / "presets")
    # Reset init flag so each test gets a fresh schema bootstrap
    monkeypatch.setattr(_db_mod, "_db_initialized", False)

    yield bus_dir


@pytest.fixture(autouse=True)
def reset_discovery_cache():
    """Clear per-runtime agent-type cache so each test sees fresh state."""
    import server._warroom as wr

    wr._agent_types_cache.clear()
    yield
    wr._agent_types_cache.clear()


@pytest.fixture(autouse=True)
def isolated_codex_cache(tmp_path, monkeypatch):
    """Point Codex discovery at empty tmp roots by default.

    Without this, Codex discovery would read the developer's real
    ``~/.codex/skills`` and ``~/.agents/skills`` directories and leak
    them into union-discovery assertions. Tests that need Codex skills
    create them under these directories and monkeypatch the adapter
    explicitly through this fixture stack (see
    ``fake_codex_skills``).
    """
    from server.runtimes.codex import CODEX

    codex_cache = tmp_path / "codex-skills"
    codex_cache.mkdir()
    shared_cache = tmp_path / "shared-skills"
    shared_cache.mkdir()
    monkeypatch.setattr(CODEX, "agents_cache_dir", lambda: codex_cache)
    monkeypatch.setattr(CODEX, "shared_skills_dir", lambda: shared_cache)
    monkeypatch.setattr(CODEX, "skill_roots", lambda: [codex_cache, shared_cache])
    yield {"codex": codex_cache, "shared": shared_cache}


@pytest.fixture()
def set_sender(monkeypatch):
    """Mock _self_agent_id to control sender identity in send_message calls.

    Usage: set_sender("alpha") before calling bm.send_message().
    Can be called multiple times to change identity mid-test. Patches the
    name as imported into bus_server, since that is the binding the tool
    handlers reach for.
    """
    import server.bus_server as bm

    def _set(agent_id: str):
        monkeypatch.setattr(bm, "_self_agent_id", lambda: agent_id)

    return _set


@pytest.fixture()
def fake_plugins(tmp_path, monkeypatch):
    """Create a fake Claude plugin cache with known agent definitions."""
    from server.runtimes.claude import CLAUDE

    cache = tmp_path / "plugins" / "cache"

    # helioy-tools agents
    ht = cache / "helioy" / "helioy-tools" / "0.1.0" / "agents"
    ht.mkdir(parents=True)
    (ht / "backend-engineer.md").write_text(
        '---\nname: backend-engineer\ndescription: "Builds APIs and services"\nmodel: opus\n---\n'
    )
    (ht / "frontend-engineer.md").write_text(
        '---\nname: frontend-engineer\ndescription: "Builds UI components"\nmodel: sonnet\n---\n'
    )

    # pr-review-toolkit agents
    prt = cache / "official" / "pr-review-toolkit" / "abc123" / "agents"
    prt.mkdir(parents=True)
    (prt / "code-reviewer.md").write_text(
        '---\nname: code-reviewer\ndescription: "Reviews code"\nmodel: opus\n---\n'
    )

    # voltagent agents (lower priority namespace)
    va = cache / "voltagent" / "voltagent-lang" / "1.0.0" / "agents"
    va.mkdir(parents=True)
    (va / "backend-engineer.md").write_text(
        '---\nname: backend-engineer\ndescription: "Voltagent backend"\nmodel: sonnet\n---\n'
    )

    monkeypatch.setattr(CLAUDE, "agents_cache_dir", lambda: cache)

    yield cache


@pytest.fixture()
def fake_codex_skills(isolated_codex_cache):
    """Create a fake Codex skills catalogue with known SKILL.md files.

    Uses the directories produced by ``isolated_codex_cache`` so Codex
    adapter discovery reads from both the runtime-local and shared trees.
    Returns the runtime-local tree for tests that need to add extra skills.
    """
    cache = isolated_codex_cache["codex"]
    shared = isolated_codex_cache["shared"]

    (cache / "agent-browser").mkdir()
    (cache / "agent-browser" / "SKILL.md").write_text(
        '---\n'
        'name: agent-browser\n'
        'description: "Automates browser interactions for web testing"\n'
        '---\n'
    )

    (cache / ".system" / "openai-docs").mkdir(parents=True)
    (cache / ".system" / "openai-docs" / "SKILL.md").write_text(
        '---\n'
        'name: openai-docs\n'
        'description: "Use official OpenAI docs"\n'
        '---\n'
    )

    (shared / "linear").mkdir()
    (shared / "linear" / "SKILL.md").write_text(
        '---\n'
        'name: linear\n'
        'description: "Manage issues in Linear"\n'
        '---\n'
    )

    # A directory with no SKILL.md should be silently skipped.
    (cache / "empty-dir").mkdir()

    yield cache


def _insert_member(conn, *, warroom_id, role, tmux_target, pane_id, now,
                   member_id=None, spawn_order=0, repo=None, runtime="claude",
                   state="pending", agent_instance_id=None):
    """Test helper: insert a warroom_members row using the canonical schema."""
    from server._db import _new_member_id

    member_id = member_id or _new_member_id()
    conn.execute(
        "INSERT INTO warroom_members "
        "(warroom_member_id, warroom_id, desired_runtime, desired_role, "
        " desired_repo, state, agent_instance_id, spawn_order, "
        " tmux_target, pane_id, created_at, updated_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (member_id, warroom_id, runtime, role, repo, state,
         agent_instance_id, spawn_order, tmux_target, pane_id, now, now),
    )
    return member_id

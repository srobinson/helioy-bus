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

    # Also patch bus_server's imported copies (used by tool functions and test assertions)
    import server.bus_server as bm

    monkeypatch.setattr(bm, "INBOX_DIR", bus_dir / "inbox")

    yield bus_dir


@pytest.fixture()
def fake_plugins(tmp_path, monkeypatch):
    """Create a fake plugin cache with known agent definitions."""
    import server._db as _db_mod
    import server._warroom as wr

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

    monkeypatch.setattr(_db_mod, "PLUGINS_CACHE", cache)
    # Clear cache so tests see fresh state
    wr._agent_types_cache.clear()
    wr._agent_types_cache_ts = 0.0

    yield cache

    # Reset cache after test
    wr._agent_types_cache.clear()
    wr._agent_types_cache_ts = 0.0

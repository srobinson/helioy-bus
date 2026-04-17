"""Tests for the shared _warroom module: frontmatter parsing and agent-type scan/cache/resolve.

Tests run against a temporary BUS_DIR via the shared isolated_bus fixture in conftest.py.
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


def test_scan_agent_types_cached_per_runtime(fake_plugins):
    """Second scoped call returns the cached list (same identity)."""
    import server._warroom as wr

    first = wr._scan_agent_types("claude")
    second = wr._scan_agent_types("claude")
    assert first is second


def test_scan_agent_types_deduplicates_versions(tmp_path, monkeypatch):
    """When multiple versions of the same plugin exist, keeps the newest."""
    import server._warroom as wr
    from server.runtimes.claude import CLAUDE

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

    monkeypatch.setattr(CLAUDE, "agents_cache_dir", lambda: cache)

    types = wr._scan_agent_types("claude")
    matches = [t for t in types if t["name"] == "my-agent"]
    assert len(matches) == 1
    assert matches[0]["summary"] == "new"
    assert matches[0]["model"] == "opus"


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

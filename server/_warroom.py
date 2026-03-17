"""Agent type discovery and resolution for warroom spawning."""

from __future__ import annotations

import re
import time
from pathlib import Path

from server import _db

# In-memory cache for agent type scanning
_agent_types_cache: list[dict] = []
_agent_types_cache_ts: float = 0.0
_AGENT_TYPES_TTL = 60.0  # seconds

# Namespace priority for short-name resolution (lower index = higher priority)
_NAMESPACE_PRIORITY = ["helioy-tools", "pr-review-toolkit"]


def _parse_frontmatter(path: Path) -> dict | None:
    """Extract scalar frontmatter fields from a markdown agent definition.

    Uses regex to avoid a pyyaml dependency. Returns None if the file
    has no valid frontmatter block.
    """
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    m = re.match(r"^---\s*\n(.*?)\n---", text, re.DOTALL)
    if not m:
        return None
    block = m.group(1)
    result: dict[str, str] = {}
    for line in block.splitlines():
        # Match key: value (scalar only, skip lists/dicts)
        kv = re.match(r'^(\w[\w-]*)\s*:\s*"?([^"\n]+?)"?\s*$', line)
        if kv:
            result[kv.group(1)] = kv.group(2).strip()
    return result if result else None


def _scan_agent_types() -> list[dict]:
    """Walk the plugin cache and return all discovered agent type definitions.

    Results are cached in memory for 60 seconds. Multiple versions of the
    same plugin are deduplicated by keeping the newest mtime.
    """
    global _agent_types_cache, _agent_types_cache_ts

    now = time.monotonic()
    if _agent_types_cache and (now - _agent_types_cache_ts) < _AGENT_TYPES_TTL:
        return _agent_types_cache

    # Discover all agents directories at any depth under the plugin cache
    agents: dict[str, dict] = {}  # keyed by qualified_name for dedup

    plugins_cache = _db.PLUGINS_CACHE
    if not plugins_cache.is_dir():
        _agent_types_cache = []
        _agent_types_cache_ts = now
        return []

    for md_path in plugins_cache.rglob("agents/*.md"):
        fm = _parse_frontmatter(md_path)
        if not fm or "name" not in fm:
            continue

        # Derive namespace from the directory structure.
        # Pattern: cache/{org}/{plugin}/{version}/agents/*.md
        # Namespace = plugin name (second component under cache).
        rel = md_path.relative_to(plugins_cache)
        parts = rel.parts
        # We need at least: org / plugin / version / agents / file.md
        if len(parts) < 4:
            continue
        namespace = parts[1]  # plugin name

        short_name = fm["name"]
        qualified = f"{namespace}:{short_name}"
        mtime = md_path.stat().st_mtime

        # Deduplicate: keep the entry with the newest mtime
        if qualified in agents and agents[qualified].get("_mtime", 0) >= mtime:
            continue

        summary = fm.get("description", "")
        # Truncate long descriptions for the discovery listing
        if len(summary) > 200:
            summary = summary[:197] + "..."

        agents[qualified] = {
            "qualified_name": qualified,
            "name": short_name,
            "namespace": namespace,
            "summary": summary,
            "model": fm.get("model", ""),
            "_mtime": mtime,
        }

    # Strip internal fields and sort
    result = []
    for entry in sorted(agents.values(), key=lambda e: e["qualified_name"]):
        clean = {k: v for k, v in entry.items() if not k.startswith("_")}
        result.append(clean)

    _agent_types_cache = result
    _agent_types_cache_ts = now
    return result


def _resolve_agent_type(name: str) -> dict | None:
    """Resolve a short or qualified agent type name to its definition.

    Resolution order:
    1. Qualified name (contains ':'): exact match.
    2. Exact short_name match with namespace priority.
    3. None if no match found.
    """
    all_types = _scan_agent_types()

    if ":" in name:
        for agent in all_types:
            if agent["qualified_name"] == name:
                return agent
        return None

    # Short name: collect all matches
    matches = [a for a in all_types if a["name"] == name]
    if not matches:
        return None
    if len(matches) == 1:
        return matches[0]

    # Multiple matches: apply namespace priority
    for ns in _NAMESPACE_PRIORITY:
        for m in matches:
            if m["namespace"] == ns:
                return m
    # Fallback: first alphabetically by namespace
    return matches[0]

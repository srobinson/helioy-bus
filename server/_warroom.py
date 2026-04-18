"""Runtime-aware agent type discovery and resolution for warroom spawning.

Each registered runtime defines its own on-disk catalogue layout via its
adapter's ``discover_agent_types()`` method. This module layers per-runtime
caching and short/qualified-name resolution on top. No Claude-specific
cache assumptions live here.
"""

from __future__ import annotations

import time

from server.runtimes import for_id, registered_adapters

# Re-export the shared frontmatter parser for tests and any external caller
# that previously imported ``_parse_frontmatter`` from this module.
from server.runtimes._frontmatter import _parse_frontmatter  # noqa: F401

# Per-runtime in-memory cache: runtime_id -> (timestamp, list_of_agent_dicts)
_agent_types_cache: dict[str, tuple[float, list[dict]]] = {}
_AGENT_TYPES_TTL = 60.0  # seconds

# Namespace priority for short-name resolution (lower index = higher priority).
# Applies within a runtime's catalogue when a short name resolves to
# multiple qualified entries.
_NAMESPACE_PRIORITY = ["helioy-tools", "pr-review-toolkit"]


def _scan_agent_types(runtime_id: str | None = None) -> list[dict]:
    """Return agent type definitions discovered by runtime adapters.

    ``runtime_id``:
      * ``None``: return the union across every registered runtime,
        sorted by ``qualified_name``. Used for runtime-agnostic flows
        (``warroom_discover`` without a filter, ``warroom_remove``
        resolving a role that could belong to any runtime).
      * a registered runtime id: scope to that runtime's adapter.

    Results are cached per runtime in memory for 60 seconds. The union
    path stitches cached entries together rather than caching the union
    directly, so a single adapter's catalogue refreshes independently.
    """
    if runtime_id is None:
        result: list[dict] = []
        for adapter in registered_adapters():
            result.extend(_scan_agent_types(adapter.runtime_id))
        result.sort(key=lambda e: e["qualified_name"])
        return result

    adapter = for_id(runtime_id)
    now = time.monotonic()
    cached = _agent_types_cache.get(runtime_id)
    if cached and (now - cached[0]) < _AGENT_TYPES_TTL:
        return cached[1]
    fresh = adapter.discover_agent_types()
    _agent_types_cache[runtime_id] = (now, fresh)
    return fresh


def _resolve_agent_type(name: str, runtime_id: str | None = None) -> dict | None:
    """Resolve a short or qualified agent type name to its definition.

    ``runtime_id`` follows the same semantics as :func:`_scan_agent_types`:
    ``None`` searches the union, an explicit id scopes to one runtime.

    Resolution order:
      1. Qualified name (contains ':'): exact match.
      2. Exact short_name match with namespace priority.
      3. ``None`` if no match is found.
    """
    all_types = _scan_agent_types(runtime_id)

    if ":" in name:
        for agent in all_types:
            if agent["qualified_name"] == name:
                return agent
        return None

    matches = [a for a in all_types if a["name"] == name]
    if not matches:
        return None
    if len(matches) == 1:
        return matches[0]

    for ns in _NAMESPACE_PRIORITY:
        for m in matches:
            if m["namespace"] == ns:
                return m
    return matches[0]

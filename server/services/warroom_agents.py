"""Agent catalogue resolution for warroom lifecycle operations."""

from __future__ import annotations

from server._warroom import _resolve_agent_type, _scan_agent_types
from server.runtimes import RuntimeAdapter, for_id, registered_adapters


def build_suggestions(needle: str, all_types: list[dict], limit: int = 5) -> list[str]:
    q = needle.lower()
    return [
        a["qualified_name"]
        for a in all_types
        if q in a["name"].lower() or q in a.get("summary", "").lower()
    ][:limit]


def resolve_spawn_agents(
    agent_names: list[str],
    *,
    runtime: str,
    default_adapter: RuntimeAdapter,
) -> tuple[list[tuple[dict, str]], list[dict]]:
    """Resolve spawn agents and the runtime each one should use.

    An explicit runtime scopes every name to that runtime. With no explicit
    runtime, short names stay on the default runtime while qualified names use
    the same cross-runtime union returned by warroom_discover.
    """
    resolved: list[tuple[dict, str]] = []
    errors: list[dict] = []
    default_runtime_id = default_adapter.runtime_id

    for agent_name in agent_names:
        scope_runtime_id: str | None = default_runtime_id
        if not runtime and ":" in agent_name:
            scope_runtime_id = None

        all_types = _scan_agent_types(scope_runtime_id)
        agent_def = _resolve_agent_type(agent_name, scope_runtime_id)
        if agent_def is None:
            errors.append(
                {
                    "agent": agent_name,
                    "error": "Unknown agent type",
                    "suggestions": build_suggestions(agent_name, all_types),
                }
            )
            continue

        runtime_id = agent_def.get("runtime") or default_runtime_id
        if runtime_id == default_runtime_id:
            adapter = default_adapter
        else:
            try:
                adapter = for_id(runtime_id)
            except KeyError:
                known = sorted(a.runtime_id for a in registered_adapters())
                errors.append(
                    {
                        "agent": agent_name,
                        "error": f"Unknown runtime {runtime_id!r}. Known: {known}",
                        "suggestions": [],
                    }
                )
                continue

        if not adapter.supports_specialist_roles:
            errors.append(
                {
                    "agent": agent_name,
                    "error": (
                        f"Runtime {adapter.runtime_id!r} does not support "
                        "specialist-role spawn. Use warroom_spawn_repos for "
                        f"general-mode {adapter.runtime_id} panes."
                    ),
                    "suggestions": [],
                }
            )
            continue

        resolved.append((agent_def, runtime_id))

    return resolved, errors

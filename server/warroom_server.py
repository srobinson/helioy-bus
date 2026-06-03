#!/usr/bin/env python3
"""helioy-warroom MCP server -- warroom lifecycle management.

Manages agent team compositions (warrooms) in tmux. Discover available
agent types, spawn multi-agent warrooms, add/remove agents, and track
warroom status.

Shares registry.db with helioy-bus via _db.py (WAL mode).

Tool handlers in this module are thin adapters over the warroom
service in `server.services.warroom`. Reconciliation work that used
to be a side effect of `warroom_status` (member agent_id backfill) is
now invoked explicitly by the status handler.
"""

from __future__ import annotations

from mcp.server.fastmcp import FastMCP

from server.services import reconciliation, warroom

# ── MCP server ────────────────────────────────────────────────────────────────

mcp = FastMCP("helioy-warroom")


# ── Warroom MCP tools ─────────────────────────────────────────────────────────


@mcp.tool()
def warroom_discover(
    query: str = "",
    namespace: str = "",
    limit: int = 20,
    runtime: str = "",
) -> dict:
    """Search available agent types across registered runtimes.

    Each runtime adapter owns its own catalogue layout (Claude plugin
    cache vs. Codex instruction files, etc.) and contributes agents via
    ``discover_agent_types()``. Results are cached per runtime with 60s TTL.

    Args:
        query: Substring match against agent name and description. Empty returns all.
        namespace: Filter to a specific namespace (e.g. 'helioy-tools', 'codex').
        limit: Maximum number of results to return (default 20).
        runtime: Scope discovery to one runtime id ('claude', 'codex', ...).
            Empty returns the union across every registered runtime.

    Returns:
        {agents: [...], total: int, namespaces: [...], runtimes: [...]}
    """
    return warroom.discover(query=query, namespace=namespace, limit=limit, runtime=runtime)


@mcp.tool()
def warroom_spawn_repos(
    window: str = "warroom",
    layout: str = "tiled",
    runtime: str = "",
) -> dict:
    """Spawn one general-role agent per helioy repo in a single tmux window.

    Repo-mode: each pane runs in the repo's directory without a specialist
    role. The concrete launch command is supplied by the runtime adapter
    selected by ``runtime`` (defaults to the incumbent runtime when empty).

    Repos are discovered by scanning HELIOY_BASE for subdirectories that
    contain a .git folder. Uses HELIOY_BASE env var (default:
    ~/Dev/LLM/DEV/helioy). Idempotent: kills any existing warroom with the
    same window name first.

    Args:
        window: tmux window name. Default "warroom".
        layout: tmux layout algorithm. Default "tiled".
        runtime: Runtime id (e.g. "claude", "codex"). Empty string uses
            the default adapter.

    Returns:
        {
          warroom_id,
          tmux_window,
          members: [{warroom_member_id, desired_role, desired_runtime,
                     desired_repo, spawn_order, agent_type, qualified_name,
                     tmux_target, pane_id, runtime}],
          spawned_at,
          messaging: {instruction},
          errors?: [...]
        }
    """
    return warroom.spawn_repos(window=window, layout=layout, runtime=runtime)


@mcp.tool()
def warroom_spawn(
    name: str,
    agents: list[str],
    cwd: str = "",
    layout: str = "tiled",
    runtime: str = "",
) -> dict:
    """Create a warroom: a tmux window with one runtime pane per agent type.

    Idempotent: kills any existing warroom with the same name first. Validates
    all agent types before spawning any panes. Returns immediately without
    waiting for agents to register on the bus.

    Args:
        name: Warroom identifier, becomes the tmux window name.
              Alphanumeric and hyphens only, 1-30 chars.
        agents: List of agent type names (qualified like 'helioy-tools:backend-engineer'
                or short like 'backend-engineer'). Maximum 8 agents.
        cwd: Working directory for all panes. Defaults to caller's cwd.
        layout: tmux layout algorithm (tiled, even-horizontal, even-vertical,
                main-horizontal, main-vertical). Default: tiled.
        runtime: Runtime id for all spawned panes (e.g. "claude", "codex").
            Empty string falls back to the default adapter ("claude") for
            short names ("backend-engineer") and plugin-namespace-qualified
            names ("helioy-tools:codebase-analyst"). Only runtime-qualified
            names ("codex:agent-browser") select a non-default runtime when
            this arg is empty.

            MoE composition gotcha: passing the same plugin-namespaced
            agent twice in `agents=[...]` does NOT give you one Claude pane
            and one Codex pane — both land on the default adapter. For MoE,
            spawn once and then `warroom_add(..., runtime="codex")` for the
            second pane. See helioy-bus/skills/warroom Mode 1.

    Returns:
        {
          warroom_id,
          tmux_window,
          members: [{warroom_member_id, desired_role, desired_runtime,
                     spawn_order, agent_type, qualified_name,
                     tmux_target, pane_id, runtime}],
          spawned_at,
          messaging: {instruction, member_types},
          errors?: [...]
        }
    """
    return warroom.spawn(name=name, agents=agents, cwd=cwd, layout=layout, runtime=runtime)


@mcp.tool()
def warroom_kill(
    name: str = "",
    kill_all: bool = False,
) -> dict:
    """Tear down a warroom by name, or all warrooms.

    Kills the tmux window and removes the warroom from the database.

    Args:
        name: Warroom name to kill. Required unless kill_all is True.
        kill_all: Kill all active warrooms. Default False.

    Returns:
        {killed: [...], errors: [...]}
    """
    return warroom.kill(name=name, kill_all=kill_all)


@mcp.tool()
def warroom_status(
    name: str = "",
) -> list[dict]:
    """Get live status of warrooms with agent registration cross-referencing.

    Cross-references warroom_members.tmux_target with the agents table to
    determine which spawned agents have registered on the bus.

    Args:
        name: Specific warroom name. Empty returns all active warrooms.

    Returns:
        List of warroom dicts:
        {warroom_id, tmux_session, tmux_window, cwd, layout,
         runtime_policy, metadata, status, created_at, members: [...]}

        Each member includes:
        {warroom_member_id, desired_runtime, desired_role, desired_repo,
         state, agent_instance_id, spawn_order, agent_type, tmux_target,
         pane_id, registered, pane_alive, created_at, updated_at, token_usage}
    """
    reconciliation.backfill_warroom_member_agent_ids(name)
    return warroom.status(name=name)


@mcp.tool()
def warroom_add(
    name: str,
    agent: str,
    cwd: str = "",
    runtime: str = "",
) -> dict:
    """Add an agent to an existing warroom.

    Splits a new pane in the warroom's tmux window and launches the
    chosen runtime with the specified agent type. Duplicate roles are
    allowed: each call creates a new stable member record. The ``runtime``
    arg lets a warroom mix runtimes across members (per-member dispatch).

    Args:
        name: Warroom identifier.
        agent: Agent type name (qualified or short).
        cwd: Working directory for the new pane. Defaults to the warroom's
             original cwd.
        runtime: Runtime id for the new member (e.g. "claude", "codex").
            Empty string falls back to the default adapter ("claude") for
            short and plugin-namespace-qualified names. For MoE second
            panes, pass `runtime="codex"` explicitly — a plugin-namespaced
            agent name like "helioy-tools:codebase-analyst" will not pick
            codex on its own.

    Returns:
        {warroom_id,
         added: {warroom_member_id, desired_role, desired_runtime,
                 spawn_order, agent_type, qualified_name, tmux_target,
                 pane_id, runtime},
         member_count}
    """
    return warroom.add(name=name, agent=agent, cwd=cwd, runtime=runtime)


@mcp.tool()
def warroom_remove(
    name: str,
    agent: str = "",
    member_id: str = "",
) -> dict:
    """Remove a member from a warroom by killing its tmux pane.

    Targets a stable member record. Pass `member_id` for unambiguous
    selection. The legacy `agent` argument is accepted for convenience and
    resolves to a unique role within the warroom; ambiguous matches return
    an error listing candidate member ids.

    If this is the last member in the warroom, the warroom itself is
    torn down.

    Args:
        name: Warroom identifier.
        agent: Agent role (qualified or short). Used when `member_id` is empty.
        member_id: Stable warroom_member_id. Wins over `agent` if both given.

    Returns:
        {warroom_id,
         removed: {warroom_member_id, desired_role},
         remaining_members,
         warroom_killed}
    """
    return warroom.remove(name=name, agent=agent, member_id=member_id)


@mcp.tool()
def warroom_presets() -> dict:
    """List available warroom preset team compositions.

    Reads preset JSON files from ~/.helioy/bus/presets/. Each preset
    defines a reusable team composition with agent types and metadata.

    Returns:
        {presets: [{name, description, agents, tags}, ...]}
    """
    return warroom.list_presets()


@mcp.tool()
def warroom_save_preset(
    name: str,
    agents: list[str],
    description: str = "",
    tags: list[str] | None = None,
) -> dict:
    """Save a warroom team composition as a reusable preset.

    Writes a JSON file to ~/.helioy/bus/presets/{name}.json.

    Args:
        name: Preset name (becomes the filename). Alphanumeric and hyphens only.
        agents: List of agent type names (qualified or short).
        description: Human-readable description of this team composition.
        tags: Optional list of tags for categorization.

    Returns:
        {saved: name, path: str}
    """
    return warroom.save_preset(name=name, agents=agents, description=description, tags=tags)


# ── Entry point ────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    mcp.run()

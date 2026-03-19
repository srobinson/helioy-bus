#!/usr/bin/env python3
"""helioy-warroom MCP server -- warroom lifecycle management.

Manages agent team compositions (warrooms) in tmux. Discover available
agent types, spawn multi-agent warrooms, add/remove agents, and track
warroom status.

Shares registry.db with helioy-bus via _db.py (WAL mode).

Internal modules:
    _db.py       - Database, path constants, logging
    _identity.py - Agent identity resolution
    _tmux.py     - tmux pane management, nudging, spawning
    _warroom.py  - Agent type discovery and resolution
"""

from __future__ import annotations

import contextlib
import json
import os
import re
import sqlite3
import subprocess

from mcp.server.fastmcp import FastMCP

from server._db import PRESETS_DIR, _now, db
from server._tmux import _spawn_pane, _tmux_check, _tmux_pane_alive
from server._warroom import (
    _agent_types_cache,  # noqa: F401 (re-exported for tests)
    _agent_types_cache_ts,  # noqa: F401
    _parse_frontmatter,  # noqa: F401
    _resolve_agent_type,
    _scan_agent_types,
)

# ── MCP server ────────────────────────────────────────────────────────────────

mcp = FastMCP("helioy-warroom")


# ── Internal helpers ──────────────────────────────────────────────────────────


def _kill_warrooms(
    conn: sqlite3.Connection, name: str, kill_all: bool
) -> list[str]:
    """Kill warrooms and remove DB records using an existing connection.

    Kills the tmux window for each matching warroom (if still alive) and
    deletes the warroom and its members from the database.

    Returns the list of killed warroom IDs.
    """
    if kill_all:
        rows = conn.execute(
            "SELECT warroom_id, tmux_session, tmux_window FROM warrooms WHERE status = 'active'"
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT warroom_id, tmux_session, tmux_window FROM warrooms WHERE warroom_id = ?",
            (name,),
        ).fetchall()

    killed = []
    for row in rows:
        wid = row["warroom_id"]
        target = f"{row['tmux_session']}:{row['tmux_window']}"
        with contextlib.suppress(subprocess.SubprocessError, FileNotFoundError):
            subprocess.run(
                ["tmux", "kill-window", "-t", target],
                capture_output=True, timeout=5,
            )
        conn.execute("DELETE FROM warroom_members WHERE warroom_id = ?", (wid,))
        conn.execute("DELETE FROM warrooms WHERE warroom_id = ?", (wid,))
        killed.append(wid)
    return killed

# ── Warroom MCP tools ─────────────────────────────────────────────────────────


@mcp.tool()
def warroom_discover(
    query: str = "",
    namespace: str = "",
    limit: int = 20,
) -> dict:
    """Search available agent types that can be spawned in a warroom.

    Scans the Claude Code plugin cache for agent definitions and returns
    matching entries. Uses an in-memory cache with 60s TTL.

    Args:
        query: Substring match against agent name and description. Empty returns all.
        namespace: Filter to a specific plugin namespace (e.g. 'helioy-tools'). Empty returns all.
        limit: Maximum number of results to return (default 20).

    Returns:
        {agents: [...], total: int, namespaces: [...]}
    """
    all_types = _scan_agent_types()

    # Collect unique namespaces
    all_namespaces = sorted({a["namespace"] for a in all_types})

    # Apply filters
    filtered = all_types
    if namespace:
        filtered = [a for a in filtered if a["namespace"] == namespace]
    if query:
        q = query.lower()
        filtered = [
            a for a in filtered
            if q in a["name"].lower() or q in a.get("summary", "").lower()
        ]

    total = len(filtered)
    return {
        "agents": filtered[:limit],
        "total": total,
        "namespaces": all_namespaces,
    }


@mcp.tool()
def warroom_spawn(
    name: str,
    agents: list[str],
    cwd: str = "",
    layout: str = "tiled",
) -> dict:
    """Create a warroom: a tmux window with one Claude Code pane per agent type.

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

    Returns:
        {warroom_id, tmux_window, members: [...], spawned_at}
    """
    # Validate name
    if not name or not re.match(r"^[a-zA-Z0-9][a-zA-Z0-9-]{0,29}$", name):
        return {
            "error": "Name must be 1-30 chars, alphanumeric and hyphens, starting with alphanumeric."
        }

    if not agents:
        return {"error": "At least one agent type is required."}
    if len(agents) > 8:
        return {"error": "Maximum 8 agents per warroom."}

    valid_layouts = {
        "tiled", "even-horizontal", "even-vertical", "main-horizontal", "main-vertical"
    }
    if layout not in valid_layouts:
        return {"error": f"Invalid layout. Choose from: {', '.join(sorted(valid_layouts))}"}

    # Check we're inside tmux
    tmux_env = os.environ.get("TMUX", "")
    if not tmux_env:
        return {"error": "Not inside a tmux session. Warroom spawn requires tmux."}

    # Resolve the current tmux session name
    try:
        session = _tmux_check("display-message", "-p", "#{session_name}")
    except RuntimeError as e:
        return {"error": f"Cannot determine tmux session: {e}"}

    if not cwd:
        cwd = os.getcwd()

    # Resolve all agent types before spawning anything
    resolved = []
    errors = []
    all_types = _scan_agent_types()
    for agent_name in agents:
        agent_def = _resolve_agent_type(agent_name)
        if agent_def is None:
            # Build fuzzy suggestions
            q = agent_name.lower()
            suggestions = [
                a["qualified_name"] for a in all_types
                if q in a["name"].lower() or q in a.get("summary", "").lower()
            ][:5]
            errors.append({
                "agent": agent_name,
                "error": "Unknown agent type",
                "suggestions": suggestions,
            })
        else:
            resolved.append(agent_def)

    if errors:
        return {"error": "Unknown agent types", "details": errors}

    # Spawn panes
    now = _now()
    members = []
    spawn_errors = []
    for i, agent_def in enumerate(resolved):
        try:
            pane_info = _spawn_pane(
                session=session,
                window=name,
                cwd=cwd,
                agent_type=agent_def["name"],
                qualified_name=agent_def["qualified_name"],
                is_first=(i == 0),
                layout=layout,
            )
            members.append(pane_info)
        except RuntimeError as e:
            spawn_errors.append({
                "agent_type": agent_def["qualified_name"],
                "error": str(e),
            })

    # Atomically replace existing record: kill old warroom then insert new one
    with db() as conn:
        _kill_warrooms(conn, name, kill_all=False)
        conn.execute(
            """INSERT OR REPLACE INTO warrooms
               (warroom_id, tmux_session, tmux_window, cwd, created_at, status)
               VALUES (?, ?, ?, ?, ?, 'active')""",
            (name, session, name, cwd, now),
        )
        for m in members:
            conn.execute(
                """INSERT OR REPLACE INTO warroom_members
                   (warroom_id, agent_type, tmux_target, pane_id, agent_id, spawned_at)
                   VALUES (?, ?, ?, ?, NULL, ?)""",
                (name, m["qualified_name"], m["tmux_target"], m["pane_id"], now),
            )

    result = {
        "warroom_id": name,
        "tmux_window": name,
        "members": members,
        "spawned_at": now,
    }
    if spawn_errors:
        result["errors"] = spawn_errors
    return result


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
    if not name and not kill_all:
        return {"error": "Provide a warroom name or set kill_all=True."}

    with db() as conn:
        killed = _kill_warrooms(conn, name, kill_all)

    return {"killed": killed, "errors": []}


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
        List of warroom status dicts with member details including
        registration state and pane liveness.
    """
    with db() as conn:
        if name:
            warrooms = conn.execute(
                "SELECT * FROM warrooms WHERE warroom_id = ?", (name,)
            ).fetchall()
        else:
            warrooms = conn.execute(
                "SELECT * FROM warrooms WHERE status = 'active'"
            ).fetchall()

        result = []
        for wr in warrooms:
            wid = wr["warroom_id"]
            members_rows = conn.execute(
                "SELECT * FROM warroom_members WHERE warroom_id = ?", (wid,)
            ).fetchall()

            members = []
            for m in members_rows:
                tmux_target = m["tmux_target"]
                pane_alive = _tmux_pane_alive(tmux_target)

                # Cross-reference with agents table to find registration
                agent_row = conn.execute(
                    "SELECT agent_id, token_usage FROM agents WHERE tmux_target = ?",
                    (tmux_target,),
                ).fetchone()

                registered = agent_row is not None
                agent_id = agent_row["agent_id"] if agent_row else m["agent_id"]
                token_usage_raw = agent_row["token_usage"] if agent_row else None
                token_usage: dict | str | None = token_usage_raw
                if token_usage_raw:
                    with contextlib.suppress(json.JSONDecodeError, TypeError):
                        token_usage = json.loads(token_usage_raw)

                # Backfill agent_id in warroom_members if newly registered
                if registered and not m["agent_id"]:
                    conn.execute(
                        "UPDATE warroom_members SET agent_id = ? "
                        "WHERE warroom_id = ? AND agent_type = ?",
                        (agent_id, wid, m["agent_type"]),
                    )

                members.append({
                    "agent_type": m["agent_type"],
                    "tmux_target": tmux_target,
                    "pane_id": m["pane_id"],
                    "agent_id": agent_id,
                    "registered": registered,
                    "pane_alive": pane_alive,
                    "spawned_at": m["spawned_at"],
                    "token_usage": token_usage,
                })

            result.append({
                "warroom_id": wid,
                "tmux_session": wr["tmux_session"],
                "tmux_window": wr["tmux_window"],
                "cwd": wr["cwd"],
                "status": wr["status"],
                "created_at": wr["created_at"],
                "members": members,
            })

    return result


@mcp.tool()
def warroom_add(
    name: str,
    agent: str,
    cwd: str = "",
) -> dict:
    """Add an agent to an existing warroom.

    Splits a new pane in the warroom's tmux window and launches Claude Code
    with the specified agent type. Each agent type can appear at most once
    per warroom.

    Args:
        name: Warroom identifier.
        agent: Agent type name (qualified or short).
        cwd: Working directory for the new pane. Defaults to the warroom's
             original cwd.

    Returns:
        {warroom_id, added: {agent_type, qualified_name, tmux_target, pane_id}, member_count}
    """
    # Resolve agent type (no db needed)
    agent_def = _resolve_agent_type(agent)
    if not agent_def:
        all_types = _scan_agent_types()
        q = agent.lower()
        suggestions = [
            a["qualified_name"] for a in all_types
            if q in a["name"].lower() or q in a.get("summary", "").lower()
        ][:5]
        return {"error": "Unknown agent type", "suggestions": suggestions}

    qn = agent_def["qualified_name"]

    # Look up warroom and check for duplicate in one connection
    with db() as conn:
        wr = conn.execute(
            "SELECT * FROM warrooms WHERE warroom_id = ? AND status = 'active'",
            (name,),
        ).fetchone()
        if not wr:
            return {"error": f"No active warroom '{name}'."}

        existing = conn.execute(
            "SELECT 1 FROM warroom_members WHERE warroom_id = ? AND agent_type = ?",
            (name, qn),
        ).fetchone()
        if existing:
            return {"error": f"Agent type '{qn}' already in warroom '{name}'. Remove it first."}

        use_cwd = cwd or wr["cwd"]

        # Spawn pane outside the hot path but inside the connection lifetime
        try:
            pane_info = _spawn_pane(
                session=wr["tmux_session"],
                window=wr["tmux_window"],
                cwd=use_cwd,
                agent_type=agent_def["name"],
                qualified_name=qn,
                is_first=False,
                layout="tiled",
            )
        except RuntimeError as e:
            return {"error": f"Spawn failed: {e}"}

        now = _now()
        conn.execute(
            """INSERT INTO warroom_members
               (warroom_id, agent_type, tmux_target, pane_id, agent_id, spawned_at)
               VALUES (?, ?, ?, ?, NULL, ?)""",
            (name, qn, pane_info["tmux_target"], pane_info["pane_id"], now),
        )
        count = conn.execute(
            "SELECT COUNT(*) FROM warroom_members WHERE warroom_id = ?", (name,)
        ).fetchone()[0]

    return {
        "warroom_id": name,
        "added": pane_info,
        "member_count": count,
    }


@mcp.tool()
def warroom_remove(
    name: str,
    agent: str,
) -> dict:
    """Remove an agent from a warroom by killing its tmux pane.

    If this is the last agent in the warroom, the warroom itself is
    torn down.

    Args:
        name: Warroom identifier.
        agent: Agent type to remove (qualified or short name).

    Returns:
        {warroom_id, removed: str, remaining_members: int, warroom_killed: bool}
    """
    agent_def = _resolve_agent_type(agent)
    qn = agent_def["qualified_name"] if agent_def else agent

    with db() as conn:
        member = conn.execute(
            "SELECT * FROM warroom_members WHERE warroom_id = ? AND agent_type = ?",
            (name, qn),
        ).fetchone()
        if not member:
            return {"error": f"No agent '{qn}' in warroom '{name}'."}

        pane_id = member["pane_id"]

        # Kill the tmux pane (may already be dead)
        with contextlib.suppress(subprocess.SubprocessError, FileNotFoundError):
            subprocess.run(
                ["tmux", "kill-pane", "-t", pane_id],
                capture_output=True, timeout=5,
            )

        # Remove the member record
        conn.execute(
            "DELETE FROM warroom_members WHERE warroom_id = ? AND agent_type = ?",
            (name, qn),
        )

        # Check remaining members
        remaining = conn.execute(
            "SELECT COUNT(*) FROM warroom_members WHERE warroom_id = ?", (name,)
        ).fetchone()[0]

        warroom_killed = False
        if remaining == 0:
            conn.execute(
                "UPDATE warrooms SET status = 'killed' WHERE warroom_id = ?",
                (name,),
            )
            warroom_killed = True
        else:
            # Reflow remaining panes
            wr = conn.execute(
                "SELECT tmux_session, tmux_window FROM warrooms WHERE warroom_id = ?",
                (name,),
            ).fetchone()
            if wr:
                with contextlib.suppress(subprocess.SubprocessError, FileNotFoundError):
                    subprocess.run(
                        ["tmux", "select-layout", "-t",
                         f"{wr['tmux_session']}:{wr['tmux_window']}", "tiled"],
                        capture_output=True, timeout=5,
                    )

    return {
        "warroom_id": name,
        "removed": qn,
        "remaining_members": remaining,
        "warroom_killed": warroom_killed,
    }


@mcp.tool()
def warroom_presets() -> dict:
    """List available warroom preset team compositions.

    Reads preset JSON files from ~/.helioy/bus/presets/. Each preset
    defines a reusable team composition with agent types and metadata.

    Returns:
        {presets: [{name, description, agents, tags}, ...]}
    """
    presets = []
    if not PRESETS_DIR.is_dir():
        return {"presets": []}

    for path in sorted(PRESETS_DIR.glob("*.json")):
        try:
            data = json.loads(path.read_text())
            presets.append({
                "name": data.get("name", path.stem),
                "description": data.get("description", ""),
                "agents": data.get("agents", []),
                "tags": data.get("tags", []),
            })
        except (json.JSONDecodeError, OSError):
            continue

    return {"presets": presets}


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
    if not name or not re.match(r"^[a-zA-Z0-9][a-zA-Z0-9-]{0,49}$", name):
        return {
            "error": "Name must be 1-50 chars, alphanumeric and hyphens, starting with alphanumeric."
        }
    if not agents:
        return {"error": "At least one agent type is required."}

    PRESETS_DIR.mkdir(parents=True, exist_ok=True)
    preset_path = PRESETS_DIR / f"{name}.json"

    data = {
        "name": name,
        "description": description,
        "agents": agents,
        "tags": tags or [],
    }

    preset_path.write_text(json.dumps(data, indent=2))
    return {"saved": name, "path": str(preset_path)}


# ── Entry point ────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    mcp.run()

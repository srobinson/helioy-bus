#!/usr/bin/env python3
"""helioy-bus MCP server -- inter-agent message bus for Claude Code instances.

stdio transport: each Claude Code instance spawns its own server process.
Shared state lives in ~/.helioy/bus/ (SQLite registry + file-based mailboxes).
All agents sharing the same filesystem share the same bus.

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
import subprocess
import tempfile
import uuid

from mcp.server.fastmcp import FastMCP

from server._db import INBOX_DIR, PRESETS_DIR, _dbg, _now, db
from server._identity import _self_agent_id
from server._tmux import (
    NUDGE_THROTTLE_SECONDS,  # noqa: F401 (re-exported for tests)
    _inbox_has_unread,  # noqa: F401
    _nudge_allowed,
    _record_nudge,
    _spawn_pane,
    _tmux_check,
    _tmux_nudge,
    _tmux_pane_alive,
)
from server._warroom import (
    _agent_types_cache,  # noqa: F401 (re-exported for tests)
    _agent_types_cache_ts,  # noqa: F401
    _parse_frontmatter,  # noqa: F401
    _resolve_agent_type,
    _scan_agent_types,
)

# ── MCP server ────────────────────────────────────────────────────────────────

mcp = FastMCP("helioy-bus")

# ── Registry tools ─────────────────────────────────────────────────────────────


@mcp.tool()
def whoami() -> dict:
    """Return this agent's identity as registered on the bus.

    Resolves the calling process's agent_id via the PID file written at
    SessionStart, then looks up the full registration record.

    Returns:
        {agent_id, agent_type, tmux_target, cwd, registered_at}
        or {error} if not registered.
    """
    agent_id = _self_agent_id()
    with db() as conn:
        row = conn.execute(
            "SELECT agent_id, agent_type, tmux_target, cwd, registered_at FROM agents WHERE agent_id = ?",
            (agent_id,),
        ).fetchone()
    if row is None:
        return {"error": f"Not registered on bus. Resolved agent_id: {agent_id!r}"}
    return dict(row)


@mcp.tool()
def register_agent(
    pwd: str,
    tmux_target: str = "",
    agent_id: str = "",
    session_id: str = "",
    agent_type: str = "general",
    profile: dict | None = None,
) -> dict:
    """Register this Claude Code instance as an agent on the helioy-bus.

    Args:
        pwd: Working directory of the Claude Code session (pass $PWD or
             $CLAUDE_PROJECT_DIR).
        tmux_target: tmux target for nudges, e.g. "main:1.0"
                     (session:window.pane). Auto-detected if omitted.
        agent_id: Override the auto-derived agent ID. Defaults to
                  "{basename(pwd)}:{tmux_target}" when tmux_target is provided,
                  otherwise basename(pwd).
        session_id: Claude Code session UUID. Set by claude-wrapper via
                    HELIOY_SESSION_ID env var. Enables JSONL stream access.
        agent_type: Specialist role of this agent (e.g. "general",
                    "backend-engineer", "mobile-engineer"). Defaults to
                    "general". Used for role-based addressing in send_message.
        profile: Optional agent profile dict with structural identity fields:
                 owns (list of repo/crate names), consumes (list of dependencies),
                 capabilities (list of available MCP server names),
                 domain (list of 1-2 word expertise tags),
                 skills (list of installed skill names).

    Returns:
        {"agent_id": str, "registered_at": str}
    """
    if not agent_id:
        base = os.path.basename(pwd.rstrip("/")) or "unknown"
        agent_id = f"{base}:{tmux_target}" if tmux_target else base

    # Pick up session_id from env if not passed directly
    if not session_id:
        session_id = os.environ.get("HELIOY_SESSION_ID", "")

    # Parent PID is the Claude Code process (we are its stdio subprocess)
    parent_pid = os.getppid()
    now = _now()
    profile_json = json.dumps(profile) if profile else None

    with db() as conn:
        conn.execute(
            """
            INSERT OR REPLACE INTO agents
                (agent_id, cwd, tmux_target, pid, session_id, agent_type, profile, registered_at, last_seen)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (agent_id, pwd, tmux_target, parent_pid, session_id, agent_type, profile_json, now, now),
        )

    # Ensure inbox directory exists
    inbox = INBOX_DIR / agent_id
    inbox.mkdir(parents=True, exist_ok=True)

    return {"agent_id": agent_id, "registered_at": now}


@mcp.tool()
def list_agents(tmux_filter: str = "") -> list[dict]:
    """List all registered agents, lazily pruning dead tmux panes.

    Args:
        tmux_filter: Optional tmux scope filter. Accepts "session" to list
                     agents in that tmux session, or "session:window" to
                     narrow to a specific window. Agents are matched by
                     their tmux_target prefix. Omit to list all agents.

    Returns a list of agent cards with: agent_id, cwd, tmux_target,
    pid, registered_at, last_seen. Agents whose tmux pane no longer
    exists are removed from the registry before returning.
    """
    with db() as conn:
        rows = conn.execute(
            "SELECT * FROM agents ORDER BY registered_at ASC"
        ).fetchall()
        agents = [dict(row) for row in rows]

        # Lazy liveness check: remove agents with dead tmux panes
        dead_ids = []
        for agent in agents:
            target = agent.get("tmux_target", "")
            if target and not _tmux_pane_alive(target):
                dead_ids.append(agent["agent_id"])

        if dead_ids:
            placeholders = ",".join("?" * len(dead_ids))
            conn.execute(
                f"DELETE FROM agents WHERE agent_id IN ({placeholders})", dead_ids
            )

    # Build prefix matcher from tmux_filter.
    # "mysession" matches "mysession:0.1", "mysession:1.0", etc.
    # "mysession:2" matches "mysession:2.0", "mysession:2.1", etc.
    if tmux_filter:
        # session:window -> "session:window." prefix; session -> "session:" prefix
        prefix = tmux_filter + ("." if ":" in tmux_filter else ":")

    result = []
    for a in agents:
        if a["agent_id"] in dead_ids:
            continue
        if tmux_filter:
            target = a.get("tmux_target", "")
            if not target.startswith(prefix):
                continue
        if a.get("profile"):
            with contextlib.suppress(json.JSONDecodeError, TypeError):
                a["profile"] = json.loads(a["profile"])
        result.append(a)
    return result


@mcp.tool()
def unregister_agent(agent_id: str) -> dict:
    """Remove an agent from the registry (call on session end).

    Args:
        agent_id: The agent ID returned by register_agent.

    Returns:
        {"unregistered": agent_id}
    """
    with db() as conn:
        conn.execute("DELETE FROM agents WHERE agent_id = ?", (agent_id,))
    return {"unregistered": agent_id}


@mcp.tool()
def heartbeat(agent_id: str) -> dict:
    """Update last_seen timestamp for an agent (call periodically for liveness).

    Args:
        agent_id: The agent ID to refresh.

    Returns:
        {"agent_id": str, "last_seen": str}
    """
    now = _now()
    with db() as conn:
        conn.execute(
            "UPDATE agents SET last_seen = ? WHERE agent_id = ?",
            (now, agent_id),
        )
    return {"agent_id": agent_id, "last_seen": now}


# ── Mailbox tools ─────────────────────────────────────────────────────────────


@mcp.tool()
def send_message(
    to: str,
    content: str,
    from_agent: str = "",
    reply_to: str = "",
    topic: str = "",
    nudge: bool = True,
) -> dict:
    """Send a message to another agent's mailbox.

    Writes an atomic JSON file to ~/.helioy/bus/inbox/{to}/ and optionally
    sends a tmux nudge to wake the recipient if it is idle.

    Args:
        to: Recipient agent_id. Use "*" to broadcast to all registered agents.
        content: Message body (plain text or markdown).
        from_agent: Sender agent_id. Inferred from cwd basename if omitted.
        reply_to: Address recipients should reply to. Defaults to from_agent.
                  Set to "*" to make replies go to all agents (group thread).
        topic: Optional thread identifier (e.g. "am-retention-2026-03-07").
               Human-readable. Used to filter messages by topic in get_messages.
        nudge: Send tmux send-keys nudge to wake idle recipient. Default True.
               Throttled to once per 30s per recipient unless inbox has unread messages.

    Returns:
        {"message_id": str, "delivered": bool, "nudged": bool,
         "recipients": [agent_id, ...]}
    """
    if not from_agent:
        from_agent = _self_agent_id()
    if not reply_to:
        reply_to = from_agent

    with db() as conn:
        if to == "*":
            # Broadcast: all registered agents except the sender
            rows = conn.execute(
                "SELECT agent_id, tmux_target FROM agents WHERE agent_id != ?",
                (from_agent,),
            ).fetchall()
            recipients = [dict(r) for r in rows]
        elif to.startswith("role:"):
            # Role-based: all agents with matching agent_type, excluding sender
            role = to[len("role:"):]
            rows = conn.execute(
                "SELECT agent_id, tmux_target FROM agents WHERE agent_type = ? AND agent_id != ?",
                (role, from_agent),
            ).fetchall()
            recipients = [dict(r) for r in rows]
            if not recipients:
                return {
                    "message_id": None,
                    "delivered": False,
                    "nudged": False,
                    "recipients": [],
                    "error": f"No agents with role '{role}' found in registry",
                }
        else:
            row = conn.execute(
                "SELECT agent_id, tmux_target FROM agents WHERE agent_id = ?",
                (to,),
            ).fetchone()
            if row is None:
                return {
                    "message_id": None,
                    "delivered": False,
                    "nudged": False,
                    "recipients": [],
                    "error": f"Recipient '{to}' not found in registry",
                }
            recipients = [dict(row)]

    message_id = str(uuid.uuid4())
    now = _now()
    nudged_targets = []
    delivered_to = []

    for recipient in recipients:
        target_id = recipient["agent_id"]
        tmux_target = recipient.get("tmux_target", "")

        # Build payload
        payload = {
            "id": message_id,
            "from": from_agent,
            "to": target_id,
            "reply_to": reply_to,
            "topic": topic or None,
            "content": content,
            "sent_at": now,
        }

        # Atomic write: temp file + rename (prevents partial reads)
        inbox = INBOX_DIR / target_id
        inbox.mkdir(parents=True, exist_ok=True)

        filename = f"{now.replace(':', '-')}_{message_id[:8]}.json"
        _dbg(f"send_message: delivering to={target_id!r} inbox={inbox} file={filename}")
        tmp_fd, tmp_path = tempfile.mkstemp(dir=str(inbox), suffix=".tmp")
        try:
            with os.fdopen(tmp_fd, "w") as f:
                json.dump(payload, f, indent=2)
            os.rename(tmp_path, str(inbox / filename))
        except Exception:
            os.unlink(tmp_path)
            raise

        delivered_to.append(target_id)

        # tmux nudge: verify pane alive, respect throttle, record on success
        if (
            nudge
            and tmux_target
            and _nudge_allowed(target_id)
            and _tmux_pane_alive(tmux_target)
            and _tmux_nudge(tmux_target)
        ):
            nudged_targets.append(target_id)
            _record_nudge(target_id)

    return {
        "message_id": message_id,
        "delivered": bool(delivered_to),
        "nudged": bool(nudged_targets),
        "recipients": delivered_to,
    }


@mcp.tool()
def get_messages(agent_id: str = "", topic: str = "") -> list[dict]:
    """Return unread messages for the calling agent, archiving them on read.

    Args:
        agent_id: Agent whose inbox to read. Defaults to basename of cwd.
        topic: If provided, return only messages matching this topic.
               Non-matching messages remain in the inbox unread.

    Returns:
        List of message dicts sorted by arrival order (oldest first).
    """
    if not agent_id:
        agent_id = _self_agent_id()

    inbox = INBOX_DIR / agent_id
    _dbg(f"get_messages: agent_id={agent_id!r} topic={topic!r} inbox={inbox} exists={inbox.exists()}")

    if not inbox.exists():
        _dbg("get_messages: inbox missing \u2192 []")
        return []

    archive = inbox / "archive"
    archive.mkdir(parents=True, exist_ok=True)

    msg_files = sorted(inbox.glob("*.json"))
    _dbg(f"get_messages: found {len(msg_files)} file(s): {[p.name for p in msg_files]}")
    messages = []

    for path in msg_files:
        try:
            data = json.loads(path.read_text())
            if topic and data.get("topic") != topic:
                continue  # leave non-matching messages in inbox
            messages.append(data)
            path.rename(archive / path.name)
        except (json.JSONDecodeError, OSError):
            continue

    _dbg(f"get_messages: returning {len(messages)} message(s)")
    return messages


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

    # Idempotent: kill existing warroom with same name
    warroom_kill(name=name)

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

    # Record in SQLite
    with db() as conn:
        conn.execute("PRAGMA foreign_keys = ON")
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
        "members": [
            {k: v for k, v in m.items() if k != "pane_id"} | {"pane_id": m["pane_id"]}
            for m in members
        ],
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

    killed = []
    errors = []

    with db() as conn:
        conn.execute("PRAGMA foreign_keys = ON")
        if kill_all:
            rows = conn.execute(
                "SELECT warroom_id, tmux_session, tmux_window FROM warrooms WHERE status = 'active'"
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT warroom_id, tmux_session, tmux_window FROM warrooms WHERE warroom_id = ?",
                (name,),
            ).fetchall()

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

    return {"killed": killed, "errors": errors}


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
                    "SELECT agent_id FROM agents WHERE tmux_target = ?",
                    (tmux_target,),
                ).fetchone()

                registered = agent_row is not None
                agent_id = agent_row["agent_id"] if agent_row else m["agent_id"]

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
    # Look up the warroom
    with db() as conn:
        wr = conn.execute(
            "SELECT * FROM warrooms WHERE warroom_id = ? AND status = 'active'",
            (name,),
        ).fetchone()
    if not wr:
        return {"error": f"No active warroom '{name}'."}

    # Resolve agent type
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

    # Check for duplicate
    with db() as conn:
        existing = conn.execute(
            "SELECT 1 FROM warroom_members WHERE warroom_id = ? AND agent_type = ?",
            (name, qn),
        ).fetchone()
    if existing:
        return {"error": f"Agent type '{qn}' already in warroom '{name}'. Remove it first."}

    use_cwd = cwd or wr["cwd"]

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
    with db() as conn:
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
        conn.execute("PRAGMA foreign_keys = ON")
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

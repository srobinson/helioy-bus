#!/usr/bin/env python3
"""helioy-bus MCP server -- inter-agent message bus for Claude Code instances.

stdio transport: each Claude Code instance spawns its own server process.
Shared state lives in ~/.helioy/bus/ (SQLite registry + file-based mailboxes).
All agents sharing the same filesystem share the same bus.

Internal modules:
    _db.py       - Database, path constants, logging
    _identity.py - Agent identity resolution
    _tmux.py     - tmux pane management, nudging
"""

from __future__ import annotations

import contextlib
import json
import os
import tempfile
import uuid

from mcp.server.fastmcp import FastMCP

from server._db import INBOX_DIR, _dbg, _now, db
from server._identity import _self_agent_id
from server._tmux import (
    _nudge_allowed,
    _record_nudge,
    _tmux_nudge,
    _tmux_pane_alive,
)

# ── MCP server ────────────────────────────────────────────────────────────────

mcp = FastMCP("helioy-bus")

# ── Registry tools ─────────────────────────────────────────────────────────────


@mcp.tool()
def whoami() -> dict:
    """Return this agent's identity as registered on the bus.

    Call this tool when the user types "whoami" or when you need to
    discover your own agent_id, agent_type, or token usage.

    Resolves the calling process's agent_id via the PID file written at
    SessionStart, then looks up the full registration record.

    Returns:
        {agent_id, agent_type, tmux_target, cwd, session_id, registered_at, token_usage}
        or {error} if not registered.
    """
    agent_id = _self_agent_id()
    with db() as conn:
        row = conn.execute(
            "SELECT agent_id, agent_type, tmux_target, cwd, session_id, registered_at, token_usage"
            " FROM agents WHERE agent_id = ?",
            (agent_id,),
        ).fetchone()
    if row is None:
        return {"error": f"Not registered on bus. Resolved agent_id: {agent_id!r}"}
    result = dict(row)
    if result.get("token_usage"):
        with contextlib.suppress(json.JSONDecodeError, TypeError):
            result["token_usage"] = json.loads(result["token_usage"])
    return result


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
        if a.get("token_usage"):
            with contextlib.suppress(json.JSONDecodeError, TypeError):
                a["token_usage"] = json.loads(a["token_usage"])
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


# ── Entry point ────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    mcp.run()

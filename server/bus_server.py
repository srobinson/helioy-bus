#!/usr/bin/env python3
"""helioy-bus MCP server -- inter-agent message bus for Claude Code instances.

stdio transport: each Claude Code instance spawns its own server process.
Shared state lives in ~/.helioy/bus/ (SQLite registry + file-based mailboxes).
All agents sharing the same filesystem share the same bus.

Tool handlers in this module are thin adapters over the application
services in `server.services`. Domain logic lives there; this module
owns argument parsing, identity resolution, and wiring observational
reads to their explicit reconciliation step.
"""

from __future__ import annotations

from mcp.server.mcpserver import MCPServer

from server._identity import _self_agent_id
from server.services import agent_registry, message, reconciliation

# ── MCP server ────────────────────────────────────────────────────────────────

mcp = MCPServer("helioy-bus")

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
    return agent_registry.whoami(agent_id=_self_agent_id())


@mcp.tool()
def register_agent(
    pwd: str,
    tmux_target: str = "",
    agent_id: str = "",
    session_id: str = "",
    agent_type: str = "general",
    runtime: str = "",
    pane_id: str = "",
    profile: dict | None = None,
) -> dict:
    """Register this runtime instance as an agent on the helioy-bus.

    Args:
        pwd: Working directory of the runtime session (pass $PWD or
             $CLAUDE_PROJECT_DIR when available).
        tmux_target: tmux target for nudges, e.g. "main:1.0"
                     (session:window.pane). Auto-detected if omitted.
        agent_id: Override the auto-derived agent ID. Defaults to the
                  canonical form produced by canonical_agent_id():
                  "{repo}:{agent_type}:{tmux_target}" when tmux_target is
                  provided, otherwise "{repo}:{agent_type}".
        session_id: Optional runtime session UUID. Set by claude-wrapper via
                    HELIOY_SESSION_ID for Claude sessions. Enables JSONL
                    stream access when available.
        agent_type: Specialist role of this agent (e.g. "general",
                    "backend-engineer", "mobile-engineer"). Defaults to
                    "general". Used for role-based addressing in send_message.
        runtime: Runtime id for this registration (e.g. "claude", "codex").
                 Empty string falls back to HELIOY_RUNTIME, then "claude".
        pane_id: Stable tmux pane id (%N) backing tmux_target. Pass
                 $TMUX_PANE when registering your own pane. Unlike
                 tmux_target it survives window re-indexing, so liveness
                 checks and nudge addressing prefer it when present.
        profile: Optional agent profile dict with structural identity fields:
                 owns (list of repo/crate names), consumes (list of dependencies),
                 capabilities (list of available MCP server names),
                 domain (list of 1-2 word expertise tags),
                 skills (list of installed skill names).

    Returns:
        {"agent_id": str, "registered_at": str}
    """
    return agent_registry.register(
        pwd=pwd,
        tmux_target=tmux_target,
        agent_id=agent_id,
        session_id=session_id,
        agent_type=agent_type,
        runtime=runtime,
        pane_id=pane_id,
        profile=profile,
    )


@mcp.tool()
def list_agents(tmux_filter: str = "", cwd_basename: str = "") -> list[dict]:
    """List all registered agents, lazily pruning dead tmux panes.

    Args:
        tmux_filter: Optional tmux target prefix to filter by. Examples:
                     "2" lists all agents in tmux session 2,
                     "2:1" narrows to window 1 of session 2,
                     "main" lists agents in the session named "main".
                     Omit to list all agents.
        cwd_basename: Optional working directory basename filter. Returns
                      all agents whose registered cwd's last path segment
                      equals this value (e.g. "api" matches "/tmp/one/api"
                      and "/tmp/two/api"). May be combined with tmux_filter.

    Returns a list of agent cards with: agent_id, cwd, tmux_target,
    pid, registered_at, last_seen. Agents whose tmux pane no longer
    exists are removed from the registry before returning.
    """
    reconciliation.sync_pane_addresses()
    reconciliation.prune_dead_agents()
    return agent_registry.list_active(tmux_filter=tmux_filter, cwd_basename=cwd_basename)


@mcp.tool()
def unregister_agent(agent_id: str) -> dict:
    """Remove an agent from the registry (call on session end).

    Args:
        agent_id: The agent ID returned by register_agent.

    Returns:
        {"unregistered": agent_id}
    """
    return agent_registry.unregister(agent_id=agent_id)


@mcp.tool()
def heartbeat(agent_id: str) -> dict:
    """Update last_seen timestamp for an agent (call periodically for liveness).

    Args:
        agent_id: The agent ID to refresh.

    Returns:
        {"agent_id": str, "last_seen": str}
    """
    return agent_registry.heartbeat(agent_id=agent_id)


# ── Mailbox tools ─────────────────────────────────────────────────────────────


@mcp.tool()
def send_message(
    to: str,
    content: str,
    reply_to: str = "",
    topic: str = "",
    nudge: bool = True,
) -> dict:
    """Send a message to one or more agents' mailboxes.

    `to` accepts multiple recipients in a single call, ";"-delimited
    (e.g. "alice;bob;role:reviewer"): one message, delivered to each
    recipient's inbox with its own optional nudge.

    Writes an atomic JSON file to ~/.helioy/bus/inbox/{to}/ and optionally
    sends a tmux nudge to wake the recipient if it is idle.

    Sender identity is resolved automatically from the calling agent's
    registration (PID file, tmux pane title, or cwd basename fallback).

    Args:
        to: Use ";" to address multiple recipients in one call
            (e.g. "alice;bob;role:reviewer"). Use a recipient agent_id,
            "*" to broadcast to all registered agents, or "role:<type>" to
            target all agents with that agent_type. Unresolved parts are
            reported in a "failed" field on the response without blocking
            delivery to the rest.
        content: Message body (plain text or markdown).
        reply_to: Address recipients should reply to. Defaults to sender.
                  Set to "*" to make replies go to all agents (group thread).
        topic: Optional thread identifier (e.g. "am-retention-2026-03-07").
               Human-readable. Used to filter messages by topic in get_messages.
        nudge: Send tmux send-keys nudge to wake idle recipient. Default True.
               Throttled to once per 30s per recipient unless inbox has unread messages.

    Returns:
        {"message_id": str, "delivered": bool, "nudged": bool,
         "recipients": [agent_id, ...],
         "failed": [{"to": str, "error": str}, ...]  # only if any part failed
        }
    """
    return message.send(
        sender_id=_self_agent_id(),
        to=to,
        content=content,
        reply_to=reply_to,
        topic=topic,
        nudge=nudge,
    )


@mcp.tool()
def nudge_message(to: str, content: str) -> dict:
    """Send a message directly to another agent's tmux pane.

    This bypasses mailbox storage and types the content into the
    recipient pane with tmux send-keys. Use it for lightweight
    coordination prompts when no durable inbox record is needed.

    Sender identity is resolved automatically from the calling agent's
    registration and is only used to exclude the caller from role and
    broadcast addressing.

    Args:
        to: Recipient agent_id. Use "*" to nudge all registered agents.
            Use "role:<type>" to nudge all agents with that agent_type.
            Use ";" to address multiple recipients in one call
            (e.g. "alice;bob"). Unresolved parts appear in "skipped".
        content: Text to type into each recipient pane and submit.

    Returns:
        {"nudged": bool, "recipients": [agent_id, ...],
         "skipped": [{"agent_id": str, "reason": str}, ...]}
    """
    return message.nudge_direct(
        sender_id=_self_agent_id(),
        to=to,
        content=content,
    )


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
    messages = message.read(agent_id=agent_id, topic=topic)
    reconciliation.prune_archived_messages(agent_id)
    return messages


# ── Entry point ────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    mcp.run()

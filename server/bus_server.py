#!/usr/bin/env python3
"""helioy-bus MCP server — inter-agent message bus for Claude Code instances.

stdio transport: each Claude Code instance spawns its own server process.
Shared state lives in ~/.helioy/bus/ (SQLite registry + file-based mailboxes).
All agents sharing the same filesystem share the same bus.
"""

from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import tempfile
import uuid
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path

from mcp.server.fastmcp import FastMCP

# ── Paths ─────────────────────────────────────────────────────────────────────

BUS_DIR = Path.home() / ".helioy" / "bus"
REGISTRY_DB = BUS_DIR / "registry.db"
INBOX_DIR = BUS_DIR / "inbox"

# ── MCP server ────────────────────────────────────────────────────────────────

mcp = FastMCP("helioy-bus")

# ── Database ──────────────────────────────────────────────────────────────────


def _init_db(conn: sqlite3.Connection) -> None:
    conn.executescript("""
        PRAGMA journal_mode=WAL;
        PRAGMA synchronous=NORMAL;
        CREATE TABLE IF NOT EXISTS agents (
            agent_id      TEXT PRIMARY KEY,
            cwd           TEXT NOT NULL,
            tmux_target   TEXT NOT NULL DEFAULT '',
            pid           INTEGER,
            registered_at TEXT NOT NULL,
            last_seen     TEXT NOT NULL
        );
    """)


@contextmanager
def db():
    BUS_DIR.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(REGISTRY_DB), timeout=10)
    conn.row_factory = sqlite3.Row
    _init_db(conn)
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _self_agent_id() -> str:
    """Resolve agent_id for the calling process via the PID file written at SessionStart.

    Tries HELIOY_BUS_CLAUDE_PID (set by proxy.py) first, then os.getppid(),
    then falls back to basename(cwd).
    """
    pids_dir = BUS_DIR / "pids"
    for pid in filter(None, [os.environ.get("HELIOY_BUS_CLAUDE_PID"), str(os.getppid())]):
        pid_file = pids_dir / pid
        if pid_file.exists():
            resolved = pid_file.read_text().strip()
            _dbg(f"_self_agent_id: pid={pid} pid_file={pid_file} → {resolved!r}")
            return resolved
    resolved = os.path.basename(os.getcwd()) or "unknown"
    _dbg(f"_self_agent_id: no pid file found (tried HELIOY_BUS_CLAUDE_PID={os.environ.get('HELIOY_BUS_CLAUDE_PID')!r} ppid={os.getppid()}) → {resolved!r}")
    return resolved


LOG_FILE = Path("/tmp/helioy-bus-debug.log")


def _dbg(msg: str) -> None:
    from datetime import datetime as _dt
    ts = _dt.now().isoformat(timespec="seconds")
    with LOG_FILE.open("a") as f:
        f.write(f"[{ts}] {msg}\n")


# ── Registry tools ─────────────────────────────────────────────────────────────


@mcp.tool()
def register_agent(
    pwd: str,
    tmux_target: str = "",
    agent_id: str = "",
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

    Returns:
        {"agent_id": str, "registered_at": str}
    """
    if not agent_id:
        base = os.path.basename(pwd.rstrip("/")) or "unknown"
        agent_id = f"{base}:{tmux_target}" if tmux_target else base

    # Parent PID is the Claude Code process (we are its stdio subprocess)
    parent_pid = os.getppid()
    now = _now()

    with db() as conn:
        conn.execute(
            """
            INSERT OR REPLACE INTO agents
                (agent_id, cwd, tmux_target, pid, registered_at, last_seen)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (agent_id, pwd, tmux_target, parent_pid, now, now),
        )

    # Ensure inbox directory exists
    inbox = INBOX_DIR / agent_id
    inbox.mkdir(parents=True, exist_ok=True)

    return {"agent_id": agent_id, "registered_at": now}


@mcp.tool()
def list_agents() -> list[dict]:
    """List all registered agents, lazily pruning dead tmux panes.

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

    return [a for a in agents if a["agent_id"] not in dead_ids]


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
    nudge: bool = True,
) -> dict:
    """Send a message to another agent's mailbox.

    Writes an atomic JSON file to ~/.helioy/bus/inbox/{to}/ and optionally
    sends a tmux nudge to wake the recipient if it is idle.

    Args:
        to: Recipient agent_id. Use "*" to broadcast to all registered agents.
        content: Message body (plain text or markdown).
        from_agent: Sender agent_id. Inferred from cwd basename if omitted.
        nudge: Send tmux send-keys nudge to wake idle recipient. Default True.

    Returns:
        {"message_id": str, "delivered": bool, "nudged": bool,
         "recipients": [agent_id, ...]}
    """
    if not from_agent:
        from_agent = _self_agent_id()

    with db() as conn:
        if to == "*":
            # Broadcast: all registered agents
            rows = conn.execute(
                "SELECT agent_id, tmux_target FROM agents"
            ).fetchall()
            recipients = [dict(r) for r in rows]
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

        # tmux nudge: verify pane is alive before sending
        if nudge and tmux_target and _tmux_pane_alive(tmux_target) and _tmux_nudge(tmux_target):
            nudged_targets.append(target_id)

    return {
        "message_id": message_id,
        "delivered": bool(delivered_to),
        "nudged": bool(nudged_targets),
        "recipients": delivered_to,
    }


@mcp.tool()
def get_messages(agent_id: str = "") -> list[dict]:
    """Return unread messages for the calling agent, archiving them on read.

    Args:
        agent_id: Agent whose inbox to read. Defaults to basename of cwd.

    Returns:
        List of message dicts sorted by arrival order (oldest first).
    """
    if not agent_id:
        agent_id = _self_agent_id()

    inbox = INBOX_DIR / agent_id
    _dbg(f"get_messages: agent_id={agent_id!r} inbox={inbox} exists={inbox.exists()}")

    if not inbox.exists():
        _dbg(f"get_messages: inbox missing → []")
        return []

    archive = inbox / "archive"
    archive.mkdir(parents=True, exist_ok=True)

    msg_files = sorted(inbox.glob("*.json"))
    _dbg(f"get_messages: found {len(msg_files)} file(s): {[p.name for p in msg_files]}")
    messages = []

    for path in msg_files:
        try:
            data = json.loads(path.read_text())
            messages.append(data)
            # Archive after reading
            path.rename(archive / path.name)
        except (json.JSONDecodeError, OSError):
            continue

    _dbg(f"get_messages: returning {len(messages)} message(s)")
    return messages


# ── tmux helpers ───────────────────────────────────────────────────────────────


def _tmux_pane_alive(target: str) -> bool:
    """Return True if the tmux target pane exists and is reachable.

    Uses list-panes rather than has-session so that a dead pane in a live
    session is correctly reported as gone.
    """
    try:
        result = subprocess.run(
            ["tmux", "list-panes", "-t", target],
            capture_output=True,
            timeout=3,
        )
        return result.returncode == 0
    except (subprocess.SubprocessError, FileNotFoundError, subprocess.TimeoutExpired):
        return False


def _tmux_nudge(tmux_target: str) -> bool:
    """Send a 'you have mail!' keystroke to wake an idle Claude session."""
    try:
        result = subprocess.run(
            ["tmux", "send-keys", "-t", tmux_target, "you have mail!", "Enter"],
            capture_output=True,
            timeout=3,
        )
        _dbg(f"_tmux_nudge: target={tmux_target!r} rc={result.returncode} stderr={result.stderr.decode().strip()!r}")
        return result.returncode == 0
    except (subprocess.SubprocessError, FileNotFoundError, subprocess.TimeoutExpired) as e:
        _dbg(f"_tmux_nudge: target={tmux_target!r} exception={e!r}")
        return False


# ── Entry point ────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    mcp.run()

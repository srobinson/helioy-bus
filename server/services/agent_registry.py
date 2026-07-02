"""Agent registration and observational listing.

Each function corresponds to one MCP tool body. List operations are
purely observational; call `reconciliation.prune_dead_agents()` before
reading if you want stale rows evicted first.
"""

from __future__ import annotations

import contextlib
import json
import os

from server import _db
from server._identity import canonical_agent_id


def whoami(*, agent_id: str) -> dict:
    """Return the registration record for the resolved agent_id."""
    with _db.db() as conn:
        row = conn.execute(
            "SELECT agent_id, agent_type, runtime, tmux_target, cwd, session_id, "
            "registered_at, token_usage FROM agents WHERE agent_id = ?",
            (agent_id,),
        ).fetchone()
    if row is None:
        return {"error": f"Not registered on bus. Resolved agent_id: {agent_id!r}"}
    result = dict(row)
    if result.get("token_usage"):
        with contextlib.suppress(json.JSONDecodeError, TypeError):
            result["token_usage"] = json.loads(result["token_usage"])
    return result


def register(
    *,
    pwd: str,
    tmux_target: str,
    agent_id: str,
    session_id: str,
    agent_type: str,
    runtime: str = "",
    pane_id: str = "",
    profile: dict | None,
) -> dict:
    """Insert or replace an agent registration.

    Pane eviction: a tmux pane hosts at most one runtime process at a
    time, so any prior row claiming our tmux_target is stale by
    definition. Evicting here is an ownership assertion from the new
    occupant, not PID-based liveness guessing.
    """
    if not agent_id:
        agent_id = canonical_agent_id(pwd, agent_type, tmux_target)

    if not session_id:
        session_id = os.environ.get("HELIOY_SESSION_ID", "")
    if not runtime:
        runtime = os.environ.get("HELIOY_RUNTIME", "claude")

    parent_pid = os.getppid()
    now = _db._now()
    profile_json = json.dumps(profile) if profile else None
    # pane_id is caller-supplied, never sniffed from this process's env:
    # tmux_target may describe a different pane than the one hosting the
    # server (tests, orchestrators registering on behalf of others), and a
    # wrong stable id is worse than none. The hook registrar passes its own
    # $TMUX_PANE; rows without pane_id fall back to tmux_target liveness.

    with _db.db() as conn:
        if tmux_target:
            conn.execute(
                "DELETE FROM agents WHERE agent_id != ? "
                "AND (tmux_target = ? OR (pane_id != '' AND pane_id = ?))",
                (agent_id, tmux_target, pane_id),
            )
        conn.execute(
            """
            INSERT OR REPLACE INTO agents
                (agent_id, cwd, tmux_target, pane_id, pid, session_id,
                 agent_type, runtime, profile, registered_at, last_seen)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                agent_id,
                pwd,
                tmux_target,
                pane_id,
                parent_pid,
                session_id,
                agent_type,
                runtime,
                profile_json,
                now,
                now,
            ),
        )

    inbox = _db.INBOX_DIR / agent_id
    inbox.mkdir(parents=True, exist_ok=True)

    return {"agent_id": agent_id, "registered_at": now}


def list_active(*, tmux_filter: str = "", cwd_basename: str = "") -> list[dict]:
    """Return all registered agents.

    Observational only. Reconciliation (eviction of dead panes / dead
    PIDs) is the caller's responsibility via
    `reconciliation.prune_dead_agents()`.

    Args:
        tmux_filter: Optional tmux target prefix (e.g. "main" or "main:0")
                     pushed down as a SQL LIKE.
        cwd_basename: Optional last-path-segment filter applied in Python
                     after the SQL fetch. Composes with `tmux_filter`.
    """
    with _db.db() as conn:
        if tmux_filter:
            sql_prefix = tmux_filter + ("." if ":" in tmux_filter else ":") + "%"
            rows = conn.execute(
                "SELECT * FROM agents WHERE tmux_target LIKE ? ORDER BY registered_at ASC",
                (sql_prefix,),
            ).fetchall()
        else:
            rows = conn.execute("SELECT * FROM agents ORDER BY registered_at ASC").fetchall()

    cwd_basename = os.path.basename(os.path.normpath(cwd_basename)) if cwd_basename else ""

    result = []
    for row in rows:
        a = dict(row)
        if cwd_basename and os.path.basename(os.path.normpath(a["cwd"])) != cwd_basename:
            continue
        if a.get("profile"):
            with contextlib.suppress(json.JSONDecodeError, TypeError):
                a["profile"] = json.loads(a["profile"])
        if a.get("token_usage"):
            with contextlib.suppress(json.JSONDecodeError, TypeError):
                a["token_usage"] = json.loads(a["token_usage"])
        result.append(a)
    return result


def unregister(*, agent_id: str) -> dict:
    with _db.db() as conn:
        conn.execute("DELETE FROM agents WHERE agent_id = ?", (agent_id,))
    return {"unregistered": agent_id}


def heartbeat(*, agent_id: str) -> dict:
    now = _db._now()
    with _db.db() as conn:
        conn.execute(
            "UPDATE agents SET last_seen = ? WHERE agent_id = ?",
            (now, agent_id),
        )
    return {"agent_id": agent_id, "last_seen": now}

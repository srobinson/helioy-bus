"""Explicit reconciliation of stale state.

Operations that used to hide inside read calls (list_agents,
get_messages, warroom_status) live here as named callables. Handlers
invoke them explicitly to preserve the existing user-visible
behaviour while keeping read services purely observational.
"""

from __future__ import annotations

import os
from datetime import UTC, datetime, timedelta

from server import _db
from server._tmux import gateway


def prune_dead_agents() -> set[str]:
    """Evict registry rows whose tmux pane is gone, or whose PID is dead.

    Returns the set of evicted agent_ids so callers can filter their
    own snapshots if needed.
    """
    with _db.db() as conn:
        alive_rows = conn.execute(
            "SELECT agent_id, tmux_target FROM agents WHERE tmux_target != ''"
        ).fetchall()
        dead_ids: set[str] = {
            r["agent_id"] for r in alive_rows if not gateway.pane_alive(r["tmux_target"])
        }
        no_tmux_rows = conn.execute(
            "SELECT agent_id, pid FROM agents WHERE tmux_target = '' AND pid IS NOT NULL"
        ).fetchall()
        for r in no_tmux_rows:
            try:
                os.kill(r["pid"], 0)
            except (OSError, ProcessLookupError):
                dead_ids.add(r["agent_id"])

        if dead_ids:
            placeholders = ",".join("?" * len(dead_ids))
            conn.execute(
                f"DELETE FROM agents WHERE agent_id IN ({placeholders})",
                list(dead_ids),
            )
    return dead_ids


def prune_archived_messages(agent_id: str, *, max_age_days: int = 7) -> int:
    """Delete archived inbox files older than max_age_days.

    Returns the count removed. No-op if the inbox or archive directory
    is missing.
    """
    inbox = _db.INBOX_DIR / agent_id
    archive = inbox / "archive"
    if not archive.exists():
        return 0
    cutoff = datetime.now(UTC) - timedelta(days=max_age_days)
    removed = 0
    for archived in archive.glob("*.json"):
        try:
            if datetime.fromtimestamp(archived.stat().st_mtime, UTC) < cutoff:
                archived.unlink()
                removed += 1
        except OSError:
            continue
    return removed


def reap_dead_warrooms(warroom_id: str = "") -> int:
    """Mark active warrooms killed when their tmux window is gone.

    The window is the warroom's lifecycle boundary. Individual member panes can
    die without ending the warroom while the window remains reachable.
    """
    updated = 0
    with _db.db() as conn:
        params: tuple[str, ...] = ()
        where = "WHERE status = 'active'"
        if warroom_id:
            where += " AND warroom_id = ?"
            params = (warroom_id,)

        rows = conn.execute(
            f"""
            SELECT warroom_id, tmux_session, tmux_window
            FROM warrooms
            {where}
            """,
            params,
        ).fetchall()

        for r in rows:
            window_target = f"{r['tmux_session']}:{r['tmux_window']}"
            if gateway.pane_alive(window_target):
                continue
            conn.execute(
                "UPDATE warrooms SET status = ? WHERE warroom_id = ? AND status = ?",
                ("killed", r["warroom_id"], "active"),
            )
            updated += 1
    return updated


def backfill_warroom_member_agent_ids(warroom_id: str = "") -> int:
    """Reconcile persisted member identity and state against live runtime state.

    A member is live only when both of these are true:

    1. An `agents` row exists for the member's `tmux_target`
    2. The pane itself is still alive

    Reconciliation is bidirectional. It promotes pending members to
    active when a live registration appears, advances
    `agent_instance_id` when a pane restarts with a new agent_id, and
    clears stale identity/state when registration disappears or the pane
    is dead. Returns the count of rows updated.
    """
    now = _db._now()
    updated = 0
    with _db.db() as conn:
        params: tuple[str, ...] = ()
        where = ""
        if warroom_id:
            where = "WHERE m.warroom_id = ?"
            params = (warroom_id,)

        rows = conn.execute(
            f"""
            SELECT m.warroom_member_id,
                   m.tmux_target,
                   m.state,
                   m.agent_instance_id,
                   a.agent_id AS registered_agent_id
            FROM warroom_members m
            LEFT JOIN agents a ON a.tmux_target = m.tmux_target
            {where}
            """,
            params,
        ).fetchall()

        for r in rows:
            pane_alive = gateway.pane_alive(r["tmux_target"])
            live_agent_id = r["registered_agent_id"] if pane_alive else None
            desired_state = "active" if live_agent_id else "pending"

            if r["agent_instance_id"] == live_agent_id and r["state"] == desired_state:
                continue

            conn.execute(
                "UPDATE warroom_members "
                "SET agent_instance_id = ?, state = ?, updated_at = ? "
                "WHERE warroom_member_id = ?",
                (live_agent_id, desired_state, now, r["warroom_member_id"]),
            )
            updated += 1
    return updated

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
            r["agent_id"] for r in alive_rows
            if not gateway.pane_alive(r["tmux_target"])
        }
        no_tmux_rows = conn.execute(
            "SELECT agent_id, pid FROM agents "
            "WHERE tmux_target = '' AND pid IS NOT NULL"
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


def backfill_warroom_member_agent_ids(warroom_id: str = "") -> int:
    """Fill in `warroom_members.agent_instance_id` from the agents table.

    Members spawn before their runtime process registers on the bus, so
    `agent_instance_id` lands NULL initially with `state='pending'`.
    Once a registration arrives whose `tmux_target` matches a member,
    this writes the agent_instance_id into the member row and advances
    state to `active`, bumping `updated_at`.

    `warroom_id` filters to one warroom; empty backfills every active
    member. Returns the count updated.
    """
    now = _db._now()
    with _db.db() as conn:
        if warroom_id:
            rows = conn.execute(
                """
                SELECT m.warroom_member_id, a.agent_id
                FROM warroom_members m
                JOIN agents a ON a.tmux_target = m.tmux_target
                WHERE m.warroom_id = ? AND m.agent_instance_id IS NULL
                """,
                (warroom_id,),
            ).fetchall()
        else:
            rows = conn.execute(
                """
                SELECT m.warroom_member_id, a.agent_id
                FROM warroom_members m
                JOIN agents a ON a.tmux_target = m.tmux_target
                WHERE m.agent_instance_id IS NULL
                """
            ).fetchall()
        for r in rows:
            conn.execute(
                "UPDATE warroom_members "
                "SET agent_instance_id = ?, state = 'active', updated_at = ? "
                "WHERE warroom_member_id = ?",
                (r["agent_id"], now, r["warroom_member_id"]),
            )
    return len(rows)

"""Shared persistence and tmux-preflight helpers for warroom service paths.

Extracted from ``server/services/warroom.py`` so that ``spawn``,
``spawn_repos``, and ``add`` share a single definition of the tmux
preflight, the ``warrooms`` upsert, the ``warroom_members`` insert, and
the pane-info enrichment step. Consolidating these keeps schema and
workflow changes from drifting between the two spawn paths.
"""

from __future__ import annotations

import os
import sqlite3

from server import _db
from server._tmux import gateway


def resolve_tmux_session() -> tuple[str | None, dict | None]:
    """Require a live TMUX session and return its name.

    Returns ``(session, None)`` when the caller is inside tmux and the
    session name resolves, else ``(None, error_dict)``. Both spawn
    paths share this preflight because a warroom cannot be created
    outside tmux.
    """
    if not os.environ.get("TMUX"):
        return None, {"error": "Not inside a tmux session. Warroom spawn requires tmux."}
    session = gateway.current_session_name()
    if session is None:
        return None, {"error": "Cannot determine tmux session"}
    return session, None


def upsert_warroom(
    conn: sqlite3.Connection,
    *,
    warroom_id: str,
    session: str,
    window: str,
    cwd: str,
    layout: str,
    now: str,
) -> None:
    """Insert or replace the active warroom row for ``warroom_id``."""
    conn.execute(
        """INSERT OR REPLACE INTO warrooms
           (warroom_id, tmux_session, tmux_window, cwd, layout,
            runtime_policy, metadata, created_at, status)
           VALUES (?, ?, ?, ?, ?, NULL, NULL, ?, 'active')""",
        (warroom_id, session, window, cwd, layout, now),
    )


def insert_warroom_member(
    conn: sqlite3.Connection,
    *,
    warroom_id: str,
    runtime_id: str,
    desired_role: str,
    desired_repo: str | None,
    spawn_order: int,
    tmux_target: str,
    pane_id: str,
    now: str,
) -> str:
    """Insert a pending warroom member row and return its new member_id."""
    member_id = _db._new_member_id()
    conn.execute(
        """INSERT INTO warroom_members
           (warroom_member_id, warroom_id, desired_runtime, desired_role,
            desired_repo, state, agent_instance_id, spawn_order,
            tmux_target, pane_id, created_at, updated_at)
           VALUES (?, ?, ?, ?, ?, 'pending', NULL, ?, ?, ?, ?, ?)""",
        (
            member_id,
            warroom_id,
            runtime_id,
            desired_role,
            desired_repo,
            spawn_order,
            tmux_target,
            pane_id,
            now,
            now,
        ),
    )
    return member_id


def tag_member_pane(
    pane_info: dict,
    *,
    warroom_member_id: str,
    desired_role: str,
    desired_runtime: str,
    spawn_order: int,
) -> None:
    """Annotate a spawned pane dict with the bookkeeping keys callers return."""
    pane_info["warroom_member_id"] = warroom_member_id
    pane_info["desired_role"] = desired_role
    pane_info["desired_runtime"] = desired_runtime
    pane_info["spawn_order"] = spawn_order

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
from server._identity import self_tmux_target
from server._tmux import gateway


def resolve_tmux_session() -> tuple[str | None, dict | None]:
    """Return the tmux session the *calling agent* lives in.

    Returns ``(session, None)`` when the caller's session resolves, else
    ``(None, error_dict)``. Both spawn paths share this preflight because
    a warroom cannot be created outside tmux.

    The registry is consulted before the environment. ``os.environ``
    describes the MCP server process, which is only a tmux descendant by
    accident of the host runtime: Codex strips ``TMUX``/``TMUX_PANE`` when
    spawning stdio servers, so an env-only check reports "not inside tmux"
    for a caller that plainly is. The registered ``tmux_target`` is also
    caller-specific, where the env fallback can only name whichever
    session is currently attached.
    """
    target = self_tmux_target()
    if target:
        session, _, _ = target.partition(":")
        if session:
            return session, None
    if not os.environ.get("TMUX"):
        return None, {"error": "Not inside a tmux session. Warroom spawn requires tmux."}
    pane = os.environ.get("TMUX_PANE", "")
    current_session = gateway.current_session_name(pane) if pane else gateway.current_session_name()
    if current_session is None:
        return None, {"error": "Cannot determine tmux session"}
    return current_session, None


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

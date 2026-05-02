"""Warroom lifecycle: spawn, kill, add/remove members, presets, status.

Pure application service. All tmux side effects go through the
`gateway` singleton. Status is observational; agent_id backfill is
exposed separately as `reconciliation.backfill_warroom_member_agent_ids`.
"""

from __future__ import annotations

import contextlib
import json
import os
import re
import sqlite3
from pathlib import Path

from server import _db
from server._tmux import gateway
from server._warroom import _resolve_agent_type, _scan_agent_types
from server._warroom_persist import (
    insert_warroom_member,
    resolve_tmux_session,
    tag_member_pane,
    upsert_warroom,
)
from server.runtimes import (
    RuntimeAdapter,
    default_adapter,
    for_id,
    registered_adapters,
)

VALID_LAYOUTS = {
    "tiled",
    "even-horizontal",
    "even-vertical",
    "main-horizontal",
    "main-vertical",
}

WARROOM_NAME_RE = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9-]{0,29}$")
PRESET_NAME_RE = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9-]{0,49}$")

_MESSAGING_INSTRUCTION = (
    "Send messages to warroom members individually by agent_id. "
    "Use nudge_message with individual agent_ids for transient tmux only prompts. "
    "Never use to:'*' or reply_to:'*' as these broadcast to every "
    "agent on the bus, not just this warroom. Use warroom_status to "
    "discover agent_ids once members register."
)


# ── Internal helpers ──────────────────────────────────────────────────────────


def kill_warrooms(conn: sqlite3.Connection, name: str, kill_all: bool) -> list[str]:
    """Kill warrooms and remove their DB records using an existing connection.

    Kills the tmux window for each matching warroom (if still alive)
    and deletes the warroom and its members from the database. Used
    both as `warroom_kill`'s dispatch target and as the idempotency
    pre-step inside `spawn` and `spawn_repos` (atomic with the
    subsequent INSERT).
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
        gateway.kill_window(row["tmux_session"], row["tmux_window"])
        conn.execute("DELETE FROM warroom_members WHERE warroom_id = ?", (wid,))
        conn.execute("DELETE FROM warrooms WHERE warroom_id = ?", (wid,))
        killed.append(wid)
    return killed


def _build_suggestions(needle: str, all_types: list[dict], limit: int = 5) -> list[str]:
    q = needle.lower()
    return [
        a["qualified_name"]
        for a in all_types
        if q in a["name"].lower() or q in a.get("summary", "").lower()
    ][:limit]


def _short_role_name(role: str) -> str:
    """Return the short-name portion of a persisted desired_role."""
    return role.rsplit(":", 1)[-1]


def _resolve_runtime(runtime: str) -> tuple[RuntimeAdapter | None, dict | None]:
    """Normalize and validate a user-supplied runtime id.

    Empty string falls back to the default adapter without re-validation
    (the default is registered by construction). A non-empty runtime is
    validated against the registry. Returns ``(adapter, None)`` on
    success or ``(None, error_dict)`` when the requested runtime is not
    registered. Returning the adapter (not just the id) lets callers
    reach capability flags like ``supports_specialist_roles`` without
    a second registry lookup.
    """
    if not runtime:
        return default_adapter(), None
    try:
        adapter = for_id(runtime)
    except KeyError:
        known = sorted(a.runtime_id for a in registered_adapters())
        return None, {"error": f"Unknown runtime {runtime!r}. Known: {known}"}
    return adapter, None


def _require_specialist_support(adapter: RuntimeAdapter) -> dict | None:
    """Return an error dict when the runtime cannot enact specialist roles.

    warroom.spawn and add both spawn panes keyed to a specialist
    qualified_name. A runtime whose ``supports_specialist_roles`` is
    ``False`` would persist role state the runtime never actually
    enacts. Reject such spawns rather than lie about the member's role.
    Returns ``None`` when the runtime is specialist-capable.
    """
    if adapter.supports_specialist_roles:
        return None
    return {
        "error": (
            f"Runtime {adapter.runtime_id!r} does not support specialist-role "
            f"spawn. Use warroom_spawn_repos for general-mode "
            f"{adapter.runtime_id} panes."
        ),
    }


def _member_repo_name(wr: sqlite3.Row, member: sqlite3.Row) -> str:
    """Return the repo segment used in a member's canonical agent id."""
    return member["desired_repo"] or os.path.basename(wr["cwd"])


def _member_agent_id(wr: sqlite3.Row, member: sqlite3.Row, tmux_target: str) -> str:
    return f"{_member_repo_name(wr, member)}:{member['desired_role']}:{tmux_target}"


def _rewrite_pid_mapping_batch(moves: list[dict]) -> None:
    """Rewrite PID mappings from a precomputed snapshot of identity moves."""
    pids_dir = _db.BUS_DIR / "pids"
    if not pids_dir.is_dir():
        return

    rewrites = []
    old_to_new = {
        move["old_agent_id"]: move["new_agent_id"]
        for move in moves
        if move["old_agent_id"] != move["new_agent_id"]
    }
    if not old_to_new:
        return

    for pid_file in pids_dir.iterdir():
        if not pid_file.is_file():
            continue
        try:
            current_agent_id = pid_file.read_text().strip()
        except OSError:
            continue
        if current_agent_id in old_to_new:
            rewrites.append((pid_file, old_to_new[current_agent_id]))

    for pid_file, new_agent_id in rewrites:
        try:
            pid_file.write_text(new_agent_id)
        except OSError:
            continue


def _merge_directory(src: Path, dst: Path) -> None:
    """Merge files from src into dst without overwriting existing files."""
    dst.mkdir(parents=True, exist_ok=True)
    for item in src.iterdir():
        target = dst / item.name
        if item.is_dir():
            _merge_directory(item, target)
            with contextlib.suppress(OSError):
                item.rmdir()
            continue

        final_target = target
        while final_target.exists():
            final_target = target.with_name(f"{target.stem}-{_db._new_member_id()}{target.suffix}")
        try:
            item.rename(final_target)
        except OSError:
            continue
    with contextlib.suppress(OSError):
        src.rmdir()


def _migrate_inbox_mapping_batch(moves: list[dict]) -> None:
    """Move unread and archived mail from old agent ids to new agent ids."""
    old_to_new = {
        move["old_agent_id"]: move["new_agent_id"]
        for move in moves
        if move["old_agent_id"] != move["new_agent_id"]
    }
    if not old_to_new:
        return

    staged = []
    for old_agent_id, new_agent_id in old_to_new.items():
        old_inbox = _db.INBOX_DIR / old_agent_id
        if not old_inbox.is_dir():
            continue
        temp_inbox = _db.INBOX_DIR / f".rekey-{_db._new_member_id()}"
        try:
            old_inbox.rename(temp_inbox)
        except OSError:
            continue
        staged.append((temp_inbox, _db.INBOX_DIR / new_agent_id))

    for temp_inbox, new_inbox in staged:
        if not new_inbox.exists():
            with contextlib.suppress(OSError):
                temp_inbox.rename(new_inbox)
            if temp_inbox.exists():
                _merge_directory(temp_inbox, new_inbox)
            continue
        _merge_directory(temp_inbox, new_inbox)


def _find_agent_registration(
    conn: sqlite3.Connection,
    *,
    old_agent_id: str,
    old_tmux_target: str,
    desired_role: str,
) -> sqlite3.Row | None:
    """Find the registry row that belongs to a member before reflow."""
    row = conn.execute(
        "SELECT rowid AS registry_rowid, * FROM agents WHERE agent_id = ?",
        (old_agent_id,),
    ).fetchone()
    if row is None and old_tmux_target:
        row = conn.execute(
            "SELECT rowid AS registry_rowid, * FROM agents WHERE tmux_target = ? AND agent_type = ?",
            (old_tmux_target, desired_role),
        ).fetchone()
    return row


def _refresh_warroom_member_targets(conn: sqlite3.Connection, wr: sqlite3.Row) -> int:
    """Reconcile persisted member identity against stable tmux pane ids."""
    now = _db._now()
    updated = 0
    members = conn.execute(
        "SELECT * FROM warroom_members WHERE warroom_id = ? ORDER BY spawn_order",
        (wr["warroom_id"],),
    ).fetchall()

    moves = []
    for member in members:
        try:
            current_target = gateway.target_for_pane(member["pane_id"])
        except RuntimeError:
            continue

        old_target = member["tmux_target"]
        old_agent_id = member["agent_instance_id"] or _member_agent_id(wr, member, old_target)
        new_agent_id = _member_agent_id(wr, member, current_target)
        registry_row = _find_agent_registration(
            conn,
            old_agent_id=old_agent_id,
            old_tmux_target=old_target,
            desired_role=member["desired_role"],
        )
        moves.append(
            {
                "member": member,
                "old_target": old_target,
                "current_target": current_target,
                "old_agent_id": old_agent_id,
                "new_agent_id": new_agent_id,
                "registry_rowid": registry_row["registry_rowid"] if registry_row else None,
            }
        )

    staged_rowids: set[int] = set()
    for move in moves:
        rowid = move["registry_rowid"]
        if rowid is None or rowid in staged_rowids:
            continue
        staged_rowids.add(rowid)
        temp_agent_id = f"__helioy_rekey__:{rowid}:{_db._new_member_id()}"
        conn.execute(
            "UPDATE agents SET agent_id = ? WHERE rowid = ?",
            (temp_agent_id, rowid),
        )

    rekeyed_rowids: set[int] = set()
    for move in moves:
        member = move["member"]
        rowid = move["registry_rowid"]
        registry_rekeyed = rowid is not None
        if rowid is not None and rowid not in rekeyed_rowids:
            conn.execute(
                "UPDATE agents "
                "SET agent_id = ?, tmux_target = ?, agent_type = ?, runtime = ? "
                "WHERE rowid = ?",
                (
                    move["new_agent_id"],
                    move["current_target"],
                    member["desired_role"],
                    member["desired_runtime"],
                    rowid,
                ),
            )
            rekeyed_rowids.add(rowid)

        with contextlib.suppress(RuntimeError):
            gateway.set_pane_title(member["pane_id"], move["new_agent_id"])

        next_agent_id = move["new_agent_id"] if registry_rekeyed else None
        next_state = "active" if next_agent_id else "pending"
        if (
            move["old_target"] == move["current_target"]
            and member["agent_instance_id"] == next_agent_id
            and member["state"] == next_state
        ):
            continue

        conn.execute(
            "UPDATE warroom_members "
            "SET tmux_target = ?, agent_instance_id = ?, state = ?, updated_at = ? "
            "WHERE warroom_member_id = ?",
            (
                move["current_target"],
                next_agent_id,
                next_state,
                now,
                member["warroom_member_id"],
            ),
        )
        updated += 1
    _rewrite_pid_mapping_batch(moves)
    _migrate_inbox_mapping_batch(moves)
    return updated


# ── Service operations ────────────────────────────────────────────────────────


def discover(
    *,
    query: str = "",
    namespace: str = "",
    limit: int = 20,
    runtime: str = "",
) -> dict:
    """Search available agent types across registered runtimes.

    Empty ``runtime`` returns the union (every registered runtime's
    catalogue). A registered runtime id scopes discovery to that runtime.
    Unknown runtime ids return a helpful error listing the registered ids.
    """
    if runtime:
        adapter, err = _resolve_runtime(runtime)
        if err:
            return err
        assert adapter is not None
        all_types = _scan_agent_types(adapter.runtime_id)
    else:
        all_types = _scan_agent_types()
    all_namespaces = sorted({a["namespace"] for a in all_types})
    all_runtimes = sorted({a.get("runtime", "") for a in all_types if a.get("runtime")})

    filtered = all_types
    if namespace:
        filtered = [a for a in filtered if a["namespace"] == namespace]
    if query:
        q = query.lower()
        filtered = [
            a for a in filtered if q in a["name"].lower() or q in a.get("summary", "").lower()
        ]

    return {
        "agents": filtered[:limit],
        "total": len(filtered),
        "namespaces": all_namespaces,
        "runtimes": all_runtimes,
    }


def spawn_repos(
    *,
    window: str = "warroom",
    layout: str = "tiled",
    runtime: str = "",
) -> dict:
    """Spawn one general-role agent per helioy repo in a single tmux window."""
    if layout not in VALID_LAYOUTS:
        return {"error": f"Invalid layout. Choose from: {', '.join(sorted(VALID_LAYOUTS))}"}

    session, err = resolve_tmux_session()
    if err:
        return err
    assert session is not None

    adapter, err = _resolve_runtime(runtime)
    if err:
        return err
    assert adapter is not None
    runtime_id = adapter.runtime_id

    base = Path(os.environ.get("HELIOY_BASE", Path.home() / "Dev/LLM/DEV/helioy"))
    if not base.is_dir():
        return {"error": f"HELIOY_BASE not found: {base}"}

    repos = sorted(p for p in base.iterdir() if p.is_dir() and (p / ".git").exists())
    if not repos:
        return {"error": f"No git repos found under {base}"}

    with _db.db() as conn:
        kill_warrooms(conn, window, kill_all=False)

    now = _db._now()
    members = []
    spawn_errors = []
    for i, repo_path in enumerate(repos):
        try:
            pane_info = gateway.spawn_pane(
                session=session,
                window=window,
                cwd=str(repo_path),
                agent_type="general",
                qualified_name=None,
                is_first=(i == 0),
                layout=layout,
                runtime=runtime_id,
            )
            pane_info["desired_repo"] = repo_path.name
            members.append(pane_info)
        except RuntimeError as e:
            spawn_errors.append({"repo": repo_path.name, "error": str(e)})

    with _db.db() as conn:
        upsert_warroom(
            conn,
            warroom_id=window,
            session=session,
            window=window,
            cwd=str(base),
            layout=layout,
            now=now,
        )
        for order, m in enumerate(members):
            role = m["qualified_name"] or m["agent_type"] or "general"
            member_id = insert_warroom_member(
                conn,
                warroom_id=window,
                runtime_id=runtime_id,
                desired_role=role,
                desired_repo=m["desired_repo"],
                spawn_order=order,
                tmux_target=m["tmux_target"],
                pane_id=m["pane_id"],
                now=now,
            )
            tag_member_pane(
                m,
                warroom_member_id=member_id,
                desired_role=role,
                desired_runtime=runtime_id,
                spawn_order=order,
            )

    result: dict = {
        "warroom_id": window,
        "tmux_window": window,
        "members": members,
        "spawned_at": now,
        "messaging": {"instruction": _MESSAGING_INSTRUCTION},
    }
    if spawn_errors:
        result["errors"] = spawn_errors
    return result


def spawn(
    *,
    name: str,
    agents: list[str],
    cwd: str = "",
    layout: str = "tiled",
    runtime: str = "",
) -> dict:
    """Create a named warroom with one runtime pane per agent type."""
    if not name or not WARROOM_NAME_RE.match(name):
        return {
            "error": "Name must be 1-30 chars, alphanumeric and hyphens, "
            "starting with alphanumeric."
        }
    if not agents:
        return {"error": "At least one agent type is required."}
    if len(agents) > 8:
        return {"error": "Maximum 8 agents per warroom."}
    if layout not in VALID_LAYOUTS:
        return {"error": f"Invalid layout. Choose from: {', '.join(sorted(VALID_LAYOUTS))}"}

    session, err = resolve_tmux_session()
    if err:
        return err
    assert session is not None

    adapter, err = _resolve_runtime(runtime)
    if err:
        return err
    assert adapter is not None
    if cap_err := _require_specialist_support(adapter):
        return cap_err
    runtime_id = adapter.runtime_id

    if not cwd:
        cwd = os.getcwd()

    resolved = []
    errors = []
    all_types = _scan_agent_types(runtime_id)
    for agent_name in agents:
        agent_def = _resolve_agent_type(agent_name, runtime_id)
        if agent_def is None:
            errors.append(
                {
                    "agent": agent_name,
                    "error": "Unknown agent type",
                    "suggestions": _build_suggestions(agent_name, all_types),
                }
            )
        else:
            resolved.append(agent_def)

    if errors:
        return {"error": "Unknown agent types", "details": errors}

    # Idempotency: tmux rejects duplicate window names, so kill first.
    with _db.db() as conn:
        kill_warrooms(conn, name, kill_all=False)

    now = _db._now()
    members = []
    spawn_errors = []
    for i, agent_def in enumerate(resolved):
        try:
            pane_info = gateway.spawn_pane(
                session=session,
                window=name,
                cwd=cwd,
                agent_type=agent_def["name"],
                qualified_name=agent_def["qualified_name"],
                is_first=(i == 0),
                layout=layout,
                runtime=runtime_id,
            )
            members.append(pane_info)
        except RuntimeError as e:
            spawn_errors.append(
                {
                    "agent_type": agent_def["qualified_name"],
                    "error": str(e),
                }
            )

    with _db.db() as conn:
        upsert_warroom(
            conn,
            warroom_id=name,
            session=session,
            window=name,
            cwd=cwd,
            layout=layout,
            now=now,
        )
        for order, m in enumerate(members):
            qn = m["qualified_name"]
            member_id = insert_warroom_member(
                conn,
                warroom_id=name,
                runtime_id=runtime_id,
                desired_role=qn,
                desired_repo=None,
                spawn_order=order,
                tmux_target=m["tmux_target"],
                pane_id=m["pane_id"],
                now=now,
            )
            tag_member_pane(
                m,
                warroom_member_id=member_id,
                desired_role=qn,
                desired_runtime=runtime_id,
                spawn_order=order,
            )

    member_types = [m["qualified_name"] or m["agent_type"] for m in members]

    result = {
        "warroom_id": name,
        "tmux_window": name,
        "members": members,
        "spawned_at": now,
        "messaging": {
            "instruction": _MESSAGING_INSTRUCTION,
            "member_types": member_types,
        },
    }
    if spawn_errors:
        result["errors"] = spawn_errors
    return result


def kill(*, name: str = "", kill_all: bool = False) -> dict:
    if not name and not kill_all:
        return {"error": "Provide a warroom name or set kill_all=True."}

    with _db.db() as conn:
        killed = kill_warrooms(conn, name, kill_all)

    return {"killed": killed, "errors": []}


def status(*, name: str = "") -> list[dict]:
    """Return live warroom status with cross-referenced agent registrations.

    Observational. Hidden writes (member agent_id backfill) live in
    `reconciliation.backfill_warroom_member_agent_ids` and the handler
    calls them before this read.
    """
    with _db.db() as conn:
        if name:
            warrooms = conn.execute(
                "SELECT * FROM warrooms WHERE warroom_id = ?", (name,)
            ).fetchall()
        else:
            warrooms = conn.execute("SELECT * FROM warrooms WHERE status = 'active'").fetchall()

        result = []
        for wr in warrooms:
            wid = wr["warroom_id"]
            members_rows = conn.execute(
                """
                SELECT wm.*,
                       a.agent_id   AS registered_agent_id,
                       a.token_usage AS agent_token_usage
                FROM warroom_members wm
                LEFT JOIN agents a ON a.tmux_target = wm.tmux_target
                WHERE wm.warroom_id = ?
                ORDER BY wm.spawn_order
                """,
                (wid,),
            ).fetchall()

            members = []
            for m in members_rows:
                tmux_target = m["tmux_target"]
                pane_alive = gateway.pane_alive(tmux_target)

                registered = pane_alive and m["registered_agent_id"] is not None
                agent_instance_id = m["registered_agent_id"] if registered else None
                state = "active" if registered else "pending"
                token_usage_raw = m["agent_token_usage"] if registered else None
                token_usage: dict | str | None = token_usage_raw
                if token_usage_raw:
                    with contextlib.suppress(json.JSONDecodeError, TypeError):
                        token_usage = json.loads(token_usage_raw)

                members.append(
                    {
                        "warroom_member_id": m["warroom_member_id"],
                        "desired_runtime": m["desired_runtime"],
                        "desired_role": m["desired_role"],
                        "desired_repo": m["desired_repo"],
                        "state": state,
                        "agent_instance_id": agent_instance_id,
                        "spawn_order": m["spawn_order"],
                        "agent_type": m["desired_role"],
                        "tmux_target": tmux_target,
                        "pane_id": m["pane_id"],
                        "registered": registered,
                        "pane_alive": pane_alive,
                        "created_at": m["created_at"],
                        "updated_at": m["updated_at"],
                        "token_usage": token_usage,
                    }
                )

            result.append(
                {
                    "warroom_id": wid,
                    "tmux_session": wr["tmux_session"],
                    "tmux_window": wr["tmux_window"],
                    "cwd": wr["cwd"],
                    "layout": wr["layout"],
                    "runtime_policy": wr["runtime_policy"],
                    "metadata": wr["metadata"],
                    "status": wr["status"],
                    "created_at": wr["created_at"],
                    "members": members,
                }
            )

    return result


def add(*, name: str, agent: str, cwd: str = "", runtime: str = "") -> dict:
    adapter, err = _resolve_runtime(runtime)
    if err:
        return err
    assert adapter is not None
    if cap_err := _require_specialist_support(adapter):
        return cap_err
    runtime_id = adapter.runtime_id

    agent_def = _resolve_agent_type(agent, runtime_id)
    if not agent_def:
        all_types = _scan_agent_types(runtime_id)
        return {
            "error": "Unknown agent type",
            "suggestions": _build_suggestions(agent, all_types),
        }

    qn = agent_def["qualified_name"]

    with _db.db() as conn:
        wr = conn.execute(
            "SELECT * FROM warrooms WHERE warroom_id = ? AND status = 'active'",
            (name,),
        ).fetchone()
        if not wr:
            return {"error": f"No active warroom '{name}'."}

        next_order = conn.execute(
            "SELECT COALESCE(MAX(spawn_order), -1) + 1 FROM warroom_members WHERE warroom_id = ?",
            (name,),
        ).fetchone()[0]

        use_cwd = cwd or wr["cwd"]

        try:
            pane_info = gateway.spawn_pane(
                session=wr["tmux_session"],
                window=wr["tmux_window"],
                cwd=use_cwd,
                agent_type=agent_def["name"],
                qualified_name=qn,
                is_first=False,
                layout=wr["layout"],
                runtime=runtime_id,
                launch=False,
            )
            _refresh_warroom_member_targets(conn, wr)
            pane_info["tmux_target"] = gateway.target_for_pane(pane_info["pane_id"])
            pane_info = gateway.launch_pane(
                pane_id=pane_info["pane_id"],
                cwd=use_cwd,
                agent_type=agent_def["name"],
                qualified_name=qn,
                runtime=runtime_id,
                tmux_target=pane_info["tmux_target"],
            )
        except RuntimeError as e:
            return {"error": f"Spawn failed: {e}"}

        now = _db._now()
        member_id = insert_warroom_member(
            conn,
            warroom_id=name,
            runtime_id=runtime_id,
            desired_role=qn,
            desired_repo=None,
            spawn_order=next_order,
            tmux_target=pane_info["tmux_target"],
            pane_id=pane_info["pane_id"],
            now=now,
        )
        count = conn.execute(
            "SELECT COUNT(*) FROM warroom_members WHERE warroom_id = ?", (name,)
        ).fetchone()[0]

    tag_member_pane(
        pane_info,
        warroom_member_id=member_id,
        desired_role=qn,
        desired_runtime=runtime_id,
        spawn_order=next_order,
    )
    return {
        "warroom_id": name,
        "added": pane_info,
        "member_count": count,
    }


def remove(*, name: str, agent: str = "", member_id: str = "") -> dict:
    if not member_id and not agent:
        return {"error": "Provide either member_id or agent."}

    with _db.db() as conn:
        if member_id:
            member = conn.execute(
                "SELECT * FROM warroom_members WHERE warroom_member_id = ? AND warroom_id = ?",
                (member_id, name),
            ).fetchone()
            if not member:
                return {"error": f"No member '{member_id}' in warroom '{name}'."}
        else:
            members = conn.execute(
                "SELECT * FROM warroom_members WHERE warroom_id = ? ORDER BY spawn_order",
                (name,),
            ).fetchall()
            if ":" in agent:
                matches = [m for m in members if m["desired_role"] == agent]
            else:
                matches = [m for m in members if _short_role_name(m["desired_role"]) == agent]
            if not matches:
                return {"error": f"No member with role '{agent}' in warroom '{name}'."}
            if len(matches) > 1:
                return {
                    "error": f"Role '{agent}' is ambiguous in warroom '{name}'.",
                    "candidates": [
                        {
                            "warroom_member_id": m["warroom_member_id"],
                            "tmux_target": m["tmux_target"],
                            "desired_repo": m["desired_repo"],
                        }
                        for m in matches
                    ],
                }
            member = matches[0]

        gateway.kill_pane(member["pane_id"])

        conn.execute(
            "DELETE FROM warroom_members WHERE warroom_member_id = ?",
            (member["warroom_member_id"],),
        )

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
            wr = conn.execute(
                "SELECT tmux_session, tmux_window, layout FROM warrooms WHERE warroom_id = ?",
                (name,),
            ).fetchone()
            if wr:
                gateway.select_layout(wr["tmux_session"], wr["tmux_window"], wr["layout"])

    return {
        "warroom_id": name,
        "removed": {
            "warroom_member_id": member["warroom_member_id"],
            "desired_role": member["desired_role"],
        },
        "remaining_members": remaining,
        "warroom_killed": warroom_killed,
    }


def list_presets() -> dict:
    presets = []
    if not _db.PRESETS_DIR.is_dir():
        return {"presets": []}

    for path in sorted(_db.PRESETS_DIR.glob("*.json")):
        try:
            data = json.loads(path.read_text())
            presets.append(
                {
                    "name": data.get("name", path.stem),
                    "description": data.get("description", ""),
                    "agents": data.get("agents", []),
                    "tags": data.get("tags", []),
                }
            )
        except (json.JSONDecodeError, OSError):
            continue

    return {"presets": presets}


def save_preset(
    *,
    name: str,
    agents: list[str],
    description: str = "",
    tags: list[str] | None = None,
) -> dict:
    if not name or not PRESET_NAME_RE.match(name):
        return {
            "error": "Name must be 1-50 chars, alphanumeric and hyphens, "
            "starting with alphanumeric."
        }
    if not agents:
        return {"error": "At least one agent type is required."}

    _db.PRESETS_DIR.mkdir(parents=True, exist_ok=True)
    preset_path = _db.PRESETS_DIR / f"{name}.json"

    data = {
        "name": name,
        "description": description,
        "agents": agents,
        "tags": tags or [],
    }

    preset_path.write_text(json.dumps(data, indent=2))
    return {"saved": name, "path": str(preset_path)}

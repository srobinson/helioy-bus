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
from server._tmux import gateway


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


def _continuity_identity(conn, *, pane_id: str, pwd: str) -> tuple[str, str] | None:
    """Return (agent_id, agent_type) of the existing row for this pane, if any.

    A pane's stable %N id is the identity anchor. When a registration
    arrives with a weak identity (address-minted fallback, or no id at
    all), the pane's existing registration is the truth: reusing it keeps
    the bus id stable across /clear, compaction, and window re-indexing.
    Without this, a re-registration after re-indexing mints the pane's
    CURRENT address into a fresh id, which can equal another agent's
    birth-address id and silently steal its row (identity takeover,
    reproduced live 2026-07-09).

    Guarded on cwd equality: a pane relaunched from a different project
    is a new agent, not a continuation.
    """
    if not pane_id:
        return None
    row = conn.execute(
        "SELECT agent_id, agent_type, cwd FROM agents WHERE pane_id = ? "
        "ORDER BY last_seen DESC LIMIT 1",
        (pane_id,),
    ).fetchone()
    if row is None or row["cwd"] != pwd:
        return None
    return row["agent_id"], row["agent_type"]


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
    pid: int | None = None,
    id_source: str = "",
) -> dict:
    """Insert or replace an agent registration.

    Pane eviction: a tmux pane hosts at most one runtime process at a
    time, so any prior row claiming our stable pane_id is stale by
    definition. The mutable tmux_target arm applies ONLY to legacy rows
    without a pane_id: a pane_id-carrying row whose target merely
    collides is a different, possibly live, pane whose address went
    stale under window re-indexing — sync_pane_addresses heals it and
    prune_dead_agents evicts it when its pane dies. Evicting it here
    deleted live survivors (reproduced live 2026-07-09).

    Identity continuity: when the caller's identity is weak (empty
    agent_id, or ``id_source == "fallback"`` from the hook resolver's
    address-minting branch), the pane's existing registration wins.

    ``pid`` lets the hook registrar pass the runtime's real PID; the
    in-process MCP path defaults to this server's parent.
    """
    if not session_id:
        session_id = os.environ.get("HELIOY_SESSION_ID", "")
    if not runtime:
        runtime = os.environ.get("HELIOY_RUNTIME", "claude")

    parent_pid = pid if pid is not None else os.getppid()
    now = _db._now()
    profile_json = json.dumps(profile) if profile else None
    # pane_id is caller-supplied, never sniffed from this process's env:
    # tmux_target may describe a different pane than the one hosting the
    # server (tests, orchestrators registering on behalf of others), and a
    # wrong stable id is worse than none. The hook registrar passes its own
    # $TMUX_PANE; rows without pane_id fall back to tmux_target liveness.

    with _db.db() as conn:
        if not agent_id or id_source == "fallback":
            reused = _continuity_identity(conn, pane_id=pane_id, pwd=pwd)
            if reused is not None:
                agent_id, agent_type = reused
        if not agent_id:
            agent_id = canonical_agent_id(pwd, agent_type, tmux_target)

        # Cross-pane takeover guard. Address-derived ids are not unique
        # over time: after window re-indexing, this pane's address (and
        # therefore its minted or title-derived id) can equal a LIVE
        # agent's birth id. INSERT OR REPLACE keyed on that id would
        # silently steal the other agent's row and misroute its mail
        # (reproduced live 2026-07-09). If the id is claimed by a
        # different pane, evict the claimant only when its pane is dead;
        # when it is alive, disambiguate our own id with the stable pane
        # id instead. Deterministic, so re-registrations converge.
        if pane_id:
            claimant = conn.execute(
                "SELECT pane_id, tmux_target FROM agents "
                "WHERE agent_id = ? AND pane_id != '' AND pane_id != ?",
                (agent_id, pane_id),
            ).fetchone()
            if claimant is not None:
                if gateway.pane_alive(claimant["pane_id"]):
                    agent_id = f"{agent_id}:{pane_id}"
                    _db._dbg(
                        f"register: id claimed by live pane {claimant['pane_id']}, "
                        f"disambiguated to {agent_id!r}"
                    )
                else:
                    conn.execute("DELETE FROM agents WHERE pane_id = ?", (claimant["pane_id"],))

        if tmux_target:
            conn.execute(
                "DELETE FROM agents WHERE agent_id != ? "
                "AND ((pane_id != '' AND pane_id = ?) OR (pane_id = '' AND tmux_target = ?))",
                (agent_id, pane_id, tmux_target),
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

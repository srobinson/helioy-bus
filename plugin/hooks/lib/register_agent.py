#!/usr/bin/env python3
"""Register a hook-launched agent with the helioy-bus registry."""

from __future__ import annotations

import os
import sys
from pathlib import Path


def main() -> None:
    root = os.environ["HELIOY_BUS_ROOT"]
    sys.path.insert(0, root)

    from server._db import _now, db
    import server._db as db_mod

    bus_dir = Path(os.environ["_HELIOY_BUS_DIR"])
    inbox_base = Path(os.environ["_HELIOY_INBOX_BASE"])
    db_mod.BUS_DIR = bus_dir
    db_mod.REGISTRY_DB = bus_dir / "registry.db"
    db_mod.INBOX_DIR = inbox_base

    agent_id = os.environ["_HELIOY_AGENT_ID"]
    inbox = inbox_base / agent_id
    inbox.mkdir(parents=True, exist_ok=True)

    with db() as conn:
        tmux_target = os.environ["_HELIOY_TMUX"]
        pane_id = os.environ.get("_HELIOY_PANE_ID", "")
        if tmux_target:
            # Pane ownership eviction: a pane hosts one runtime at a time.
            # Match on the stable pane_id too, so a stale row whose
            # tmux_target drifted under window re-indexing is still evicted.
            conn.execute(
                "DELETE FROM agents WHERE agent_id != ? "
                "AND (tmux_target = ? OR (pane_id != '' AND pane_id = ?))",
                (agent_id, tmux_target, pane_id),
            )
        conn.execute(
            """
            INSERT OR REPLACE INTO agents
                (agent_id, cwd, tmux_target, pane_id, pid, session_id,
                 agent_type, runtime, registered_at, last_seen)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                agent_id,
                os.environ["_HELIOY_PWD"],
                tmux_target,
                pane_id,
                int(os.environ["_HELIOY_PID"]),
                os.environ.get("_HELIOY_SESSION_ID", ""),
                os.environ.get("_HELIOY_AGENT_TYPE", "general"),
                os.environ.get("_HELIOY_RUNTIME", "claude"),
                _now(),
                _now(),
            ),
        )


if __name__ == "__main__":
    main()

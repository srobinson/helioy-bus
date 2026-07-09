#!/usr/bin/env python3
"""Register a hook-launched agent with the helioy-bus registry.

Thin env-to-kwargs shim over ``server.services.agent_registry.register``
so the hook path and the MCP tool path share one implementation of
eviction scoping and identity continuity. The two used to carry
separate copies of the eviction SQL, which is how the survivor-eviction
bug outlived the bus-prune.sh fix.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path


def main() -> None:
    root = os.environ["HELIOY_BUS_ROOT"]
    sys.path.insert(0, root)

    import server._db as db_mod
    from server.services import agent_registry

    bus_dir = Path(os.environ["_HELIOY_BUS_DIR"])
    inbox_base = Path(os.environ["_HELIOY_INBOX_BASE"])
    db_mod.BUS_DIR = bus_dir
    db_mod.REGISTRY_DB = bus_dir / "registry.db"
    db_mod.INBOX_DIR = inbox_base

    result = agent_registry.register(
        pwd=os.environ["_HELIOY_PWD"],
        tmux_target=os.environ["_HELIOY_TMUX"],
        agent_id=os.environ["_HELIOY_AGENT_ID"],
        session_id=os.environ.get("_HELIOY_SESSION_ID", ""),
        agent_type=os.environ.get("_HELIOY_AGENT_TYPE", "general"),
        runtime=os.environ.get("_HELIOY_RUNTIME", "claude"),
        pane_id=os.environ.get("_HELIOY_PANE_ID", ""),
        profile=None,
        pid=int(os.environ["_HELIOY_PID"]),
        id_source=os.environ.get("_HELIOY_ID_SOURCE", ""),
    )

    # Identity continuity may have adopted a different agent_id than the
    # shell resolver minted; the PID mapping written by bus-register.sh
    # before this script ran must follow the final id or self-identity
    # (whoami, send_message sender resolution) would diverge.
    final_id = result["agent_id"]
    pids_dir = bus_dir / "pids"
    pids_dir.mkdir(parents=True, exist_ok=True)
    (pids_dir / os.environ["_HELIOY_PID"]).write_text(final_id)


if __name__ == "__main__":
    main()

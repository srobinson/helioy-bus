"""End-to-end smoke test for the helioy-bus lifecycle.

This is the single operational workflow exercise: a real SessionStart
hook registers the agent, the MCP tool surface sends a message to a
peer, listing and token capture reach the live registry row, the peer
reads and archives the message, and the SessionEnd hook cleans up.

If any boundary between the shell hooks, the Python services, and the
SQLite registry is wrong, this test fails — catching failures that the
in-process handler tests cannot see because they bypass the hooks.
"""

from __future__ import annotations

import json
import os
import sqlite3
import subprocess
from datetime import UTC, datetime
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
REGISTER_HOOK = REPO_ROOT / "plugin" / "hooks" / "bus-register.sh"
UNREGISTER_HOOK = REPO_ROOT / "plugin" / "hooks" / "bus-unregister.sh"

SMOKE_PROJECT_DIR = "/tmp/helioy-smoke-repo"
SMOKE_AGENT_ID = "helioy-smoke-repo:general"
PEER_AGENT_ID = "peer-repo:general"


def _run_hook(
    hook: Path, bus_dir: Path, *, stdin: str = ""
) -> subprocess.CompletedProcess:
    env = {
        **os.environ,
        "HELIOY_BUS_DIR": str(bus_dir),
        "HELIOY_BUS_PYTHON_PATH": str(REPO_ROOT),
        "CLAUDE_PROJECT_DIR": SMOKE_PROJECT_DIR,
        "PWD": SMOKE_PROJECT_DIR,
    }
    for key in ("TMUX", "TMUX_PANE"):
        env.pop(key, None)
    return subprocess.run(
        ["bash", str(hook)],
        input=stdin,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )


def test_full_lifecycle_smoke(isolated_bus, set_sender):
    """Register via hook, send, list, capture tokens, read, unregister via hook."""
    import server.bus_server as bm
    from server import _db

    # 1. Register the smoke agent via the real SessionStart hook.
    reg = _run_hook(
        REGISTER_HOOK, isolated_bus, stdin='{"session_id":"smoke-session"}'
    )
    assert reg.returncode == 0, reg.stderr
    assert (isolated_bus / "pids" / str(os.getpid())).read_text().strip() == (
        SMOKE_AGENT_ID
    )

    # 2. Register a peer in-process so send has a real target.
    bm.register_agent(
        pwd="/tmp/peer-repo",
        agent_id=PEER_AGENT_ID,
        session_id="peer-session",
    )

    # 3. Send from smoke -> peer via the MCP tool surface.
    set_sender(SMOKE_AGENT_ID)
    result = bm.send_message(
        to=PEER_AGENT_ID,
        content="hello from smoke",
        topic="smoke",
        nudge=False,
    )
    assert result["delivered"] is True
    assert result["recipients"] == [PEER_AGENT_ID]

    # 4. list_agents surfaces both rows (registered by hook + by handler).
    active_ids = {a["agent_id"] for a in bm.list_agents()}
    assert {SMOKE_AGENT_ID, PEER_AGENT_ID} <= active_ids

    # 5. Simulate token capture — token-capture.sh writes token_usage via
    # the same SQL path. Exercise the read side through whoami so a
    # misaligned JSON decode or column rename surfaces here.
    now = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    usage = {"tokens": 4242, "updated": now}
    with _db.db() as conn:
        conn.execute(
            "UPDATE agents SET token_usage = ? WHERE agent_id = ?",
            (json.dumps(usage), SMOKE_AGENT_ID),
        )
    set_sender(SMOKE_AGENT_ID)
    info = bm.whoami()
    assert info["token_usage"] == usage

    # 6. Peer reads the message — unread surfaces, archive happens.
    msgs = bm.get_messages(agent_id=PEER_AGENT_ID)
    assert len(msgs) == 1
    assert msgs[0]["content"] == "hello from smoke"
    assert msgs[0]["from"] == SMOKE_AGENT_ID
    assert msgs[0]["topic"] == "smoke"

    # Second read returns nothing (archived); archive file present.
    assert bm.get_messages(agent_id=PEER_AGENT_ID) == []
    archived = list((isolated_bus / "inbox" / PEER_AGENT_ID / "archive").glob("*.json"))
    assert len(archived) == 1

    # 7. Unregister the smoke agent via the real SessionEnd hook.
    env = {**os.environ, "HELIOY_BUS_DIR": str(isolated_bus)}
    for key in ("TMUX", "TMUX_PANE"):
        env.pop(key, None)
    unreg = subprocess.run(
        ["bash", str(UNREGISTER_HOOK)],
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert unreg.returncode == 0, unreg.stderr

    # Smoke agent gone; peer untouched; PID file removed.
    remaining = {a["agent_id"] for a in bm.list_agents()}
    assert SMOKE_AGENT_ID not in remaining
    assert PEER_AGENT_ID in remaining
    assert not (isolated_bus / "pids" / str(os.getpid())).exists()

    # Confirm the cleanup hit the DB directly too (guards against list_agents
    # hiding the row via lazy pruning heuristics).
    conn = sqlite3.connect(isolated_bus / "registry.db")
    try:
        rows = conn.execute(
            "SELECT agent_id FROM agents WHERE agent_id = ?", (SMOKE_AGENT_ID,)
        ).fetchall()
    finally:
        conn.close()
    assert rows == []

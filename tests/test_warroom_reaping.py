"""Tests for warroom teardown reaping."""

from __future__ import annotations

import sqlite3
import subprocess
from pathlib import Path

from server._tmux import gateway
from tests.conftest import _insert_member
from tests.test_shell_hooks import _hook_env

REPO_ROOT = Path(__file__).resolve().parent.parent
BUS_PRUNE_HOOK = REPO_ROOT / "plugin" / "hooks" / "bus-prune.sh"


def _insert_warroom(conn, *, warroom_id: str, status: str, now: str) -> None:
    conn.execute(
        "INSERT INTO warrooms (warroom_id, tmux_session, tmux_window, cwd, created_at, status) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (warroom_id, "main", warroom_id, "/tmp", now, status),
    )


def test_warroom_status_reaps_dead_window(monkeypatch):
    """Status marks an active warroom killed when its tmux window is gone."""
    import server.warroom_server as wm
    from server._db import _now, db

    now = _now()
    with db() as conn:
        conn.execute("PRAGMA foreign_keys = ON")
        _insert_warroom(conn, warroom_id="dead-window-wr", status="active", now=now)
        _insert_member(
            conn,
            warroom_id="dead-window-wr",
            role="helioy-tools:backend-engineer",
            tmux_target="main:2.0",
            pane_id="%20",
            now=now,
        )

    monkeypatch.setattr(gateway, "pane_alive", lambda t: t != "main:dead-window-wr")

    statuses = wm.warroom_status()
    assert statuses == []

    named = wm.warroom_status(name="dead-window-wr")
    assert len(named) == 1
    assert named[0]["status"] == "killed"

    with db() as conn:
        row = conn.execute(
            "SELECT status FROM warrooms WHERE warroom_id = ?",
            ("dead-window-wr",),
        ).fetchone()
        assert row["status"] == "killed"


def test_warroom_status_keeps_live_window_active(monkeypatch):
    """A live warroom window remains active even if a member pane is gone."""
    import server.warroom_server as wm
    from server._db import _now, db

    now = _now()
    with db() as conn:
        conn.execute("PRAGMA foreign_keys = ON")
        _insert_warroom(conn, warroom_id="live-window-wr", status="active", now=now)
        _insert_member(
            conn,
            warroom_id="live-window-wr",
            role="helioy-tools:backend-engineer",
            tmux_target="main:2.0",
            pane_id="%20",
            now=now,
            state="active",
            agent_instance_id="project:helioy-tools:backend-engineer:main:2.0",
        )

    monkeypatch.setattr(gateway, "pane_alive", lambda t: t == "main:live-window-wr")

    statuses = wm.warroom_status()
    assert len(statuses) == 1
    assert statuses[0]["warroom_id"] == "live-window-wr"
    assert statuses[0]["status"] == "active"
    assert statuses[0]["members"][0]["state"] == "pending"

    with db() as conn:
        row = conn.execute(
            "SELECT status FROM warrooms WHERE warroom_id = ?",
            ("live-window-wr",),
        ).fetchone()
        assert row["status"] == "active"


def test_bus_prune_reaps_dead_warroom_window(isolated_bus, tmp_path):
    """The tmux hook flips active warrooms to killed when their window is gone."""
    from server._db import _now, db

    tmux_stub = tmp_path / "tmux"
    tmux_stub.write_text(
        "#!/bin/sh\n"
        'for arg in "$@"; do\n'
        '    if [ "$arg" = "main:live-window-wr" ]; then\n'
        "        exit 0\n"
        "    fi\n"
        "done\n"
        'if [ "$1" = "list-panes" ]; then\n'
        "    exit 1\n"
        "fi\n"
        "    exit 0\n"
    )
    tmux_stub.chmod(0o755)

    now = _now()
    with db() as conn:
        _insert_warroom(conn, warroom_id="dead-window-wr", status="active", now=now)
        _insert_warroom(conn, warroom_id="live-window-wr", status="active", now=now)
        _insert_warroom(conn, warroom_id="already-killed-wr", status="killed", now=now)

    result = subprocess.run(
        ["bash", str(BUS_PRUNE_HOOK)],
        env=_hook_env(isolated_bus, TMUX_BIN=str(tmux_stub)),
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr

    conn = sqlite3.connect(isolated_bus / "registry.db")
    try:
        rows = dict(conn.execute("SELECT warroom_id, status FROM warrooms").fetchall())
    finally:
        conn.close()

    assert rows["dead-window-wr"] == "killed"
    assert rows["live-window-wr"] == "active"
    assert rows["already-killed-wr"] == "killed"


def test_bus_prune_evicts_dead_pane_and_readdresses_survivor(isolated_bus, tmp_path):
    """After a kill re-indexes windows, the hook must evict the dead pane's
    row (its stable pane_id is gone) and refresh the survivor's drifted
    tmux_target — not the reverse. Regression: the pre-pane_id hook keyed
    on tmux_target, so it deleted the SURVIVOR (whose old address had
    vanished) and kept the corpse (whose address the survivor had just
    inherited)."""
    from server._db import _now, db

    tmux_stub = tmp_path / "tmux"
    # Post-kill reality: only pane %535 exists, now at addrsync:1.1.
    tmux_stub.write_text(
        "#!/bin/sh\n"
        'if [ "$1" = "list-panes" ] && [ "$2" = "-a" ]; then\n'
        "    printf '%s\\n' '%535 addrsync:1.1'\n"
        "    exit 0\n"
        "fi\n"
        "exit 1\n"
    )
    tmux_stub.chmod(0o755)

    now = _now()
    with db() as conn:
        conn.executemany(
            "INSERT INTO agents (agent_id, cwd, tmux_target, pane_id, registered_at, last_seen) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            [
                ("wr-one:general:addrsync:1.1", "/tmp/wr-one", "addrsync:1.1", "%534", now, now),
                ("wr-two:general:addrsync:2.1", "/tmp/wr-two", "addrsync:2.1", "%535", now, now),
            ],
        )

    result = subprocess.run(
        ["bash", str(BUS_PRUNE_HOOK)],
        env=_hook_env(isolated_bus, TMUX_BIN=str(tmux_stub)),
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr

    conn = sqlite3.connect(isolated_bus / "registry.db")
    try:
        rows = dict(
            conn.execute("SELECT agent_id, tmux_target FROM agents").fetchall()
        )
    finally:
        conn.close()

    assert "wr-one:general:addrsync:1.1" not in rows, "dead pane's row survived"
    assert rows == {"wr-two:general:addrsync:2.1": "addrsync:1.1"}


def test_bus_prune_keeps_pane_id_rows_when_snapshot_fails(isolated_bus, tmp_path):
    """A failed snapshot means "cannot verify", never "all dead"."""
    from server._db import _now, db

    tmux_stub = tmp_path / "tmux"
    tmux_stub.write_text("#!/bin/sh\nexit 1\n")
    tmux_stub.chmod(0o755)

    now = _now()
    with db() as conn:
        conn.execute(
            "INSERT INTO agents (agent_id, cwd, tmux_target, pane_id, registered_at, last_seen) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            ("keep:general:6:1.1", "/tmp/keep", "6:1.1", "%7", now, now),
        )

    result = subprocess.run(
        ["bash", str(BUS_PRUNE_HOOK)],
        env=_hook_env(isolated_bus, TMUX_BIN=str(tmux_stub)),
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr

    conn = sqlite3.connect(isolated_bus / "registry.db")
    try:
        (count,) = conn.execute("SELECT COUNT(*) FROM agents").fetchone()
    finally:
        conn.close()
    assert count == 1

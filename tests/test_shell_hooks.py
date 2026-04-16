"""Contract tests for the SessionStart and SessionEnd shell hooks.

These tests invoke `plugin/hooks/bus-register.sh` and
`plugin/hooks/bus-unregister.sh` as real subprocesses so the DB writes,
PID-file writes, and pane eviction behavior are exercised through the
same code path production uses. The in-process `isolated_bus` fixture
and the shell hooks both resolve paths through `HELIOY_BUS_DIR`, so a
single `tmp_path/bus` backs both sides.
"""

from __future__ import annotations

import os
import sqlite3
import subprocess
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
REGISTER_HOOK = REPO_ROOT / "plugin" / "hooks" / "bus-register.sh"
UNREGISTER_HOOK = REPO_ROOT / "plugin" / "hooks" / "bus-unregister.sh"
RESOLVE_IDENTITY_TESTS = REPO_ROOT / "tests" / "test_resolve_identity.sh"

SMOKE_PROJECT_DIR = "/tmp/helioy-shell-hooks-repo"
SMOKE_AGENT_ID = "helioy-shell-hooks-repo:general"


def _hook_env(bus_dir: Path, **overrides: str) -> dict[str, str]:
    """Build a minimal env for hook invocation.

    Drops TMUX/TMUX_PANE so the fallback identity branch runs by default;
    tests that exercise tmux paths re-add them via overrides.
    """
    env = {
        **os.environ,
        "HELIOY_BUS_DIR": str(bus_dir),
        "HELIOY_BUS_PYTHON_PATH": str(REPO_ROOT),
        "CLAUDE_PROJECT_DIR": SMOKE_PROJECT_DIR,
        "PWD": SMOKE_PROJECT_DIR,
    }
    for key in ("TMUX", "TMUX_PANE"):
        env.pop(key, None)
    env.update(overrides)
    return env


def _run_register(bus_dir: Path, **env_overrides: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["bash", str(REGISTER_HOOK)],
        input='{"session_id":"test-sid"}',
        env=_hook_env(bus_dir, **env_overrides),
        capture_output=True,
        text=True,
        check=False,
    )


def test_resolve_identity_script_passes():
    """The orphan bash harness must run under the default CI path."""
    result = subprocess.run(
        ["bash", str(RESOLVE_IDENTITY_TESTS)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, (
        f"resolve-identity.sh tests failed\nstdout:\n{result.stdout}\n"
        f"stderr:\n{result.stderr}"
    )


def test_bus_register_writes_pid_file_and_db_row(isolated_bus):
    result = _run_register(isolated_bus)
    assert result.returncode == 0, result.stderr

    pid_file = isolated_bus / "pids" / str(os.getpid())
    assert pid_file.exists()
    assert pid_file.read_text().strip() == SMOKE_AGENT_ID

    conn = sqlite3.connect(isolated_bus / "registry.db")
    try:
        rows = conn.execute(
            "SELECT agent_id, cwd, agent_type, session_id FROM agents"
        ).fetchall()
    finally:
        conn.close()
    assert rows == [(SMOKE_AGENT_ID, SMOKE_PROJECT_DIR, "general", "test-sid")]


def test_bus_unregister_removes_row_and_pid_file(isolated_bus):
    assert _run_register(isolated_bus).returncode == 0

    pid_file = isolated_bus / "pids" / str(os.getpid())
    db_path = isolated_bus / "registry.db"
    assert pid_file.exists()

    env = {**os.environ, "HELIOY_BUS_DIR": str(isolated_bus)}
    for key in ("TMUX", "TMUX_PANE"):
        env.pop(key, None)

    result = subprocess.run(
        ["bash", str(UNREGISTER_HOOK)],
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert not pid_file.exists()

    conn = sqlite3.connect(db_path)
    try:
        (count,) = conn.execute("SELECT COUNT(*) FROM agents").fetchone()
    finally:
        conn.close()
    assert count == 0


def test_bus_register_evicts_stale_tmux_target(isolated_bus):
    """A new registration with a known tmux_target evicts any prior row
    claiming that same target. This is the ownership assertion the hook
    makes when a pane is reused across sessions.
    """
    # Bootstrap schema via a no-tmux register, then seed a stale row
    # pointing at the target we are about to reclaim.
    assert _run_register(isolated_bus).returncode == 0

    stale_target = "fake-session:0.0"
    conn = sqlite3.connect(isolated_bus / "registry.db")
    try:
        conn.execute(
            "INSERT INTO agents "
            "(agent_id, cwd, tmux_target, pid, session_id, agent_type, "
            "registered_at, last_seen) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                "stale:agent:fake-session:0.0",
                "/tmp/other-repo",
                stale_target,
                99999,
                "old-sid",
                "general",
                "2020-01-01T00:00:00+00:00",
                "2020-01-01T00:00:00+00:00",
            ),
        )
        conn.commit()
    finally:
        conn.close()

    # Re-run register with TMUX set + explicit target override so the
    # hook derives tmux_target = stale_target and triggers eviction.
    result = _run_register(
        isolated_bus,
        TMUX="/tmp/fake-tmux-socket,1234,0",
        TMUX_PANE="%0",
        HELIOY_BUS_TMUX=stale_target,
    )
    assert result.returncode == 0, result.stderr

    conn = sqlite3.connect(isolated_bus / "registry.db")
    try:
        rows = conn.execute(
            "SELECT agent_id FROM agents WHERE tmux_target = ?",
            (stale_target,),
        ).fetchall()
    finally:
        conn.close()

    assert len(rows) == 1
    assert rows[0][0] == f"{SMOKE_AGENT_ID}:{stale_target}"
    assert rows[0][0] != "stale:agent:fake-session:0.0"

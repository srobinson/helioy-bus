"""Session identity fallback for the SessionEnd unregister hook."""

from __future__ import annotations

import os
import sqlite3
import subprocess

from tests.test_shell_hooks import UNREGISTER_HOOK, _hook_env, _run_register


def test_unregister_uses_session_id_when_pid_mapping_is_missing(isolated_bus, tmp_path):
    """SessionEnd must remove the registered agent even when its hook runs
    from Codex's memories directory and the PID mapping is unavailable."""
    assert _run_register(isolated_bus).returncode == 0
    (isolated_bus / "pids" / str(os.getpid())).unlink()

    memories = tmp_path / ".codex" / "memories"
    memories.mkdir(parents=True)
    result = subprocess.run(
        ["bash", str(UNREGISTER_HOOK)],
        input='{"session_id":"test-sid","reason":"other"}',
        env=_hook_env(
            isolated_bus,
            CLAUDE_PROJECT_DIR="",
            HELIOY_BUS_CWD="",
        ),
        cwd=memories,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    with sqlite3.connect(isolated_bus / "registry.db") as conn:
        assert conn.execute("SELECT COUNT(*) FROM agents").fetchone() == (0,)

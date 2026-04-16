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
CODEX_LAUNCH = REPO_ROOT / "plugin" / "hooks" / "codex-launch.sh"
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


# ── codex-launch.sh: register + unregister lifecycle ─────────────────────────


def _install_codex_stub(tmp_dir: Path, marker: Path) -> Path:
    """Write a fake codex on PATH that records invocation and exits clean.

    Returns the directory to prepend to PATH. Codex receives
    --dangerously-bypass-approvals-and-sandbox; the stub ignores args.
    """
    stub_dir = tmp_dir / "stub-bin"
    stub_dir.mkdir()
    stub = stub_dir / "codex"
    stub.write_text(f"#!/bin/sh\necho codex-ran > {marker}\nexit 0\n")
    stub.chmod(0o755)
    return stub_dir


def test_codex_launch_wrapper_registers_unregisters_and_runs_codex(
    isolated_bus, tmp_path
):
    """End-to-end: wrapper registers agent, runs codex, unregisters on exit."""
    marker = tmp_path / "codex-ran.log"
    stub_dir = _install_codex_stub(tmp_path, marker)

    env = _hook_env(isolated_bus)
    # Prepend the stub dir so `codex` in the wrapper resolves to our fake.
    env["PATH"] = f"{stub_dir}:{env.get('PATH', '')}"

    result = subprocess.run(
        ["bash", str(CODEX_LAUNCH)],
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, (
        f"wrapper failed\nstdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    )

    # Stub codex actually ran.
    assert marker.exists(), "wrapper did not invoke codex"

    # Unregister trap fired on clean exit: agent row is gone.
    conn = sqlite3.connect(isolated_bus / "registry.db")
    try:
        (count,) = conn.execute("SELECT COUNT(*) FROM agents").fetchone()
    finally:
        conn.close()
    assert count == 0, "unregister trap did not remove the agent row"


def test_codex_launch_wrapper_records_codex_agent_type_in_registry(
    isolated_bus, tmp_path
):
    """During the codex session the agent row carries agent_type=general
    and the PID file matches the wrapper PID (so self-identity resolves).
    The stub codex pauses so we can inspect the registry mid-flight.
    """
    ready = tmp_path / "codex-ready"
    stub_dir = tmp_path / "stub-bin"
    stub_dir.mkdir()
    stub = stub_dir / "codex"
    # Signal ready, then wait for a sentinel file before exiting.
    stub.write_text(
        "#!/bin/sh\n"
        f"touch {ready}\n"
        f"while [ ! -f {tmp_path}/codex-done ]; do sleep 0.05; done\n"
        "exit 0\n"
    )
    stub.chmod(0o755)

    env = _hook_env(isolated_bus)
    env["PATH"] = f"{stub_dir}:{env.get('PATH', '')}"

    proc = subprocess.Popen(
        ["bash", str(CODEX_LAUNCH)],
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    try:
        # Wait for the stub to signal ready (wrapper has finished register).
        deadline = 5.0
        import time as _t
        while not ready.exists() and deadline > 0:
            _t.sleep(0.05)
            deadline -= 0.05
        assert ready.exists(), "codex stub never started"

        # Agent row exists with agent_type=general.
        conn = sqlite3.connect(isolated_bus / "registry.db")
        try:
            rows = conn.execute(
                "SELECT agent_id, pid, agent_type FROM agents"
            ).fetchall()
        finally:
            conn.close()
        assert len(rows) == 1, f"expected one agent row, got {rows}"
        agent_id, pid, agent_type = rows[0]
        assert agent_id == SMOKE_AGENT_ID
        assert agent_type == "general"
        # PID file exists at pids/<wrapper_pid> with agent_id contents.
        pid_file = isolated_bus / "pids" / str(pid)
        assert pid_file.exists(), f"PID file missing at {pid_file}"
        assert pid_file.read_text().strip() == SMOKE_AGENT_ID
    finally:
        (tmp_path / "codex-done").touch()
        proc.wait(timeout=5)

    # Clean exit → unregister trap removed the row.
    assert proc.returncode == 0
    conn = sqlite3.connect(isolated_bus / "registry.db")
    try:
        (count,) = conn.execute("SELECT COUNT(*) FROM agents").fetchone()
    finally:
        conn.close()
    assert count == 0


def test_codex_launch_wrapper_cleans_up_when_codex_fails(
    isolated_bus, tmp_path
):
    """If codex exits non-zero, the EXIT trap still runs unregister."""
    stub_dir = tmp_path / "stub-bin"
    stub_dir.mkdir()
    stub = stub_dir / "codex"
    stub.write_text("#!/bin/sh\nexit 42\n")
    stub.chmod(0o755)

    env = _hook_env(isolated_bus)
    env["PATH"] = f"{stub_dir}:{env.get('PATH', '')}"

    result = subprocess.run(
        ["bash", str(CODEX_LAUNCH)],
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    # Wrapper propagates codex's exit status via `set -e`.
    assert result.returncode == 42, result.stderr

    conn = sqlite3.connect(isolated_bus / "registry.db")
    try:
        (count,) = conn.execute("SELECT COUNT(*) FROM agents").fetchone()
    finally:
        conn.close()
    assert count == 0, "unregister trap must fire even on codex failure"

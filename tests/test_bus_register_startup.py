"""Startup behavior for the SessionStart register hook."""

from __future__ import annotations

import os
import sqlite3
import subprocess
from pathlib import Path

from tests.test_shell_hooks import REGISTER_HOOK, SMOKE_AGENT_ID, _hook_env


def _run_with_open_stdin(bus_dir: Path, payload: str = "") -> subprocess.CompletedProcess:
    proc = subprocess.Popen(
        ["bash", str(REGISTER_HOOK)],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=_hook_env(bus_dir, HELIOY_BUS_REGISTER_TIMEOUT_SECONDS="2"),
    )
    assert proc.stdin is not None
    if payload:
        proc.stdin.write(payload)
        proc.stdin.flush()

    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        proc.kill()
        stdout, stderr = proc.communicate()
        raise AssertionError(f"register hook hung\nstdout:\n{stdout}\nstderr:\n{stderr}")

    stdout, stderr = proc.communicate()
    return subprocess.CompletedProcess(proc.args, proc.returncode, stdout, stderr)


def _registered_session_id(bus_dir: Path) -> str:
    conn = sqlite3.connect(bus_dir / "registry.db")
    try:
        row = conn.execute(
            "SELECT session_id FROM agents WHERE agent_id = ?",
            (SMOKE_AGENT_ID,),
        ).fetchone()
    finally:
        conn.close()
    assert row is not None
    return row[0]


def test_register_hook_reads_newline_less_json(isolated_bus):
    result = _run_with_open_stdin(isolated_bus, '{"session_id":"test-sid"}')

    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "{}"
    assert _registered_session_id(isolated_bus) == "test-sid"


def test_register_hook_does_not_block_on_open_stdin(isolated_bus):
    result = _run_with_open_stdin(isolated_bus)

    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "{}"
    assert (isolated_bus / "pids" / str(os.getpid())).exists()

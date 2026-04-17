"""Contract tests for the SessionStart and SessionEnd shell hooks.

These tests invoke `plugin/hooks/bus-register.sh` and
`plugin/hooks/bus-unregister.sh` as real subprocesses so the DB writes,
PID-file writes, and pane eviction behavior are exercised through the
same code path production uses. The in-process `isolated_bus` fixture
and the shell hooks both resolve paths through `HELIOY_BUS_DIR`, so a
single `tmp_path/bus` backs both sides.
"""

from __future__ import annotations

import json
import os
import sqlite3
import subprocess
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
REGISTER_HOOK = REPO_ROOT / "plugin" / "hooks" / "bus-register.sh"
UNREGISTER_HOOK = REPO_ROOT / "plugin" / "hooks" / "bus-unregister.sh"
CODEX_LAUNCH = REPO_ROOT / "plugin" / "hooks" / "codex-launch.sh"
CHECK_MAIL_HOOK = REPO_ROOT / "plugin" / "hooks" / "check-mail.sh"
TOKEN_CAPTURE_HOOK = REPO_ROOT / "plugin" / "hooks" / "token-capture.sh"
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


# ── check-mail.sh: unread inbox surfacing via additionalContext ──────────────


def _run_check_mail(
    bus_dir: Path, *, stdin: str = ""
) -> subprocess.CompletedProcess:
    # check-mail.sh reads HELIOY_BUS_INBOX (not HELIOY_BUS_DIR); isolate both.
    return subprocess.run(
        ["bash", str(CHECK_MAIL_HOOK)],
        input=stdin,
        env=_hook_env(bus_dir, HELIOY_BUS_INBOX=str(bus_dir / "inbox")),
        capture_output=True,
        text=True,
        check=False,
    )


def _seed_inbox_message(
    bus_dir: Path, agent_id: str, *, sender: str, filename: str
) -> None:
    mailbox = bus_dir / "inbox" / agent_id
    mailbox.mkdir(parents=True, exist_ok=True)
    (mailbox / filename).write_text(
        json.dumps(
            {"from": sender, "to": agent_id, "content": "hi", "topic": ""}
        )
    )


def test_check_mail_surfaces_pending_messages_via_additional_context(
    isolated_bus,
):
    """Two seeded messages → hook emits PreToolUse additionalContext listing both senders."""
    assert _run_register(isolated_bus).returncode == 0
    _seed_inbox_message(
        isolated_bus, SMOKE_AGENT_ID, sender="alpha", filename="0001.json"
    )
    _seed_inbox_message(
        isolated_bus, SMOKE_AGENT_ID, sender="beta", filename="0002.json"
    )

    result = _run_check_mail(isolated_bus)
    assert result.returncode == 0, result.stderr

    payload = json.loads(result.stdout)
    hook_out = payload["hookSpecificOutput"]
    assert hook_out["hookEventName"] == "PreToolUse"
    ctx = hook_out["additionalContext"]
    assert "2 pending message(s)" in ctx
    assert f"'{SMOKE_AGENT_ID}'" in ctx
    assert "alpha" in ctx
    assert "beta" in ctx


def test_check_mail_switches_event_name_for_user_prompt_submit_input(
    isolated_bus,
):
    """stdin carrying a `prompt` field flips event to UserPromptSubmit."""
    assert _run_register(isolated_bus).returncode == 0
    _seed_inbox_message(
        isolated_bus, SMOKE_AGENT_ID, sender="alpha", filename="0001.json"
    )

    result = _run_check_mail(isolated_bus, stdin='{"prompt":"hello"}')
    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)
    assert payload["hookSpecificOutput"]["hookEventName"] == "UserPromptSubmit"


def test_check_mail_does_not_drain_inbox(isolated_bus):
    """Hook is read-only: messages stay in the inbox for get_messages to drain."""
    assert _run_register(isolated_bus).returncode == 0
    _seed_inbox_message(
        isolated_bus, SMOKE_AGENT_ID, sender="alpha", filename="0001.json"
    )
    mailbox = isolated_bus / "inbox" / SMOKE_AGENT_ID
    before = {p.name for p in mailbox.iterdir()}

    assert _run_check_mail(isolated_bus).returncode == 0

    after = {p.name for p in mailbox.iterdir()}
    assert before == after


def test_check_mail_silent_when_inbox_empty(isolated_bus):
    """Registered agent with no pending messages → hook emits nothing.

    bus-register.sh creates the mailbox directory on register, so this
    exercises the 0-matching-files branch of the `find | sort` pipeline.
    """
    assert _run_register(isolated_bus).returncode == 0
    mailbox = isolated_bus / "inbox" / SMOKE_AGENT_ID
    assert mailbox.is_dir()
    assert list(mailbox.glob("*.json")) == []

    result = _run_check_mail(isolated_bus)
    assert result.returncode == 0, result.stderr
    assert result.stdout == ""


# ── token-capture.sh: real tmux pane → registry token_usage ──────────────────


def _install_tmux_capture_stub(stub_dir: Path) -> None:
    """Write a fake `tmux` that echoes $_HELIOY_TEST_TMUX_CAPTURE for capture-pane.

    Passing the fake pane content via env (not baked into the script) means
    each test can vary capture output without rewriting the stub.
    """
    stub_dir.mkdir(exist_ok=True)
    stub = stub_dir / "tmux"
    stub.write_text(
        '#!/bin/sh\n'
        'if [ "$1" = "capture-pane" ]; then\n'
        '    printf %s "$_HELIOY_TEST_TMUX_CAPTURE"\n'
        '    exit 0\n'
        'fi\n'
        'exit 1\n'
    )
    stub.chmod(0o755)


def _run_token_capture(
    bus_dir: Path,
    tmp_path: Path,
    capture_output: str,
    *,
    tmux_pane: str = "%0",
) -> subprocess.CompletedProcess:
    stub_dir = tmp_path / "stub-bin"
    _install_tmux_capture_stub(stub_dir)
    env = _hook_env(
        bus_dir,
        TMUX_PANE=tmux_pane,
        _HELIOY_TEST_TMUX_CAPTURE=capture_output,
    )
    env["PATH"] = f"{stub_dir}:{env.get('PATH', '')}"
    return subprocess.run(
        ["bash", str(TOKEN_CAPTURE_HOOK)],
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )


def _token_usage_for(bus_dir: Path, agent_id: str) -> dict:
    conn = sqlite3.connect(bus_dir / "registry.db")
    try:
        row = conn.execute(
            "SELECT token_usage FROM agents WHERE agent_id = ?",
            (agent_id,),
        ).fetchone()
    finally:
        conn.close()
    return {} if row is None else json.loads(row[0])


def test_token_capture_writes_token_usage_to_registry(isolated_bus, tmp_path):
    """Happy path: hook extracts `<N> tokens` from stubbed pane and updates the row."""
    assert _run_register(isolated_bus).returncode == 0

    status_line = "session | claude | 4.7 opus | 12345 tokens · auto-compact\n"
    result = _run_token_capture(isolated_bus, tmp_path, status_line)
    assert result.returncode == 0, result.stderr

    usage = _token_usage_for(isolated_bus, SMOKE_AGENT_ID)
    assert usage["tokens"] == 12345
    assert usage["updated"].endswith("Z")


def test_token_capture_takes_last_match_on_status_line(isolated_bus, tmp_path):
    """The grep pipeline pins the *last* `<N> tokens` match; guard against drift."""
    assert _run_register(isolated_bus).returncode == 0
    pane = (
        "100 tokens (input)\n"
        "200 tokens (output)\n"
        "42 tokens total\n"
    )
    result = _run_token_capture(isolated_bus, tmp_path, pane)
    assert result.returncode == 0, result.stderr
    assert _token_usage_for(isolated_bus, SMOKE_AGENT_ID)["tokens"] == 42


def test_token_capture_no_ops_without_tmux_pane(isolated_bus, tmp_path):
    """Missing TMUX_PANE → early exit, registry untouched."""
    assert _run_register(isolated_bus).returncode == 0

    env = _hook_env(isolated_bus)  # _hook_env already strips TMUX_PANE
    result = subprocess.run(
        ["bash", str(TOKEN_CAPTURE_HOOK)],
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0
    assert _token_usage_for(isolated_bus, SMOKE_AGENT_ID) == {}


def test_token_capture_no_ops_without_pid_file(isolated_bus, tmp_path):
    """No PID file → hook cannot resolve agent_id; DB must remain empty."""
    result = _run_token_capture(isolated_bus, tmp_path, "99 tokens\n")
    assert result.returncode == 0
    # No register ever ran, so registry.db never got bootstrapped.
    assert not (isolated_bus / "registry.db").exists()


def test_token_capture_no_ops_when_tokens_absent_from_pane(isolated_bus, tmp_path):
    """No `<digits> tokens` match → registry row keeps its default empty usage."""
    assert _run_register(isolated_bus).returncode == 0
    result = _run_token_capture(
        isolated_bus, tmp_path, "prompt > no relevant content here\n"
    )
    assert result.returncode == 0
    assert _token_usage_for(isolated_bus, SMOKE_AGENT_ID) == {}

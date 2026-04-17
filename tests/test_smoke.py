"""End-to-end smoke test for the helioy-bus lifecycle.

This is the single operational workflow exercise: a real SessionStart
hook registers the agent, the MCP tool surface sends a message to a
peer, the real token-capture hook updates token_usage, the peer reads
and archives the message, and the SessionEnd hook cleans up.

If any boundary between the shell hooks, the Python services, and the
SQLite registry is wrong, this test fails, catching failures that the
in-process handler tests cannot see because they bypass the hooks.
"""

from __future__ import annotations

import os
import sqlite3
import subprocess
import time
from pathlib import Path

from server.runtimes.codex import CODEX_MESSAGE_SUFFIX

REPO_ROOT = Path(__file__).resolve().parent.parent
REGISTER_HOOK = REPO_ROOT / "plugin" / "hooks" / "bus-register.sh"
UNREGISTER_HOOK = REPO_ROOT / "plugin" / "hooks" / "bus-unregister.sh"
CODEX_LAUNCH = REPO_ROOT / "plugin" / "hooks" / "codex-launch.sh"
TOKEN_CAPTURE_HOOK = REPO_ROOT / "plugin" / "hooks" / "token-capture.sh"

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


def _install_tmux_stub(tmp_dir: Path) -> Path:
    """Write a tmux stub that serves identity and liveness from env."""
    stub_dir = tmp_dir / "stub-bin"
    stub_dir.mkdir()
    stub = stub_dir / "tmux"
    stub.write_text(
        "#!/bin/sh\n"
        "cmd=$1\n"
        "shift\n"
        "case \"$cmd\" in\n"
        "  display-message)\n"
        "    fmt=${!#}\n"
        "    while [ $# -gt 0 ]; do\n"
        "      case \"$1\" in\n"
        "        -p)\n"
        "          shift\n"
        "          ;;\n"
        "        -t)\n"
        "          shift 2\n"
        "          ;;\n"
        "        *)\n"
        "          shift\n"
        "          ;;\n"
        "      esac\n"
        "    done\n"
        "    if [ \"$fmt\" = '#{pane_title}' ]; then\n"
        "      printf %s \"${HELIOY_TEST_PANE_TITLE:-}\"\n"
        "      exit 0\n"
        "    fi\n"
        "    if [ \"$fmt\" = '#{session_name}:#{window_index}.#{pane_index}' ]; then\n"
        "      printf %s \"${HELIOY_TEST_TMUX_TARGET:-}\"\n"
        "      exit 0\n"
        "    fi\n"
        "    if [ \"$fmt\" = '#{session_name}' ]; then\n"
        "      printf %s \"${HELIOY_TEST_SESSION_NAME:-main}\"\n"
        "      exit 0\n"
        "    fi\n"
        "    exit 1\n"
        "    ;;\n"
        "  list-panes)\n"
        "    target=\"\"\n"
        "    while [ $# -gt 0 ]; do\n"
        "      case \"$1\" in\n"
        "        -t)\n"
        "          target=$2\n"
        "          shift 2\n"
        "          ;;\n"
        "        *)\n"
        "          shift\n"
        "          ;;\n"
        "      esac\n"
        "    done\n"
        "    case \",${HELIOY_TEST_ALIVE_TARGETS:-},\" in\n"
        "      *,$target,*) exit 0 ;;\n"
        "    esac\n"
        "    exit 1\n"
        "    ;;\n"
        "  set-hook)\n"
        "    exit 0\n"
        "    ;;\n"
        "esac\n"
        "exit 1\n"
    )
    stub.chmod(0o755)
    return stub_dir


def test_full_lifecycle_smoke(isolated_bus, set_sender, tmp_path):
    """Register via hook, send, list, capture tokens, read, unregister via hook."""
    import server.bus_server as bm

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

    # 5. Capture tokens via the real token-capture.sh hook with a tmux
    # capture-pane stub on PATH. Exercises extraction, DB write, and the
    # whoami read path end-to-end.
    stub_dir = tmp_path / "stub-bin"
    stub_dir.mkdir()
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

    capture_env = {
        **os.environ,
        "HELIOY_BUS_DIR": str(isolated_bus),
        "TMUX_PANE": "%0",
        "_HELIOY_TEST_TMUX_CAPTURE": "claude 4.7 opus | 4242 tokens · compact\n",
        "PATH": f"{stub_dir}:{os.environ.get('PATH', '')}",
    }
    capture = subprocess.run(
        ["bash", str(TOKEN_CAPTURE_HOOK)],
        env=capture_env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert capture.returncode == 0, capture.stderr

    set_sender(SMOKE_AGENT_ID)
    info = bm.whoami()
    assert info["token_usage"]["tokens"] == 4242
    assert info["token_usage"]["updated"].endswith("Z")

    # 6. Peer reads the message: unread surfaces, archive happens.
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


def test_mixed_runtime_tmux_identity_smoke(
    isolated_bus, set_sender, tmp_path, monkeypatch
):
    """Smoke the live startup path with tmux-qualified Claude and Codex peers."""
    import server.bus_server as bm
    from server._tmux import gateway

    smoke_target = "main:1.0"
    smoke_id = f"helioy-smoke-repo:general:{smoke_target}"
    codex_project_dir = "/tmp/helioy-codex-peer"
    codex_target = "main:1.1"
    codex_id = f"helioy-codex-peer:general:{codex_target}"

    stub_dir = _install_tmux_stub(tmp_path)
    stub_codex = stub_dir / "codex"
    ready = tmp_path / "codex-ready"
    done = tmp_path / "codex-done"
    stub_codex.write_text(
        "#!/bin/sh\n"
        f"touch {ready}\n"
        f"while [ ! -f {done} ]; do sleep 0.05; done\n"
        "exit 0\n"
    )
    stub_codex.chmod(0o755)

    common_env = {
        **os.environ,
        "HELIOY_BUS_DIR": str(isolated_bus),
        "HELIOY_BUS_PYTHON_PATH": str(REPO_ROOT),
        "PATH": f"{stub_dir}:{os.environ.get('PATH', '')}",
        "TMUX": "/tmp/fake-tmux-socket,1234,0",
        "HELIOY_TEST_ALIVE_TARGETS": f"{smoke_target},{codex_target}",
    }

    smoke_env = {
        **common_env,
        "CLAUDE_PROJECT_DIR": SMOKE_PROJECT_DIR,
        "PWD": SMOKE_PROJECT_DIR,
        "TMUX_PANE": "%10",
        "HELIOY_TEST_TMUX_TARGET": smoke_target,
        "HELIOY_TEST_PANE_TITLE": smoke_id,
        "HELIOY_TEST_SESSION_NAME": "main",
    }
    reg = subprocess.run(
        ["bash", str(REGISTER_HOOK)],
        input='{"session_id":"smoke-session"}',
        env=smoke_env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert reg.returncode == 0, reg.stderr
    assert (isolated_bus / "pids" / str(os.getpid())).read_text().strip() == smoke_id

    codex_env = {
        **common_env,
        "CLAUDE_PROJECT_DIR": codex_project_dir,
        "PWD": codex_project_dir,
        "TMUX_PANE": "%11",
        "HELIOY_TEST_TMUX_TARGET": codex_target,
        "HELIOY_TEST_PANE_TITLE": codex_id,
        "HELIOY_TEST_SESSION_NAME": "main",
    }
    proc = subprocess.Popen(
        ["bash", str(CODEX_LAUNCH)],
        env=codex_env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        monkeypatch.setenv("PATH", common_env["PATH"])
        monkeypatch.setenv("HELIOY_TEST_ALIVE_TARGETS", common_env["HELIOY_TEST_ALIVE_TARGETS"])

        deadline = time.time() + 5.0
        while not ready.exists() and time.time() < deadline:
            time.sleep(0.05)
        assert ready.exists(), "codex startup never reached the live session"

        deadline = time.time() + 5.0
        found = False
        while time.time() < deadline:
            active = {a["agent_id"]: a for a in bm.list_agents()}
            if smoke_id in active and codex_id in active:
                found = True
                break
            time.sleep(0.05)
        assert found, f"expected tmux-qualified identities, got {active}"
        assert active[smoke_id]["runtime"] == "claude"
        assert active[codex_id]["runtime"] == "codex"
        assert active[smoke_id]["tmux_target"] == smoke_target
        assert active[codex_id]["tmux_target"] == codex_target

        set_sender(smoke_id)
        result = bm.send_message(
            to=codex_id,
            content="hello codex",
            topic="mixed-smoke",
            nudge=False,
        )
        assert result["delivered"] is True
        assert result["recipients"] == [codex_id]

        msgs = bm.get_messages(agent_id=codex_id)
        assert len(msgs) == 1
        assert msgs[0]["from"] == smoke_id
        assert msgs[0]["topic"] == "mixed-smoke"
        # Codex recipients receive the runtime authorization preamble on
        # every payload; claude recipients (checked below) do not.
        assert msgs[0]["content"] == "hello codex" + CODEX_MESSAGE_SUFFIX

        set_sender(codex_id)
        reply = bm.send_message(
            to=smoke_id,
            content="hello claude",
            topic="mixed-smoke",
            nudge=False,
        )
        assert reply["delivered"] is True
        assert reply["recipients"] == [smoke_id]

        smoke_msgs = bm.get_messages(agent_id=smoke_id)
        assert len(smoke_msgs) == 1
        assert smoke_msgs[0]["from"] == codex_id
        assert smoke_msgs[0]["content"] == "hello claude"
    finally:
        done.touch()
        proc.wait(timeout=5)

    env = {**smoke_env, "HELIOY_BUS_DIR": str(isolated_bus)}
    unreg = subprocess.run(
        ["bash", str(UNREGISTER_HOOK)],
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert unreg.returncode == 0, unreg.stderr

    remaining = {a["agent_id"] for a in bm.list_agents()}
    assert smoke_id not in remaining
    assert codex_id not in remaining

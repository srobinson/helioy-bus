"""Tests for resolve_tmux_session(): the warroom spawn tmux preflight.

The preflight answers one question: which tmux session does the *calling
agent* live in. The MCP server process is not a reliable witness. Codex
spawns stdio servers with an allowlist environment that drops TMUX and
TMUX_PANE, and `tmux display-message` without a target names whichever
session happens to be attached. Both are covered here.

Tests run against a temporary BUS_DIR via the shared isolated_bus fixture
in conftest.py.
"""

from __future__ import annotations

from server._tmux import gateway
from server._warroom_persist import resolve_tmux_session


def _register(monkeypatch, agent_id: str, tmux_target: str) -> None:
    """Register ``agent_id`` at ``tmux_target`` and make it the caller."""
    import server._db as _db_mod
    import server.bus_server as bm

    bm.register_agent(pwd="/tmp/myproj", agent_id=agent_id, tmux_target=tmux_target)
    pids_dir = _db_mod.BUS_DIR / "pids"
    pids_dir.mkdir(parents=True, exist_ok=True)
    (pids_dir / "99999").write_text(agent_id)
    monkeypatch.setenv("HELIOY_BUS_CLAUDE_PID", "99999")


def test_resolves_from_registry_when_env_is_stripped(isolated_bus, monkeypatch):
    """Codex strips TMUX from MCP stdio children; the caller is still in tmux.

    Regression: an env-only check reported "Not inside a tmux session" for
    a caller whose pane was live, because os.environ described the server.
    """
    _register(monkeypatch, "myproj:general:6:1.1", "6:1.1")
    monkeypatch.delenv("TMUX", raising=False)
    monkeypatch.delenv("TMUX_PANE", raising=False)

    session, err = resolve_tmux_session()

    assert err is None
    assert session == "6"


def test_registry_wins_over_attached_session(isolated_bus, monkeypatch):
    """The caller's session, not whichever session tmux has attached.

    Regression: `display-message -p '#{session_name}'` carries no target,
    so a caller in session `6` spawned its warroom into `other` whenever a
    different client was attached.
    """
    _register(monkeypatch, "myproj:general:6:1.1", "6:1.1")
    monkeypatch.setenv("TMUX", "/tmp/tmux-1000/default,12345,0")
    monkeypatch.setattr(gateway, "_run", lambda *args, **kw: "other")

    session, err = resolve_tmux_session()

    assert err is None
    assert session == "6"


def test_env_fallback_targets_the_callers_pane(isolated_bus, monkeypatch):
    """Unregistered caller: the env path still scopes the lookup to its pane."""
    monkeypatch.setenv("TMUX", "/tmp/tmux-1000/default,12345,0")
    monkeypatch.setenv("TMUX_PANE", "%5")
    seen: list[tuple[str, ...]] = []

    def fake_run(*args, **kw):
        seen.append(args)
        return "main"

    monkeypatch.setattr(gateway, "_run", fake_run)

    session, err = resolve_tmux_session()

    assert err is None
    assert session == "main"
    assert ("display-message", "-p", "-t", "%5", "#{session_name}") in seen


def test_errors_when_neither_registry_nor_env_knows(isolated_bus, monkeypatch):
    """No registration and no tmux env: the preflight still refuses."""
    monkeypatch.delenv("TMUX", raising=False)
    monkeypatch.delenv("TMUX_PANE", raising=False)
    monkeypatch.delenv("HELIOY_BUS_CLAUDE_PID", raising=False)

    session, err = resolve_tmux_session()

    assert session is None
    assert err is not None
    assert "tmux" in err["error"].lower()

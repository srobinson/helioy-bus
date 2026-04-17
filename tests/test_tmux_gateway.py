"""Adapter tests for TmuxGateway, the single tmux boundary.

These tests stub `subprocess.run` to exercise failure, timeout, copy-mode,
and best-effort paths without shelling out to a real tmux binary.
"""

from __future__ import annotations

import subprocess
from unittest.mock import patch

import pytest

from server._tmux import TmuxGateway


# ── _run: strict, raises on failure ──────────────────────────────────────────


class _FakeResult:
    def __init__(self, returncode: int, stdout: bytes = b"", stderr: bytes = b""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


def test_run_returns_stripped_stdout_on_success():
    gw = TmuxGateway()
    with patch("server._tmux.subprocess.run", return_value=_FakeResult(0, b"main\n")):
        assert gw._run("display-message", "-p", "#{session_name}") == "main"


def test_run_raises_on_nonzero_exit():
    gw = TmuxGateway()
    fake = _FakeResult(1, stderr=b"no server running\n")
    with patch("server._tmux.subprocess.run", return_value=fake):
        with pytest.raises(RuntimeError, match="tmux list-panes failed: no server running"):
            gw._run("list-panes", "-t", "main")


def test_run_raises_when_tmux_not_installed():
    gw = TmuxGateway()
    with patch("server._tmux.subprocess.run", side_effect=FileNotFoundError):
        with pytest.raises(RuntimeError, match="tmux is not installed"):
            gw._run("has-session")


def test_run_raises_on_timeout():
    gw = TmuxGateway()
    exc = subprocess.TimeoutExpired(cmd=["tmux", "list-panes"], timeout=3)
    with patch("server._tmux.subprocess.run", side_effect=exc):
        with pytest.raises(RuntimeError, match="tmux list-panes timed out"):
            gw._run("list-panes", "-t", "main")


def test_run_passes_custom_timeout_through_to_subprocess():
    gw = TmuxGateway(default_timeout=5.0)
    with patch("server._tmux.subprocess.run", return_value=_FakeResult(0)) as mock_run:
        gw._run("display-message", "-p", "x", timeout=1.5)
    assert mock_run.call_args.kwargs["timeout"] == 1.5


def test_run_uses_default_timeout_when_unspecified():
    gw = TmuxGateway(default_timeout=7.0)
    with patch("server._tmux.subprocess.run", return_value=_FakeResult(0)) as mock_run:
        gw._run("display-message", "-p", "x")
    assert mock_run.call_args.kwargs["timeout"] == 7.0


# ── _run_silent: best-effort, swallows failure ───────────────────────────────


def test_run_silent_returns_true_on_success():
    gw = TmuxGateway()
    with patch("server._tmux.subprocess.run", return_value=_FakeResult(0)):
        assert gw._run_silent("list-panes", "-t", "main") is True


def test_run_silent_returns_false_on_failure():
    gw = TmuxGateway()
    with patch("server._tmux.subprocess.run", return_value=_FakeResult(1, stderr=b"boom")):
        assert gw._run_silent("list-panes", "-t", "gone") is False


def test_run_silent_swallows_timeout():
    gw = TmuxGateway()
    exc = subprocess.TimeoutExpired(cmd=["tmux"], timeout=1)
    with patch("server._tmux.subprocess.run", side_effect=exc):
        assert gw._run_silent("list-panes", "-t", "x") is False


# ── pane lifecycle ───────────────────────────────────────────────────────────


def test_pane_alive_true_when_list_panes_succeeds():
    gw = TmuxGateway()
    with patch("server._tmux.subprocess.run", return_value=_FakeResult(0)):
        assert gw.pane_alive("main:1.0") is True


def test_pane_alive_false_when_list_panes_fails():
    gw = TmuxGateway()
    with patch("server._tmux.subprocess.run", return_value=_FakeResult(1)):
        assert gw.pane_alive("dead:9.9") is False


def test_kill_window_uses_exact_match_prefix():
    gw = TmuxGateway()
    with patch("server._tmux.subprocess.run", return_value=_FakeResult(0)) as mock_run:
        gw.kill_window("main", "eng")
    args = mock_run.call_args.args[0]
    assert args == ["tmux", "kill-window", "-t", "main:=eng"]


def test_kill_pane_is_best_effort():
    gw = TmuxGateway()
    with patch("server._tmux.subprocess.run", return_value=_FakeResult(1, stderr=b"no such pane")):
        assert gw.kill_pane("%99") is False


def test_select_layout_best_effort_returns_false_on_failure():
    gw = TmuxGateway()
    with patch("server._tmux.subprocess.run", return_value=_FakeResult(1)):
        assert gw.select_layout("main", "eng", "tiled") is False


# ── environment ──────────────────────────────────────────────────────────────


def test_inside_tmux_reflects_env(monkeypatch):
    gw = TmuxGateway()
    monkeypatch.setenv("TMUX", "/tmp/tmux-1000/default,12345,0")
    assert gw.inside_tmux() is True
    monkeypatch.delenv("TMUX", raising=False)
    assert gw.inside_tmux() is False


def test_current_session_name_returns_none_when_no_tmux():
    gw = TmuxGateway()
    with patch("server._tmux.subprocess.run", side_effect=FileNotFoundError):
        assert gw.current_session_name() is None


def test_current_session_name_returns_name_on_success():
    gw = TmuxGateway()
    with patch("server._tmux.subprocess.run", return_value=_FakeResult(0, b"work\n")):
        assert gw.current_session_name() == "work"


# ── nudge: copy-mode handling ────────────────────────────────────────────────


def _scripted_run(responses):
    """Build a subprocess.run side_effect that returns successive FakeResults.

    `responses` is a list of (stdout, returncode) tuples or exceptions.
    """
    it = iter(responses)

    def side_effect(*args, **kw):
        nxt = next(it)
        if isinstance(nxt, Exception):
            raise nxt
        stdout, rc = nxt
        return _FakeResult(rc, stdout=stdout)

    return side_effect


def test_nudge_sends_cancel_before_text_when_pane_in_copy_mode():
    """When pane_in_mode == '1' (copy-mode), nudge sends -X cancel first."""
    gw = TmuxGateway()
    responses = [
        (b"1\n", 0),   # display-message pane_in_mode -> "1"
        (b"", 0),      # send-keys -X cancel
        (b"", 0),      # send-keys -l "you have mail!"
        (b"", 0),      # send-keys Enter
    ]
    with patch(
        "server._tmux.subprocess.run", side_effect=_scripted_run(responses)
    ) as mock_run:
        assert gw.nudge("main:1.0") is True

    calls = [call.args[0] for call in mock_run.call_args_list]
    assert calls[0][1] == "display-message"
    assert calls[1][1:] == ["send-keys", "-t", "main:1.0", "-X", "cancel"]
    assert calls[2][1:] == ["send-keys", "-t", "main:1.0", "-l", "you have mail!"]
    assert calls[3][1:] == ["send-keys", "-t", "main:1.0", "Enter"]


def test_nudge_suppresses_codex_runtime_without_tmux_text():
    gw = TmuxGateway()
    with patch("server._tmux.subprocess.run") as mock_run:
        assert gw.nudge("main:1.0", runtime="codex") is False
    mock_run.assert_not_called()


def test_nudge_skips_cancel_when_pane_not_in_copy_mode():
    """When pane_in_mode != '1', nudge goes straight to text + Enter."""
    gw = TmuxGateway()
    responses = [
        (b"0\n", 0),   # not in copy-mode
        (b"", 0),      # send-keys -l
        (b"", 0),      # send-keys Enter
    ]
    with patch(
        "server._tmux.subprocess.run", side_effect=_scripted_run(responses)
    ) as mock_run:
        assert gw.nudge("main:1.0") is True

    commands = [call.args[0][1] for call in mock_run.call_args_list]
    assert commands == ["display-message", "send-keys", "send-keys"]


def test_nudge_returns_false_when_mode_check_fails():
    gw = TmuxGateway()
    exc = subprocess.TimeoutExpired(cmd=["tmux"], timeout=3)
    with patch("server._tmux.subprocess.run", side_effect=exc):
        assert gw.nudge("main:1.0") is False


def test_nudge_returns_false_when_text_send_fails():
    gw = TmuxGateway()
    responses = [
        (b"0\n", 0),   # mode check ok
        (b"", 1),      # text send fails
    ]
    with patch("server._tmux.subprocess.run", side_effect=_scripted_run(responses)):
        assert gw.nudge("main:1.0") is False


def test_nudge_returns_false_when_enter_fails():
    gw = TmuxGateway()
    responses = [
        (b"0\n", 0),   # mode check ok
        (b"", 0),      # text send ok
        (b"", 1),      # Enter fails
    ]
    with patch("server._tmux.subprocess.run", side_effect=_scripted_run(responses)):
        assert gw.nudge("main:1.0") is False

"""Hot-reload proxy restart robustness.

The proxy sits between Claude Code and the inner MCP server and restarts
the inner server when a watched ``server/**/*.py`` file changes. A restart
that fails to complete must never wedge the proxy: while ``_restarting`` is
True the proxy buffers every client message into ``pending`` and forwards
nothing, so a stuck flag or an unbounded read turns every subsequent
``tools/call`` into a permanent hang.
"""

from __future__ import annotations

import asyncio
import types
from pathlib import Path

import pytest
import watchfiles

import server.proxy as proxy_mod
from server.proxy import HotReloadProxy

INIT_LINE = b'{"jsonrpc":"2.0","id":1,"method":"initialize","params":{}}\n'


def _fake_proc(*, stdin=None, stdout=None):
    proc = types.SimpleNamespace(returncode=None, stdin=stdin, stdout=stdout)
    proc.kill = lambda: None

    async def _wait():
        return 0

    proc.wait = _wait
    return proc


class _Writer:
    def write(self, data):  # noqa: D401 - trivial sink
        pass

    async def drain(self):
        pass


class _HangingReader:
    """readline() never returns, simulating an inner that never replies."""

    async def readline(self):
        await asyncio.Event().wait()
        return b""


async def test_restart_does_not_wedge_when_inner_init_never_replies(monkeypatch):
    proxy = HotReloadProxy(Path("server/warroom_server.py"))
    proxy.init_line = INIT_LINE

    async def fake_spawn():
        proxy.proc = _fake_proc(stdin=_Writer(), stdout=_HangingReader())

    monkeypatch.setattr(proxy, "_spawn", fake_spawn)
    monkeypatch.setattr(proxy_mod, "INIT_REPLAY_TIMEOUT", 0.1)

    # Must finish (bounded replay) and clear the flag so forwarding resumes.
    await asyncio.wait_for(proxy._restart(), timeout=3)
    assert proxy._restarting is False


async def test_restart_resets_flag_on_unexpected_error(monkeypatch):
    proxy = HotReloadProxy(Path("server/warroom_server.py"))

    async def fake_spawn():
        proxy.proc = _fake_proc()

    async def boom():
        raise RuntimeError("inner blew up during init")

    monkeypatch.setattr(proxy, "_spawn", fake_spawn)
    monkeypatch.setattr(proxy, "_replay_init", boom)

    with pytest.raises(RuntimeError):
        await proxy._restart()
    # The finally must guarantee the flag is cleared even when restart raises.
    assert proxy._restarting is False


async def test_watch_survives_restart_error_and_ignores_non_source(monkeypatch):
    proxy = HotReloadProxy(Path("server/warroom_server.py"))
    restarts: list[int] = []

    async def boom_restart():
        restarts.append(1)
        raise RuntimeError("restart failed")

    monkeypatch.setattr(proxy, "_restart", boom_restart)

    async def fake_awatch(_path):
        # pycache churn must not trigger a restart...
        yield {("modified", "/repo/server/__pycache__/x.cpython-312.pyc")}
        # ...a real source edit must, and a raising restart must not kill watch.
        yield {("modified", "/repo/server/warroom_server.py")}

    monkeypatch.setattr(watchfiles, "awatch", fake_awatch)

    await asyncio.wait_for(proxy._watch(), timeout=3)
    assert restarts == [1]

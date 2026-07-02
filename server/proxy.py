#!/usr/bin/env python3
"""helioy-bus hot-reload stdio proxy.

Sits between Claude Code and bus_server.py. Watches server/ for .py changes
and transparently restarts the inner server without breaking the outer stdio
connection. Claude Code never sees a disconnect.

  Claude Code ──stdin──▶ [proxy] ──stdin──▶ bus_server.py
             ◀──stdout── [proxy] ◀──stdout──

On file change:
  1. Set restarting flag, buffer all incoming messages
  2. Kill inner server
  3. Spawn fresh inner server
  4. Replay captured initialize request, discard inner response
  5. Send notifications/initialized to complete inner handshake
  6. Drain buffered messages
  7. Resume normal forwarding
"""

from __future__ import annotations

import asyncio
import json
import sys
from collections.abc import Mapping
from pathlib import Path

WATCH_DIR = Path(__file__).parent
PYTHON = sys.executable

# Bound the wait for the inner server's initialize response during a restart.
# While restarting, the proxy buffers all client traffic and forwards nothing,
# so a slow or dead inner must never block this read indefinitely.
INIT_REPLAY_TIMEOUT = 10.0

# Max bytes in a single JSON-RPC line the proxy will buffer. asyncio's stream
# default is 64KB, but MCP responses routinely exceed that (e.g. warroom_status
# with no filter is ~77KB). readline() raises on a line past its limit, which
# the forward loop would swallow and spin on, hanging the call. Size generously.
STREAM_LIMIT = 16 * 1024 * 1024


def _log(msg: str) -> None:
    print(f"[helioy-bus proxy] {msg}", file=sys.stderr, flush=True)


def build_inner_env(environ: Mapping[str, str], parent_pid: int) -> dict[str, str]:
    """Build the env for the inner server, setting the active runtime's
    self_pid_env only when no upstream wrapper has done so.

    When a wrapper (e.g. ``runtime-launch.sh``) has already exported its
    runtime's PID env, preserve it. When nothing upstream has, fall back
    to the default adapter and seed its env var with the parent PID,
    the historical Claude-only path.
    """
    from server.runtimes import default_adapter, registered_adapters

    active = next(
        (a for a in registered_adapters() if a.self_pid_env in environ),
        default_adapter(),
    )
    env = dict(environ)
    env.setdefault(active.self_pid_env, str(parent_pid))
    return env


class HotReloadProxy:
    def __init__(self, server_script: Path) -> None:
        self.server_script = server_script
        self.proc: asyncio.subprocess.Process | None = None
        self.init_line: bytes | None = None  # raw bytes of the initialize request
        self.pending: list[bytes] = []  # messages buffered during restart
        self._restarting = False

    # ── Inner process lifecycle ────────────────────────────────────────────────

    async def _spawn(self) -> None:
        import os

        env = build_inner_env(os.environ, os.getppid())
        self.proc = await asyncio.create_subprocess_exec(
            PYTHON,
            str(self.server_script),
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=sys.stderr,
            env=env,
            limit=STREAM_LIMIT,
        )

    async def _replay_init(self) -> None:
        if (
            not self.init_line
            or not self.proc
            or self.proc.stdin is None
            or self.proc.stdout is None
        ):
            return
        proc = self.proc
        assert proc.stdin is not None and proc.stdout is not None
        writer = proc.stdin
        reader = proc.stdout
        try:
            # Send initialize to new inner server
            writer.write(self.init_line)
            await writer.drain()
            # Discard inner server's initialize response; outer client already
            # got one. Bounded so a dead or slow inner cannot wedge the proxy.
            await asyncio.wait_for(reader.readline(), timeout=INIT_REPLAY_TIMEOUT)
            # Complete the inner handshake
            notif = json.dumps({"jsonrpc": "2.0", "method": "notifications/initialized"}) + "\n"
            writer.write(notif.encode())
            await writer.drain()
        except (TimeoutError, OSError) as exc:
            _log(f"inner init replay failed ({exc!r}); resuming forwarding")

    async def _restart(self) -> None:
        self._restarting = True
        _log("file changed, restarting inner server")
        try:
            if self.proc and self.proc.returncode is None:
                self.proc.kill()
                await self.proc.wait()
            await self._spawn()
            await self._replay_init()
            assert self.proc is not None and self.proc.stdin is not None
            stdin = self.proc.stdin
            for msg in self.pending:
                stdin.write(msg)
            if self.pending:
                await stdin.drain()
            self.pending.clear()
            _log("inner server ready")
        finally:
            # Always clear the flag: a restart that bails out mid-way must not
            # leave the proxy buffering client traffic forever.
            self._restarting = False

    # ── Forward loops ──────────────────────────────────────────────────────────

    async def _stdin_to_inner(self, stdin: asyncio.StreamReader) -> None:
        while True:
            line = await stdin.readline()
            if not line:
                break
            # Capture initialize for replay after restarts
            try:
                if json.loads(line).get("method") == "initialize" and self.init_line is None:
                    self.init_line = line
            except (json.JSONDecodeError, AttributeError):
                pass
            if self._restarting or not self.proc or self.proc.stdin is None:
                self.pending.append(line)
            else:
                writer = self.proc.stdin
                writer.write(line)
                try:
                    await writer.drain()
                except BrokenPipeError:
                    self.pending.append(line)

    async def _inner_to_stdout(self) -> None:
        out = sys.stdout.buffer
        while True:
            if self._restarting or not self.proc or self.proc.stdout is None:
                await asyncio.sleep(0.005)
                continue
            stdout = self.proc.stdout
            try:
                line = await stdout.readline()
            except Exception:
                await asyncio.sleep(0.005)
                continue
            if line:
                out.write(line)
                out.flush()

    # ── File watcher ───────────────────────────────────────────────────────────

    async def _watch(self) -> None:
        from watchfiles import awatch

        async for changes in awatch(str(WATCH_DIR)):
            if not any(p.endswith(".py") and "__pycache__" not in p for _, p in changes):
                continue
            try:
                await self._restart()
            except Exception as exc:  # noqa: BLE001 - one bad restart must not kill the proxy
                _log(f"restart failed ({exc!r}); proxy still serving inner server")

    # ── Entry point ────────────────────────────────────────────────────────────

    async def run(self) -> None:
        loop = asyncio.get_event_loop()
        stdin_reader = asyncio.StreamReader(limit=STREAM_LIMIT)
        await loop.connect_read_pipe(
            lambda: asyncio.StreamReaderProtocol(stdin_reader),
            sys.stdin.buffer,
        )
        await self._spawn()
        await asyncio.gather(
            self._stdin_to_inner(stdin_reader),
            self._inner_to_stdout(),
            self._watch(),
        )


if __name__ == "__main__":
    target = sys.argv[1] if len(sys.argv) > 1 else "bus_server.py"
    asyncio.run(HotReloadProxy(WATCH_DIR / target).run())

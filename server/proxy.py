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


def _log(msg: str) -> None:
    print(f"[helioy-bus proxy] {msg}", file=sys.stderr, flush=True)


def build_inner_env(environ: Mapping[str, str], parent_pid: int) -> dict[str, str]:
    """Build the env for the inner server, setting the active runtime's
    self_pid_env only when no upstream wrapper has done so.

    When a wrapper (e.g. ``codex-launch.sh``) has already exported its
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
        # Send initialize to new inner server
        writer.write(self.init_line)
        await writer.drain()
        # Discard inner server's initialize response; outer client already got one
        await reader.readline()
        # Complete the inner handshake
        notif = json.dumps({"jsonrpc": "2.0", "method": "notifications/initialized"}) + "\n"
        writer.write(notif.encode())
        await writer.drain()

    async def _restart(self) -> None:
        self._restarting = True
        _log("file changed, restarting inner server")
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
        self._restarting = False
        _log("inner server ready")

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
            if any(p.endswith(".py") for _, p in changes):
                await self._restart()

    # ── Entry point ────────────────────────────────────────────────────────────

    async def run(self) -> None:
        loop = asyncio.get_event_loop()
        stdin_reader = asyncio.StreamReader()
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

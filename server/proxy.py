#!/usr/bin/env python3
"""helioy-bus hot-reload stdio proxy.

Sits between Claude Code and bus_server.py. Watches server/ for .py changes
and transparently restarts the inner server without breaking the outer stdio
connection. Claude Code never sees a disconnect.

  Claude Code ──stdin──▶ [proxy] ──stdin──▶ bus_server.py
             ◀──stdout── [proxy] ◀──stdout──

On file change:
  1. Set restarting flag — buffer all incoming messages
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
from pathlib import Path

WATCH_DIR = Path(__file__).parent
PYTHON = sys.executable


def _log(msg: str) -> None:
    print(f"[helioy-bus proxy] {msg}", file=sys.stderr, flush=True)


class HotReloadProxy:
    def __init__(self) -> None:
        self.proc: asyncio.subprocess.Process | None = None
        self.init_line: bytes | None = None  # raw bytes of the initialize request
        self.pending: list[bytes] = []       # messages buffered during restart
        self._restarting = False

    # ── Inner process lifecycle ────────────────────────────────────────────────

    async def _spawn(self) -> None:
        import os
        env = {**os.environ, "HELIOY_BUS_CLAUDE_PID": str(os.getppid())}
        self.proc = await asyncio.create_subprocess_exec(
            PYTHON, str(SERVER_SCRIPT),
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=sys.stderr,
            env=env,
        )

    async def _replay_init(self) -> None:
        if not self.init_line or not self.proc:
            return
        # Send initialize to new inner server
        self.proc.stdin.write(self.init_line)
        await self.proc.stdin.drain()
        # Discard inner server's initialize response — outer client already got one
        await self.proc.stdout.readline()
        # Complete the inner handshake
        notif = json.dumps({"jsonrpc": "2.0", "method": "notifications/initialized"}) + "\n"
        self.proc.stdin.write(notif.encode())
        await self.proc.stdin.drain()

    async def _restart(self) -> None:
        self._restarting = True
        _log("file changed — restarting inner server")
        if self.proc and self.proc.returncode is None:
            self.proc.kill()
            await self.proc.wait()
        await self._spawn()
        await self._replay_init()
        for msg in self.pending:
            self.proc.stdin.write(msg)
        if self.pending:
            await self.proc.stdin.drain()
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
            if self._restarting or not self.proc:
                self.pending.append(line)
            else:
                self.proc.stdin.write(line)
                try:
                    await self.proc.stdin.drain()
                except BrokenPipeError:
                    self.pending.append(line)

    async def _inner_to_stdout(self) -> None:
        out = sys.stdout.buffer
        while True:
            if self._restarting or not self.proc:
                await asyncio.sleep(0.005)
                continue
            try:
                line = await self.proc.stdout.readline()
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
    SERVER_SCRIPT = WATCH_DIR / target
    asyncio.run(HotReloadProxy().run())

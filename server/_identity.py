"""Agent identity resolution for helioy-bus."""

from __future__ import annotations

import os

from server import _db


def _self_agent_id() -> str:
    """Resolve agent_id for the calling process via the PID file written at SessionStart.

    Tries HELIOY_BUS_CLAUDE_PID (set by proxy.py) first, then os.getppid(),
    then falls back to basename(cwd).
    """
    pids_dir = _db.BUS_DIR / "pids"
    for pid in filter(None, [os.environ.get("HELIOY_BUS_CLAUDE_PID"), str(os.getppid())]):
        pid_file = pids_dir / pid
        if pid_file.exists():
            resolved = pid_file.read_text().strip()
            _db._dbg(f"_self_agent_id: pid={pid} pid_file={pid_file} \u2192 {resolved!r}")
            return resolved
    resolved = os.path.basename(os.getcwd()) or "unknown"
    _db._dbg(
        f"_self_agent_id: no pid file found "
        f"(tried HELIOY_BUS_CLAUDE_PID={os.environ.get('HELIOY_BUS_CLAUDE_PID')!r} "
        f"ppid={os.getppid()}) \u2192 {resolved!r}"
    )
    return resolved

"""Agent identity resolution for helioy-bus."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

from server import _db

# Path to the authoritative shell resolver.
# Works in development / editable-install layouts where server/ and plugin/
# are siblings under the same repo root. Not available in wheel installs
# (plugin/ is excluded from the wheel), in which case we fall back to
# basename(cwd).
_RESOLVE_IDENTITY_SH = (
    Path(__file__).parent.parent / "plugin" / "hooks" / "lib" / "resolve-identity.sh"
)


def _self_agent_id() -> str:
    """Resolve agent_id for the calling process.

    Fast path: reads the PID file written by bus-register.sh at SessionStart.
    This is the common case and costs a single stat + read.

    Slow path: shells out to resolve-identity.sh (the authoritative resolver)
    to produce a consistent identity when the PID file is absent. Only fires
    in edge cases (e.g. MCP server started before the SessionStart hook ran).

    Last resort: basename(cwd) — maintains availability at the cost of identity
    divergence.
    """
    pids_dir = _db.BUS_DIR / "pids"
    for pid in filter(None, [os.environ.get("HELIOY_BUS_CLAUDE_PID"), str(os.getppid())]):
        pid_file = pids_dir / pid
        if pid_file.exists():
            resolved = pid_file.read_text().strip()
            _db._dbg(f"_self_agent_id: pid={pid} \u2192 {resolved!r}")
            return resolved

    # Slow path: delegate to the authoritative shell resolver for consistency
    if _RESOLVE_IDENTITY_SH.exists():
        try:
            result = subprocess.run(
                [
                    "bash", "-c",
                    f"source {_RESOLVE_IDENTITY_SH} && resolve_agent_id"
                    " && printf '%s' \"$HELIOY_AGENT_ID\"",
                ],
                capture_output=True,
                timeout=3,
            )
            if result.returncode == 0:
                resolved = result.stdout.decode().strip()
                if resolved:
                    _db._dbg(f"_self_agent_id: shell resolver \u2192 {resolved!r}")
                    return resolved
        except (subprocess.SubprocessError, OSError):
            pass

    # Last resort: basename(cwd)
    resolved = os.path.basename(os.getcwd()) or "unknown"
    _db._dbg(
        f"_self_agent_id: no pid file, shell resolver unavailable "
        f"(HELIOY_BUS_CLAUDE_PID={os.environ.get('HELIOY_BUS_CLAUDE_PID')!r} "
        f"ppid={os.getppid()}) \u2192 {resolved!r}"
    )
    return resolved

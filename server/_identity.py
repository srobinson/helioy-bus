"""Agent identity resolution for helioy-bus.

Canonical identity contract
---------------------------
Every path that derives an agent's primary identity MUST produce the same
string for the same ``(cwd, agent_type, tmux_target)`` tuple. The canonical
shape is:

    With tmux:    ``{repo}:{agent_type}:{tmux_target}``   e.g. ``fmm:general:7:2.1``
    Without tmux: ``{repo}:{agent_type}``                 e.g. ``fmm:general``

where

* ``repo``        = ``basename(cwd)`` or ``"unknown"`` when empty
* ``agent_type``  = caller-supplied role (default ``"general"``)
* ``tmux_target`` = ``"{session}:{window}.{pane}"`` when in tmux, else empty

This shape matches the pane title emitted by warroom/crew spawn and
accepted by ``resolve-identity.sh``, so pane-title, hook fallback,
MCP ``register_agent()``, and ``_self_agent_id()`` converge on one id.

Legacy shapes that are rejected by this module:

* bare ``basename(cwd)``
* bare ``agent_type``
* ``{basename}:{tmux_target}`` without ``agent_type``

Those are the historical divergent paths and the root cause of
split-identity bugs. ``canonical_agent_id()`` is the single source of
truth. Call it from every Python path that auto-derives identity.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

from server import _db
from server.runtimes import default_adapter

# Path to the authoritative shell resolver.
# Works in development / editable-install layouts where server/ and plugin/
# are siblings under the same repo root. Not available in wheel installs
# (plugin/ is excluded from the wheel), in which case we fall back to
# canonical_agent_id() for a consistent shape.
_RESOLVE_IDENTITY_SH = (
    Path(__file__).parent.parent / "plugin" / "hooks" / "lib" / "resolve-identity.sh"
)


def canonical_agent_id(
    pwd: str,
    agent_type: str = "general",
    tmux_target: str = "",
) -> str:
    """Return the canonical primary identity for an agent.

    Single source of truth for Python-side identity derivation. Used by
    ``register_agent()`` auto-derivation and ``_self_agent_id()`` fallback
    so every path agrees on the same id for the same live process.
    """
    repo = os.path.basename(pwd.rstrip("/")) or "unknown"
    agent_type = agent_type or "general"
    if tmux_target:
        return f"{repo}:{agent_type}:{tmux_target}"
    return f"{repo}:{agent_type}"


def _self_agent_id() -> str:
    """Resolve agent_id for the calling process.

    Fast path: reads the PID file written by bus-register.sh at SessionStart.
    This is the common case and costs a single stat + read.

    Slow path: shells out to resolve-identity.sh (the authoritative resolver)
    to produce a consistent identity when the PID file is absent. Only fires
    in edge cases (e.g. MCP server started before the SessionStart hook ran).

    Last resort: ``canonical_agent_id()`` — produces the same shape every
    other path produces, so availability never comes at the cost of identity
    divergence.
    """
    pids_dir = _db.BUS_DIR / "pids"
    self_pid_env = default_adapter().self_pid_env
    for pid in filter(None, [os.environ.get(self_pid_env), str(os.getppid())]):
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

    # Last resort: canonical form from env + cwd. Never bare basename.
    agent_type = (
        os.environ.get("HELIOY_AGENT_TYPE")
        or os.environ.get("HELIOY_BUS_AGENT_TYPE")
        or "general"
    )
    tmux_target = os.environ.get("HELIOY_BUS_TMUX", "")
    resolved = canonical_agent_id(os.getcwd(), agent_type, tmux_target)
    _db._dbg(
        f"_self_agent_id: no pid file, shell resolver unavailable "
        f"({self_pid_env}={os.environ.get(self_pid_env)!r} "
        f"ppid={os.getppid()}) \u2192 {resolved!r}"
    )
    return resolved

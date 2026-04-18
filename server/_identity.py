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
from server.runtimes import registered_adapters

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

    Registry path: when the PID file is absent, consult the agents table
    keyed on the caller's tmux pane. Registration writes the authoritative
    row for this pane; this lookup reuses it as the source of truth. Covers
    runtimes whose host process PID is not knowable at hook-run time (e.g.
    Codex, whose launch wrapper registers before the codex binary exists).

    Slow path: shells out to resolve-identity.sh (the authoritative resolver)
    to produce a consistent identity when neither the PID file nor a registry
    row is available.

    Last resort: ``canonical_agent_id()`` produces the same shape every
    other path produces, so availability never comes at the cost of identity
    divergence.
    """
    pids_dir = _db.BUS_DIR / "pids"
    pid_envs = [a.self_pid_env for a in registered_adapters()]
    env_pids = [os.environ.get(e) for e in pid_envs]
    for pid in filter(None, [*env_pids, str(os.getppid())]):
        pid_file = pids_dir / pid
        if pid_file.exists():
            resolved = pid_file.read_text().strip()
            _db._dbg(f"_self_agent_id: pid={pid} \u2192 {resolved!r}")
            return resolved

    # Registry path: the authoritative row for this pane already exists
    # if registration ran. Two sub-strategies, each runtime-neutral:
    #
    #   1. tmux_target lookup — uses HELIOY_BUS_TMUX or TMUX_PANE when the
    #      MCP server inherits tmux env. Claude and hook-bootstrapped
    #      runtimes always do. Codex strips env before spawning MCP
    #      subprocesses, so this tier silently misses under Codex.
    #   2. pid ancestor walk — walks up the MCP server's ppid chain and
    #      looks for any ancestor pid registered in the agents table.
    #      The codex-launch wrapper registers with its own pid
    #      (HELIOY_BUS_CODEX_PID=$$) and stays alive as codex's parent,
    #      so the wrapper's pid is always an ancestor of any MCP process
    #      codex spawns. This tier carries Codex when env does not.
    tmux_target = _resolve_tmux_target()
    if tmux_target:
        resolved = _lookup_agent_by_tmux(tmux_target)
        if resolved:
            _db._dbg(f"_self_agent_id: registry tmux_target={tmux_target} \u2192 {resolved!r}")
            return resolved

    resolved = _lookup_agent_by_pid_ancestry(os.getpid())
    if resolved:
        _db._dbg(f"_self_agent_id: registry pid-ancestry \u2192 {resolved!r}")
        return resolved

    # Slow path: delegate to the authoritative shell resolver for consistency
    if _RESOLVE_IDENTITY_SH.exists():
        try:
            result = subprocess.run(
                [
                    "bash",
                    "-c",
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
        os.environ.get("HELIOY_AGENT_TYPE") or os.environ.get("HELIOY_BUS_AGENT_TYPE") or "general"
    )
    tmux_target = os.environ.get("HELIOY_BUS_TMUX", "")
    resolved = canonical_agent_id(os.getcwd(), agent_type, tmux_target)
    env_map = {e: os.environ.get(e) for e in pid_envs}
    _db._dbg(
        f"_self_agent_id: no pid file, shell resolver unavailable "
        f"({env_map!r} ppid={os.getppid()}) \u2192 {resolved!r}"
    )
    return resolved


def _resolve_tmux_target() -> str:
    """Return ``{session}:{window}.{pane}`` for the caller, or ``""``.

    Prefers ``HELIOY_BUS_TMUX`` (set by the bus-register hook) when present.
    Falls back to ``tmux display-message`` against ``TMUX_PANE``, which is
    inherited by every descendant of the pane's shell (codex, the MCP
    server it spawns, etc.) whenever the pane lives inside a tmux session.
    """
    cached = os.environ.get("HELIOY_BUS_TMUX", "")
    if cached:
        return cached
    pane = os.environ.get("TMUX_PANE", "")
    if not pane or not os.environ.get("TMUX"):
        return ""
    try:
        result = subprocess.run(
            [
                "tmux",
                "display-message",
                "-p",
                "-t",
                pane,
                "#{session_name}:#{window_index}.#{pane_index}",
            ],
            capture_output=True,
            timeout=3,
        )
    except (subprocess.SubprocessError, OSError):
        return ""
    if result.returncode != 0:
        return ""
    return result.stdout.decode().strip()


def _lookup_agent_by_tmux(tmux_target: str) -> str:
    """Return the registered agent_id for ``tmux_target``, or ``""``.

    Defensive against a missing or uninitialised registry: returns empty
    string rather than raising so ``_self_agent_id`` can continue to the
    next fallback tier.
    """
    try:
        with _db.db() as conn:
            row = conn.execute(
                "SELECT agent_id FROM agents WHERE tmux_target = ?",
                (tmux_target,),
            ).fetchone()
    except Exception:
        return ""
    if row is None:
        return ""
    return row["agent_id"]


def _parent_pid(pid: int) -> int:
    """Return ``pid``'s parent pid via ``ps``, or ``0`` when unreachable."""
    try:
        result = subprocess.run(
            ["ps", "-o", "ppid=", "-p", str(pid)],
            capture_output=True,
            timeout=3,
        )
    except (subprocess.SubprocessError, OSError):
        return 0
    if result.returncode != 0:
        return 0
    text = result.stdout.decode().strip()
    try:
        return int(text) if text else 0
    except ValueError:
        return 0


def _lookup_agent_by_pid_ancestry(start_pid: int) -> str:
    """Walk up the process tree from ``start_pid`` and return the first
    ancestor pid registered in the agents table.

    Bounded to 32 hops to prevent runaway walks when ``ps`` misbehaves.
    Stops at pid 1 / 0. Runtime-neutral: any registration that recorded
    an ancestor pid will be found, regardless of how that runtime manages
    env propagation to MCP subprocesses.
    """
    try:
        with _db.db() as conn:
            pid = start_pid
            for _ in range(32):
                pid = _parent_pid(pid)
                if pid <= 1:
                    return ""
                row = conn.execute(
                    "SELECT agent_id FROM agents WHERE pid = ?",
                    (pid,),
                ).fetchone()
                if row is not None:
                    return row["agent_id"]
    except Exception:
        return ""
    return ""

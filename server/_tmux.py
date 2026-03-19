"""tmux integration helpers for helioy-bus: pane liveness, nudging, and spawning."""

from __future__ import annotations

import os
import subprocess
from datetime import UTC, datetime, timedelta

from server import _db

NUDGE_THROTTLE_SECONDS = 30  # 30 seconds


def _inbox_has_unread(agent_id: str) -> bool:
    """Return True if the agent's inbox contains unread messages."""
    inbox = _db.INBOX_DIR / agent_id
    if not inbox.exists():
        return False
    return bool(list(inbox.glob("*.json")))


def _nudge_allowed(agent_id: str) -> bool:
    """Return True if a nudge should be sent to the agent.

    Allows re-nudging within the throttle window if the inbox still has
    unread messages, meaning the previous nudge did not wake the agent.
    """
    with _db.db() as conn:
        row = conn.execute(
            "SELECT nudged_at FROM nudge_log WHERE agent_id = ? ORDER BY nudged_at DESC LIMIT 1",
            (agent_id,),
        ).fetchone()
        if row is None:
            return True
        last = row["nudged_at"]
        cutoff_dt = datetime.now(UTC) - timedelta(seconds=NUDGE_THROTTLE_SECONDS)
        if last < cutoff_dt.isoformat():
            return True  # throttle window expired
        # Within throttle window, but previous nudge may not have worked.
        # If unread messages remain, the agent never woke up. Re-nudge.
        if _inbox_has_unread(agent_id):
            _db._dbg(
                f"_nudge_allowed: {agent_id!r} throttled but inbox has unread messages, "
                "allowing re-nudge"
            )
            return True
        return False


def _record_nudge(agent_id: str) -> None:
    with _db.db() as conn:
        conn.execute(
            "INSERT INTO nudge_log (agent_id, nudged_at) VALUES (?, ?)",
            (agent_id, _db._now()),
        )
        # Prune old entries (keep last 24h)
        conn.execute(
            "DELETE FROM nudge_log WHERE nudged_at < ?",
            ((datetime.now(UTC) - timedelta(hours=24)).isoformat(),),
        )


def _tmux_pane_alive(target: str) -> bool:
    """Return True if the tmux target pane exists and is reachable.

    Uses list-panes rather than has-session so that a dead pane in a live
    session is correctly reported as gone.
    """
    try:
        result = subprocess.run(
            ["tmux", "list-panes", "-t", target],
            capture_output=True,
            timeout=3,
        )
        return result.returncode == 0
    except (subprocess.SubprocessError, FileNotFoundError, subprocess.TimeoutExpired):
        return False


def _tmux_nudge(tmux_target: str) -> bool:
    """Send a 'you have mail!' keystroke to wake an idle Claude session.

    Exits copy-mode first if the pane is in it (common when user scrolls),
    then sends literal text followed by Enter as a separate key.
    """
    try:
        # Exit copy-mode if active. -X cancel writes nothing to the app's PTY.
        mode_result = subprocess.run(
            ["tmux", "display-message", "-t", tmux_target, "-p", "#{pane_in_mode}"],
            capture_output=True,
            timeout=3,
        )
        if mode_result.returncode == 0 and mode_result.stdout.decode().strip() == "1":
            subprocess.run(
                ["tmux", "send-keys", "-t", tmux_target, "-X", "cancel"],
                capture_output=True,
                timeout=3,
            )
            _db._dbg(f"_tmux_nudge: exited copy-mode on {tmux_target!r}")

        # Send literal text, then Enter as a named key (separate call).
        result = subprocess.run(
            ["tmux", "send-keys", "-t", tmux_target, "-l", "you have mail!"],
            capture_output=True,
            timeout=3,
        )
        if result.returncode != 0:
            _db._dbg(
                f"_tmux_nudge: target={tmux_target!r} text rc={result.returncode} "
                f"stderr={result.stderr.decode().strip()!r}"
            )
            return False

        result = subprocess.run(
            ["tmux", "send-keys", "-t", tmux_target, "Enter"],
            capture_output=True,
            timeout=3,
        )
        _db._dbg(
            f"_tmux_nudge: target={tmux_target!r} rc={result.returncode} "
            f"stderr={result.stderr.decode().strip()!r}"
        )
        return result.returncode == 0
    except (subprocess.SubprocessError, FileNotFoundError, subprocess.TimeoutExpired) as e:
        _db._dbg(f"_tmux_nudge: target={tmux_target!r} exception={e!r}")
        return False


def _tmux_check(*args: str) -> str:
    """Run a tmux command and return stdout. Raises RuntimeError on failure."""
    try:
        result = subprocess.run(
            ["tmux", *args],
            capture_output=True,
            timeout=5,
        )
        if result.returncode != 0:
            stderr = result.stderr.decode().strip()
            raise RuntimeError(f"tmux {args[0]} failed: {stderr}")
        return result.stdout.decode().strip()
    except FileNotFoundError as err:
        raise RuntimeError("tmux is not installed or not in PATH") from err
    except subprocess.TimeoutExpired as err:
        raise RuntimeError(f"tmux {args[0]} timed out") from err


def _spawn_pane(
    session: str,
    window: str,
    cwd: str,
    agent_type: str,
    qualified_name: str | None,
    is_first: bool,
    layout: str,
) -> dict:
    """Create a single tmux pane running a Claude Code agent.

    Returns a dict with tmux_target, pane_id, agent_type, and qualified_name.
    The ordering contract: pane title is set BEFORE send-keys so that
    identity resolution works when the SessionStart hook fires.

    When qualified_name is None, spawns a general Claude session without
    the --agent flag (repo-mode).
    """
    repo = os.path.basename(cwd)

    if is_first:
        # Create new window (first pane comes free)
        # Use -a to append after current window, avoiding index collisions.
        # Trailing colon on session ensures tmux targets the session, not a window.
        raw = _tmux_check(
            "new-window", "-a", "-t", f"{session}:", "-n", window,
            "-c", cwd, "-P", "-F", "#{pane_id}",
        )
        pane_id = raw.strip()
    else:
        # Split from the window target
        raw = _tmux_check(
            "split-window", "-t", f"{session}:{window}",
            "-c", cwd, "-P", "-F", "#{pane_id}",
        )
        pane_id = raw.strip()

    # Resolve the full tmux_target (session:window.pane)
    _tmux_check(
        "display-message", "-t", pane_id,
        "-p", "#{session_id}:#{window_index}.#{pane_index}",
    )
    # display-message returns $N:W.P but we need the session name, not $id
    # Use list-panes to get the canonical target
    pane_info = _tmux_check(
        "display-message", "-t", pane_id,
        "-p", "#{session_name}:#{window_index}.#{pane_index}",
    )
    tmux_target = pane_info.strip()

    # Set pane title BEFORE launching claude (identity resolution depends on this)
    display_name = qualified_name if qualified_name is not None else agent_type
    identity = f"{repo}:{display_name}:{tmux_target}"
    _tmux_check("select-pane", "-t", pane_id, "-T", identity)

    # Lock pane rename (window-level, only needed once per window)
    if is_first:
        _tmux_check(
            "set-option", "-t", f"{session}:{window}",
            "allow-rename", "off",
        )

    # Launch claude code, with or without a specific agent type.
    # Both paths use --dangerously-skip-permissions so warroom agents run
    # autonomously without interactive permission prompts.
    if qualified_name is not None:
        cmd = f"claude --dangerously-skip-permissions --agent {qualified_name}"
    else:
        cmd = "claude --dangerously-skip-permissions"
    _tmux_check("send-keys", "-t", pane_id, cmd, "Enter")

    # Reflow layout after each split
    _tmux_check("select-layout", "-t", f"{session}:{window}", layout)

    return {
        "agent_type": agent_type,
        "qualified_name": qualified_name,
        "tmux_target": tmux_target,
        "pane_id": pane_id,
    }

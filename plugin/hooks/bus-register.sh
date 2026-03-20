#!/usr/bin/env bash
# bus-register.sh — SessionStart hook for helioy-bus
#
# Registers this Claude Code instance directly into the bus SQLite registry.
# Uses direct DB writes to avoid MCP subprocess overhead in lifecycle hooks.
# Gracefully no-ops if Python or the bus dir is unavailable.
#
# Configured in /Users/alphab/Dev/LLM/DEV/helioy/helioy-plugins/plugins/helioy-bus/hooks/hooks.json as a SessionStart hook.

set -euo pipefail

BUS_DIR="${HELIOY_BUS_DIR:-$HOME/.helioy/bus}"
DB_PATH="$BUS_DIR/registry.db"
INBOX_BASE="$BUS_DIR/inbox"

# Resolve identity via shared lib (pane-title-first, then basename fallback).
# Exports: HELIOY_AGENT_ID, HELIOY_AGENT_TYPE, HELIOY_AGENT_REPO
HOOKS_LIB="$(dirname "$0")/lib/resolve-identity.sh"
# shellcheck source=lib/resolve-identity.sh
source "$HOOKS_LIB"
resolve_agent_id

AGENT_ID="$HELIOY_AGENT_ID"
AGENT_TYPE="$HELIOY_AGENT_TYPE"

# Derive TMUX_TARGET for the registry record (used for nudges).
TMUX_TARGET=""
if [[ -n "${TMUX_PANE:-}" && -n "${TMUX:-}" ]]; then
    TMUX_TARGET="${HELIOY_BUS_TMUX:-$(tmux display-message -p -t "$TMUX_PANE" \
        '#{session_name}:#{window_index}.#{pane_index}' 2>/dev/null || echo "")}"
fi

# Working directory for this session
if [[ -n "${CLAUDE_PROJECT_DIR:-}" ]]; then
    PWD_EFFECTIVE="$CLAUDE_PROJECT_DIR"
else
    PWD_EFFECTIVE="${PWD:-}"
fi

# Session ID: prefer stdin JSON (always available in hooks), fall back to env.
STDIN_JSON=$(cat)
SESSION_ID=$(echo "$STDIN_JSON" | jq -r '.session_id // empty' 2>/dev/null || true)
SESSION_ID="${SESSION_ID:-${HELIOY_SESSION_ID:-${CLAUDE_SESSION_ID:-}}}"

# Write PID → agent_id mapping so hooks and server tools can self-identify
PIDS_DIR="$BUS_DIR/pids"
mkdir -p "$PIDS_DIR"
echo "$AGENT_ID" > "$PIDS_DIR/$PPID"

# Derive the helioy-bus repo root so server._db is importable.
# BASH_SOURCE resolution follows symlinks to the real script location.
# CLAUDE_PLUGIN_ROOT points to the plugin cache (not the repo), so we ignore it.
# HELIOY_BUS_PYTHON_PATH is an explicit override that always wins.
_script="${BASH_SOURCE[0]}"
while [[ -L "$_script" ]]; do
    _dir="$(cd "$(dirname "$_script")" && pwd)"
    _target="$(readlink "$_script")"
    [[ "$_target" != /* ]] && _target="$_dir/$_target"
    _script="$_target"
done
_BASH_SOURCE_ROOT="$(cd "$(dirname "$_script")/../.." && pwd)"
unset _script _dir _target

# Priority: explicit override > BASH_SOURCE (always correct for absolute hook paths).
# CLAUDE_PLUGIN_ROOT points to the plugin cache, not the repo, so never use it.
if [[ -n "${HELIOY_BUS_PYTHON_PATH:-}" ]]; then
    HELIOY_BUS_ROOT="$HELIOY_BUS_PYTHON_PATH"
else
    HELIOY_BUS_ROOT="$_BASH_SOURCE_ROOT"
fi
unset _BASH_SOURCE_ROOT

# Write directly to SQLite via _db.py (single source of truth for schema).
# All values passed through environment variables — never interpolated
# into Python source — to prevent injection when paths contain special chars.
LOG_DIR="$BUS_DIR/logs"
mkdir -p "$LOG_DIR"
PY_STDERR=$(mktemp)

set +e
_HELIOY_BUS_DIR="$BUS_DIR" \
_HELIOY_INBOX_BASE="$INBOX_BASE" \
_HELIOY_AGENT_ID="$AGENT_ID" \
_HELIOY_PWD="$PWD_EFFECTIVE" \
_HELIOY_TMUX="$TMUX_TARGET" \
_HELIOY_SESSION_ID="$SESSION_ID" \
_HELIOY_AGENT_TYPE="$AGENT_TYPE" \
_HELIOY_PID="$PPID" \
HELIOY_BUS_ROOT="$HELIOY_BUS_ROOT" \
python3 - <<'PYEOF' 2>"$PY_STDERR"
import os, sys
from pathlib import Path

# Make server._db importable from the repo root
sys.path.insert(0, os.environ["HELIOY_BUS_ROOT"])

from server._db import BUS_DIR as _default_bus_dir, INBOX_DIR as _default_inbox_dir
from server._db import _now, db
import server._db as _db_mod

# Override paths with hook-supplied values (may differ from defaults)
bus_dir = Path(os.environ["_HELIOY_BUS_DIR"])
inbox_base = Path(os.environ["_HELIOY_INBOX_BASE"])
_db_mod.BUS_DIR = bus_dir
_db_mod.REGISTRY_DB = bus_dir / "registry.db"
_db_mod.INBOX_DIR = inbox_base

# Create inbox directory for this agent
inbox = inbox_base / os.environ["_HELIOY_AGENT_ID"]
inbox.mkdir(parents=True, exist_ok=True)

# Bootstrap schema (idempotent) and register in one transaction.
# Use parent PID (Claude Code process), not this subprocess PID.
with db() as conn:
    conn.execute(
        """
        INSERT OR REPLACE INTO agents
            (agent_id, cwd, tmux_target, pid, session_id, agent_type, registered_at, last_seen)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            os.environ["_HELIOY_AGENT_ID"],
            os.environ["_HELIOY_PWD"],
            os.environ["_HELIOY_TMUX"],
            int(os.environ["_HELIOY_PID"]),
            os.environ.get("_HELIOY_SESSION_ID", ""),
            os.environ.get("_HELIOY_AGENT_TYPE", "general"),
            _now(), _now(),
        ),
    )
PYEOF
PY_EXIT=$?

if [[ $PY_EXIT -ne 0 ]]; then
    printf '[%s] bus-register FAIL agent_id=%s exit=%d\nstderr: %s\n' \
        "$(date -u +%Y-%m-%dT%H:%M:%S+00:00)" "$AGENT_ID" "$PY_EXIT" \
        "$(cat "$PY_STDERR")" >> "$LOG_DIR/hook-errors.log"
fi
rm -f "$PY_STDERR"
set -e

# Prune stale PID files for processes that no longer exist.
# Runs on every SessionStart to prevent unbounded growth.
for pid_file in "$PIDS_DIR"/*; do
    [[ -f "$pid_file" ]] || continue
    pid_num="${pid_file##*/}"
    # Skip non-numeric filenames (e.g. .token_watcher artifacts)
    [[ "$pid_num" =~ ^[0-9]+$ ]] || continue
    # Skip our own entry
    [[ "$pid_num" == "$PPID" ]] && continue
    # Remove if the process no longer exists
    if ! kill -0 "$pid_num" 2>/dev/null; then
        rm -f "$pid_file"
    fi
done

# Install tmux hooks for proactive registry cleanup on kill-pane/kill-window.
# Uses indexed array slots [99] to avoid clobbering user hooks.
# Idempotent: re-setting the same index overwrites the previous value.
# Passes TMUX_BIN explicitly because run-shell has a minimal PATH.
if [[ -n "${TMUX:-}" ]]; then
    PRUNE_SCRIPT="$HELIOY_BUS_ROOT/plugin/hooks/bus-prune.sh"
    TMUX_BIN="$(command -v tmux)"
    tmux set-hook -g 'after-kill-pane[99]' \
        "run-shell \"TMUX_BIN=$TMUX_BIN $PRUNE_SCRIPT\"" 2>/dev/null || true
    tmux set-hook -g 'window-unlinked[99]' \
        "run-shell \"TMUX_BIN=$TMUX_BIN $PRUNE_SCRIPT\"" 2>/dev/null || true
fi

# Emit empty JSON (hooks require valid JSON or no output)
echo "{}"
